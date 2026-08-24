"""An alternative-to-`vm.py` execution path: JIT-compiles `ir.py`'s IR to real LLVM IR via
`llvmlite`, then executes it through an MCJIT execution engine, going straight through machine
code rather than `vm.py`'s bytecode interpreter loop.

**Scope, stated plainly, not hidden.** This backend covers Int/Bool/Float arithmetic (`+ - * / %`
over Int, `+. -. *. /.` over Float — see `types.py`), comparisons (over Int/Bool or over Float,
each with the matching LLVM instruction family — see `_compile_binary`), `if`/`else`, closures
(see below), `String`/`List` values (see below), and function calls — including recursion, the
property that actually proves a real function-calling convention rather than a single expression
evaluator. Codegen is type-directed: every IR node carries its resolved `types.Type` (see `ir.py`),
and `_llvm_type_for` is the single place that maps a Peregrine type to an LLVM type (`Int`/`Bool`
-> `i64`, `Float` -> a genuine LLVM `double`, `String`/`List<T>` -> a 2-word struct, a function type
-> a pointer to the 2-word closure struct — see below) — `_compile_binary`/`_compile_unary`/
`_compile_if` all branch off an operand's or a branch's resolved type to pick the matching
instruction family (`add` vs `fadd`, `icmp_signed` vs `fcmp_ordered`, ...) rather than assuming
everything is `i64`. Any IR node outside this subset raises `LlvmBackendUnsupportedError` with a
specific message naming the construct, rather than silently miscompiling or producing a wrong
result.

**Update, implemented: top-level `let` globals as values.** A top-level `let` (`ir.IrGlobalLet`)
now compiles to a real `llvmlite.ir.GlobalVariable` (`internal` linkage, one per global — see
`build_llvm_module`) that any compiled function body can load through `_compile_name`'s `is_global`
branch, exactly like a `fn` name resolves to a callable. Its initializer is computed once, before
any "real" code runs, by one synthesized `void()` module-constructor function
(`_build_global_ctor`) that compiles every global's initializer expression in `program.decls`
source order and stores each result into its `GlobalVariable` — the LLVM-IR equivalent of `vm.py`'s
own synthesized `<init>` bytecode chunk (see `bytecode.py`'s `compile_program`), same
"initialize every global once, in declaration order, before `main`" semantics, just expressed as
real machine code instead of an interpreted chunk. That constructor is registered in the standard
`@llvm.global_ctors` array (an `appending`-linkage global holding `{ i32 priority, void()* fn, i8*
data }` entries — see `_build_global_ctor`), which `run_llvm`'s existing
`engine.run_static_constructors()` call (already present, already run after `finalize_object()` and
before `entry` is looked up) invokes automatically — no change to `run_llvm`'s own call sequence
was needed, only to what `build_llvm_module` registers for it to run. A later global's initializer
may reference an earlier one by name (matching this language's sequential top-level scoping — see
`types.py`'s `infer_program`/`ir.py`'s `lower_program`, both of which only make a top-level name
visible to declarations *after* it) and may call a top-level `fn`, including one that itself
doesn't depend on evaluation order; a global cannot legally reference a *later* global (or itself),
matching `vm.py`'s own identical limitation. This closes what was previously this backend's last
stated scope gap: a program with a top-level `let` referenced as a value now compiles and runs
correctly via `run_llvm`, not just one where nothing touches the `let`.

**Update, implemented: closures.** A closure value — whether a genuine capturing lambda
(`ir.IrClosure`) or a top-level `fn` referenced as a first-class value (see `_compile_name`) — is a
heap-allocated (`malloc`'d, never `free`'d — see the note at the `malloc` declaration in
`build_llvm_module`) 2-word struct `{ i8* fn_ptr, i8* env_ptr }`. Compiling an `ir.IrClosure`
synthesizes a new top-level LLVM function (an internal name like `<lambda>.3`, unique via
`_ModuleCtx.fresh_name`) whose *actual* signature is `(i8* env, <declared param types...>) -> <body
type>`; at the closure's *creation* site, the values of `closure.captures` (as `ir.py`'s
free-variable analysis already computed them, in order) are copied — by value, matching this
language's no-reassignment semantics (see `ir.py`'s module docstring, point 3) — into a freshly
`malloc`'d per-closure capture struct shaped exactly like `closure.captures`, whose per-field LLVM
types come from the *creation-site* `env` dict's already-known `llvmlite.ir.Value.type` for each
captured name (no separate type bookkeeping needed — the SSA values flowing through codegen already
carry their own LLVM types). Calling a closure value (`_compile_call`'s indirect path, used for any
callee that isn't a direct `ir.IrName(is_global=True)` reference to an already-declared top-level
`fn`) loads `fn_ptr`/`env_ptr` back out, bitcasts the opaque `fn_ptr` to the exact
`(i8*, <arg types>) -> <ret type>` function-pointer type the call site's own resolved types demand
(computed via `_llvm_type_for`, exactly as everywhere else), and calls it with `env_ptr` prepended
to the compiled argument values. A top-level `fn` referenced as a value (not called directly by
name) is wrapped in this same representation by a synthesized thunk (`_build_top_level_wrapper`,
cached per function name) that accepts-and-ignores an `env` parameter and tail-calls straight
through to the real function — this keeps every closure value, capturing or not, uniformly callable
through the one indirect-call path, matching `vm.py`'s own design note that a non-capturing
`Closure` is just one with `captured=()`.

**Update, implemented: `String`/`List`.** Neither type has a mutating, concatenating, or indexing
operator in this language (see `types.py`; `ir.IrStr` and `ir.IrListLit` are the only
value-producing forms), so both compile to a plain 2-word-struct *value* (not an opaque handle) with
no runtime/GC story needed. A `String` is `{ i64 len, i8* data }`, where `data` points at the
literal's UTF-8 bytes embedded as a module-level constant global array (no allocation — a literal's
bytes are compile-time-known). A `List<T>` is `{ i64 len, T* data }`, where `data` is `malloc`'d
(sized `len * sizeof(T)`, `sizeof` computed via the classic null-pointer-GEP-then-`ptrtoint` trick
so it's correct for the real JIT target without needing a `DataLayout` at IR-build time) and each
item stored via a straight-line unrolled GEP+store sequence — the item count is always
compile-time-known from the literal itself, so no loop is needed. Both values flow through the rest
of codegen exactly like an Int/Bool/Float SSA value: as a local, a function argument, a return value
between Peregrine functions, recursion, etc. **The outer JIT boundary stays numeric-only**:
`_ctypes_type_for`/`run_llvm`'s `entry` function must still resolve to Int/Bool/Float end to end (a
`String`/`List`-typed `entry` still raises `LlvmBackendUnsupportedError` — there is no sensible
`ctypes` representation for a raw heap struct without also building a "materialize an LLVM value
back into a Python object" step, which is a separate, larger undertaking outside this backend's
scope). So a program with a `String`/`List`-typed `entry` still fails to *run* via `run_llvm`, but
any `String`/`List` value used purely *inside* the compiled call graph — arguments, locals, return
values passed between two Peregrine `fn`s — now compiles and runs correctly.

**Bool is represented as `i64` (0/1), not LLVM's native `i1`.** `i1` is the semantically correct
choice for a boolean at the IR level, but its C calling-convention treatment (whether it arrives
sign- or zero-extended in a register) is platform/ABI-dependent in exactly the way that makes
calling into it through `ctypes.CFUNCTYPE` from Python fragile. Every comparison/logical op still
computes with a genuine `icmp`, immediately zero-extended to `i64` — this is a representation
choice for the *outermost* value boundary, not a weakening of the boolean logic itself.

**Lazy `llvmlite` import.** This module is the only place in the package that imports `llvmlite` —
`perelang/__init__.py` never does, so `import perelang` (and everything except this module) works
with no `llvm` extra installed. Every public function here re-raises a clear `ImportError` with
installation instructions if `llvmlite` is missing, rather than failing at package-import time
with a confusing traceback pointing at unrelated code.
"""

