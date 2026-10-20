#!/usr/bin/env python3
"""Exercise the emitted Windows stack probe without claiming Windows execution."""

import argparse
import re
import subprocess
from pathlib import Path


def large_source(path):
    # Keeping many call results live across later calls needs more than
    # one 4 KiB page at -O0, without relying on high function arities.
    n = 600
    body = "0"
    for _ in range(n):
        body = f"(+ (one x) {body})"
    path.write_text(
        "(import IO)\n(:: one (-> Int Int))\n(fn (one x) (+ x 1))\n"
        f"(:: many (-> Int Int))\n(fn (many x) {body})\n"
        "(:: main Int)\n;@axiom:effect(io)\n(fn (main)\n  {\n"
        f"    (let good (== (many 0) {n}))\n"
        '    (println (if good "ok   a large call frame" "FAIL a large call frame"))\n'
        "    (if good 0 1)\n  })\n"
    )
    path.with_suffix(".out").write_text("ok   a large call frame\n")


def run(args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def verify(module, work):
    ir = module.read_text()
    helper = re.search(r"^define (?:weak_odr )?void @__chkstk\(\) naked \{.*?^\}", ir, re.M | re.S)
    assert helper, f"no emitted __chkstk in {module}"
    helper = helper.group()
    triple = re.search(r"Default target: (\S+)", run(["llc", "--version"], capture_output=True).stdout).group(1)
    arm = triple.startswith(("aarch64", "arm64"))
    assert arm == ("aarch64-pc-windows" in ir[:100]), "choose the module matching the host architecture"
    work.mkdir(parents=True, exist_ok=True)
    # The helper has no OS calls. Replacing only the module triple lets
    # the current machine execute its instructions with its own ABI.
    prefix = "_" if "apple" in triple else ""
    probe_symbol = prefix + "__chkstk"
    call_symbol = prefix + "probe_call"
    asm = f".text\n.globl {call_symbol}\n{call_symbol}:\n"
    if arm:
        asm += f"""  stp x29, x30, [sp, #-48]!
  stp x19, x20, [sp, #16]
  str x21, [sp, #32]
  mov x29, sp
  mov x19, sp
  mov x20, x2
  mov x21, x0
  mov x15, x1
  mov x0, #77
  mov sp, x21
  bl {probe_symbol}
  mov x9, sp
  mov sp, x19
  stp x9, x15, [x20]
  str x0, [x20, #16]
  ldr x21, [sp, #32]
  ldp x19, x20, [sp, #16]
  ldp x29, x30, [sp], #48
  ret
"""
        # The loop's read, as distinct from the last read below label 2.
        read = r"ldr xzr, [x17]\0Asub x16"
        ablated = helper.replace(read, r"nop\0Asub x16", 1)
    else:
        asm += f"""  pushq %rbp
  movq %rsp, %rbp
  pushq %rbx
  pushq %r12
  movq %rsp, %rbx
  movq %rdx, %r12
  movq %rdi, %rsp
  movq %rsi, %rax
  movq $77, %rcx
  callq {probe_symbol}
  movq %rsp, 0(%r12)
  movq %rax, 8(%r12)
  movq %rcx, 16(%r12)
  movq %rbx, %rsp
  popq %r12
  popq %rbx
  popq %rbp
  retq
"""
        read = r"testb $$0, (%r11)\0Asubq $$4096, %r10"
        ablated = helper.replace(read, r"nop\0Asubq $$4096, %r10", 1)
    assert helper != ablated, "the intermediate-page ablation changed nothing"
    wrapper = work / "wrapper.s"
    wrapper.write_text(asm)
    harness = work / "harness.c"
    harness.write_text(r'''
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/mman.h>
#include <sys/resource.h>
#include <sys/wait.h>
#include <unistd.h>
extern void probe_call(void *, uintptr_t, uintptr_t *);
int main(void) {
    long page = sysconf(_SC_PAGESIZE);
    /* The Windows helper steps by 4096 even where the host maps 16 KiB pages. */
    size_t span = (size_t)page * 8;
    char *p = mmap(0, span, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON, -1, 0);
    if (p == MAP_FAILED) return 2;
    char *top = p + span - 16;
    uintptr_t out[3];
    size_t sizes[] = {0, 16, 4096, 4112, (size_t)page * 4};
    for (int i = 0; i < 5; i++) {
        uintptr_t size = sizes[i];
#if defined(__aarch64__) || defined(__arm64__)
        size /= 16;
#endif
        probe_call(top, size, out);
        if (out[0] != (uintptr_t)top || out[1] != size || out[2] != 77) {
            fprintf(stderr, "probe changed SP, its size register or an argument register\n");
            return 1;
        }
    }
    /* The requested end is mapped; an intermediate page is protected.
       Reading only the end of the allocation would miss this guard. */
    if (mprotect(p + page * 4, (size_t)page, PROT_NONE) != 0) return 2;
    pid_t child = fork();
    if (child < 0) return 2;
    if (child == 0) {
        struct rlimit lim = {0, 0};
        setrlimit(RLIMIT_CORE, &lim);
        uintptr_t size = (uintptr_t)page * 6;
#if defined(__aarch64__) || defined(__arm64__)
        size /= 16;
#endif
        probe_call(top, size, out);
        _exit(0);
    }
    int status = 0;
    if (waitpid(child, &status, 0) != child) return 2;
    munmap(p, span);
    if (!WIFSIGNALED(status) || (WTERMSIG(status) != SIGSEGV && WTERMSIG(status) != SIGBUS)) {
        fprintf(stderr, "probe skipped the protected intermediate page\n");
        return 1;
    }
    puts("ok   the emitted stack probe preserves its ABI and touches an intermediate guard");
    return 0;
}
''')
    for label, body in (("real", helper), ("ablated", ablated)):
        source = work / f"{label}.ll"
        source.write_text(f'target triple = "{triple}"\n' + body + "\n")
        obj = work / f"{label}.o"
        run(["llc", "-O2", "-filetype=obj", "-relocation-model=pic", str(source), "-o", str(obj)])
        binary = work / label
        run(["cc", str(harness), str(wrapper), str(obj), "-o", str(binary)])
        result = subprocess.run([str(binary)], text=True, capture_output=True)
        if label == "real":
            assert result.returncode == 0, result.stderr
            print(result.stdout, end="")
        else:
            assert result.returncode == 1 and "skipped the protected" in result.stderr, result.stderr
            print("ok   removing the intermediate-page read makes the guard check fail")


parser = argparse.ArgumentParser(description=__doc__)
sub = parser.add_subparsers(dest="command", required=True)
p = sub.add_parser("source")
p.add_argument("path", type=Path)
p = sub.add_parser("native")
p.add_argument("module", type=Path)
p.add_argument("work", type=Path)
a = parser.parse_args()
if a.command == "source":
    large_source(a.path)
else:
    verify(a.module, a.work)
