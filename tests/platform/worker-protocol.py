#!/usr/bin/env python3
"""Exercise the bounded worker wire format on the native host at O0/O2."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


PROBE = r'''
(import Internal.WorkerProtocol)
(import Internal.Raw)
(import Str)
(import Err)
(import IO)

(:: expect (-> String Bool Int))
;@axiom:effect(io)
(fn (expect label good)
  { (println (concat (if good "ok   " "FAIL ") label)) (if good 0 1) })

(:: errorIs (-> (Result String Error) Int Bool))
(fn (errorIs result code)
  (match result ((Ok _) false) ((Err e) (== (errCode e) code))))

(:: textIs (-> (Result String Error) String Bool))
(fn (textIs result text)
  (match result ((Ok s) (strEq s text)) ((Err _) false)))

(:: rejectsWord (-> Int Int Int Int Bool))
;@axiom:effect(unsafe)
(fn (rejectsWord addr total word value)
  { (let saved (__load64 addr word))
    (__store64 addr word value)
    (let rejected (! (workerProtocolValidate addr total)))
    (__store64 addr word saved)
    rejected })

(pub :: workerProtocolProbe Int)
;@axiom:effect(io)
;@axiom:effect(unsafe)
(pub fn (workerProtocolProbe)
  {
    (let mut bad 0)
    (set bad (+ bad (expect "empty payloads need only the header" (== (workerProtocolBytes 0 0) 96))))
    (set bad (+ bad (expect "input is padded, result is bounded" (== (workerProtocolBytes 7 9) 113))))
    (set bad (+ bad (expect "negative input bound" (== (workerProtocolBytes -1 0) -22))))
    (set bad (+ bad (expect "negative result bound" (== (workerProtocolBytes 0 -1) -22))))
    (set bad (+ bad (expect "largest representable mapping" (== (workerProtocolBytes 9223372036854775704 7) 9223372036854775807))))
    (set bad (+ bad (expect "sum overflow" (== (workerProtocolBytes 9223372036854775704 8) -22))))
    (set bad (+ bad (expect "alignment overflow" (== (workerProtocolBytes 9223372036854775705 0) -22))))
    (let addr (rawAlloc 256))
    (__store64 addr 0 123)
    (set bad (+ bad (expect "oversize input refuses before writing" (&& (== (workerProtocolInit addr 113 7 9 0 0 0 "12345678") -22) (== (__load64 addr 0) 123)))))
    (set bad (+ bad (expect "native stack rejects a hidden byte bound" (== (workerProtocolInit addr 113 7 9 0 0 8 "") -22))))
    (set bad (+ bad (expect "negative arena budget" (== (workerProtocolInit addr 113 7 9 -1 0 0 "") -22))))
    (set bad (+ bad (expect "initialise UTF8 input" (== (workerProtocolInit addr 113 7 9 8192 0 0 "hé") 0))))
    (set bad (+ bad (expect "valid header" (workerProtocolValidate addr 113))))
    (set bad (+ bad (expect "input is a bounded copy" (textIs (workerProtocolInput addr 113) "hé"))))
    (set bad (+ bad (expect "arena and stack options" (&& (== (workerProtocolMemory addr) 8192) (&& (== (workerProtocolStackKind addr) 0) (== (workerProtocolStackBytes addr) 0))))))
    (set bad (+ bad (expect "pending is no answer" (errorIs (workerProtocolResult addr 113) 22))))
    (set bad (+ bad (expect "independent mapping length" (! (workerProtocolValidate addr 112)))))
    (set bad (+ bad (expect "unaligned mapping refused" (! (workerProtocolValidate (+ addr 1) 113)))))
    (set bad (+ bad (expect "null mapping refused" (! (workerProtocolValidate 0 113)))))
    (set bad (+ bad (expect "short mapping refused" (! (workerProtocolValidate addr 95)))))
    (let words (rawAlloc 192))
    ; Each invalid field is restored before testing the next one.
    (rawSetWord words 0 0) (rawSetWord words 1 114)
    (rawSetWord words 2 -1) (rawSetWord words 3 8)
    (rawSetWord words 4 -1) (rawSetWord words 5 10)
    (rawSetWord words 6 3) (rawSetWord words 7 2)
    (rawSetWord words 8 -1) (rawSetWord words 9 2)
    (rawSetWord words 10 8) (rawSetWord words 11 2)
    (for i in 0..12
      (set bad (+ bad (expect "corrupt header field"
        (rejectsWord addr 113 i (rawGetWord words i))))))
    (set bad (+ bad (expect "cancellation begins clear" (! (workerProtocolCancelled addr)))))
    (workerProtocolCancel addr)
    (set bad (+ bad (expect "cancellation is published" (workerProtocolCancelled addr))))
    (let binary (strAlloc 3))
    (rawPutByte (strData binary) 0 0)
    (rawPutByte (strData binary) 1 255)
    (rawPutByte (strData binary) 2 65)
    (set bad (+ bad (expect "binary answer is published" (== (workerProtocolAnswer addr 113 binary) 0))))
    (set bad (+ bad (expect "binary answer is copied" (textIs (workerProtocolResult addr 113) binary))))
    (set bad (+ bad (expect "duplicate answer refused" (== (workerProtocolAnswer addr 113 "again") 22))))
    (workerProtocolInit addr 113 7 9 0 0 0 "")
    (rawPutByte addr 104 77)
    (set bad (+ bad (expect "oversize answer publishes refusal" (== (workerProtocolAnswer addr 113 "0123456789") 1003))))
    (set bad (+ bad (expect "oversize answer copied no bytes" (== (rawGetByte addr 104) 77))))
    (set bad (+ bad (expect "oversize result stays an error" (errorIs (workerProtocolResult addr 113) 1003))))
    (workerProtocolInit addr 96 0 0 0 0 0 "")
    (set bad (+ bad (expect "empty answer fits an empty result bound" (&& (== (workerProtocolAnswer addr 96 "") 0) (textIs (workerProtocolResult addr 96) "")))))
    (workerProtocolInit addr 113 7 9 0 1 65536 "")
    (set bad (+ bad (expect "explicit stack bytes survive transport" (&& (workerProtocolValidate addr 113) (&& (== (workerProtocolStackKind addr) 1) (== (workerProtocolStackBytes addr) 65536))))))
    bad
  })
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--axiom", required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    axiom = str(Path(args.axiom).resolve())
    with tempfile.TemporaryDirectory(prefix="axiom-worker-protocol-") as directory:
        work = Path(directory)
        library = work / "stdlib"
        shutil.copytree(root / "stdlib", library)
        (library / "WorkerProtocolProbe.ax").write_text(PROBE)
        source = work / "main.ax"
        source.write_text("(import WorkerProtocolProbe)\n(:: main Int)\n"
                          ";@axiom:effect(io)\n(fn (main) workerProtocolProbe)\n")
        env = dict(os.environ, AXIOM_STDLIB=str(library))
        for level in (0, 2):
            binary = work / f"probe-O{level}"
            built = subprocess.run([axiom, "--diagnostic-format=ai", "build", "--input", str(source),
                                    "--output", str(binary), "--opt", str(level)],
                                   env=env, text=True, capture_output=True)
            if built.returncode:
                raise SystemExit(built.stdout + built.stderr)
            result = subprocess.run([str(binary)], text=True, capture_output=True, timeout=10)
            lines = result.stdout.splitlines()
            if result.returncode or len(lines) != 41 or any(not line.startswith("ok   ") for line in lines):
                raise SystemExit(f"FAIL worker protocol -O{level}: exit {result.returncode}, "
                                 f"{len(lines)} cases\n{result.stdout}{result.stderr}")
            print(f"ok   worker protocol: {len(lines)} native host cases at -O{level}")

        # The corrupt-header cases must detect removal of the payload
        # bounds check, with a runnable binary rather than a build error.
        protocol = library / "Internal/WorkerProtocol.ax"
        text = protocol.read_text()
        before = "(&& headerOk (&& inputOk "
        assert text.count(before) == 1, "protocol ablation lost its checked condition"
        protocol.write_text(text.replace(before, "(&& headerOk (&& true "))
        binary = work / "probe-ablated"
        subprocess.run([axiom, "--diagnostic-format=ai", "build", "--input", str(source),
                        "--output", str(binary), "--opt", "2"], env=env, check=True,
                       stdout=subprocess.DEVNULL)
        result = subprocess.run([str(binary)], text=True, capture_output=True, timeout=10)
        lines = result.stdout.splitlines()
        failures = [line for line in lines if line.startswith("FAIL ")]
        if result.returncode != 2 or len(lines) != 41 or len(failures) != 2:
            raise SystemExit(f"FAIL worker bounds ablation: exit {result.returncode}\n"
                             f"{result.stdout}{result.stderr}")
        print("ok   removing payload bounds validation fails two corrupt-header cases")


if __name__ == "__main__":
    main()
