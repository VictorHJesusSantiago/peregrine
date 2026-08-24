# ADR 0003: LLVM backend scope — no closures, no heap-allocated values

## Status

Accepted.

## Context

`perelang` ships two execution backends over the same `ir.IrProgram`: the bytecode VM (`vm.py`),
which supports the full language, and an LLVM JIT backend (`llvm_backend.py`) via `llvmlite`
(confirmed genuinely functional in this environment — a real `add(a, b)` function was
JIT-compiled and executed through `ctypes`, returning the correct result, before committing to
build this backend at all).

The two backends are not required to have identical coverage. The interesting, credibility-bearing
property an LLVM backend needs to prove is: real machine-code generation, a real function-calling
convention, and real recursion working end-to-end — not 100% feature parity with the bytecode
interpreter. Building full parity would require solving two problems that are each substantial on
their own:

1. **Closures.** `ir.IrClosure.captures` already gives the exact free-variable list — the missing
   piece is a runtime representation (a heap-allocated capture struct) and an extended calling
   convention (every closure call needs an implicit environment argument alongside its declared
   parameters), plus JIT-time struct-layout codegen for each distinct closure shape.
2. **`String`/`List` values.** Both are heap-allocated in the bytecode VM (Python's own `str`/
   `tuple`, backed by Python's memory management). A raw-LLVM version needs an actual allocator and
   some GC or ownership story before either type can exist at that level — otherwise every list
   literal would leak.

## Decision

Scope the LLVM backend to: `Int`/`Bool` values (with `Bool` represented as `i64`, not `i1` — see
the note in `llvm_backend.py`'s module docstring on why, specifically to sidestep `i1`'s
ABI-dependent return-value promotion rules when calling back into Python via `ctypes`), arithmetic,
comparisons, `if`/`else`, and non-closure top-level function calls including recursion. Every
construct outside this subset — a closure, a `String`, a `List`, a reference to a top-level `let`,
a `Float` — raises a specific `LlvmBackendUnsupportedError` naming exactly what's unsupported,
rather than silently producing a wrong result or a confusing generic crash.

## Consequences

- The backend's test suite (`test_llvm_backend.py`) proves the meaningful thing: recursive
  factorial and Fibonacci produce bit-identical results to the bytecode VM's own versions of the
  same tests, plus a dedicated suite of "unsupported construct raises cleanly" tests.
- `pere run --backend llvm` is a real, usable option for programs that stay within this subset —
  which includes every purely-numeric/recursive algorithm, the most common thing anyone would want
  a JIT for in the first place.
- If closures were added later, the natural extension point is `_compile_closure` in
  `bytecode.py`'s equivalent — `_FunctionCodegen` in `llvm_backend.py` — gaining a heap-struct
  capture representation; this wasn't attempted here because it's a genuinely separate unit of
  work, not a quick addition, and honestly reporting that boundary was judged more valuable than a
  half-working closure implementation.

**Update, implemented:** `Float` support has been added — `_llvm_type_for` maps `Float` to a
genuine LLVM `double` (alongside `Int`/`Bool` -> `i64`), and codegen is now type-directed:
`_compile_binary`/`_compile_unary` pick `fadd`/`fsub`/`fmul`/`fdiv`/`fneg`/`fcmp_ordered` over
`add`/`sub`/`mul`/`sdiv`/`icmp_signed` based on the operand's resolved type, and `_compile_if`'s
merge-point `phi` is likewise typed off the branch's resolved type rather than hardcoded to `i64`.
`run_llvm`'s `ctypes` call signature is built from `entry`'s actual declared parameter/return types
(`c_double` for `Float`, `c_int64` for `Int`/`Bool`) instead of assuming `i64` for everything, so a
`Float`-returning or `Float`-parameter entry function now calls correctly. `Float` support was only
possible once `docs/adr/0002-arithmetic-monomorphic-over-int.md`'s `+. -. *. /.` operators existed
for this backend's `IrBinary`/`IrUnary` nodes to actually carry a `Float`-typed shape to compile in
the first place — see that ADR's own update note.

**Update, implemented:** closures and `String`/`List` values have both been added, closing the two
substantial gaps described above. Closures: every closure value (a genuine capturing lambda or a
top-level `fn` used as a first-class value) is a `malloc`'d 2-word `{ i8* fn_ptr, i8* env_ptr }`
struct; a lambda literal synthesizes its own top-level LLVM function with an extra leading `i8* env`
parameter, and its creation site `malloc`s a per-closure capture struct (shaped from
`ir.IrClosure.captures`, typed off each captured SSA value's own already-known LLVM type) and copies
the captured values into it. `_compile_call` keeps the original direct-call fast path for a known
top-level `fn` referenced by name, and gained an indirect path — bitcast the closure's opaque
`fn_ptr` to the exact signature the call site's resolved types demand, then call it with `env_ptr`
prepended — for everything else (a closure stored in a local, an immediately-invoked lambda, or a
top-level `fn` passed around as a value, which gets wrapped in the same representation via a
synthesized env-ignoring thunk). `String`/`List`: since neither type has a mutating, concatenating,
or indexing operator in this language, both compile to a plain-value 2-word struct (`{ i64 len, i8*
data }` for `String`, `{ i64 len, T* data }` for `List<T>`) rather than needing any GC/ownership
story — a `String` literal's bytes are a constant global array (no allocation), a `List` literal's
backing storage is a single `malloc` plus a straight-line unrolled store per item (the count is
always compile-time-known). Neither closures' capture environments nor `List` backing storage are
ever `free`'d — a deliberate arena/leak simplification, acceptable for a short-lived JIT-executed
program, stated plainly in `llvm_backend.py`'s module docstring rather than hidden. The one
boundary that remains exactly as scoped originally: `run_llvm`'s `entry` function must still resolve
to Int/Bool/Float end-to-end (`_ctypes_type_for` has no `ctypes` representation for a raw heap
struct), and a top-level `let` referenced as a *value* is still unsupported — both still raise
`LlvmBackendUnsupportedError` exactly as before. See `llvm_backend.py`'s module docstring for the
full mechanism and `test_llvm_backend.py` for the closure/`String`/`List` test coverage.

**Update, implemented:** a top-level `let` referenced as a value has been added, closing what was
by then this backend's last remaining scope gap (the `entry`-boundary Int/Bool/Float restriction
noted just above is unrelated and still stands). Every `ir.IrGlobalLet` compiles to a real `internal`
`llvmlite.ir.GlobalVariable`; one synthesized `void()` module-constructor function compiles each
global's initializer, in `program.decls` source order, and stores the result — the LLVM-IR sibling
of `vm.py`'s own synthesized `<init>` bytecode chunk (`bytecode.py`'s `compile_program`), same
"initialize every global once, in order, before anything else runs" semantics. That constructor is
registered via the standard `@llvm.global_ctors` mechanism (`llvmlite.ir` has no dedicated helper
for it, so it's built by hand: an `appending`-linkage global array of `{ i32 priority, void()* fn,
i8* data }` structs), which `run_llvm`'s pre-existing `engine.run_static_constructors()` call already
invokes — no change to `run_llvm`'s own sequencing was needed. `_compile_name`'s `is_global` branch
now resolves a name against the global-variable table before falling back to the top-level-function
table, so a `let` is usable as a value from any compiled function body, not just the constructor. See
`llvm_backend.py`'s module docstring for the full mechanism and `test_llvm_backend.py`'s
`TestTopLevelGlobals` for coverage, including a global initialized from a real function call rather
than a literal.
