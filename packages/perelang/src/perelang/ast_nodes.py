"""The AST. Every node is a frozen dataclass carrying its source `Span`; `Expr` and `Decl` are
plain `Union` type aliases over them (Python has no sealed-class enforcement, but combined with
`match` statements and `typing.assert_never` in a final `case _:` — see every consumer of these
types — this gets the same "add a variant, the type checker flags every unhandled match" property
a real discriminated union gives, checked by `mypy --strict`, not just convention).
"""

from __future__ import annotations

from dataclasses import dataclass

from perelang.errors import Span


@dataclass(frozen=True, slots=True)
class TypeAnn:
    """A syntactic type annotation as written in source — `Int`, `Bool`, `List<Int>`, a function
    type `(Int, Int) -> Int`. Distinct from `types.Type`, the internal representation type
    inference actually computes with: this is what a user *wrote* (or omitted); `types.Type` is
    what the checker *infers*, always, whether or not one of these was present."""

    name: str
    args: tuple[TypeAnn, ...]
    span: Span


@dataclass(frozen=True, slots=True)
class Param:
    name: str
    type_ann: TypeAnn | None
    span: Span


# ---- Expressions -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IntLit:
    value: int
    span: Span


@dataclass(frozen=True, slots=True)
class FloatLit:
    value: float
    span: Span


@dataclass(frozen=True, slots=True)
class StringLit:
    value: str
    span: Span


@dataclass(frozen=True, slots=True)
class BoolLit:
    value: bool
    span: Span


@dataclass(frozen=True, slots=True)
class Ident:
    name: str
    span: Span


@dataclass(frozen=True, slots=True)
class Unary:
    op: str  # "-" | "!"
    operand: Expr
    span: Span


@dataclass(frozen=True, slots=True)
class Binary:
    op: str
    left: Expr
    right: Expr
    span: Span


@dataclass(frozen=True, slots=True)
class If:
    cond: Expr
    then_branch: BlockExpr
    else_branch: BlockExpr
    span: Span


@dataclass(frozen=True, slots=True)
class Call:
    callee: Expr
    args: tuple[Expr, ...]
    span: Span


@dataclass(frozen=True, slots=True)
class Lambda:
    params: tuple[Param, ...]
    body: Expr
    span: Span


@dataclass(frozen=True, slots=True)
class ListLit:
    items: tuple[Expr, ...]
    span: Span


@dataclass(frozen=True, slots=True)
class LetDecl:
    """Both a top-level declaration and a statement inside a `BlockExpr` — the same node, since it
    means the same thing (bind a name, in scope for whatever follows) in both positions."""

    name: str
    type_ann: TypeAnn | None
    value: Expr
    span: Span


@dataclass(frozen=True, slots=True)
class ExprStmt:
    """A non-tail expression inside a block, evaluated for effect, its value discarded — the
    `f();` in `{ f(); g() }`, as opposed to `g()`'s value, which is the block's own value."""

    expr: Expr
    span: Span


Stmt = LetDecl | ExprStmt


@dataclass(frozen=True, slots=True)
class BlockExpr:
    """`{ stmt* tail? }` — `tail is None` means the block's value is `Unit`, the same way a
    function body with no trailing expression means "returns nothing" in most languages with
    block-expressions (Rust's `()` is the direct inspiration here)."""

    stmts: tuple[Stmt, ...]
    tail: Expr | None
    span: Span


Expr = (
    IntLit
    | FloatLit
    | StringLit
    | BoolLit
    | Ident
    | Unary
    | Binary
    | If
    | Call
    | Lambda
    | ListLit
    | BlockExpr
)


def expr_span(expr: Expr) -> Span:
    return expr.span


# ---- Top-level declarations -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FnDecl:
    name: str
    params: tuple[Param, ...]
    return_type_ann: TypeAnn | None
    body: Expr
    span: Span


Decl = LetDecl | FnDecl


@dataclass(frozen=True, slots=True)
class Program:
    decls: tuple[Decl, ...]