from __future__ import annotations

import ctypes
import threading

from perelang import ir
from perelang.types import TCon, TFun, Type

try:
    from llvmlite import binding as llvm
    from llvmlite import ir as llvmir

    _LLVMLITE_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only in an environment without the `llvm` extra
    _LLVMLITE_AVAILABLE = False


class LlvmBackendUnsupportedError(Exception):
    """A well-typed Peregrine program used a construct outside this backend's stated scope (a
    `String`/`List`/closure-typed `entry` at the `ctypes` boundary, a Unit-valued function body,
    ...) — not a bug, a documented boundary. Not a `PeregrineError` subclass: this isn't a
    language-level compile error (the program is perfectly valid Peregrine, `vm.py` runs it fine),
    it's this specific backend declining a request outside what it implements."""


def _require_llvmlite() -> None:
    if not _LLVMLITE_AVAILABLE:
        raise ImportError(
            "the LLVM backend requires llvmlite (`pip install perelang[llvm]`, i.e. llvmlite>=0.48); "
            "it is not installed in this environment"
        )


_INT64 = None  # set lazily in _types(), since llvmir isn't importable at module load time otherwise


def _types() -> tuple[object, object]:
    """`(i64, i64)` — both `Int` and `Bool` compile to `i64` (see module docstring)."""
    return llvmir.IntType(64), llvmir.IntType(64)


def _i8ptr() -> object:
    return llvmir.PointerType(llvmir.IntType(8))


def _i32(value: int) -> object:
    return llvmir.Constant(llvmir.IntType(32), value)


def _closure_struct_type() -> object:
    """The uniform 2-word heap runtime representation shared by *every* closure value this backend
    produces — a genuine capturing lambda (`_FunctionCodegen._compile_closure`) and a top-level
    `fn` referenced as a first-class value (`_compile_name` /
    `_FunctionCodegen._wrap_top_level_function_as_closure`) alike: `{ i8* fn_ptr, i8* env_ptr }`.
    `fn_ptr` is bitcast to the exact call signature needed at each call site (see
    `_FunctionCodegen._compile_call`); `env_ptr` points at a per-closure capture struct, or is
    unused/null when there is nothing to capture."""
    p = _i8ptr()
    return llvmir.LiteralStructType([p, p])


def _string_struct_type() -> object:
    return llvmir.LiteralStructType([llvmir.IntType(64), _i8ptr()])


def _list_struct_type(elem_ty: Type) -> object:
    return llvmir.LiteralStructType([llvmir.IntType(64), llvmir.PointerType(_llvm_type_for(elem_ty))])


def _llvm_type_for(ty: Type) -> object:
    if isinstance(ty, TCon) and ty.name in ("Int", "Bool") and not ty.args:
        return llvmir.IntType(64)
    if isinstance(ty, TCon) and ty.name == "Float" and not ty.args:
        return llvmir.DoubleType()
    if isinstance(ty, TCon) and ty.name == "String" and not ty.args:
        return _string_struct_type()
    if isinstance(ty, TCon) and ty.name == "List" and len(ty.args) == 1:
        return _list_struct_type(ty.args[0])
    if isinstance(ty, TFun):
        # Every closure value — capturing or not, a lambda literal or a top-level `fn` referenced
        # as a value — is a pointer to the same uniform 2-word struct (see module docstring and
        # `_closure_struct_type`).
        return llvmir.PointerType(_closure_struct_type())
    from perelang.types import type_to_str

    raise LlvmBackendUnsupportedError(f"the LLVM backend does not support values of type {type_to_str(ty)}")


