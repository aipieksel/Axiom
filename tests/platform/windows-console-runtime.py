#!/usr/bin/env python3
"""Execute the emitted Windows control runtime with host-side WinAPI mocks."""

import argparse
import json
import re
import subprocess
from pathlib import Path


def run(args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def emitted_source(source):
    text = source.read_text()
    start = text.index("(pub fn (emitWinConsoleRuntime cg)")
    end = text.index("\n(pub ::", start)
    literals = re.findall(r'\(emitLine\s+cg\s+("(?:[^"\\]|\\.)*")\)', text[start:end])
    body = "\n".join(json.loads(value) for value in literals) + "\n"
    assert "define internal i64 @__axiom_console_take" in body
    return body


def module_source(module):
    text = module.read_text()
    globals_ = re.findall(r"^@__axiom_console_\w+ = .*", text, re.M)
    functions = re.findall(
        r"^define internal (?:i32|i64) @__axiom_console_\w+\([^\n]*\) [^{]*\{.*?^\}",
        text,
        re.M | re.S,
    )
    assert len(globals_) == 4 and len(functions) == 3, "incomplete emitted control runtime"
    return "\n".join(globals_ + functions) + "\n"


WRAPPERS = r'''
define i64 @test_block(i64 %mask) {
  %v = call i64 @__axiom_console_block(i64 %mask)
  ret i64 %v
}
define i64 @test_take(i64 %mask) {
  %v = call i64 @__axiom_console_take(i64 %mask)
  ret i64 %v
}
define i32 @test_handler(i32 %event) {
  %v = call i32 @__axiom_console_handler(i32 %event)
  ret i32 %v
}
define void @test_reset() {
  store i64 0, ptr @__axiom_console_mask
  store i64 0, ptr @__axiom_console_pending
  store i32 0, ptr @__axiom_console_registered
  store i32 0, ptr @__axiom_console_lock
  ret void
}
define i32 @mainCRTStartup() {
  %v = call i64 @test_block(i64 1048578)
  %s = call i64 @test_take(i64 1048578)
  %r = add i64 %v, %s
  %ret = trunc i64 %r to i32
  ret i32 %ret
}
'''


HARNESS = r'''
#define _DEFAULT_SOURCE 1
#include <pthread.h>
#include <sched.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

extern int64_t test_block(int64_t);
extern int64_t test_take(int64_t);
extern int32_t test_handler(int32_t);
extern void test_reset(void);
static _Atomic int calls, fail_install, send_during_install, install_delivery;
static _Atomic uint64_t last_error;
static _Atomic int start;
enum { INT_MASK = 2, BREAK_MASK = 1048576, BOTH = 1048578, THREADS = 16 };

uint64_t GetLastError(void) { return atomic_load(&last_error); }
uint64_t SetConsoleCtrlHandler(uint64_t address, uint64_t add) {
    if (add != 1) abort();
    atomic_fetch_add(&calls, 1);
    /* Widening BOOL/DWORD must not let unspecified upper bits affect results. */
    if (atomic_load(&fail_install)) return 0x123400000000ULL;
    if (atomic_load(&send_during_install)) {
        int32_t (*handler)(int32_t) = (void *)(uintptr_t)address;
        atomic_store(&install_delivery, handler(0) + handler(1));
    }
    usleep(1000);
    return 0x123400000001ULL;
}

#define CHECK(c, label) do { if (!(c)) { fprintf(stderr, "FAIL %s\n", label); return 1; } } while (0)
static void reset(void) {
    test_reset();
    atomic_store(&calls, 0);
    atomic_store(&fail_install, 0);
    atomic_store(&send_during_install, 0);
    atomic_store(&install_delivery, 0);
    atomic_store(&last_error, 0);
    atomic_store(&start, 0);
}
struct task { int op, value; int64_t result; };
static void *worker(void *p) {
    struct task *t = p;
    while (!atomic_load_explicit(&start, memory_order_acquire)) sched_yield();
    if (t->op == 0) t->result = test_block(t->value);
    if (t->op == 1) {
        t->result = 0;
        for (int i = 0; i < 20000; ++i) t->result += test_handler(t->value);
    }
    if (t->op == 2) t->result = test_take(BOTH);
    return 0;
}
static void concurrent(struct task *tasks) {
    pthread_t threads[THREADS];
    atomic_store(&start, 0);
    for (int i = 0; i < THREADS; ++i)
        if (pthread_create(&threads[i], 0, worker, &tasks[i])) abort();
    atomic_store_explicit(&start, 1, memory_order_release);
    for (int i = 0; i < THREADS; ++i)
        if (pthread_join(threads[i], 0)) abort();
}
int main(void) {
    reset();
    CHECK(test_block(0) == 0 && atomic_load(&calls) == 0, "empty mask does not register");
    CHECK(test_block(1) == -87 && test_block(-1) == -87 && atomic_load(&calls) == 0,
          "unsupported masks do not register");
    CHECK(test_handler(0) == 0 && test_handler(1) == 0, "unblocked events pass through");

    atomic_store(&fail_install, 1);
    atomic_store(&last_error, 0x123400000005ULL);
    CHECK(test_block(INT_MASK) == -5, "registration preserves raw low DWORD error");
    CHECK(test_handler(0) == 0 && test_take(BOTH) == 0, "failed registration rolls mask back");
    atomic_store(&last_error, 0);
    CHECK(test_block(INT_MASK) == -1, "zero last error has a failure fallback");
    CHECK(atomic_load(&calls) == 2, "failed registration can retry");
    atomic_store(&fail_install, 0);
    atomic_store(&send_during_install, 1);
    CHECK(test_block(BOTH) == 0 && atomic_load(&install_delivery) == 2,
          "mask is published before handler installation");
    CHECK(test_take(INT_MASK) == 2 && test_take(INT_MASK) == 0,
          "taking one event preserves the other pending bit");
    CHECK(test_take(BREAK_MASK) == 21 && test_take(BOTH) == 0,
          "break maps to its own signal and drains once");
    CHECK(test_block(BOTH) == 0 && atomic_load(&calls) == 3, "registration is process owned");
    for (int event = 2; event <= 6; ++event)
        CHECK(test_handler(event) == 0, "close logoff shutdown and unknown events pass through");
    CHECK(test_take(BOTH) == 0, "ignored events do not become pending");

    reset();
    struct task tasks[THREADS];
    for (int i = 0; i < THREADS; ++i)
        tasks[i] = (struct task){0, i & 1 ? INT_MASK : BREAK_MASK, -99};
    concurrent(tasks);
    for (int i = 0; i < THREADS; ++i)
        CHECK(tasks[i].result == 0, "concurrent block succeeds");
    CHECK(atomic_load(&calls) == 1, "concurrent block registers exactly once");
    CHECK(test_handler(0) == 1 && test_handler(1) == 1, "concurrent masks accumulate");
    CHECK(test_take(BOTH) == 2 && test_take(BOTH) == 21, "accumulated mask takes both events");

    for (int i = 0; i < THREADS; ++i) tasks[i] = (struct task){1, i & 1, -99};
    concurrent(tasks);
    for (int i = 0; i < THREADS; ++i)
        CHECK(tasks[i].result == 20000, "concurrent callbacks handle every requested event");
    CHECK(test_take(BREAK_MASK) == 21 && test_take(INT_MASK) == 2 && test_take(BOTH) == 0,
          "concurrent callbacks preserve both coalesced bits");

    for (int round = 0; round < 40; ++round) {
        CHECK(test_handler(0) == 1 && test_handler(1) == 1, "queue both events");
        for (int i = 0; i < THREADS; ++i) tasks[i] = (struct task){2, 0, -99};
        concurrent(tasks);
        int interrupts = 0, breaks = 0, empty = 0;
        for (int i = 0; i < THREADS; ++i) {
            interrupts += tasks[i].result == 2;
            breaks += tasks[i].result == 21;
            empty += tasks[i].result == 0;
        }
        CHECK(interrupts == 1 && breaks == 1 && empty == THREADS - 2,
              "concurrent readers deliver each pending bit exactly once");
    }
    puts("ok   Windows control runtime registration, filtering and concurrent delivery");
    return 0;
}
'''


def verify(source, work, module=None):
    body = emitted_source(source)
    if module:
        actual = module_source(module)
        # Comparing nonempty lines also tolerates spacing between emitted definitions.
        assert actual.split() == body.split(), "compiled control runtime differs from its emitter"
        body = actual
    work.mkdir(parents=True, exist_ok=True)
    declarations = "declare i64 @SetConsoleCtrlHandler(i64, i64)\ndeclare i64 @GetLastError()\n"
    triple = re.search(r"Default target: (\S+)", run(["llc", "--version"], capture_output=True).stdout).group(1)
    harness = work / "control.c"
    harness.write_text(HARNESS)
    # Changing the cmpxchg desired value to the old value leaves the event pending.
    # The unmodified harness must reject this broken queue at both optimisation levels.
    ablated = body.replace("i64 %pending, i64 %rest acq_rel acquire", "i64 %pending, i64 %pending acq_rel acquire")
    assert ablated != body, "the pending-bit ablation changed nothing"
    for level in (0, 2):
        for label, runtime in (("real", body), ("ablated", ablated)):
            path = work / f"control-{label}-O{level}.ll"
            path.write_text(f'target triple = "{triple}"\n' + declarations + runtime + WRAPPERS + "attributes #0 = { nounwind }\n")
            obj = path.with_suffix(".o")
            run(["clang", "-target", triple, f"-O{level}", "-c", str(path), "-o", str(obj)])
            binary = work / f"control-{label}-O{level}"
            run(["cc", "-std=c11", "-pthread", f"-O{level}", str(harness), str(obj), "-o", str(binary)])
            result = subprocess.run([str(binary)], text=True, capture_output=True, timeout=30)
            if label == "real":
                assert result.returncode == 0, result.stdout + result.stderr
                print(result.stdout.rstrip() + f" [host mocks, -O{level}]")
            else:
                assert result.returncode == 1 and "FAIL taking one event" in result.stderr, result.stdout + result.stderr
                print(f"ok   removing pending-bit consumption fails the same harness [-O{level}]")

    definition = work / "kernel32.def"
    definition.write_text("LIBRARY kernel32.dll\nEXPORTS\nSetConsoleCtrlHandler\nGetLastError\n")
    for name, triple, machine, dll_machine, pe_machine in (
        ("x64", "x86_64-pc-windows-msvc", "x64", "i386:x86-64", "IMAGE_FILE_MACHINE_AMD64"),
        ("arm64", "aarch64-pc-windows-msvc", "arm64", "arm64", "IMAGE_FILE_MACHINE_ARM64"),
    ):
        library = work / f"kernel32-{name}.lib"
        run(["llvm-dlltool", "-m", dll_machine, "-d", str(definition), "-l", str(library)])
        for level in (0, 2):
            path = work / f"control-{name}-O{level}.ll"
            imports = declarations.replace("declare i64", "declare dllimport i64")
            path.write_text(f'target triple = "{triple}"\n' + imports + body + WRAPPERS + "attributes #0 = { nounwind }\n")
            optimised = path.with_name(path.stem + "-opt.ll")
            run(["opt", f"-passes=default<O{level}>", "-S", str(path), "-o", str(optimised)])
            obj = path.with_suffix(".obj")
            run(["llc", f"-O{level}", "-filetype=obj", str(optimised), "-o", str(obj)])
            binary = path.with_suffix(".exe")
            run(["lld-link", "-subsystem:console", "-entry:mainCRTStartup", f"-machine:{machine}", f"-out:{binary}", str(obj), str(library)])
            headers = run(["llvm-readobj", "--file-headers", str(binary)], capture_output=True).stdout
            assert pe_machine in headers
            print(f"ok   Windows control runtime assembles and links [{name}, -O{level}; not executed]")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="self_host/codegen.ax")
    parser.add_argument("work", type=Path, help="isolated output directory")
    parser.add_argument("--module", type=Path, help="also verify the actual emitted LLVM module")
    args = parser.parse_args()
    verify(args.source, args.work, args.module)
