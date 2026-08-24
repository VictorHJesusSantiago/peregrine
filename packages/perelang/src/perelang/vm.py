"""A stack-based virtual machine executing `bytecode.py`'s instruction set.

**Runtime value representation.** `Value` is a plain Python-native union (`int | float | bool |
str | tuple[Value, ...] | Closure | Unit`) rather than a hand-rolled tagged struct (e.g. a
`@dataclass Value` wrapping a `kind` tag and a payload). This is a deliberate choice, not laziness:
every Peregrine runtime type already has an exact, unambiguous native Python counterpart (`Int` ->
`int`, `Float` -> `float`, `Bool` -> `bool`, `String` -> `str`, `List<T>` -> `tuple[Value, ...]` —
immutable, since this language has no mutation; see `Closure` below for functions), so a wrapper
tag would carry zero extra information beyond `type(value)`, while costing an allocation and an
unwrap on every single operation. The one place this native representation needs care is that
`bool` is a subclass of `int` in Python — every arithmetic helper below explicitly rejects `bool`
operands rather than silently treating `True + True` as `2`.

**Closures.** A `Closure` is `(FunctionProto, captured values)` — both a plain top-level `fn` and a
capturing lambda are this same runtime type (a non-capturing function is just a `Closure` with
`captured=()`, registered once at program start-up; see `run_program`), so `CALL` never branches on
"is this a real function." Capture is by value, matching `ir.py`'s design note: this language has
no reassignment, so a captured binding's value can never change after the closure was created.

**Bytecode carries real source spans.** Every `bytecode.Instr` carries the span of the IR node that
produced it (see `bytecode.py`'s `_current_span` mechanism), so every `PeregrineRuntimeError` raised
from inside the main execution loop below (division/modulo by zero, a non-Bool `if` condition, an
unbound global, calling a non-function, an unsupported comparison, a unary-operator type mismatch)
is attributed to the *currently executing instruction's* real `instr.span` — the exact source
location of the operation that faulted, not a placeholder. The one exception is genuinely
unavoidable: the arity check at the very top of `call_closure`, for the outermost call `run_program`
makes with no caller of its own (there is no call-site span to attribute a top-level arity mismatch
to) — that one call still falls back to `_RUNTIME_SPAN`, a placeholder `Span` shared with
`bytecode.py` (see `bytecode.NO_SPAN`). Every *other* call — one `CALL` opcode invoking another —
threads the calling instruction's own span through as `call_closure`'s `calling_span` argument, so
even a wrong-arity error raised many frames deep still points at the real call expression that
triggered it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from perelang import ast_nodes as ast
from perelang.bytecode import NO_SPAN, BytecodeProgram, FunctionProto, OpCode, compile_program
from perelang.errors import PeregrineRuntimeError, Span
from perelang.ir import lower_program
from perelang.parser import parse_program

_RUNTIME_SPAN = NO_SPAN


class Unit:
    """The single value every `Unit`-typed expression evaluates to (an `if`/block with no tail, a
    non-tail `ExprStmt`'s discarded result never reaches here at all — but a *block whose value is
    Unit* still needs an actual value to push, per the stack-discipline invariant documented in
    `bytecode.py`). Kept as its own singleton class rather than reusing Python's `None`: `None`
    already means "absent" all over this codebase's own data (`ast_nodes.BlockExpr.tail`,
    `Param.type_ann`, ...) — a runtime value that is *present* and prints as `()` is a different
    concept from "nothing was provided here," and conflating them would make `Value` include
    `None` for two unrelated reasons."""

    _instance: Unit | None = None

    def __new__(cls) -> Unit:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "()"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Unit)

    def __hash__(self) -> int:
        return 0


UNIT = Unit()


@dataclass(frozen=True, slots=True)
class Closure:
    proto: FunctionProto
    captured: tuple[Value, ...]


Value = int | float | bool | str | tuple["Value", ...] | Closure | Unit


def value_to_str(value: Value) -> str:
    """Peregrine's own rendering, not Python's `repr`/`str` — `True`/`False` not `True`/`False`
    with Python capitalization quirks for other types, strings unquoted like a language's own
    `print` would show them, lists with Peregrine's `[..]` syntax, closures as an opaque token."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    if isinstance(value, tuple):
        return "[" + ", ".join(value_to_str(v) for v in value) + "]"
    if isinstance(value, Closure):
        return f"<function {value.proto.name}>"
    if isinstance(value, Unit):
        return "()"
    return str(value)


def _is_number(v: Value) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool)


def _check_numeric(op: str, a: Value, b: Value, span: Span) -> None:
    if not (_is_number(a) and _is_number(b)):
        raise PeregrineRuntimeError(
            f"'{op}' requires two numeric operands, got {type(a).__name__} and {type(b).__name__}", span
        )


