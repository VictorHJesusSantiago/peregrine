"""Static checks beyond what `types.py`'s Hindley-Milner inference already rejects. The type
checker only ever complains about *unsound* programs (an unbound name, a unification failure); it
has nothing to say about a program that type-checks fine but is probably a mistake — an unused
binding, a parameter nothing reads, an `if` whose branches happen to be identical, a name that
quietly shadows an outer one. That's this module's whole job.

Each check is a free function `_check_<name>(decl_or_program) -> list[Finding]`; `lint_program`
just concatenates all of them. Kept as free functions over a single `Program` walk (rather than one
combined visitor) because the checks genuinely don't share state — unused-binding tracking needs a
read-count per scope, shadow detection needs an enclosing-names stack, identical-branches needs
none of that — forcing them into one traversal would only tangle them together for no benefit.

**Scope note on "read."** A name is "used" here if any `Ident` node anywhere in its scope
references it — this deliberately does not attempt reachability/dead-code analysis (a use inside a
branch that can never run still counts as a use). That matches what every mainstream linter's
"unused variable" check does (Python's `pyflakes`, Rust's `unused_variables`): syntactic-use
tracking, not control-flow-sensitive liveness.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass

from perelang import ast_nodes as ast
from perelang.errors import Span
from perelang.parser import parse_program

# ---- Finding ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Finding:
    message: str
    span: Span
    severity: str = "warning"  # this linter never hard-errors; every finding is advisory

    def __str__(self) -> str:
        return f"{self.span}: {self.severity}: {self.message}"


# ---- collecting every `Ident` read in an expression/block ---------------------------------------


def _idents_in_expr(expr: ast.Expr) -> list[ast.Ident]:
    match expr:
        case ast.IntLit() | ast.FloatLit() | ast.StringLit() | ast.BoolLit():
            return []
        case ast.Ident():
            return [expr]
        case ast.Unary(_op, operand, _span):
            return _idents_in_expr(operand)
        case ast.Binary(_op, left, right, _span):
            return _idents_in_expr(left) + _idents_in_expr(right)
        case ast.If(cond, then_b, else_b, _span):
            return _idents_in_expr(cond) + _idents_in_block(then_b) + _idents_in_block(else_b)
        case ast.Call(callee, args, _span):
            result = _idents_in_expr(callee)
            for a in args:
                result += _idents_in_expr(a)
            return result
        case ast.Lambda(_params, body, _span):
            return _idents_in_expr(body)
        case ast.ListLit(items, _span):
            result = []
            for i in items:
                result += _idents_in_expr(i)
            return result
        case ast.BlockExpr():
            return _idents_in_block(expr)


def _idents_in_block(block: ast.BlockExpr) -> list[ast.Ident]:
    result: list[ast.Ident] = []
    for stmt in block.stmts:
        match stmt:
            case ast.LetDecl(_name, _type_ann, value, _span):
                result += _idents_in_expr(value)
            case ast.ExprStmt(expr, _span):
                result += _idents_in_expr(expr)
    if block.tail is not None:
        result += _idents_in_expr(block.tail)
    return result


# ---- check: unused `let` bindings ----------------------------------------------------------------


def _check_unused_lets_in_block(block: ast.BlockExpr) -> list[Finding]:
    findings: list[Finding] = []
    for i, stmt in enumerate(block.stmts):
        if not isinstance(stmt, ast.LetDecl):
            continue
        # "Read after the point of binding, anywhere in the rest of this block" — later sibling
        # statements, the tail, and (transitively) any nested block/lambda within them, since a
        # closure capturing the name still counts as a use.
        later_idents: list[ast.Ident] = []
        for later in block.stmts[i + 1 :]:
            match later:
                case ast.LetDecl(_n, _ta, value, _sp):
                    later_idents += _idents_in_expr(value)
                case ast.ExprStmt(expr, _sp):
                    later_idents += _idents_in_expr(expr)
        if block.tail is not None:
            later_idents += _idents_in_expr(block.tail)
        if not any(ident.name == stmt.name for ident in later_idents):
            findings.append(Finding(f"unused binding {stmt.name!r}", stmt.span))
    return findings


def _walk_blocks_for_unused_lets(expr: ast.Expr) -> list[Finding]:
    """Recurses into every nested block (an `if`'s branches, a lambda's body if it's a block, ...)
    so a `let` shadowed several levels deep still gets checked, not just top-level function bodies."""
    findings: list[Finding] = []
    match expr:
        case ast.BlockExpr():
            findings += _check_unused_lets_in_block(expr)
            for stmt in expr.stmts:
                if isinstance(stmt, ast.LetDecl):
                    findings += _walk_blocks_for_unused_lets(stmt.value)
                else:
                    findings += _walk_blocks_for_unused_lets(stmt.expr)
            if expr.tail is not None:
                findings += _walk_blocks_for_unused_lets(expr.tail)
        case ast.If(cond, then_b, else_b, _span):
            findings += _walk_blocks_for_unused_lets(cond)
            findings += _walk_blocks_for_unused_lets(then_b)
            findings += _walk_blocks_for_unused_lets(else_b)
        case ast.Unary(_op, operand, _span):
            findings += _walk_blocks_for_unused_lets(operand)
        case ast.Binary(_op, left, right, _span):
            findings += _walk_blocks_for_unused_lets(left)
            findings += _walk_blocks_for_unused_lets(right)
        case ast.Call(callee, args, _span):
            findings += _walk_blocks_for_unused_lets(callee)
            for a in args:
                findings += _walk_blocks_for_unused_lets(a)
        case ast.Lambda(_params, body, _span):
            findings += _walk_blocks_for_unused_lets(body)
        case ast.ListLit(items, _span):
            for i in items:
                findings += _walk_blocks_for_unused_lets(i)
        case ast.IntLit() | ast.FloatLit() | ast.StringLit() | ast.BoolLit() | ast.Ident():
            pass
    return findings


# ---- check: unused function parameters -----------------------------------------------------------


def _check_unused_params(fn: ast.FnDecl) -> list[Finding]:
    used = {ident.name for ident in _idents_in_expr(fn.body)}
    return [
        Finding(f"unused parameter {p.name!r} in fn {fn.name!r}", p.span)
        for p in fn.params
        if p.name not in used
    ]


# ---- check: identical if/else branches -------------------------------------------------------------


def _block_structurally_equal(a: ast.BlockExpr, b: ast.BlockExpr) -> bool:
    """Structural equality ignoring `Span` (two branches typed identically but at different source
    locations should still be flagged — that's the whole point of the check)."""
    return _strip_spans(a) == _strip_spans(b)


def _strip_spans(node: object) -> object:
    if isinstance(node, Span):
        return None
    if isinstance(node, tuple):
        return tuple(_strip_spans(x) for x in node)
    if dataclasses.is_dataclass(node) and not isinstance(node, type):
        return (
            type(node).__name__,
            tuple((f.name, _strip_spans(getattr(node, f.name))) for f in dataclasses.fields(node) if f.name != "span"),
        )
    return node


def _check_identical_branches(expr: ast.Expr) -> list[Finding]:
    findings: list[Finding] = []
    match expr:
        case ast.If(cond, then_b, else_b, span):
            findings += _check_identical_branches(cond)
            findings += _check_identical_branches(then_b)
            findings += _check_identical_branches(else_b)
            if _block_structurally_equal(then_b, else_b):
                findings.append(Finding("'if' and 'else' branches are identical", span))
        case ast.BlockExpr(stmts, tail, _span):
            for stmt in stmts:
                if isinstance(stmt, ast.LetDecl):
                    findings += _check_identical_branches(stmt.value)
                else:
                    findings += _check_identical_branches(stmt.expr)
            if tail is not None:
                findings += _check_identical_branches(tail)
        case ast.Unary(_op, operand, _span):
            findings += _check_identical_branches(operand)
        case ast.Binary(_op, left, right, _span):
            findings += _check_identical_branches(left)
            findings += _check_identical_branches(right)
        case ast.Call(callee, args, _span):
            findings += _check_identical_branches(callee)
            for a in args:
                findings += _check_identical_branches(a)
        case ast.Lambda(_params, body, _span):
            findings += _check_identical_branches(body)
        case ast.ListLit(items, _span):
            for i in items:
                findings += _check_identical_branches(i)
        case ast.IntLit() | ast.FloatLit() | ast.StringLit() | ast.BoolLit() | ast.Ident():
            pass
    return findings


# ---- check: shadowing an enclosing scope's name ----------------------------------------------------


def _check_shadowing_in_block(block: ast.BlockExpr, outer: frozenset[str]) -> list[Finding]:
    findings: list[Finding] = []
    scope = outer
    for stmt in block.stmts:
        match stmt:
            case ast.LetDecl(name, _type_ann, value, span):
                if name in scope:
                    findings.append(Finding(f"{name!r} shadows a binding from an enclosing scope", span))
                findings += _check_shadowing_in_expr(value, scope)
                scope = scope | {name}
            case ast.ExprStmt(expr, _span):
                findings += _check_shadowing_in_expr(expr, scope)
    if block.tail is not None:
        findings += _check_shadowing_in_expr(block.tail, scope)
    return findings


def _check_shadowing_in_expr(expr: ast.Expr, outer: frozenset[str]) -> list[Finding]:
    findings: list[Finding] = []
    match expr:
        case ast.BlockExpr():
            findings += _check_shadowing_in_block(expr, outer)
        case ast.If(cond, then_b, else_b, _span):
            findings += _check_shadowing_in_expr(cond, outer)
            findings += _check_shadowing_in_block(then_b, outer)
            findings += _check_shadowing_in_block(else_b, outer)
        case ast.Unary(_op, operand, _span):
            findings += _check_shadowing_in_expr(operand, outer)
        case ast.Binary(_op, left, right, _span):
            findings += _check_shadowing_in_expr(left, outer)
            findings += _check_shadowing_in_expr(right, outer)
        case ast.Call(callee, args, _span):
            findings += _check_shadowing_in_expr(callee, outer)
            for a in args:
                findings += _check_shadowing_in_expr(a, outer)
        case ast.Lambda(params, body, _span):
            for p in params:
                if p.name in outer:
                    findings.append(
                        Finding(f"parameter {p.name!r} shadows a binding from an enclosing scope", p.span)
                    )
            inner = outer | {p.name for p in params}
            findings += _check_shadowing_in_expr(body, inner)
        case ast.ListLit(items, _span):
            for i in items:
                findings += _check_shadowing_in_expr(i, outer)
        case ast.IntLit() | ast.FloatLit() | ast.StringLit() | ast.BoolLit() | ast.Ident():
            pass
    return findings


# ---- top level --------------------------------------------------------------------------------


def lint_program(program: ast.Program) -> list[Finding]:
    """Every finding across every top-level declaration, in source order. Never raises — a program
    that doesn't even parse never reaches here (that's `ParseError`'s job); everything this module
    finds is advisory, not a hard error, which is why `Finding.severity` defaults to `"warning"`."""
    findings: list[Finding] = []
    for decl in program.decls:
        match decl:
            case ast.FnDecl(_name, params, _ret, body, _span):
                findings += _check_unused_params(decl)
                if isinstance(body, ast.BlockExpr):
                    findings += _walk_blocks_for_unused_lets(body)
                    findings += _check_identical_branches(body)
                    param_names = frozenset(p.name for p in params)
                    # Top-level names are deliberately excluded from the shadowing baseline: every
                    # top-level `fn`/`let` is visible from every other declaration's body by
                    # design (mutual recursion), so a parameter or local reusing a sibling
                    # top-level name is completely ordinary here, not a shadowing mistake — only
                    # a *nested* scope (a local shadowing a param, a block-local shadowing an
                    # outer block-local) is the kind of surprise this check is for.
                    findings += _check_shadowing_in_block(body, param_names)
            case ast.LetDecl(_name, _type_ann, value, _span):
                findings += _walk_blocks_for_unused_lets(value)
                findings += _check_identical_branches(value)
                findings += _check_shadowing_in_expr(value, frozenset())
    return findings


def lint_source(source: str) -> list[Finding]:
    return lint_program(parse_program(source))
