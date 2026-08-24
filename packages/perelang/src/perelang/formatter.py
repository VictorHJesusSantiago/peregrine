"""An AST-driven pretty-printer: `format_program` walks a parsed `ast_nodes.Program` (never the
raw source text — no regex, no string-munging) and re-emits canonical Peregrine source, matching
the 2-space-indent, brace-on-same-line convention already used throughout `examples/*.pgr`.

**Why precedence-aware, not just "print what's there."** The parser (`parser.py`) never builds a
"parenthesized expression" node — `(expr)` in source just returns `expr` itself (see
`Parser._parse_primary`'s `LPAREN` case). That means grouping parentheses are *not* recoverable
from the AST as data; the only way to reconstruct source that reparses to the same tree is to
re-derive, from each operator's precedence and associativity, exactly where parentheses are
*required* to prevent a different (looser) parse — the same table `parser.py` already encodes
positionally (as its call-chain), reused here as a numeric ladder. Two subtleties that would
silently corrupt round-tripping if skipped: a left-associative operator's *right* operand needs a
strictly higher minimum precedence than its own (`a - (b - c)` must not print as `a - b - c`,
which would reparse left-associatively as `(a - b) - c`), and a `Lambda`/`Binary`/`Unary` used as a
`Call`'s callee must always be parenthesized (none of those three is a `primary` in the grammar, so
an unparenthesized one there would either fail to parse or, for `Lambda`, silently absorb the call's
`(...)` into its own body instead of being called).

**Block inlining.** A block with no statements whose tail is a "simple" expression (anything except
another `BlockExpr` or `If`) renders on one line as `{ tail }`; every other block renders multi-line
with a closing `}` on its own line at the enclosing indent. This one rule reproduces every block in
`examples/*.pgr` exactly: `fn main() -> Int { fact(10) }` inlines (tail is a plain `Call`), while
`fact`'s own body — whose tail is an `If` — does not, even though the *inner* `if`'s own two
branches (tails `1` and `n * fact(n - 1)`, both simple) each inline in turn. Deliberately not a
line-length heuristic: the naive width check would also inline `fact`'s whole body (it fits under
100 columns) and disagree with the examples' own chosen convention, which draws the line at
expression *kind*, not length.

**Comments are unrecoverable here, by construction.** `lexer.py` discards `//` comments during
tokenization (see its module docstring) and the AST carries no comment nodes, so a formatter that
only ever walks the AST — which this one is, deliberately, per the task's own instruction not to
regex the source — has no way to know a comment existed, let alone where to put it back. Formatting
a file that has comments will silently drop them. This is a real, known limitation, not an
oversight: recovering comments would require either a comment-preserving token stream (a change to
`lexer.py`/`tokens.py` explicitly ruled out of scope) or a second, separate raw-source scan glued
back onto AST positions after the fact — the latter is exactly what `docs_gen.py` does for the one
case that actually needs it (doc comments), but wiring that same trick generally into a pretty-
printer is a substantially bigger undertaking than this pass had room for.
"""

from __future__ import annotations

from perelang import ast_nodes as ast
from perelang.parser import parse_program

_INDENT = "  "
_LINE_WIDTH = 100

# Binding power, low to high — mirrors parser.py's precedence-climbing call chain
# (_parse_or -> _parse_and -> _parse_equality -> _parse_comparison -> _parse_additive ->
# _parse_multiplicative) positionally; here it's just a lookup table instead of nested calls.
_BINARY_PREC: dict[str, int] = {
    "||": 1,
    "&&": 2,
    "==": 3,
    "!=": 3,
    "<": 4,
    "<=": 4,
    ">": 4,
    ">=": 4,
    "+": 5,
    "-": 5,
    "+.": 5,
    "-.": 5,
    "*": 6,
    "/": 6,
    "%": 6,
    "*.": 6,
    "/.": 6,
}
_UNARY_PREC = 7
_CALL_PREC = 8
_ATOM_PREC = 9