def _is_float_type(ty: Type) -> bool:
    """Whether an already-resolved IR node's `.ty` is `Float` — the single predicate every
    type-directed codegen branch below (`_compile_binary`/`_compile_unary`/`_compile_if`) uses to
    pick `fadd`/`fsub`/... over `add`/`sub`/..., rather than each re-deriving it independently."""
    return isinstance(ty, TCon) and ty.name == "Float" and not ty.args


_INIT_LOCK = threading.Lock()
_INITIALIZED = False


def _ensure_native_target_initialized() -> None:
    """`llvm.initialize()` itself is deprecated in the installed `llvmlite` (0.48) and now *raises*
    rather than no-ops — confirmed directly in this environment. Only the two calls below are
    needed to JIT for the host target; guarded by a lock + flag since MCJIT setup is not something
    you want two threads racing on the first call."""
    global _INITIALIZED
    with _INIT_LOCK:
        if not _INITIALIZED:
            llvm.initialize_native_target()
            llvm.initialize_native_asmprinter()
            _INITIALIZED = True


class _ModuleCtx:
    """Shared, whole-module codegen state threaded through every `_FunctionCodegen` instance — one
    gets created per top-level `fn` body *and* per synthesized closure body (see
    `_FunctionCodegen._build_closure_function`), but they all share the same `module`, the same
    `malloc` declaration, the same name-keyed table of already-declared top-level functions (so a
    closure or an indirect call compiled while working on *any* function's body can still directly
    call any other top-level function), and the same fresh-name counter / wrapper cache so two
    closures synthesized while compiling two different functions never collide on a generated
    name."""

    def __init__(
        self,
        module: llvmir.Module,
        functions: dict[str, llvmir.Function],
        malloc_fn: llvmir.Function,
        global_vars: dict[str, llvmir.GlobalVariable],
    ) -> None:
        self.module = module
        self.functions = functions
        self.malloc_fn = malloc_fn
        # Top-level `let` name -> its backing `GlobalVariable` (see `build_llvm_module` and the
        # module docstring's `@llvm.global_ctors` note). Populated with every `ir.IrGlobalLet` in
        # `program.decls` *before* any function body (including the synthesized module constructor
        # itself) is compiled, so a reference to one — from an ordinary function or from a later
        # global's own initializer — always resolves (see `_FunctionCodegen._compile_name`).
        self.global_vars = global_vars
        self._name_counter = 0
        # Top-level `fn` name -> the synthesized `(i8* env, ...) -> ret` thunk wrapping it (see
        # `_FunctionCodegen._build_top_level_wrapper`) — built at most once per function, even if
        # that function is referenced as a first-class value more than once across the program.
        self.fn_value_wrappers: dict[str, llvmir.Function] = {}

    def fresh_name(self, prefix: str) -> str:
        self._name_counter += 1
        return f"{prefix}.{self._name_counter}"