def _add(a: Value, b: Value, span: Span) -> Value:
    _check_numeric("+", a, b, span)
    assert isinstance(a, int | float) and isinstance(b, int | float)
    return a + b


def _sub(a: Value, b: Value, span: Span) -> Value:
    _check_numeric("-", a, b, span)
    assert isinstance(a, int | float) and isinstance(b, int | float)
    return a - b


def _mul(a: Value, b: Value, span: Span) -> Value:
    _check_numeric("*", a, b, span)
    assert isinstance(a, int | float) and isinstance(b, int | float)
    return a * b


def _div(a: Value, b: Value, span: Span) -> Value:
    _check_numeric("/", a, b, span)
    assert isinstance(a, int | float) and isinstance(b, int | float)
    if b == 0:
        raise PeregrineRuntimeError("division by zero", span)
    if isinstance(a, int) and isinstance(b, int):
        # Truncates toward negative infinity (Python `//`) — every Int `/` (as opposed to the
        # Float `/.` added alongside `+. -. *.`, see `types.py`) reachable from a type-checked
        # Peregrine program has two Int operands, so this is the branch `pere run` exercises for
        # plain `/`; the `else` (true division) fires both for source-level `/.` on two Floats and
        # for bytecode `test_vm.py` constructs built by hand.
        return a // b
    return a / b


def _mod(a: Value, b: Value, span: Span) -> Value:
    _check_numeric("%", a, b, span)
    assert isinstance(a, int | float) and isinstance(b, int | float)
    if b == 0:
        raise PeregrineRuntimeError("modulo by zero", span)
    return a % b


def _neg(v: Value, span: Span) -> Value:
    if not _is_number(v):
        raise PeregrineRuntimeError(f"unary '-' requires a numeric operand, got {type(v).__name__}", span)
    assert isinstance(v, int | float)
    return -v


def _compare(op: OpCode, a: Value, b: Value, span: Span) -> Value:
    try:
        if op is OpCode.EQ:
            return a == b
        if op is OpCode.NEQ:
            return a != b
        if op is OpCode.LT:
            return a < b  # type: ignore[operator]
        if op is OpCode.LTE:
            return a <= b  # type: ignore[operator]
        if op is OpCode.GT:
            return a > b  # type: ignore[operator]
        if op is OpCode.GTE:
            return a >= b  # type: ignore[operator]
    except TypeError as exc:
        raise PeregrineRuntimeError(
            f"unsupported comparison between {type(a).__name__} and {type(b).__name__}", span
        ) from exc
    raise AssertionError(f"{op!r} is not a comparison opcode")  # pragma: no cover


_ARITH: dict[OpCode, Callable[[Value, Value, Span], Value]] = {
    OpCode.ADD: _add,
    OpCode.SUB: _sub,
    OpCode.MUL: _mul,
    OpCode.DIV: _div,
    OpCode.MOD: _mod,
}

_COMPARISONS = frozenset({OpCode.EQ, OpCode.NEQ, OpCode.LT, OpCode.LTE, OpCode.GT, OpCode.GTE})