# A block's tail inlines onto one line only if it's one of these "self-contained" expression
# kinds — never a BlockExpr or an If, which each carry their own multi-line-worthy structure (see
# the module docstring's note on why this is a kind check, not a width check).
_INLINE_TAIL_TYPES = (
    ast.IntLit,
    ast.FloatLit,
    ast.StringLit,
    ast.BoolLit,
    ast.Ident,
    ast.Unary,
    ast.Binary,
    ast.Call,
    ast.ListLit,
    ast.Lambda,
)


def _prec_of(expr: ast.Expr) -> int:
    if isinstance(expr, ast.Binary):
        return _BINARY_PREC[expr.op]
    if isinstance(expr, ast.Unary):
        return _UNARY_PREC
    if isinstance(expr, ast.Call):
        return _CALL_PREC
    return _ATOM_PREC


def _escape_string(value: str) -> str:
    out = []
    for c in value:
        if c == "\\":
            out.append("\\\\")
        elif c == '"':
            out.append('\\"')
        elif c == "\n":
            out.append("\\n")
        elif c == "\t":
            out.append("\\t")
        else:
            out.append(c)
    return "".join(out)


def _fmt_float(value: float) -> str:
    # `repr` always includes a decimal point for a float (`repr(3.0) == "3.0"`), which is exactly
    # what the lexer requires to re-tokenize this as FLOAT rather than INT (`_lex_number` only
    # takes the float branch when a '.' is followed by another digit).
    return repr(value)


def _fmt_type_ann(ann: ast.TypeAnn) -> str:
    if not ann.args:
        return ann.name
    return f"{ann.name}<{', '.join(_fmt_type_ann(a) for a in ann.args)}>"


def _fmt_params(params: tuple[ast.Param, ...]) -> str:
    parts = []
    for p in params:
        if p.type_ann is not None:
            parts.append(f"{p.name}: {_fmt_type_ann(p.type_ann)}")
        else:
            parts.append(p.name)
    return ", ".join(parts)


def _fmt_callee(callee: ast.Expr, level: int) -> str:
    # Lambda/Binary/Unary are not `primary` productions the grammar's `_parse_call` can consume
    # directly as a callee — see the module docstring — so they always need parens here, unlike
    # every other expression kind (Ident, literals, Call, ListLit, If, BlockExpr are all `primary`).
    text = _fmt_expr(callee, 0, level)
    if isinstance(callee, ast.Lambda | ast.Binary | ast.Unary):
        return f"({text})"
    return text


def _fmt_expr(expr: ast.Expr, min_prec: int, level: int) -> str:
    text = _fmt_expr_inner(expr, level)
    if _prec_of(expr) < min_prec:
        return f"({text})"
    return text


def _fmt_expr_inner(expr: ast.Expr, level: int) -> str:
    match expr:
        case ast.IntLit(value, _span):
            return str(value)
        case ast.FloatLit(value, _span):
            return _fmt_float(value)
        case ast.StringLit(value, _span):
            return f'"{_escape_string(value)}"'
        case ast.BoolLit(value, _span):
            return "true" if value else "false"
        case ast.Ident(name, _span):
            return name
        case ast.Unary(op, operand, _span):
            return f"{op}{_fmt_expr(operand, _UNARY_PREC, level)}"
        case ast.Binary(op, left, right, _span):
            prec = _BINARY_PREC[op]
            # Right operand gets `prec + 1`: every one of this language's binary operators is
            # left-associative (the parser's `while` loops fold left), so a right child with this
            # *same* precedence must be parenthesized or it would silently re-associate on reparse.
            left_s = _fmt_expr(left, prec, level)
            right_s = _fmt_expr(right, prec + 1, level)
            return f"{left_s} {op} {right_s}"
        case ast.If(cond, then_branch, else_branch, _span):
            cond_s = _fmt_expr(cond, 0, level)
            then_s = _fmt_block(then_branch, level)
            else_s = _fmt_block(else_branch, level)
            return f"if {cond_s} {then_s} else {else_s}"
        case ast.Call(callee, args, _span):
            callee_s = _fmt_callee(callee, level)
            args_s = ", ".join(_fmt_expr(a, 0, level) for a in args)
            return f"{callee_s}({args_s})"
        case ast.Lambda(params, body, _span):
            return f"|{_fmt_params(params)}| {_fmt_expr(body, 0, level)}"
        case ast.ListLit(items, _span):
            return "[" + ", ".join(_fmt_expr(i, 0, level) for i in items) + "]"
        case ast.BlockExpr():
            return _fmt_block(expr, level)


