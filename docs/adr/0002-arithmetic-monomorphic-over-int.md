# ADR 0002: Arithmetic operators are monomorphic over `Int`

## Status

Accepted (with a stated, honest follow-up — see `docs/ROADMAP.md`).

## Context

`perelang`'s type checker (`types.py`) is a real Hindley-Milner implementation (Algorithm W) with
both `Int` and `Float` as first-class types. The question that had to be settled before writing
`_infer_binary` was: what does `+` do when one or both operands are `Float`?

Two honest options existed:

1. **A numeric tower**: `+` accepts `Int` or `Float` operands, with implicit widening
   (`Int + Float -> Float`) and a type-class-like mechanism (or ad-hoc special-casing in `unify`)
   to make this work without breaking Hindley-Milner's principal-types guarantee.
2. **Monomorphic-over-`Int`**: `+ - * / %` unify both operands, and their result, to `Int`,
   unconditionally. `Float` remains a real type — a literal, storable in a `List<Float>`, comparable
   with `==` — it just has no arithmetic operator that accepts it.

Option 1 is real functionality real languages have, but it's also where a lot of accidental
complexity lives in practice (implicit numeric coercion is a classic source of surprising
behavior, and doing it *correctly* inside an HM inference engine — without silently unifying types
that shouldn't unify — is a meaningfully larger undertaking than the rest of `_infer_binary`
combined).

## Decision

Ship option 2. `_ARITHMETIC` operators unify both operands to `T_INT`, full stop. This is not a
novel choice — OCaml, one of the canonical HM-typed languages, draws exactly this line today: `+`
is `int -> int -> int`, and `+.` is the separate `float -> float -> float` operator, with no
implicit coercion between them.

## Consequences

- Every arithmetic test in this codebase is written against `Int`. `Float` has tests for literals,
  storage, and comparison, but never arithmetic — that gap is real, not an oversight the tests
  happen to not cover.
- The honest completion of this feature, if it happened, is adding a second set of operators
  (`+. -. *. /.` or similar) that unify to `T_FLOAT` — the same *kind* of change OCaml made, not a
  bigger refactor of `unify`/`TypeScheme`/`generalize`. This is listed explicitly in
  `docs/ROADMAP.md` as genuine follow-up work, not hidden as if `Float` support were complete.
- This also means the LLVM backend (which reuses the same IR) never needed to solve float codegen
  either — its own stated scope cut (`docs/adr/0003-llvm-backend-scope-cuts.md`) inherits this one
  rather than needing an independent justification for excluding `Float`.

**Update, implemented:** the second operator set predicted above has now been added exactly as
described — `+. -. *. /.` (`tokens.py`'s `PLUSDOT`/`MINUSDOT`/`STARDOT`/`SLASHDOT`) unify both
operands and the result to `T_FLOAT` via `Inferencer._FLOAT_ARITHMETIC`, mirrored in `ir.py`'s
`_Lowerer`, `bytecode.py` (reusing the existing `ADD`/`SUB`/`MUL`/`DIV` opcodes — `vm.py` already
dispatched on the Python runtime type of its operands, not on which source operator produced them),
and the LLVM backend (`fadd`/`fsub`/`fmul`/`fdiv`/`fcmp_ordered`, type-directed — see
`docs/adr/0003-llvm-backend-scope-cuts.md`'s own update note). Plain `+ - * / %` remain exactly as
monomorphic over `Int` as this ADR originally decided — `1 +. 2.0` is still a type error, matching
OCaml's own no-implicit-coercion behavior. See `examples/circle_area.pgr` for an end-to-end example.