class VM:
    """Holds the one piece of state that outlives a single function call: the globals table. A
    fresh `VM()` per `run_program` call is the norm (see `cli.py`/`repl.py`), but the REPL reuses
    one `VM` across lines specifically so `let`s from earlier lines stay visible (see `repl.py`)."""

    def __init__(self) -> None:
        self.globals: dict[str, Value] = {}

    def run_program(self, program: BytecodeProgram) -> Value:
        for name, proto in program.functions.items():
            self.globals[name] = Closure(proto, ())
        self.call_closure(Closure(program.init, ()), ())
        main = self.globals.get("main")
        if isinstance(main, Closure) and main.proto.arity == 0:
            return self.call_closure(main, ())
        if program.last_global is not None:
            return self.globals[program.last_global]
        return UNIT

    def call_closure(self, closure: Closure, args: tuple[Value, ...], calling_span: Span | None = None) -> Value:
        """`calling_span` is the span of the `CALL` instruction that triggered this invocation —
        supplied by the `OpCode.CALL` handler below (using its own `instr.span`) for every *nested*
        call, so an arity-mismatch error raised many frames deep still points at the real call
        expression that caused it. `None` (the default) is for the two calls with no caller of
        their own: `run_program`'s initial `<init>` chunk and its optional `main` call — there is
        no source-level call expression to blame for either, so those fall back to the placeholder
        `_RUNTIME_SPAN`."""
        proto = closure.proto
        if len(args) != proto.arity:
            raise PeregrineRuntimeError(
                f"{proto.name} expects {proto.arity} argument(s), got {len(args)}",
                calling_span if calling_span is not None else _RUNTIME_SPAN,
            )
        locals_: list[Value] = list(args) + [UNIT] * (proto.num_locals - len(args))
        captured = closure.captured
        stack: list[Value] = []
        code = proto.code
        constants = proto.constants
        ip = 0
        while ip < len(code):
            instr = code[ip]
            ip += 1
            op = instr.op

            if op is OpCode.CONST:
                value = constants[instr.arg]
                assert not isinstance(value, FunctionProto), "CONST must not reference a FunctionProto"
                stack.append(value)
            elif op is OpCode.PUSH_UNIT:
                stack.append(UNIT)
            elif op is OpCode.LOAD_LOCAL:
                stack.append(locals_[instr.arg])
            elif op is OpCode.STORE_LOCAL:
                locals_[instr.arg] = stack.pop()
            elif op is OpCode.LOAD_GLOBAL:
                name = constants[instr.arg]
                assert isinstance(name, str)
                if name not in self.globals:
                    raise PeregrineRuntimeError(f"unbound global {name!r}", instr.span)
                stack.append(self.globals[name])
            elif op is OpCode.STORE_GLOBAL:
                name = constants[instr.arg]
                assert isinstance(name, str)
                self.globals[name] = stack.pop()
            elif op is OpCode.LOAD_CAPTURE:
                stack.append(captured[instr.arg])
            elif op is OpCode.POP:
                stack.pop()
            elif op is OpCode.NEG:
                stack.append(_neg(stack.pop(), instr.span))
            elif op is OpCode.NOT:
                operand = stack.pop()
                if not isinstance(operand, bool):
                    raise PeregrineRuntimeError(f"unary '!' requires a Bool, got {type(operand).__name__}", instr.span)
                stack.append(not operand)
            elif op in _ARITH:
                right = stack.pop()
                left = stack.pop()
                stack.append(_ARITH[op](left, right, instr.span))
            elif op in _COMPARISONS:
                right = stack.pop()
                left = stack.pop()
                stack.append(_compare(op, left, right, instr.span))
            elif op is OpCode.JUMP:
                ip = instr.arg
            elif op is OpCode.JUMP_IF_FALSE:
                cond = stack.pop()
                if not isinstance(cond, bool):
                    raise PeregrineRuntimeError(f"'if' condition must be Bool, got {type(cond).__name__}", instr.span)
                if not cond:
                    ip = instr.arg
            elif op is OpCode.JUMP_IF_TRUE:
                cond = stack.pop()
                if not isinstance(cond, bool):
                    raise PeregrineRuntimeError(f"condition must be Bool, got {type(cond).__name__}", instr.span)
                if cond:
                    ip = instr.arg
            elif op is OpCode.CALL:
                argc = instr.arg
                call_args = tuple(stack[len(stack) - argc :])
                del stack[len(stack) - argc :]
                callee = stack.pop()
                if not isinstance(callee, Closure):
                    raise PeregrineRuntimeError(
                        f"attempted to call a non-function value {value_to_str(callee)!r}", instr.span
                    )
                stack.append(self.call_closure(callee, call_args, instr.span))
            elif op is OpCode.MAKE_LIST:
                n = instr.arg
                items: Value = tuple(stack[len(stack) - n :])
                del stack[len(stack) - n :]
                stack.append(items)
            elif op is OpCode.MAKE_CLOSURE:
                proto_const = constants[instr.arg]
                assert isinstance(proto_const, FunctionProto)
                n = len(proto_const.capture_names)
                captured_values = tuple(stack[len(stack) - n :])
                del stack[len(stack) - n :]
                stack.append(Closure(proto_const, captured_values))
            else:
                raise AssertionError(f"unhandled opcode {op!r}")  # pragma: no cover

        if len(stack) != 1:
            raise AssertionError(  # pragma: no cover
                f"{proto.name}: frame ended with {len(stack)} stack value(s), expected exactly 1 — a compiler bug"
            )
        return stack[0]


def run_ast(program: ast.Program) -> Value:
    """Lower, compile, and run an already-parsed program in a fresh `VM`. Type inference happens
    as part of lowering (see `ir.lower_program`) — a `TypeCheckError` here means the program never
    reached the VM at all."""
    return VM().run_program(compile_program(lower_program(program)))


def run_source(source: str) -> Value:
    """The full pipeline in one call: lex -> parse -> infer+lower -> compile -> run. Used by
    `cli.py`'s `run` subcommand and by this package's end-to-end tests; `repl.py` uses the VM
    directly instead, since a REPL needs one `VM` (and its globals) to persist across lines."""
    return run_ast(parse_program(source))