def _fmt_stmt(stmt: ast.Stmt, level: int) -> str:
    match stmt:
        case ast.LetDecl():
            return _fmt_let(stmt, level)
        case ast.ExprStmt(expr, _span):
            return f"{_fmt_expr(expr, 0, level)};"


def _fmt_let(decl: ast.LetDecl, level: int) -> str:
    ann = f": {_fmt_type_ann(decl.type_ann)}" if decl.type_ann is not None else ""
    return f"let {decl.name}{ann} = {_fmt_expr(decl.value, 0, level)};"


def _is_inline_candidate(tail: ast.Expr) -> bool:
    return isinstance(tail, _INLINE_TAIL_TYPES)


def _fmt_block(block: ast.BlockExpr, level: int) -> str:
    if not block.stmts and block.tail is None:
        return "{}"
    if not block.stmts and block.tail is not None and _is_inline_candidate(block.tail):
        inline = f"{{ {_fmt_expr(block.tail, 0, level)} }}"
        if "\n" not in inline and len(inline) <= _LINE_WIDTH:
            return inline

    inner_level = level + 1
    inner_indent = _INDENT * inner_level
    lines = [inner_indent + _fmt_stmt(s, inner_level) for s in block.stmts]
    if block.tail is not None:
        lines.append(inner_indent + _fmt_expr(block.tail, 0, inner_level))
    closing_indent = _INDENT * level
    return "{\n" + "\n".join(lines) + "\n" + closing_indent + "}"


def _fmt_fn_decl(decl: ast.FnDecl) -> str:
    params = _fmt_params(decl.params)
    ret = f" -> {_fmt_type_ann(decl.return_type_ann)}" if decl.return_type_ann is not None else ""
    # The grammar's `fn_decl` production always parses a `block_expr` for the body (see
    # parser.py's `_parse_fn_decl`) — `FnDecl.body`'s static type is the wider `Expr` union only
    # because that field is shared structurally with nothing narrower, not because a non-block
    # body is reachable; `ir.py`'s lowerer asserts the same thing for the same reason.
    assert isinstance(decl.body, ast.BlockExpr), "fn body must be a block (guaranteed by the parser)"
    body = _fmt_block(decl.body, 0)
    return f"fn {decl.name}({params}){ret} {body}"


def _fmt_decl(decl: ast.Decl) -> str:
    match decl:
        case ast.LetDecl():
            return _fmt_let(decl, 0)
        case ast.FnDecl():
            return _fmt_fn_decl(decl)


def format_program(program: ast.Program) -> str:
    """Canonical source text for `program`. Top-level declarations are separated by one blank
    line each (matching `examples/*.pgr`), with a single trailing newline; an empty program
    formats to the empty string."""
    if not program.decls:
        return ""
    return "\n\n".join(_fmt_decl(d) for d in program.decls) + "\n"


def format_source(source: str) -> str:
    """Parse-then-print in one call — what `pere fmt` actually uses. Raises whatever
    `parser.parse_program` raises (`LexError`/`ParseError`) on invalid input; there is nothing
    sensible to format if `source` isn't valid Peregrine."""
    return format_program(parse_program(source))
