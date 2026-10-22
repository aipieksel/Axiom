#!/usr/bin/env python3
"""Execute the emitted Windows control runtime with host-side WinAPI mocks."""

import argparse
import re
import subprocess
from pathlib import Path


def run(args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


class Atom(str):
    pass


class Block(tuple):
    pass


def source_tokens(text):
    """Read the emitter's small source fragment without evaluating Axiom code."""
    escapes = {'n': '\n', 't': '\t', 'r': '\r', '\\': '\\', '"': '"', "'": "'", '0': '\0'}
    i = 0
    while i < len(text):
        if text[i].isspace():
            i += 1
        elif text[i] == ';':
            end = text.find('\n', i)
            i = len(text) if end < 0 else end + 1
        elif text.startswith('#|', i):
            depth = 1
            i += 2
            while depth and i < len(text):
                if text.startswith('#|', i):
                    depth += 1
                    i += 2
                elif text.startswith('|#', i):
                    depth -= 1
                    i += 2
                else:
                    i += 1
            if depth:
                raise ValueError('unterminated emitter block comment')
        elif text[i] in '(){}':
            yield ('delimiter', text[i])
            i += 1
        elif text[i] == '"':
            i += 1
            value = []
            while i < len(text) and text[i] != '"':
                if text[i] == '\\':
                    i += 1
                    if i == len(text) or text[i] not in escapes:
                        raise ValueError('unsupported or unterminated emitter string escape')
                    value.append(escapes[text[i]])
                else:
                    value.append(text[i])
                i += 1
            if i == len(text):
                raise ValueError('unterminated emitter string')
            i += 1
            yield ('string', ''.join(value))
        else:
            start = i
            while i < len(text) and not text[i].isspace() and text[i] not in '(){};"':
                i += 1
            yield ('atom', text[start:i])


def source_form(tokens, first=None):
    token = next(tokens, None) if first is None else first
    if token is None:
        raise ValueError('incomplete emitter form')
    kind, value = token
    if kind == 'atom':
        return Atom(value)
    if kind == 'string':
        return value
    if value not in '({':
        raise ValueError(f'unexpected emitter delimiter {value!r}')
    end = ')' if value == '(' else '}'
    children = []
    while True:
        token = next(tokens, None)
        if token is None:
            raise ValueError('unterminated emitter form')
        if token[0] == 'delimiter' and token[1] in ')}':
            if token[1] != end:
                raise ValueError('mismatched emitter delimiters')
            return Block(children) if value == '{' else tuple(children)
        children.append(source_form(tokens, token))


def constant_expression(form):
    """The supported forms are closed; an unknown expression is a failure."""
    if isinstance(form, Atom):
        if not re.fullmatch(r'-?[0-9]+', form):
            raise ValueError(f'nonconstant emitter atom {form!r}')
        value = int(form)
        if not -(1 << 63) <= value < (1 << 63):
            raise ValueError('emitter integer is outside signed 64-bit range')
        return value
    if isinstance(form, str):
        return form
    if type(form) is not tuple or not form or not isinstance(form[0], Atom):
        raise ValueError(f'unsupported emitter expression {form!r}')
    operation, *arguments = form
    arities = {'concat': 2, 'fmtInt': 1, '<<': 2}
    if operation not in arities or len(arguments) != arities[operation]:
        raise ValueError(f'unsupported emitter operation or arity: {operation!r}')
    values = [constant_expression(value) for value in arguments]
    if operation == 'concat':
        if any(type(value) is not str for value in values):
            raise ValueError('constant concat requires two Strings')
        return ''.join(values)
    if any(type(value) is not int for value in values):
        raise ValueError(f'constant {operation} requires Int arguments')
    if operation == 'fmtInt':
        return str(values[0])
    value, shift = values
    if not 0 <= shift < 64:
        raise ValueError('constant shift count is outside 0..63')
    value = (value << shift) & ((1 << 64) - 1)
    return value if value < (1 << 63) else value - (1 << 64)


def emitted_lines(text):
    marker = '(pub fn (emitWinConsoleRuntime cg)'
    if text.count(marker) != 1:
        raise ValueError('expected exactly one console emitter definition')
    form = source_form(iter(source_tokens(text[text.index(marker):])))
    if (type(form) is not tuple or len(form) != 4 or form[:2] != (Atom('pub'), Atom('fn'))
            or form[2] != (Atom('emitWinConsoleRuntime'), Atom('cg'))
            or not isinstance(form[3], Block)):
        raise ValueError('unsupported console emitter definition')
    statements = form[3]
    if not statements or type(statements[-1]) is not Atom or statements[-1] != 'cg':
        raise ValueError('console emitter must return its context')
    lines = []
    for statement in statements[:-1]:
        if (type(statement) is not tuple or len(statement) != 3
                or any(type(value) is not Atom for value in statement[:2])
                or statement[:2] != (Atom('emitLine'), Atom('cg'))):
            raise ValueError(f'unsupported console emitter statement {statement!r}')
        value = constant_expression(statement[2])
        if type(value) is not str:
            raise ValueError('emitLine requires a constant String')
        lines.append(value)
    return lines


def emitted_source(source):
    body = '\n'.join(emitted_lines(source.read_text())) + '\n'
    assert "define internal i64 @__axiom_console_take" in body
    return body


def source_reader_selftest():
    def emitter(body):
        return '(pub fn (emitWinConsoleRuntime cg) { ' + body + ' cg })'

    positive = [
        ('(emitLine cg "literal (parentheses)")', ['literal (parentheses)']),
        ('(emitLine cg (concat "prefix " (concat (fmtInt (<< 1 20)) " suffix")))',
         ['prefix 1048576 suffix']),
        ('(emitLine cg (fmtInt (<< 1 63)))', ['-9223372036854775808']),
        ('(emitLine cg (fmtInt (<< -1 0)))', ['-1']),
        (r'(emitLine cg "quote\" slash\\ zero\0 newline\n")', ['quote" slash\\ zero\0 newline\n']),
        ('; (emitLine cg unknown)\n(emitLine cg "one") #| (emitLine cg bad) #| nested |# |#', ['one']),
        ('(emitLine cg "(emitLine cg unknown)")', ['(emitLine cg unknown)']),
    ]
    for source, expected in positive:
        assert emitted_lines(emitter(source)) == expected, source
    negative = [
        '(emitLine cg unknown)', '(emitLine cg (unknown "x"))', '(emitLine cg 1)',
        '(emitLine cg (concat "x"))', '(emitLine cg (concat "x" "y" "z"))',
        '(emitLine cg (concat "x" 1))', '(emitLine cg (fmtInt "1"))',
        '(emitLine cg (fmtInt 1 2))', '(emitLine cg (<< 1 "2"))',
        '(emitLine cg (fmtInt (<< 1 -1)))', '(emitLine cg (fmtInt (<< 1 64)))',
        '(emitLine cg (fmtInt 9223372036854775808))', '(emitLine other "x")',
        '(emitLine "cg" "x")', '("emitLine" cg "x")',
        '(emitLine cg "x" "y")', '(if true (emitLine cg "x") (emitLine cg "y"))',
        '(let value "x")', r'(emitLine cg "bad\q")', '(emitLine cg "unterminated)',
        '(emitLine cg "x"}', '#| unterminated', '(emitLine cg "x"',
    ]
    refused = [emitter(source) for source in negative] + [
        '(pub fn (other cg) { cg })',
        emitter('') + emitter(''),
        '(pub fn (emitWinConsoleRuntime cg) { (emitLine cg "x") })',
        '(pub fn (emitWinConsoleRuntime cg) { "cg" })',
    ]
    for source in refused:
        try:
            emitted_lines(source)
        except ValueError:
            continue
        raise AssertionError(f'unsupported emitter expression was accepted: {source}')
    print(f'ok   strict console source reader: {len(positive)} accepted and {len(refused)} refused forms')


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
    source_reader_selftest()
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
    parser.add_argument("source", type=Path, nargs='?', help="core/codegen.ax")
    parser.add_argument("work", type=Path, nargs='?', help="isolated output directory")
    parser.add_argument("--module", type=Path, help="also verify the actual emitted LLVM module")
    parser.add_argument("--selftest", action='store_true', help="test the static source reader without a toolchain")
    args = parser.parse_args()
    if args.selftest:
        source_reader_selftest()
    elif args.source is None or args.work is None:
        parser.error('source and work are required unless --selftest is used')
    else:
        verify(args.source, args.work, args.module)
