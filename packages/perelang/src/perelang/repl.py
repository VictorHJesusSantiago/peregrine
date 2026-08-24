"""An interactive REPL driving the full pipeline (lex -> parse -> infer+lower -> compile -> run) on
each submitted chunk, in the house style of separating a testable core from IO wiring: `Repl` and
`run_lines` never touch stdin/stdout, only `main()` does.

**Multi-line accumulation.** A chunk is "complete" once its running `{`/`(`/`[` bracket depth
returns to zero (see `_bracket_balance`) — the approach the top-level task description itself
suggested. This is intentionally simple, not exhaustive: `let x =\n  5;` (a `let` split across
lines with no open bracket in sight) would be submitted, and fail to parse, one line too early —
real interactive use overwhelmingly either fits a `let` on one line or spans multiple lines inside
a `{ }`/`( )`, which this handles correctly; tracking "the previous line ended in a binary operator
or bare `=`" as an extra continuation signal would close that gap; there wasn't time to add it, and
correctness for the language's core `let`/`fn`/`if`/lambda forms mattered more.

**Cross-line name resolution.** Each freshly-lowered `ast.Program` gets a brand-new `TypeEnv` (see
`ir.py`) — a REPL that only ever *incrementally* extended one `VM`'s globals table would find that
a later line referencing an earlier line's `let` fails to *type-check*, since nothing during
lowering would know that name exists (`test_vm.py`'s `TestVmReuseAcrossCalls` works through this in
detail). So `Repl` instead keeps the verbatim source text of every previously accepted declaration
and re-parses, re-infers, and re-runs *all of it* on every new line. This is only correct because
Peregrine has no observable side effects beyond a returned value or a raised error — no I/O, no
mutation — so re-evaluating an already-accepted `let`'s initializer a second (or Nth) time can
never produce a different value; "recompile everything" and "incrementally extend one VM" are
behaviorally identical, and the former needs none of the incremental-type-environment machinery the
latter would.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field

from perelang.bytecode import compile_program
from perelang.errors import PeregrineError
from perelang.ir import IrFunction, IrGlobalLet, IrProgram, lower_program
from perelang.parser import parse_expr, parse_program
from perelang.types import TFun, type_to_str
from perelang.vm import VM, value_to_str

PROMPT = "pere> "
CONTINUATION_PROMPT = "...   "
_BANNER = "Peregrine REPL — Ctrl-D (or Ctrl-Z on Windows) to exit."


def _bracket_balance(line: str) -> int:
    """Net change in `{([`/`})]` nesting depth contributed by one physical line, ignoring bracket
    characters that appear inside a string literal or after a `//` comment starts — matching what
    `lexer.py` itself would actually tokenize. Does *not* track whether a string literal is left
    open across a line boundary (see the module docstring's note on multi-line `let`s for the same
    kind of accepted gap): each line is scanned independently, starting outside any string."""
    depth = 0
    in_string = False
    i = 0
    while i < len(line):
        c = line[i]
        if in_string:
            if c == "\\":
                i += 1  # skip whatever's escaped, including a literal '"'
            elif c == '"':
                in_string = False
        elif c == '"':
            in_string = True
        elif c == "/" and i + 1 < len(line) and line[i + 1] == "/":
            break
        elif c in "({[":
            depth += 1
        elif c in ")}]":
            depth -= 1
        i += 1
    return depth


@dataclass
class Repl:
    """The testable core. `submit(line)` returns the text to print for that line, or `None` while
    still accumulating a multi-line chunk (the caller should print a continuation prompt and keep
    reading in that case, not blank output)."""

    session: list[str] = field(default_factory=list)
    _buffer: list[str] = field(default_factory=list)
    _pending_balance: int = 0

    def prompt(self) -> str:
        return CONTINUATION_PROMPT if self._buffer else PROMPT

    def submit(self, line: str) -> str | None:
        self._buffer.append(line)
        self._pending_balance += _bracket_balance(line)
        if self._pending_balance > 0:
            return None
        chunk = "\n".join(self._buffer)
        self._buffer = []
        self._pending_balance = 0
        if not chunk.strip():
            return None
        return self._evaluate(chunk)

    def _evaluate(self, chunk: str) -> str:
        try:
            new_names, decl_source, is_bare_expr = _as_decl_source(chunk, len(self.session))
        except PeregrineError as exc:
            return f"error: {exc}"

        full_source = "\n".join([*self.session, decl_source])
        try:
            program = parse_program(full_source)
            ir_program = lower_program(program)
            bytecode = compile_program(ir_program)
            vm = VM()
            vm.run_program(bytecode)
        except PeregrineError as exc:
            return f"error: {exc}"

        self.session.append(decl_source)
        return _describe(new_names, ir_program, vm, is_bare_expr)


def _as_decl_source(chunk: str, session_len: int) -> tuple[tuple[str, ...], str, bool]:
    """Returns `(names this chunk introduces, source text to append to the session, was it a bare
    expression)`. A chunk that already parses as one or more `let`/`fn` declarations is used
    as-is; a bare expression (`1 + 2`, with no trailing `;` and no `let`) is wrapped into a
    synthetic top-level `let` so it fits this language's decl-only top-level grammar (there is no
    bare-expression top-level form — see `parser.py`'s grammar comment) — purely a REPL
    convenience, the same trick a Python REPL uses to auto-name and echo a bare expression's value.
    """
    try:
        program = parse_program(chunk)
        return tuple(d.name for d in program.decls), chunk, False
    except PeregrineError:
        pass
    parse_expr(chunk)  # raises (propagating out) if this isn't a valid expression either
    name = f"_{session_len}"
    return (name,), f"let {name} = ({chunk});", True


def _describe(names: tuple[str, ...], ir_program: IrProgram, vm: VM, is_bare_expr: bool) -> str:
    by_name: dict[str, IrFunction | IrGlobalLet] = {d.name: d for d in ir_program.decls}
    lines: list[str] = []
    for name in names:
        decl = by_name[name]
        if isinstance(decl, IrFunction):
            fn_type = TFun(decl.param_types, decl.ret_type)
            lines.append(f"fn {name} : {type_to_str(fn_type)}")
        elif is_bare_expr:
            lines.append(f"{value_to_str(vm.globals[name])} : {type_to_str(decl.ty)}")
        else:
            lines.append(f"{name} : {type_to_str(decl.ty)} = {value_to_str(vm.globals[name])}")
    return "\n".join(lines)


def run_lines(lines: Iterable[str]) -> Iterator[str]:
    """The testable entry point `tests/test_repl.py` drives directly: feed it any iterable of
    physical lines (a `list[str]`, a file, `sys.stdin`), get back the output text for each
    completed chunk — no real terminal required."""
    repl = Repl()
    for line in lines:
        output = repl.submit(line)
        if output is not None:
            yield output


def main() -> None:
    """Thin IO wiring around `Repl`: prints a prompt before each read (real REPLs want a
    continuation prompt to *appear*, which `run_lines` alone — a pure generator over already-
    collected lines — has no opportunity to do), reads with `input()`, and exits cleanly on
    Ctrl-D/Ctrl-Z (`EOFError`) or Ctrl-C (`KeyboardInterrupt`)."""
    print(_BANNER)
    repl = Repl()
    while True:
        try:
            line = input(repl.prompt())
        except (EOFError, KeyboardInterrupt):
            print()
            return
        output = repl.submit(line)
        if output is not None:
            print(output)


if __name__ == "__main__":
    main()