class _FunctionCodegen:
    """Compiles one function body (a real `ir.IrFunction`'s, or a synthesized closure's) into an
    already-declared `llvmlite.ir.Function`. Locals are plain SSA values in a Python dict, not
    stack-allocated/`alloca`'d slots — sound because this language has no reassignment (see
    `ir.py`/`bytecode.py`'s identical reasoning for why captures and locals never need mutation
    machinery), so a `let`-bound name can just *be* the `llvmlite.ir.Value` its initializer
    produced, forever."""

    def __init__(self, builder: llvmir.IRBuilder, ctx: _ModuleCtx) -> None:
        self._builder = builder
        self._ctx = ctx

    def compile_expr(self, expr: ir.IrExpr, env: dict[str, llvmir.Value]) -> llvmir.Value:
        match expr:
            case ir.IrInt(value, _ty):
                return llvmir.Constant(llvmir.IntType(64), value)
            case ir.IrBool(value, _ty):
                return llvmir.Constant(llvmir.IntType(64), int(value))
            case ir.IrFloat(value, _ty):
                return llvmir.Constant(llvmir.DoubleType(), value)
            case ir.IrStr(value, _ty):
                return self._compile_str_lit(value)
            case ir.IrName(name, is_global, _ty):
                return self._compile_name(name, is_global, env)
            case ir.IrUnary(op, operand, _ty):
                return self._compile_unary(op, operand, env)
            case ir.IrBinary(op, left, right, _ty):
                return self._compile_binary(op, left, right, env)
            case ir.IrIf(cond, then_b, else_b, _ty):
                return self._compile_if(cond, then_b, else_b, env)
            case ir.IrCall(callee, args, ty):
                return self._compile_call(callee, args, ty, env)
            case ir.IrListLit(items, ty):
                return self._compile_list_lit(items, ty, env)
            case ir.IrClosure() as closure_node:
                return self._compile_closure(closure_node, env)
            case ir.IrBlock():
                return self.compile_block(expr, env)

    def _compile_name(self, name: str, is_global: bool, env: dict[str, llvmir.Value]) -> llvmir.Value:
        if is_global:
            # A top-level `let`'s name and a top-level `fn`'s name can never collide in a program
            # `ir.py`'s lowering actually produced from source that reached this far: both go
            # through the same sequential `TypeEnv`/lowering environment (see `types.py`'s
            # `infer_program` and `ir.py`'s `lower_program`), so `name` here is always exactly one
            # of "a `let` declared somewhere in `program.decls`" or "a `fn` declared somewhere in
            # `program.decls`" — never ambiguously both at once for the *same* reference. Globals
            # are checked first since they're the newly-added case; a `let` never appears in
            # `self._ctx.functions` and a `fn` never appears in `self._ctx.global_vars`, so the
            # order between the two checks doesn't actually matter.
            global_var = self._ctx.global_vars.get(name)
            if global_var is not None:
                return self._builder.load(global_var)
            target = self._ctx.functions.get(name)
            if target is None:
                raise AssertionError(f"{name!r} is marked global but is neither a known `fn` nor `let` — a lowering bug")
            # A top-level `fn` referenced as a first-class value (not called directly by name, see
            # `_compile_call`'s fast path) — wrap it in the same closure representation a lambda
            # uses, so it flows uniformly through the indirect-call path (see module docstring).
            return self._wrap_top_level_function_as_closure(target)
        if name not in env:
            raise AssertionError(f"{name!r} unresolved in LLVM codegen — a lowering bug in ir.py")
        return env[name]

    def _compile_unary(self, op: str, operand: ir.IrExpr, env: dict[str, llvmir.Value]) -> llvmir.Value:
        val = self.compile_expr(operand, env)
        if op == "-":
            return self._builder.sub(llvmir.Constant(llvmir.IntType(64), 0), val)
        if op == "-.":
            return self._builder.fneg(val)
        if op == "!":
            # `val` is 0/1 in an i64 (see module docstring); flip via `1 - val`.
            return self._builder.sub(llvmir.Constant(llvmir.IntType(64), 1), val)
        raise LlvmBackendUnsupportedError(f"unknown unary operator {op!r}")

    _ICMP = {"==": "==", "!=": "!=", "<": "<", "<=": "<=", ">": ">", ">=": ">="}
    # `+. -. *. /.` (see `types.py`'s `_FLOAT_ARITHMETIC`) lower to the exact same `IrBinary` shape
    # as their Int counterparts (see `ir.py`), distinguished only by `left.ty`/`right.ty` — this
    # maps each dotted operator to the base symbol so the dispatch below is written once, keyed on
    # the base symbol, with `_is_float_type` deciding which LLVM instruction family to emit.
    _FLOAT_OP_BASE = {"+.": "+", "-.": "-", "*.": "*", "/.": "/"}

    def _compile_binary(self, op: str, left: ir.IrExpr, right: ir.IrExpr, env: dict[str, llvmir.Value]) -> llvmir.Value:
        if op == "&&":
            return self._compile_short_circuit(left, right, env, is_and=True)
        if op == "||":
            return self._compile_short_circuit(left, right, env, is_and=False)

        lv = self.compile_expr(left, env)
        rv = self.compile_expr(right, env)
        # Both operands are guaranteed the same numeric type by the type checker (see `types.py`'s
        # `_ARITHMETIC`/`_FLOAT_ARITHMETIC`/`_COMPARISON`), so checking `left.ty` alone is enough.
        is_float = _is_float_type(left.ty)
        base_op = self._FLOAT_OP_BASE.get(op, op)
        if base_op == "+":
            return self._builder.fadd(lv, rv) if is_float else self._builder.add(lv, rv)
        if base_op == "-":
            return self._builder.fsub(lv, rv) if is_float else self._builder.sub(lv, rv)
        if base_op == "*":
            return self._builder.fmul(lv, rv) if is_float else self._builder.mul(lv, rv)
        if base_op == "/":
            return self._builder.fdiv(lv, rv) if is_float else self._builder.sdiv(lv, rv)
        if base_op == "%":
            return self._builder.srem(lv, rv)
        if base_op in self._ICMP:
            cmp_val = (
                self._builder.fcmp_ordered(self._ICMP[base_op], lv, rv)
                if is_float
                else self._builder.icmp_signed(self._ICMP[base_op], lv, rv)
            )
            return self._builder.zext(cmp_val, llvmir.IntType(64))
        raise LlvmBackendUnsupportedError(f"unknown binary operator {op!r}")

    def _compile_short_circuit(
        self, left: ir.IrExpr, right: ir.IrExpr, env: dict[str, llvmir.Value], *, is_and: bool
    ) -> llvmir.Value:
        lv = self.compile_expr(left, env)
        lv_bool = self._builder.icmp_signed("!=", lv, llvmir.Constant(llvmir.IntType(64), 0))
        rhs_bb = self._builder.append_basic_block("sc.rhs")
        merge_bb = self._builder.append_basic_block("sc.merge")
        short_circuit_bb = self._builder.block
        if is_and:
            self._builder.cbranch(lv_bool, rhs_bb, merge_bb)
        else:
            self._builder.cbranch(lv_bool, merge_bb, rhs_bb)

        self._builder.position_at_end(rhs_bb)
        rv = self.compile_expr(right, env)
        rhs_end_bb = self._builder.block
        self._builder.branch(merge_bb)

        self._builder.position_at_end(merge_bb)
        # Deliberately still hardcoded `i64`, unlike `_compile_if`'s phi above: `&&`/`||` are
        # Bool-only at the type level (see `types.py`'s `_LOGICAL`) and always will be — there is
        # no `Float` short-circuit to generalize this for.
        phi = self._builder.phi(llvmir.IntType(64))
        phi.add_incoming(lv, short_circuit_bb)
        phi.add_incoming(rv, rhs_end_bb)
        return phi

    def _compile_if(
        self, cond: ir.IrExpr, then_b: ir.IrBlock, else_b: ir.IrBlock, env: dict[str, llvmir.Value]
    ) -> llvmir.Value:
        cond_val = self.compile_expr(cond, env)
        cond_bool = self._builder.icmp_signed("!=", cond_val, llvmir.Constant(llvmir.IntType(64), 0))
        then_bb = self._builder.append_basic_block("if.then")
        else_bb = self._builder.append_basic_block("if.else")
        merge_bb = self._builder.append_basic_block("if.merge")
        self._builder.cbranch(cond_bool, then_bb, else_bb)

        self._builder.position_at_end(then_bb)
        then_val = self.compile_block(then_b, env)
        then_end_bb = self._builder.block
        self._builder.branch(merge_bb)

        self._builder.position_at_end(else_bb)
        else_val = self.compile_block(else_b, env)
        else_end_bb = self._builder.block
        self._builder.branch(merge_bb)

        self._builder.position_at_end(merge_bb)
        # Type-directed, unlike `_compile_short_circuit`'s phi below: an `if`/`else` can now yield
        # a `Float` (e.g. `if cond { 1.0 } else { 2.0 }`), so the merge point's phi must match
        # whatever `then_b`/`else_b` actually resolved to, not be hardcoded to `i64`.
        phi = self._builder.phi(_llvm_type_for(then_b.ty))
        phi.add_incoming(then_val, then_end_bb)
        phi.add_incoming(else_val, else_end_bb)
        return phi

    def _compile_call(
        self, callee: ir.IrExpr, args: tuple[ir.IrExpr, ...], ret_ty: Type, env: dict[str, llvmir.Value]
    ) -> llvmir.Value:
        # Fast path: a direct call to a known top-level `fn` by name — a plain LLVM `call` to the
        # already-declared `llvmlite.ir.Function`, no indirection through a closure struct needed.
        if isinstance(callee, ir.IrName) and callee.is_global and callee.name in self._ctx.functions:
            target = self._ctx.functions[callee.name]
            arg_vals = [self.compile_expr(a, env) for a in args]
            return self._builder.call(target, arg_vals)

        # Indirect path: `callee` compiles to a closure-struct pointer (see module docstring) — a
        # local/parameter holding a closure value, a closure literal called immediately, or a
        # top-level `fn` referenced as a value and wrapped into this same representation by
        # `_compile_name`. The expected LLVM signature at this call site is known from `IrCall`'s
        # own resolved argument/return types (the type checker already guarantees `callee`'s
        # function type matches them), so the opaque `fn_ptr` is bitcast to that exact
        # `(i8*, <arg types>) -> <ret type>` function-pointer type before being called, with
        # `env_ptr` passed as the leading argument.
        closure_val = self.compile_expr(callee, env)
        fn_field_ptr = self._builder.gep(closure_val, [_i32(0), _i32(0)], inbounds=True)
        env_field_ptr = self._builder.gep(closure_val, [_i32(0), _i32(1)], inbounds=True)
        fn_ptr = self._builder.load(fn_field_ptr)
        env_ptr = self._builder.load(env_field_ptr)

        arg_vals = [self.compile_expr(a, env) for a in args]
        target_fnty = llvmir.FunctionType(_llvm_type_for(ret_ty), [_i8ptr(), *(_llvm_type_for(a.ty) for a in args)])
        typed_fn_ptr = self._builder.bitcast(fn_ptr, llvmir.PointerType(target_fnty))
        return self._builder.call(typed_fn_ptr, [env_ptr, *arg_vals])

    def _compile_str_lit(self, value: str) -> llvmir.Value:
        """`ir.IrStr` is the only String-producing form in this language (no concatenation, no
        formatting — see `types.py`), so a String literal's bytes are always compile-time-known:
        embedded as a `constant`, `internal`-linkage global byte array, with the `{ len, data }`
        struct value built at each use site from a GEP to that array's first element — no
        allocation needed."""
        data = value.encode("utf-8")
        arr_ty = llvmir.ArrayType(llvmir.IntType(8), len(data))
        gvar = llvmir.GlobalVariable(self._ctx.module, arr_ty, name=self._ctx.fresh_name("str"))
        gvar.global_constant = True
        gvar.linkage = "internal"
        gvar.initializer = llvmir.Constant(arr_ty, bytearray(data))
        data_ptr = self._builder.gep(gvar, [_i32(0), _i32(0)], inbounds=True)
        result: llvmir.Value = llvmir.Constant(_string_struct_type(), llvmir.Undefined)
        result = self._builder.insert_value(result, llvmir.Constant(llvmir.IntType(64), len(data)), 0)
        result = self._builder.insert_value(result, data_ptr, 1)
        return result

    def _compile_list_lit(self, items: tuple[ir.IrExpr, ...], ty: Type, env: dict[str, llvmir.Value]) -> llvmir.Value:
        """`ir.IrListLit` is the only List-constructing form, always with a compile-time-known item
        count — so the backing storage is a single `malloc` sized `len * sizeof(elem)` (`sizeof`
        via `_sizeof`'s null-pointer-GEP trick) followed by a straight-line unrolled GEP+store per
        item, no loop needed."""
        assert isinstance(ty, TCon) and ty.name == "List" and len(ty.args) == 1
        elem_ty = ty.args[0]
        elem_llvm_ty = _llvm_type_for(elem_ty)
        elem_ptr_ty = llvmir.PointerType(elem_llvm_ty)
        n = len(items)
        if n == 0:
            # Nothing to store, and therefore nothing worth `malloc`ing — a null data pointer is
            # never dereferenced since `len` is 0 and this language has no indexing operator.
            data_ptr: llvmir.Value = llvmir.Constant(elem_ptr_ty, None)
        else:
            elem_size = self._sizeof(elem_llvm_ty)
            total_size = self._builder.mul(elem_size, llvmir.Constant(llvmir.IntType(64), n))
            raw = self._builder.call(self._ctx.malloc_fn, [total_size])
            data_ptr = self._builder.bitcast(raw, elem_ptr_ty)
            for i, item in enumerate(items):
                item_val = self.compile_expr(item, env)
                elem_ptr = self._builder.gep(data_ptr, [_i32(i)], inbounds=True)
                self._builder.store(item_val, elem_ptr)
        list_ty = _list_struct_type(elem_ty)
        result: llvmir.Value = llvmir.Constant(list_ty, llvmir.Undefined)
        result = self._builder.insert_value(result, llvmir.Constant(llvmir.IntType(64), n), 0)
        result = self._builder.insert_value(result, data_ptr, 1)
        return result

    def _sizeof(self, llvm_ty: object) -> llvmir.Value:
        """The classic "null-pointer GEP" trick to compute `sizeof(llvm_ty)` in bytes as a runtime
        `i64`, without needing a `TargetData`/`DataLayout` at IR-construction time: indexing one
        element past a null pointer of that type and `ptrtoint`-ing the result yields exactly the
        type's size once LLVM lowers it for the real target."""
        null_ptr = llvmir.Constant(llvmir.PointerType(llvm_ty), None)
        one_past = self._builder.gep(null_ptr, [_i32(1)], inbounds=True)
        return self._builder.ptrtoint(one_past, llvmir.IntType(64))

    def _make_closure_struct(self, fn_ptr: llvmir.Value, env_ptr: llvmir.Value) -> llvmir.Value:
        """`malloc`s the outer 2-word `{ i8* fn_ptr, i8* env_ptr }` struct (see module docstring)
        and stores the given fields into it — the shared tail end of both `_compile_closure` (a
        genuine lambda) and `_wrap_top_level_function_as_closure` (a top-level `fn` used as a
        value)."""
        closure_ty = _closure_struct_type()
        size = self._sizeof(closure_ty)
        raw = self._builder.call(self._ctx.malloc_fn, [size])
        typed = self._builder.bitcast(raw, llvmir.PointerType(closure_ty))
        fn_field = self._builder.gep(typed, [_i32(0), _i32(0)], inbounds=True)
        self._builder.store(fn_ptr, fn_field)
        env_field = self._builder.gep(typed, [_i32(0), _i32(1)], inbounds=True)
        self._builder.store(env_ptr, env_field)
        return typed

    def _compile_closure(self, closure: ir.IrClosure, env: dict[str, llvmir.Value]) -> llvmir.Value:
        """Creates a closure value at the point `closure` (a lambda literal) is evaluated. Params
        with no captures still go through this exact path (see module docstring) — `closure.captures`
        is simply empty, so the capture struct is a zero-field struct and `env_ptr` ends up null."""
        capture_types = [env[name].type for name in closure.captures]
        capture_struct_ty = llvmir.LiteralStructType(capture_types)
        fn = self._build_closure_function(closure, capture_struct_ty)

        env_ptr: llvmir.Value
        if closure.captures:
            env_size = self._sizeof(capture_struct_ty)
            raw_env = self._builder.call(self._ctx.malloc_fn, [env_size])
            typed_env_ptr = self._builder.bitcast(raw_env, llvmir.PointerType(capture_struct_ty))
            for i, name in enumerate(closure.captures):
                field_ptr = self._builder.gep(typed_env_ptr, [_i32(0), _i32(i)], inbounds=True)
                self._builder.store(env[name], field_ptr)
            env_ptr = raw_env  # already `i8*`, exactly what the outer closure struct wants
        else:
            env_ptr = llvmir.Constant(_i8ptr(), None)

        fn_ptr = self._builder.bitcast(fn, _i8ptr())
        return self._make_closure_struct(fn_ptr, env_ptr)

    def _build_closure_function(self, closure: ir.IrClosure, capture_struct_ty: llvmir.BaseStructType) -> llvmir.Function:
        """Synthesizes the actual LLVM function a closure literal's body compiles into: signature
        `(i8* env, <declared param types>) -> <body type>`. At entry, the incoming opaque `env` is
        bitcast to `capture_struct_ty*` (shaped exactly like `closure.captures`, decided by the
        *caller*, `_compile_closure`, from the creation site's own `env` dict) and each field is
        GEP+loaded into this function's own local `env` dict under its original name — so the rest
        of `compile_expr`'s existing name-resolution logic (a plain dict lookup) works completely
        unchanged once this setup is done."""
        assert isinstance(closure.ty, TFun)
        param_llvm_types = [_llvm_type_for(pt) for pt in closure.param_types]
        ret_llvm_type = _llvm_type_for(closure.ty.ret)
        fnty = llvmir.FunctionType(ret_llvm_type, [_i8ptr(), *param_llvm_types])
        fn = llvmir.Function(self._ctx.module, fnty, name=self._ctx.fresh_name("<lambda>"))

        block = fn.append_basic_block("entry")
        inner_builder = llvmir.IRBuilder(block)
        inner_env: dict[str, llvmir.Value] = {}
        if closure.captures:
            env_arg = inner_builder.bitcast(fn.args[0], llvmir.PointerType(capture_struct_ty))
            for i, name in enumerate(closure.captures):
                field_ptr = inner_builder.gep(env_arg, [_i32(0), _i32(i)], inbounds=True)
                inner_env[name] = inner_builder.load(field_ptr)
        for pname, arg_val in zip(closure.params, fn.args[1:], strict=True):
            inner_env[pname] = arg_val

        inner_codegen = _FunctionCodegen(inner_builder, self._ctx)
        result = inner_codegen.compile_expr(closure.body, inner_env)
        inner_builder.ret(result)
        return fn

    def _wrap_top_level_function_as_closure(self, target: llvmir.Function) -> llvmir.Value:
        wrapper = self._ctx.fn_value_wrappers.get(target.name)
        if wrapper is None:
            wrapper = self._build_top_level_wrapper(target)
            self._ctx.fn_value_wrappers[target.name] = wrapper
        fn_ptr = self._builder.bitcast(wrapper, _i8ptr())
        env_ptr = llvmir.Constant(_i8ptr(), None)  # a top-level function captures nothing
        return self._make_closure_struct(fn_ptr, env_ptr)

    def _build_top_level_wrapper(self, target: llvmir.Function) -> llvmir.Function:
        """A top-level `fn`, once referenced as a first-class value (see `_compile_name`), needs an
        `(i8* env, ...) -> ret` entry point to be uniformly callable through `_compile_call`'s
        indirect path alongside genuine closures — its own compiled signature has no leading `env`
        parameter at all. This synthesizes exactly that: a thunk with the closure calling
        convention that ignores its `env` argument and calls straight through to `target`. Cached
        per top-level function name in `_ModuleCtx.fn_value_wrappers`, so referencing the same
        function as a value more than once reuses one wrapper instead of emitting duplicates."""
        target_fnty = target.function_type
        fnty = llvmir.FunctionType(target_fnty.return_type, [_i8ptr(), *target_fnty.args])
        wrapper = llvmir.Function(self._ctx.module, fnty, name=self._ctx.fresh_name(f"{target.name}.fnval"))
        block = wrapper.append_basic_block("entry")
        b = llvmir.IRBuilder(block)
        result = b.call(target, list(wrapper.args[1:]))
        b.ret(result)
        return wrapper

    def compile_block(self, block: ir.IrBlock, env: dict[str, llvmir.Value]) -> llvmir.Value:
        local_env = dict(env)
        for stmt in block.stmts:
            match stmt:
                case ir.IrLet(name, value):
                    local_env[name] = self.compile_expr(value, local_env)
                case ir.IrExprStmt(inner):
                    self.compile_expr(inner, local_env)
        if block.tail is None:
            raise LlvmBackendUnsupportedError(
                "the LLVM backend does not support Unit-valued blocks (every compiled function must "
                "return an Int, Bool, Float, String, List, or function value)"
            )
        return self.compile_expr(block.tail, local_env)


