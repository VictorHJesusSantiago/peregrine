"""Peregrine: a small, statically-typed programming language with a complete toolchain.

Pipeline: :mod:`perelang.lexer` -> :mod:`perelang.parser` -> :mod:`perelang.ast_nodes` ->
:mod:`perelang.typecheck` (Hindley-Milner inference) -> :mod:`perelang.ir` ->
:mod:`perelang.bytecode` -> :mod:`perelang.vm`, with :mod:`perelang.llvm_backend` as an
optional, genuinely working second backend (see that module's own docstring for what "optional"
means here).
"""

from __future__ import annotations

__version__ = "0.1.0"
