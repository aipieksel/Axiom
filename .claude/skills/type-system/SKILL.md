---
name: type-system
description: Load before adding or changing an Axiom language feature, compiler pass, standard-library API, or type. Axiom is a systems language in the Rust sense: type safety and memory safety are the product, not a later cleanup. New work is typed in every pass that reads the fact, from the first commit. A missing value is Option or Result. One pass becoming typed does not mean the compiler is typed.
---

# Type the fact in every pass

Axiom is a systems language. A value is a word, a scalar, or a
reference the memory model can account for. The type of that value is
what lets the compiler refuse a use that would dangle, alias, or
silently mean two things. That is the same priority Rust has: if the
type is wrong, the program is wrong, even when it appears to run.

The failure this skill exists to stop is a feature that looks finished
because one pass was updated. The parser grew an `Option`, the checker
still stored a zero, codegen still branched on null, and the tools
still printed the old sentinel. Each pass was locally consistent.
None of them was the compiler. The hole stayed open until a later
migration had to find every site by hand.

Do not repeat that. A new fact is typed everywhere it is read, in the
commit that introduces it.

## What "typed" means here

Give every new function a signature. `(:: name Type)` is the contract
the later passes trust. A body that needs another type gets `AX3089`
at the definition. Do not leave a parameter to default to `Int` and
discover the real type three passes later.

A missing value is `None` or `Err`. An empty success is `(Some ...)`
or `(Ok ...)`, and those two are different. `0`, `""`, `-1`, and a
null word are not missing-value types. They are ordinary data, and
using them as sentinels is how a later pass stops being able to tell
"nothing" from "the value zero".

Match every constructor. Poison (`TAG_T_ERR`, `mkTErr`, `tyIsErr`)
means a diagnostic was already reported. It is not a stand-in for a
type you have not decided. `axiom-helper` owns the poison and
diagnostic rules. Follow them. Do not invent a second error channel.

`?` propagates through a tail, a branch, a `let` body, a loop, a
`region`, and a `handle`. The enclosing function answers the `Err`,
and nothing after the `?` runs. A `Result` leaves a `region` only when
both its payload and its error are scalars. A `String` error does not
leave: the region would reclaim it (`AX3059`). Take that `Result`
apart with `match`, or change the error to a scalar before the `?`.

A region answers its body's type. That type is part of the feature.
A reference result is refused. Do not smuggle the reference out
through an integer cast or an untyped slot.

## Compiler-wide means every reader

The pipeline is lexer, parser, expand, typecheck, codegen, then the
tools that read what those passes record: LSP, metadata, the driver,
format, and explain. Codegen does not import a private copy of the
checker's story. It consumes the types the check produced. If you
change what a construct means, name every pass that branches on the
old shape and update those branches in the same change.

Before you call the change done, search the tree for the old shape.
A zero used as "no node", a null child, a negative index used as
"not found", or a string used as "absent" is a site. Either it is now
`Option` or `Result`, or you can say in one sentence why that
particular word is not a value. "The checker is done" is not that
sentence.

Work through the passes in pipeline order when the fact crosses them.
Do not land the checker and leave codegen, the expander, or the LSP
for a follow-up that nobody owns. A follow-up that is truly separate
stays named, with the searches that still hit, and the feature is not
described as complete.

## What stays a number

Some words are numbers on purpose. Leave them numbers.

- Offsets, arities, bit masks, flags, and counts.
- A span or a source location stored in the sealed AST and span ABI.
- The front-end pack wire format, raw memory, and the FFI word.
- A checked fact codegen is required to trust, once the checker has
  accepted it. Do not re-decide that fact with a sentinel.

If you are about to store "missing" in one of these, stop. The missing
case belongs in an `Option` beside the number, or the number's type is
wrong. A comment that says "zero means none here" is the sentinel
coming back.

## Review before you finish

Run this on the change, not on the whole language:

1. Every new or edited function has a signature, and the signature
   names `Option`, `Result`, or a real type where a value can be
   absent or can fail.
2. `match` covers the constructors. A wildcard is a decision, not a
   way to skip a constructor you do not want to name.
3. Search the passes that read this fact. The same search is clean,
   or the remaining hits are numbers from the list above.
4. The test shows the typed case: `None` against `(Some empty)`,
   `Err` against a successful zero, a scalar `Result` leaving a
   region, a reference `Result` refused. A test that only runs the
   happy path does not hold the type.
5. Diagnostics go through `mkDiag`. A new code is explained, and an
   old code is not renumbered. `axiom-helper` is the diagnostic
   guide.
6. Spec rules keep their identifiers. `docs/memory-model.md` and
   `docs/error-model.md` describe the behaviour you just made true.
   `docs-style` is the page guide. One "Not yet" line and a roadmap
   link if something is still open. Do not open a second roadmap.

`axiom --diagnostic-format=ai check` on the changed program, and
`axiom fmt` on changed `.ax` files. That is the local bar. GitHub CI
runs the broad suite. Do not treat a green checker shard as proof
that codegen, the expander, metadata, and the tools agree.

## How this fits the rest of the work

Develop on `nightly`. Keep `stable` releasable. One change is one
typed fact, pushed when that fact holds in the passes it touches.
Do not widen a commit into a repository-wide migration unless that
migration is the task. Do not leave a new sentinel behind because a
migration is happening somewhere else.

Load `axiom-helper` for diagnostics, effects, poison, and the
pipeline. Load `docs-style` before editing a page. This skill does
not replace either. It decides whether the feature is allowed to
exist as an untyped hole. It is not.