def build_llvm_module(program: ir.IrProgram) -> llvmir.Module:
    """Compiles every `ir.IrFunction` in `program` into one `llvmlite.ir.Module`, plus — if
    `program` has any top-level `let`s — one synthesized module-constructor function that
    initializes every `IrGlobalLet` global, in source order, wired into `@llvm.global_ctors` (see
    module docstring) so `run_llvm`'s existing `engine.run_static_constructors()` call actually
    runs it before `entry` is looked up. This is the LLVM-backend equivalent of `vm.py`'s
    synthesized `<init>` chunk (see `bytecode.py`'s `compile_program`): same "compile every
    top-level `let`'s initializer once, in declaration order, before anything else runs" idea,
    expressed through the standard LLVM global-constructor mechanism instead of a bytecode chunk
    the VM interprets first."""
    _require_llvmlite()
    module = llvmir.Module(name="perelang")
    functions: dict[str, llvmir.Function] = {}
    global_vars: dict[str, llvmir.GlobalVariable] = {}

    # `malloc` is declared once per module, external, and — deliberately — never paired with a
    # `free` call anywhere in this backend: a closure's capture environment (`_compile_closure`)
    # and a List literal's backing storage (`_FunctionCodegen._compile_list_lit`) are both
    # `malloc`'d and simply never released. This is an arena/leak model, stated plainly rather than
    # hidden: acceptable for a short-lived JIT-executed program (see `run_llvm`, which JITs, runs
    # one call, and returns) with no long-running-process memory-pressure concern, not something
    # that would be acceptable in a persistent server process.
    malloc_fnty = llvmir.FunctionType(llvmir.PointerType(llvmir.IntType(8)), [llvmir.IntType(64)])
    malloc_fn = llvmir.Function(module, malloc_fnty, name="malloc")

    ctx = _ModuleCtx(module, functions, malloc_fn, global_vars)

    fn_decls = [d for d in program.decls if isinstance(d, ir.IrFunction)]
    global_decls = [d for d in program.decls if isinstance(d, ir.IrGlobalLet)]
    for decl in fn_decls:
        param_types = [_llvm_type_for(t) for t in decl.param_types]
        ret_type = _llvm_type_for(decl.ret_type)
        fnty = llvmir.FunctionType(ret_type, param_types)
        functions[decl.name] = llvmir.Function(module, fnty, name=decl.name)

    # Every global variable is declared (with a placeholder zero/undef initializer — LLVM IR
    # requires *some* initializer for a `global` with `internal` linkage to be well-formed, even
    # though its real value is only known once the constructor below runs) before any function
    # body — including the constructor's own body — is compiled, so a reference to a global from
    # *any* function (or from another global's own initializer, for one declared earlier in
    # `program.decls`) always resolves via `_FunctionCodegen._compile_name`.
    for gdecl in global_decls:
        gvar = llvmir.GlobalVariable(module, _llvm_type_for(gdecl.ty), name=f"{gdecl.name}.global")
        gvar.linkage = "internal"
        gvar.initializer = llvmir.Constant(_llvm_type_for(gdecl.ty), None)
        global_vars[gdecl.name] = gvar

    for decl in fn_decls:
        llvm_func = functions[decl.name]
        block = llvm_func.append_basic_block("entry")
        builder = llvmir.IRBuilder(block)
        env = dict(zip(decl.params, llvm_func.args, strict=True))
        codegen = _FunctionCodegen(builder, ctx)
        result = codegen.compile_block(decl.body, env)
        builder.ret(result)

    if global_decls:
        _build_global_ctor(module, ctx, global_decls)

    return module


