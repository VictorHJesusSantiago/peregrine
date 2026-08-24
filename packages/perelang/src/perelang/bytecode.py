"""Compiles `ir.py`'s IR into a small stack-machine instruction set `vm.py` executes.

**Stack discipline.** Every compiled *expression* leaves exactly one value on the stack net of
whatever it consumed; every compiled *statement* (a block's `IrLet`/`IrExprStmt`) leaves none. A
block with no tail compiles its implicit `Unit` via `PUSH_UNIT` to keep that invariant — so `if`'s
two branches, a function body, and a nested block-as-expression are all just "some code that nets
+1", and `if`/`else` needs nothing fancier than a jump over a jump: no separate basic-block CFG,
matching `ir.py`'s choice not to desugar `if` into one either (see its docstring).

**Locals** are flat stack slots, one per parameter and per `let` (including inside nested blocks)
in a function — allocated by a monotonically increasing counter that is never reused across a
sibling or exited block, trading a few wasted slots for not needing to track slot lifetimes.
Lexical shadowing (`let x = 1; let x = x + 1;`, or the same name in two `if` branches meaning two
different bindings) is handled by a compile-time scope stack of `name -> slot` dicts, pushed on
block entry and popped on exit — resolution walks innermost-out.

**Closures.** `ir.IrClosure.captures` already lists exactly which outer names a lambda's body
reads (see `ir.py`). Compiling an `IrClosure` therefore: (1) compiles its body into its own
`FunctionProto` with those names pre-registered as `LOAD_CAPTURE`-resolvable, then (2), back in
the *enclosing* function being compiled, emits code to push each captured name's current value (a
`LOAD_LOCAL` or `LOAD_CAPTURE`, resolved in the enclosing scope) followed by `MAKE_CLOSURE`, which
pops exactly that many values and bundles them into a `vm.Closure` alongside the prototype. This
is the classic value-capturing closure-compilation scheme (as in, e.g., Lua's upvalues or "Crafting
Interpreters"' clox), simplified because this language has no reassignment: a captured binding's
value can never change after the closure is created, so there is no need for the more elaborate
machinery (heap-boxing, upvalue "closing") a language with mutable captures requires.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

from perelang import ir
from perelang.errors import Span

# The placeholder span for an instruction that was never explicitly attributed to a source node —
# see `_FunctionCompiler._current_span` below and `vm.py`'s module docstring. `vm.py` imports this
# same constant (as `_RUNTIME_SPAN`) for the one runtime error that genuinely has no calling span
# to attribute itself to (the top-level program's own arity check), so there is exactly one
# definition of "no real span" shared by both modules.
NO_SPAN = Span(start=0, end=0, line=0, column=0)


class OpCode(enum.Enum):
    CONST = enum.auto()  # arg: constants index -> push constants[arg]
    PUSH_UNIT = enum.auto()  # push the Unit value
    LOAD_LOCAL = enum.auto()  # arg: local slot -> push locals[arg]
    STORE_LOCAL = enum.auto()  # arg: local slot -> locals[arg] = pop()
    LOAD_GLOBAL = enum.auto()  # arg: constants index holding a name -> push globals[name]
    STORE_GLOBAL = enum.auto()  # arg: constants index holding a name -> globals[name] = pop()
    LOAD_CAPTURE = enum.auto()  # arg: index into the running closure's captured tuple -> push it
    POP = enum.auto()  # discard the top of stack (a non-tail statement expression's value)

    NEG = enum.auto()
    NOT = enum.auto()
    ADD = enum.auto()
    SUB = enum.auto()
    MUL = enum.auto()
    DIV = enum.auto()
    MOD = enum.auto()
    EQ = enum.auto()
    NEQ = enum.auto()
    LT = enum.auto()
    LTE = enum.auto()
    GT = enum.auto()
    GTE = enum.auto()

    JUMP = enum.auto()  # arg: absolute instruction index -> ip = arg
    JUMP_IF_FALSE = enum.auto()  # pop; arg: absolute instruction index -> if not popped: ip = arg
    JUMP_IF_TRUE = enum.auto()  # pop; arg: absolute instruction index -> if popped: ip = arg

    CALL = enum.auto()  # arg: argument count N; stack [...,closure,a1..aN] -> [...,result]
    MAKE_LIST = enum.auto()  # arg: item count N; stack [...,i1..iN] -> [...,(i1,...,iN)]
    MAKE_CLOSURE = enum.auto()  # arg: constants index holding a FunctionProto; pops len(captures)


@dataclass(frozen=True, slots=True)
class Instr:
    op: OpCode
    arg: int = 0
    span: Span = NO_SPAN


# A constants-pool entry is either a plain runtime-representable literal or a nested function
# prototype (for `MAKE_CLOSURE` and, at the top level, every compiled `fn`/lambda). The `type`
# statement (PEP 695) creates a lazily-evaluated alias, so the forward reference to `FunctionProto`
# (defined below, and itself containing a `tuple[Constant, ...]` field) needs no string quoting.
type Constant = int | float | str | bool | FunctionProto


@dataclass(frozen=True, slots=True)
class FunctionProto:
    """One compiled function body — a top-level `fn`, a lambda, or the synthesized `<init>` chunk
    (see `BytecodeProgram`). `capture_names` records what a lambda captures (empty for a
    non-capturing `fn`/lambda) purely for introspection/debugging (e.g. a REPL's `repr` of a
    closure) — the VM never needs it at call time, since a `vm.Closure`'s already-captured values
    are indexed positionally via `LOAD_CAPTURE`, not looked up by name."""

    name: str
    arity: int
    num_locals: int
    code: tuple[Instr, ...]
    constants: tuple[Constant, ...]
    capture_names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BytecodeProgram:
    """`init` runs every top-level `let`'s initializer once, in source order, storing each into the
    VM's globals table (see `vm.py`) — `functions` are registered into that same table as
    zero-capture closures *before* `init` runs, so a `let` initializer or any function body can
    reference any function regardless of declaration order (forward references to a not-yet-run
    `let`, however, still fail — inherited from `types.py`'s sequential environment, see
    `ir.py`). `last_global` names the most recently declared top-level `let`, used by `vm.py` as a
    fallback "the program's value" when there's no zero-argument `main` to call (see `cli.py`)."""

    functions: dict[str, FunctionProto]
    init: FunctionProto
    last_global: str | None


