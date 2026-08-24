"""Every compiler-phase error shares one base class and one thing in common: a source `Span`.

Keeping these as plain, narrow exception classes (one per phase: `LexError`, `ParseError`,
`TypeCheckError`) rather than a single generic `CompileError(phase, message)` is what lets a caller
(the CLI, the LSP server, a test) catch exactly the phase it cares about — `except ParseError`
means "the source has a syntax problem," not "something went wrong somewhere."
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Span:
    """A half-open character range `[start, end)` into the original source text, plus the 1-based line/column of `start` — computed once at the point of the error, not carried around per-token, since most tokens never end up in an error message."""

    start: int
    end: int
    line: int
    column: int

    def __str__(self) -> str:
        return f"line {self.line}, column {self.column}"


class PeregrineError(Exception):
    """Base for every phase's error. Never raised directly."""

    def __init__(self, message: str, span: Span) -> None:
        super().__init__(f"{message} ({span})")
        self.message = message
        self.span = span


class LexError(PeregrineError):
    pass


class ParseError(PeregrineError):
    pass


class TypeCheckError(PeregrineError):
    """Deliberately not named `TypeError` — that's a Python builtin, and shadowing it would make
    every `except TypeError` elsewhere in a caller's own code accidentally also catch this."""

    pass


class PeregrineRuntimeError(PeregrineError):
    """Raised by `vm.py` (and, for the operations it covers, `llvm_backend.py`'s JIT wrapper) for a
    fault only detectable at execution time, not by static type inference — division/modulo by
    zero being the only one static typing can never rule out for this language. Named
    `PeregrineRuntimeError`, not `RuntimeError`, for the same reason `TypeCheckError` isn't named
    `TypeError`: shadowing the Python builtin would make a caller's own `except RuntimeError`
    accidentally catch this too (and separately, `RuntimeError_` would fail ruff's N818, which
    requires exception class names to end in `Error` — `PeregrineRuntimeError` already does)."""

    pass