def _build_global_ctor(module: llvmir.Module, ctx: _ModuleCtx, global_decls: list[ir.IrGlobalLet]) -> None:
    """Synthesizes one `void()` function that compiles every top-level `let`'s initializer, in
    `program.decls` order (`global_decls` is already filtered from `program.decls` in that order —
    see `build_llvm_module`), storing each result into its `GlobalVariable`, then registers that
    function in `@llvm.global_ctors` — the standard LLVM mechanism `run_llvm`'s
    `engine.run_static_constructors()` call already invokes after `finalize_object()` and before
    `entry` is looked up (see module docstring). One combined constructor (not one per global) is
    enough, and matches `vm.py`'s own single `<init>` chunk."""
    ctor_fnty = llvmir.FunctionType(llvmir.VoidType(), [])
    ctor_fn = llvmir.Function(module, ctor_fnty, name=ctx.fresh_name("__perelang_global_ctor"))
    block = ctor_fn.append_basic_block("entry")
    builder = llvmir.IRBuilder(block)
    codegen = _FunctionCodegen(builder, ctx)
    # A fresh, empty `env` per global: a top-level `let` initializer has no locals/params of its
    # own to seed it with — anything it legally references is either another top-level `fn` (in
    # `ctx.functions`) or an earlier-declared top-level `let` (in `ctx.global_vars`), both resolved
    # through `_compile_name`'s `is_global` branch, not through this `env` dict.
    for gdecl in global_decls:
        value = codegen.compile_expr(gdecl.value, {})
        builder.store(value, ctx.global_vars[gdecl.name])
    builder.ret_void()

    # The raw LLVM IR shape for `@llvm.global_ctors`: an `appending`-linkage global array of
    # `{ i32 priority, void()* fn, i8* data }` structs — `llvmlite.ir` has no higher-level helper
    # for this (checked: no `ctor`/`global_ctors`-related API on `llvmir.Module`/`GlobalVariable`),
    # so it's built by hand from the pieces `llvmlite.ir` does provide. Priority `65535` is the
    # conventional "no particular ordering requested" value real Clang emits; `data` is unused
    # (null) since nothing here needs the `COMDAT`-association use case that field exists for.
    i8ptr = _i8ptr()
    entry_ty = llvmir.LiteralStructType([llvmir.IntType(32), llvmir.PointerType(ctor_fnty), i8ptr])
    array_ty = llvmir.ArrayType(entry_ty, 1)
    ctors_var = llvmir.GlobalVariable(module, array_ty, name="llvm.global_ctors")
    ctors_var.linkage = "appending"
    entry_const = llvmir.Constant(
        entry_ty, [llvmir.Constant(llvmir.IntType(32), 65535), ctor_fn, llvmir.Constant(i8ptr, None)]
    )
    ctors_var.initializer = llvmir.Constant(array_ty, [entry_const])