_BINOP = {
    "+": OpCode.ADD,
    "-": OpCode.SUB,
    "*": OpCode.MUL,
    "/": OpCode.DIV,
    "%": OpCode.MOD,
    # The `+. -. *. /.` Float operators reuse the exact same opcodes as their Int counterparts —
    # `vm.py`'s `_add`/`_sub`/`_mul`/`_div` dispatch on the Python runtime type of the operands
    # (`_is_number`), not on which source operator produced them, so no new opcode is needed (see
    # `vm.py`'s module docstring and `types.py`'s `_FLOAT_ARITHMETIC`).
    "+.": OpCode.ADD,
    "-.": OpCode.SUB,
    "*.": OpCode.MUL,
    "/.": OpCode.DIV,
    "==": OpCode.EQ,
    "!=": OpCode.NEQ,
    "<": OpCode.LT,
    "<=": OpCode.LTE,
    ">": OpCode.GT,
    ">=": OpCode.GTE,
}


class _FunctionCompiler:
    def __init__(self, capture_names: tuple[str, ...]) -> None:
        self._scopes: list[dict[str, int]] = [{}]
        self._captures = {name: i for i, name in enumerate(capture_names)}
        self._capture_names = capture_names
        self._next_slot = 0
        self._code: list[Instr] = []
        self._constants: list[Constant] = []
        # The span attributed to whatever instruction `_emit` produces next — updated once per IR
        # node visited (see `compile_expr`/`compile_block`) rather than threaded explicitly through
        # every `_emit` call site. Source spans nest predictably during a tree-walk, so "whatever
        # node we're currently compiling" is the right span for the instructions that node emits;
        # see the module docstring for where this is deliberately re-set mid-expression (e.g.
        # `_compile_binary`) so an operator's own instruction gets the *whole* expression's span
        # rather than trailing off with the last operand's.
        self._current_span: Span = NO_SPAN

    def declare_param(self, name: str) -> None:
        self._declare_local(name)

    def _declare_local(self, name: str) -> int:
        slot = self._next_slot
        self._next_slot += 1
        self._scopes[-1][name] = slot
        return slot

    def _resolve_local(self, name: str) -> int | None:
        for scope in reversed(self._scopes):
            if name in scope:
                return scope[name]
        return None

    def _emit(self, op: OpCode, arg: int = 0) -> int:
        self._code.append(Instr(op, arg, self._current_span))
        return len(self._code) - 1

    def _patch_to_here(self, idx: int) -> None:
        self._code[idx] = Instr(self._code[idx].op, len(self._code), self._code[idx].span)

    def _const(self, value: Constant) -> int:
        self._constants.append(value)
        return len(self._constants) - 1

    def emit_store_global(self, name: str, span: Span) -> None:
        """Used only by `compile_program` for the synthesized `<init>` chunk — an ordinary function
        body never assigns to a global (there is no assignment statement in this language; a `let`
        only ever introduces a fresh local binding, see `ast_nodes.LetDecl`)."""
        self._current_span = span
        self._emit(OpCode.STORE_GLOBAL, self._const(name))

    def emit_push_unit(self) -> None:
        self._emit(OpCode.PUSH_UNIT)

    def finish(self, name: str, arity: int) -> FunctionProto:
        return FunctionProto(
            name, arity, self._next_slot, tuple(self._code), tuple(self._constants), self._capture_names
        )

    # -- expressions --------------------------------------------------------------------------

    def compile_expr(self, expr: ir.IrExpr) -> None:
        self._current_span = expr.span
        match expr:
            case ir.IrInt(value, _ty, _span):
                self._emit(OpCode.CONST, self._const(value))
            case ir.IrFloat(value, _ty, _span):
                self._emit(OpCode.CONST, self._const(value))
            case ir.IrStr(value, _ty, _span):
                self._emit(OpCode.CONST, self._const(value))
            case ir.IrBool(value, _ty, _span):
                self._emit(OpCode.CONST, self._const(value))
            case ir.IrName(name, is_global, _ty, _span):
                self._compile_name(name, is_global)
            case ir.IrUnary(op, operand, _ty, span):
                self.compile_expr(operand)
                self._current_span = span
                self._emit(OpCode.NEG if op in ("-", "-.") else OpCode.NOT)
            case ir.IrBinary(op, left, right, _ty, span):
                self._compile_binary(op, left, right, span)
            case ir.IrIf(cond, then_b, else_b, _ty, span):
                self._compile_if(cond, then_b, else_b, span)
            case ir.IrCall(callee, args, _ty, span):
                self.compile_expr(callee)
                for a in args:
                    self.compile_expr(a)
                self._current_span = span
                self._emit(OpCode.CALL, len(args))
            case ir.IrListLit(items, _ty, span):
                for item in items:
                    self.compile_expr(item)
                self._current_span = span
                self._emit(OpCode.MAKE_LIST, len(items))
            case ir.IrClosure() as closure:
                self._compile_closure(closure)
            case ir.IrBlock():
                self.compile_block(expr)

    def _compile_name(self, name: str, is_global: bool) -> None:
        if is_global:
            self._emit(OpCode.LOAD_GLOBAL, self._const(name))
            return
        slot = self._resolve_local(name)
        if slot is not None:
            self._emit(OpCode.LOAD_LOCAL, slot)
            return
        capture_idx = self._captures.get(name)
        if capture_idx is None:
            raise AssertionError(f"{name!r} resolved to neither a local nor a capture — a lowering bug in ir.py")
        self._emit(OpCode.LOAD_CAPTURE, capture_idx)

    def _compile_binary(self, op: str, left: ir.IrExpr, right: ir.IrExpr, span: Span) -> None:
        # `&&`/`||` short-circuit: the right operand must not even be *evaluated* when the left
        # already determines the result (e.g. `false && (1 / 0 == 0)` must not divide by zero) —
        # so unlike every other binary operator, these compile to jumps, not a plain 2-operand op.
        if op == "&&":
            self.compile_expr(left)
            self._current_span = span
            jump_false = self._emit(OpCode.JUMP_IF_FALSE, -1)
            self.compile_expr(right)
            self._current_span = span
            jump_end = self._emit(OpCode.JUMP, -1)
            self._patch_to_here(jump_false)
            self._current_span = span
            self._emit(OpCode.CONST, self._const(False))
            self._patch_to_here(jump_end)
            return
        if op == "||":
            self.compile_expr(left)
            self._current_span = span
            jump_true = self._emit(OpCode.JUMP_IF_TRUE, -1)
            self.compile_expr(right)
            self._current_span = span
            jump_end = self._emit(OpCode.JUMP, -1)
            self._patch_to_here(jump_true)
            self._current_span = span
            self._emit(OpCode.CONST, self._const(True))
            self._patch_to_here(jump_end)
            return
        self.compile_expr(left)
        self.compile_expr(right)
        # Reset to the *whole binary expression's* span, not the right operand's — the operator
        # instruction (ADD, DIV, ...) is what can actually fault at run time (division by zero),
        # and it should point at `a / b` as a whole, not wherever `b` happened to end.
        self._current_span = span
        self._emit(_BINOP[op])

    def _compile_if(self, cond: ir.IrExpr, then_b: ir.IrBlock, else_b: ir.IrBlock, span: Span) -> None:
        self.compile_expr(cond)
        self._current_span = span
        jump_to_else = self._emit(OpCode.JUMP_IF_FALSE, -1)
        self.compile_block(then_b)
        self._current_span = span
        jump_to_end = self._emit(OpCode.JUMP, -1)
        self._patch_to_here(jump_to_else)
        self.compile_block(else_b)
        self._patch_to_here(jump_to_end)

    def _compile_closure(self, closure: ir.IrClosure) -> None:
        sub = _FunctionCompiler(closure.captures)
        for p in closure.params:
            sub.declare_param(p)
        sub.compile_expr(closure.body)
        proto = sub.finish("<lambda>", len(closure.params))
        for name in closure.captures:
            self._compile_name(name, is_global=False)
        self._emit(OpCode.MAKE_CLOSURE, self._const(proto))

    # -- blocks and statements -----------------------------------------------------------------

    def compile_block(self, block: ir.IrBlock) -> None:
        self._scopes.append({})
        for stmt in block.stmts:
            match stmt:
                case ir.IrLet(name, value, span):
                    self.compile_expr(value)
                    self._current_span = span
                    slot = self._declare_local(name)
                    self._emit(OpCode.STORE_LOCAL, slot)
                case ir.IrExprStmt(inner, span):
                    self.compile_expr(inner)
                    self._current_span = span
                    self._emit(OpCode.POP)
        if block.tail is None:
            self._current_span = block.span
            self._emit(OpCode.PUSH_UNIT)
        else:
            self.compile_expr(block.tail)
        self._scopes.pop()


def compile_program(program: ir.IrProgram) -> BytecodeProgram:
    functions: dict[str, FunctionProto] = {}
    init_fc = _FunctionCompiler(())
    last_global: str | None = None

    for decl in program.decls:
        match decl:
            case ir.IrFunction(name, params, _param_types, _ret_type, body, _span):
                fc = _FunctionCompiler(())
                for p in params:
                    fc.declare_param(p)
                fc.compile_expr(body)
                functions[name] = fc.finish(name, len(params))
            case ir.IrGlobalLet(name, value, _ty, span):
                init_fc.compile_expr(value)
                init_fc.emit_store_global(name, span)
                last_global = name

    init_fc.emit_push_unit()
    return BytecodeProgram(functions, init_fc.finish("<init>", 0), last_global)