def _ctypes_type_for(ty: Type) -> type[ctypes.c_int64] | type[ctypes.c_double]:
    """The `ctypes` counterpart of `_llvm_type_for`'s LLVM type for the same Peregrine type —
    deliberately derived from the same `_is_float_type` predicate `_llvm_type_for` itself is built
    on, so the two mappings (LLVM type for codegen, `ctypes` type for calling back into Python)
    cannot silently drift apart: an `Int`/`Bool` is `i64`/`c_int64`, a `Float` is `double`/
    `c_double`, and both decisions are made by asking the same question. `String`/`List`/function
    types have no `ctypes` counterpart here and always fall through to the `raise` below — see the
    module docstring's note on why `entry` must stay Int/Bool/Float end to end."""
    if _is_float_type(ty):
        return ctypes.c_double
    if isinstance(ty, TCon) and ty.name in ("Int", "Bool") and not ty.args:
        return ctypes.c_int64
    from perelang.types import type_to_str

    raise LlvmBackendUnsupportedError(f"the LLVM backend does not support values of type {type_to_str(ty)}")


def run_llvm(program: ir.IrProgram, entry: str = "main", args: tuple[int | float, ...] = ()) -> int | float:
    """JIT-compiles `program` and calls `entry` (a zero-or-more-argument top-level `fn` over
    Int/Bool/Float) with `args`, returning its result as a Python `int` or `float` depending on
    `entry`'s actual declared return type (looked up in `program.decls` and used to build the
    `ctypes` call signature — see `_ctypes_type_for` — rather than the old hardcoded-`i64`-always
    signature, which could never have called a `Float`-returning function correctly)."""
    _require_llvmlite()

    entry_decl = next(
        (d for d in program.decls if isinstance(d, ir.IrFunction) and d.name == entry), None
    )
    if entry_decl is None:
        raise LlvmBackendUnsupportedError(f"no function named {entry!r} declared in this program")

    _ensure_native_target_initialized()

    module = build_llvm_module(program)
    module_ir = str(module)

    target = llvm.Target.from_default_triple()
    target_machine = target.create_target_machine()
    backing_mod = llvm.parse_assembly(module_ir)
    backing_mod.verify()
    engine = llvm.create_mcjit_compiler(backing_mod, target_machine)
    engine.finalize_object()
    engine.run_static_constructors()

    try:
        func_ptr = engine.get_function_address(entry)
    except Exception as exc:  # pragma: no cover - llvmlite raises a plain Exception on lookup failure
        raise LlvmBackendUnsupportedError(f"no compiled function named {entry!r}") from exc
    if func_ptr == 0:
        raise LlvmBackendUnsupportedError(f"no compiled function named {entry!r}")

    arg_ctypes = [_ctypes_type_for(t) for t in entry_decl.param_types]
    ret_ctype = _ctypes_type_for(entry_decl.ret_type)
    cfunc = ctypes.CFUNCTYPE(ret_ctype, *arg_ctypes)(func_ptr)
    result = cfunc(*args)
    return float(result) if ret_ctype is ctypes.c_double else int(result)
