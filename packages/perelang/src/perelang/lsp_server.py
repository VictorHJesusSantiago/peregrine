"""A hand-written JSON-RPC-over-stdio Language Server Protocol server — no `pygls` (it isn't
installed in this environment and the task explicitly rules out `pip install`-ing it), which is
also the more consistent choice: every other phase of this compiler (lexer, parser, bytecode VM,
LLVM backend) is hand-written with no framework standing between the code and what it does, and
the LSP transport is no different in kind from any of those — a length-prefixed message framing
(`Content-Length: N\\r\\n\\r\\n<json>`) plus a JSON-RPC method dispatch, both small enough to write
directly.

Structured the same way `repl.py` structures itself (see that module's own docstring for the
rationale): a testable core (`LspCore.handle`, a pure function of "one already-decoded request
dict in, a list of response/notification dicts out" with no IO) is kept completely separate from
the wire framing (`read_message`/`write_message`, the `Content-Length` header parsing) and from the
thin `serve()`/`main()` that wires the two to real stdin/stdout. `tests/test_lsp_server.py` drives
`LspCore` directly with constructed dicts — no subprocess, no socket, no real client needed.

**Scope.** Implemented: `initialize`/`initialized`, `textDocument/didOpen`,
`textDocument/didChange` (full-document sync only — `textDocumentSync: 1`, so every change carries
the document's complete new text, never a range delta; simpler to implement correctly, and
Peregrine source files are small enough that incremental sync would only add complexity, not
speed), `textDocument/didClose`, `textDocument/publishDiagnostics` (pushed after every open/change),
`shutdown`/`exit`. Diagnostics merge `LexError`/`ParseError`/`TypeCheckError` (severity 1, "Error")
with `linter.py`'s findings (severity 2, "Warning") into one list per document, recomputed from
scratch on every request — the same "no incremental state, just recompile everything" choice
`repl.py` already made for the same reason (see its docstring): correctness is trivial when there's
no cache to invalidate, and a single source file is cheap enough to reparse on every keystroke.

**`textDocument/hover`**, now covering both scopes. A top-level `fn`/`let` name still resolves via
`types.infer_program` (which only ever returns per-top-level-name types) and shows `name : type`,
exactly as before. A *local* (a parameter, a `let` inside a function body, a lambda parameter) now
also resolves — `ir.py`'s `IrExpr`/`IrLet` nodes carry a real source `Span` on every node as of this
pass (previously the blocker noted here and in `ir.py`'s own docstring lineage through `vm.py`'s),
so `_hover_local` runs `ir.lower_program` and walks the resulting `IrProgram` (`_find_ir_node_at`)
for the innermost node containing the cursor, reporting an `IrName`'s or `IrLet`'s `.ty` the same
way. Hovering anything else (an operator, a literal, a call's parentheses) intentionally shows
nothing, matching the existing "only an identifier" scope of the top-level path — this doesn't
attempt "show the type of any subexpression."

**`textDocument/definition`, `textDocument/references`, `textDocument/rename`,
`textDocument/completion`.** All four are pure lexical scope resolution over the AST — no IR, no
type information needed, since the question they answer ("what does this name refer to,"
"what's in scope here") is answered entirely by `ast_nodes.py`'s already-span-carrying nodes.
`_locate_in_program` is the one traversal all four share: given a cursor offset, it returns both
the scope chain visible at that point (every top-level `fn`/`let`, the enclosing function's
params, every `let` already bound earlier in the same block — nested blocks/lambdas each push
their own frame) and whatever AST node sits directly under the cursor (an `Ident` *use*, or a
`FnDecl`/`LetDecl`/`Param` *declaration* if the cursor is on a binding's own name). Resolving a use
to its declaration is then just a scope-chain lookup, shadowing falls out for free (the innermost
frame containing the name wins), and go-to-definition/references/rename/completion are each a thin
wrapper around that one shared result:
- *go-to-definition* returns the resolved binding's own name-token span (see `_name_spans` — since
  `ast_nodes.py` only carries a *whole-declaration* span for `FnDecl`/`LetDecl`, not a separate
  name-token span, this re-tokenizes the source once per request with `lexer.tokenize` purely to
  recover the identifier token immediately following each `let`/`fn` keyword — no `ast_nodes.py`
  change needed; `Param`'s own span already *is* its name token).
- *find-references* resolves every `Ident` in the whole program the same way and keeps the ones
  whose resolved binding is the same object (identity, not name equality — a shadowing `let x`
  elsewhere is a different binding and correctly excluded); a recursive call inside a function's
  own body resolves to that function, same as any other use.
- *rename* is find-references plus a validity check on the new name (`lexer._is_ident_start`/
  `_is_ident_part`, plus rejecting `tokens.KEYWORDS`) and a `WorkspaceEdit` built from the same
  span list — it never touches the document text itself, only computes the edit, per LSP's own
  division of labor between server (compute) and client (apply).
- *completion* returns every `tokens.KEYWORDS` entry plus every name in the scope chain at the
  cursor, which is exactly what already respects "not yet declared" sequential block scoping,
  since that's the same chain go-to-definition resolves against.

**Genuine remaining limits, stated plainly.** Everything above resolves within one open document —
there is no cross-file/cross-module symbol table, so go-to-definition/references/rename never
reach into a different `.pgr` file (this server only ever tracks the documents a client has
opened, and Peregrine has no `import`/module system for them to reference each other through
regardless). Completion is keyword-and-in-scope-identifier only: no member-access completion, no
snippet expansion, no type-directed filtering of candidates by the expected call-site type — those
are each real, separate scope. Hover's "innermost IR node" search only ever produces a message for
an `IrName`/`IrLet` (an identifier), not an arbitrary subexpression like `a + b` as a whole. Not
built at all: signature help — cut for time, not attempted, and not claimed.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from typing import Any, BinaryIO

from perelang import ast_nodes as ast
from perelang import ir
from perelang.errors import LexError, ParseError, PeregrineError, Span, TypeCheckError
from perelang.lexer import _is_ident_part, _is_ident_start, tokenize
from perelang.linter import lint_program
from perelang.parser import parse_program
from perelang.tokens import KEYWORDS, TokenKind
from perelang.types import infer_program, type_to_str

JsonRpcMessage = dict[str, Any]

_SERVER_INFO = {"name": "perelang-lsp", "version": "0.1.0"}
_SEVERITY_ERROR = 1
_SEVERITY_WARNING = 2

# ---- position/offset conversion ------------------------------------------------------------------


def _offset_to_position(source: str, offset: int) -> dict[str, int]:
    """`Span.start`/`Span.end` are absolute character offsets into `source` (see `errors.py`); LSP
    wants 0-based `{line, character}`. Scanning `source` up to `offset` for this (rather than
    trusting `Span.line`/`Span.column`, which are 1-based and only ever computed for a span's
    *start*, never its end) handles both ends of a span uniformly with one function."""
    offset = max(0, min(offset, len(source)))
    line = source.count("\n", 0, offset)
    last_newline = source.rfind("\n", 0, offset)
    character = offset - (last_newline + 1)
    return {"line": line, "character": character}


def _position_to_offset(source: str, line: int, character: int) -> int:
    lines = source.split("\n")
    offset = sum(len(text) + 1 for text in lines[:line])
    return offset + character


def _span_to_range(source: str, span: Span) -> dict[str, Any]:
    return {"start": _offset_to_position(source, span.start), "end": _offset_to_position(source, span.end)}


def _diagnostic(source: str, span: Span, message: str, severity: int) -> dict[str, Any]:
    return {
        "range": _span_to_range(source, span),
        "severity": severity,
        "source": "perelang",
        "message": message,
    }


def compute_diagnostics(source: str) -> list[dict[str, Any]]:
    """Every diagnostic for one document's current text, freshly computed: a lex/parse error stops
    here (nothing downstream can run on unparseable text), a type error is reported *and* the
    linter still runs (its checks are purely syntactic — see `linter.py` — so they're still
    meaningful on a program that fails to type-check)."""
    try:
        program = parse_program(source)
    except (LexError, ParseError) as exc:
        return [_diagnostic(source, exc.span, exc.message, _SEVERITY_ERROR)]

    diagnostics: list[dict[str, Any]] = []
    try:
        infer_program(program)
    except TypeCheckError as exc:
        diagnostics.append(_diagnostic(source, exc.span, exc.message, _SEVERITY_ERROR))

    for finding in lint_program(program):
        diagnostics.append(_diagnostic(source, finding.span, finding.message, _SEVERITY_WARNING))
    return diagnostics


# ---- hover: resolve an Ident under the cursor to a top-level declaration's type -------------------


def _idents_containing(expr: ast.Expr, offset: int) -> list[ast.Ident]:
    match expr:
        case ast.Ident():
            return [expr] if expr.span.start <= offset < expr.span.end else []
        case ast.IntLit() | ast.FloatLit() | ast.StringLit() | ast.BoolLit():
            return []
        case ast.Unary(_op, operand, _span):
            return _idents_containing(operand, offset)
        case ast.Binary(_op, left, right, _span):
            return _idents_containing(left, offset) + _idents_containing(right, offset)
        case ast.If(cond, then_b, else_b, _span):
            return (
                _idents_containing(cond, offset)
                + _idents_in_block_containing(then_b, offset)
                + _idents_in_block_containing(else_b, offset)
            )
        case ast.Call(callee, args, _span):
            result = _idents_containing(callee, offset)
            for a in args:
                result += _idents_containing(a, offset)
            return result
        case ast.Lambda(_params, body, _span):
            return _idents_containing(body, offset)
        case ast.ListLit(items, _span):
            result = []
            for i in items:
                result += _idents_containing(i, offset)
            return result
        case ast.BlockExpr():
            return _idents_in_block_containing(expr, offset)


def _idents_in_block_containing(block: ast.BlockExpr, offset: int) -> list[ast.Ident]:
    result: list[ast.Ident] = []
    for stmt in block.stmts:
        match stmt:
            case ast.LetDecl(_name, _ta, value, _span):
                result += _idents_containing(value, offset)
            case ast.ExprStmt(expr, _span):
                result += _idents_containing(expr, offset)
    if block.tail is not None:
        result += _idents_containing(block.tail, offset)
    return result


def _find_ident_at(program: ast.Program, offset: int) -> ast.Ident | None:
    candidates: list[ast.Ident] = []
    for decl in program.decls:
        match decl:
            case ast.FnDecl(_name, _params, _ret, body, _span):
                if isinstance(body, ast.BlockExpr):
                    candidates += _idents_in_block_containing(body, offset)
            case ast.LetDecl(_name, _ta, value, _span):
                candidates += _idents_containing(value, offset)
    if not candidates:
        return None
    # The smallest containing span wins — relevant only in pathological zero/negative-width cases,
    # since two distinct `Ident`s can never otherwise overlap.
    return min(candidates, key=lambda i: i.span.end - i.span.start)


# ---- hover for locals: innermost-node lookup over the span-carrying IR ----------------------------


def _contains(span: Span, offset: int) -> bool:
    return span.start <= offset < span.end


IrHoverTarget = ir.IrExpr | ir.IrLet  # `IrLet` carries no `.ty` of its own -- see `_hover_local`


def _find_ir_node_at(program: ir.IrProgram, offset: int) -> IrHoverTarget | None:
    for decl in program.decls:
        if not _contains(decl.span, offset):
            continue
        match decl:
            case ir.IrFunction(_name, _params, _pt, _rt, body, _span):
                return _find_ir_node_in_block(body, offset)
            case ir.IrGlobalLet(_name, value, _ty, _span):
                return _find_ir_in_expr(value, offset)
    return None


def _find_ir_in_expr(node: ir.IrExpr, offset: int) -> IrHoverTarget | None:
    """The innermost `IrExpr` (by source span) containing `offset`, walking the same node shapes
    `ir.py`'s own `_resolve_ir_expr`/`_free_local_names` already pattern-match over -- falling back
    to `node` itself (not `None`) once its span is confirmed to contain `offset` but no child's
    does, so a hover on, say, the parens of a call still reports *something* (the call's own
    type), rather than nothing."""
    if not _contains(node.span, offset):
        return None
    match node:
        case ir.IrInt() | ir.IrFloat() | ir.IrStr() | ir.IrBool() | ir.IrName():
            return node
        case ir.IrUnary(_op, operand, _ty, _span):
            return _find_ir_in_expr(operand, offset) or node
        case ir.IrBinary(_op, left, right, _ty, _span):
            return _find_ir_in_expr(left, offset) or _find_ir_in_expr(right, offset) or node
        case ir.IrIf(cond, then_b, else_b, _ty, _span):
            return (
                _find_ir_in_expr(cond, offset)
                or _find_ir_node_in_block(then_b, offset)
                or _find_ir_node_in_block(else_b, offset)
                or node
            )
        case ir.IrCall(callee, args, _ty, _span):
            found = _find_ir_in_expr(callee, offset)
            if found is not None:
                return found
            for a in args:
                found = _find_ir_in_expr(a, offset)
                if found is not None:
                    return found
            return node
        case ir.IrListLit(items, _ty, _span):
            for i in items:
                found = _find_ir_in_expr(i, offset)
                if found is not None:
                    return found
            return node
        case ir.IrClosure(_params, _pt, body, _captures, _ty, _span):
            return _find_ir_in_expr(body, offset) or node
        case ir.IrBlock():
            return _find_ir_node_in_block(node, offset) or node


def _find_ir_node_in_block(block: ir.IrBlock, offset: int) -> IrHoverTarget | None:
    if not _contains(block.span, offset):
        return None
    for stmt in block.stmts:
        match stmt:
            case ir.IrLet(_name, value, span):
                if _contains(span, offset):
                    return _find_ir_in_expr(value, offset) or stmt
            case ir.IrExprStmt(expr, span):
                if _contains(span, offset):
                    return _find_ir_in_expr(expr, offset)
    if block.tail is not None and _contains(block.tail.span, offset):
        return _find_ir_in_expr(block.tail, offset)
    return block


# ---- shared AST scope resolution: go-to-definition, references, rename, completion ----------------
#
# `_Binding` is whatever AST node a name is actually declared by -- a top-level `FnDecl`/`LetDecl`,
# or a `Param`/local `LetDecl` inside a function body. `_locate_in_program` is the one traversal
# every feature below is built on: it walks down to whatever offset the cursor is at, threading a
# scope chain (innermost frame last) the same way `ir.py`'s `_Lowerer`/`lower_block` thread
# `locals_`, and returns both that chain and whatever node sits directly under the cursor (an
# `Ident` *use*, or the declaration itself if the cursor is on a binding's own name).

_Binding = ast.FnDecl | ast.LetDecl | ast.Param
_ScopeChain = list[dict[str, _Binding]]
_LocateResult = tuple[_ScopeChain, "ast.Ident | _Binding | None"]


def _name_spans(source: str) -> dict[int, Span]:
    """Maps every `let`/`fn` keyword token's start offset to the span of the very next token (the
    identifier being declared). `ast_nodes.py` only carries a *whole-declaration* span for
    `FnDecl`/`LetDecl` (see its own docstring on why a separate name-token span was never added),
    so this re-derives the exact name-token span by tokenizing the source directly -- no change to
    `ast_nodes.py` needed, and it works uniformly for top-level and block-local `let`s alike, since
    both start with a `LET` token."""
    tokens = tokenize(source)
    spans: dict[int, Span] = {}
    for i, tok in enumerate(tokens):
        if tok.kind in (TokenKind.LET, TokenKind.FN) and i + 1 < len(tokens):
            spans[tok.span.start] = tokens[i + 1].span
    return spans


def _def_span(name_spans: dict[int, Span], binding: _Binding) -> Span:
    """A `Param`'s own `.span` already *is* its name token (see `parser.py`'s `_parse_params`); a
    `FnDecl`/`LetDecl`'s name token is looked up in `name_spans`, falling back to the whole
    declaration's span in the (should-be-unreachable) case a keyword has no following token."""
    if isinstance(binding, ast.Param):
        return binding.span
    return name_spans.get(binding.span.start, binding.span)


def _locate_in_program(program: ast.Program, name_spans: dict[int, Span], offset: int) -> _LocateResult:
    top_level: dict[str, _Binding] = {decl.name: decl for decl in program.decls}
    scope: _ScopeChain = [top_level]
    for decl in program.decls:
        name_span = name_spans.get(decl.span.start)
        if name_span is not None and _contains(name_span, offset):
            return scope, decl
        match decl:
            case ast.FnDecl(_name, params, _ret, body, _span):
                for p in params:
                    if _contains(p.span, offset):
                        return scope, p
                param_frame: dict[str, _Binding] = {p.name: p for p in params}
                fn_scope = [*scope, param_frame]
                if isinstance(body, ast.BlockExpr) and _contains(body.span, offset):
                    return _locate_in_block(body, offset, fn_scope, name_spans)
            case ast.LetDecl(_name, _ta, value, _span):
                if _contains(value.span, offset):
                    return _locate_in_expr(value, offset, scope, name_spans)
    return scope, None


def _locate_in_block(
    block: ast.BlockExpr, offset: int, scope: _ScopeChain, name_spans: dict[int, Span]
) -> _LocateResult:
    """Threads a fresh frame through `block.stmts` in source order, adding each `let` to it only
    *after* passing that `let`'s own span -- so a `let`'s own value expression never sees its own
    name (matching `ir.py`'s `lower_block`), and a name only becomes visible to sibling statements
    that come after it (sequential same-block scoping, matching `types.py`'s block-scoping)."""
    frame: dict[str, _Binding] = {}
    cur_scope = [*scope, frame]
    for stmt in block.stmts:
        if isinstance(stmt, ast.LetDecl):
            name_span = name_spans.get(stmt.span.start)
            if name_span is not None and _contains(name_span, offset):
                return cur_scope, stmt
            if _contains(stmt.value.span, offset):
                return _locate_in_expr(stmt.value, offset, cur_scope, name_spans)
            if offset < stmt.span.start:
                return cur_scope, None
            frame[stmt.name] = stmt
        else:
            if _contains(stmt.expr.span, offset):
                return _locate_in_expr(stmt.expr, offset, cur_scope, name_spans)
            if offset < stmt.span.start:
                return cur_scope, None
    if block.tail is not None and _contains(block.tail.span, offset):
        return _locate_in_expr(block.tail, offset, cur_scope, name_spans)
    return cur_scope, None


def _locate_in_expr(
    expr: ast.Expr, offset: int, scope: _ScopeChain, name_spans: dict[int, Span]
) -> _LocateResult:
    match expr:
        case ast.Ident():
            return scope, (expr if _contains(expr.span, offset) else None)
        case ast.IntLit() | ast.FloatLit() | ast.StringLit() | ast.BoolLit():
            return scope, None
        case ast.Unary(_op, operand, _span):
            return _locate_in_expr(operand, offset, scope, name_spans)
        case ast.Binary(_op, left, right, _span):
            if _contains(left.span, offset):
                return _locate_in_expr(left, offset, scope, name_spans)
            if _contains(right.span, offset):
                return _locate_in_expr(right, offset, scope, name_spans)
            return scope, None
        case ast.If(cond, then_b, else_b, _span):
            if _contains(cond.span, offset):
                return _locate_in_expr(cond, offset, scope, name_spans)
            if _contains(then_b.span, offset):
                return _locate_in_block(then_b, offset, scope, name_spans)
            if _contains(else_b.span, offset):
                return _locate_in_block(else_b, offset, scope, name_spans)
            return scope, None
        case ast.Call(callee, args, _span):
            if _contains(callee.span, offset):
                return _locate_in_expr(callee, offset, scope, name_spans)
            for a in args:
                if _contains(a.span, offset):
                    return _locate_in_expr(a, offset, scope, name_spans)
            return scope, None
        case ast.Lambda(params, body, _span):
            for p in params:
                if _contains(p.span, offset):
                    return scope, p
            lambda_frame: dict[str, _Binding] = {p.name: p for p in params}
            inner_scope = [*scope, lambda_frame]
            if _contains(body.span, offset):
                return _locate_in_expr(body, offset, inner_scope, name_spans)
            return inner_scope, None
        case ast.ListLit(items, _span):
            for i in items:
                if _contains(i.span, offset):
                    return _locate_in_expr(i, offset, scope, name_spans)
            return scope, None
        case ast.BlockExpr():
            return _locate_in_block(expr, offset, scope, name_spans)


def _resolve_in_scope(scope: _ScopeChain, name: str) -> _Binding | None:
    for frame in reversed(scope):
        if name in frame:
            return frame[name]
    return None


def _resolve_ident(program: ast.Program, name_spans: dict[int, Span], ident: ast.Ident) -> _Binding | None:
    scope, _ = _locate_in_program(program, name_spans, ident.span.start)
    return _resolve_in_scope(scope, ident.name)


def _binding_at_cursor(program: ast.Program, name_spans: dict[int, Span], offset: int) -> _Binding | None:
    """The binding the cursor is *at* -- either directly on a declaration's own name (the node
    found *is* the binding), or on a use (an `Ident`, resolved through the scope chain visible at
    that point)."""
    _, node = _locate_in_program(program, name_spans, offset)
    if node is None:
        return None
    if isinstance(node, ast.Ident):
        return _resolve_ident(program, name_spans, node)
    return node


def _all_idents_in_expr(expr: ast.Expr) -> list[ast.Ident]:
    match expr:
        case ast.Ident():
            return [expr]
        case ast.IntLit() | ast.FloatLit() | ast.StringLit() | ast.BoolLit():
            return []
        case ast.Unary(_op, operand, _span):
            return _all_idents_in_expr(operand)
        case ast.Binary(_op, left, right, _span):
            return _all_idents_in_expr(left) + _all_idents_in_expr(right)
        case ast.If(cond, then_b, else_b, _span):
            return _all_idents_in_expr(cond) + _all_idents_in_block(then_b) + _all_idents_in_block(else_b)
        case ast.Call(callee, args, _span):
            result = _all_idents_in_expr(callee)
            for a in args:
                result += _all_idents_in_expr(a)
            return result
        case ast.Lambda(_params, body, _span):
            return _all_idents_in_expr(body)
        case ast.ListLit(items, _span):
            result = []
            for i in items:
                result += _all_idents_in_expr(i)
            return result
        case ast.BlockExpr():
            return _all_idents_in_block(expr)


def _all_idents_in_block(block: ast.BlockExpr) -> list[ast.Ident]:
    result: list[ast.Ident] = []
    for stmt in block.stmts:
        if isinstance(stmt, ast.LetDecl):
            result += _all_idents_in_expr(stmt.value)
        else:
            result += _all_idents_in_expr(stmt.expr)
    if block.tail is not None:
        result += _all_idents_in_expr(block.tail)
    return result


def _all_idents_in_program(program: ast.Program) -> list[ast.Ident]:
    result: list[ast.Ident] = []
    for decl in program.decls:
        if isinstance(decl, ast.FnDecl):
            if isinstance(decl.body, ast.BlockExpr):
                result += _all_idents_in_block(decl.body)
        else:
            result += _all_idents_in_expr(decl.value)
    return result


def _all_reference_spans(
    program: ast.Program, name_spans: dict[int, Span], binding: _Binding, *, include_declaration: bool
) -> list[Span]:
    """Every use of `binding` (an `Ident` resolving, by identity, to that exact declaration node --
    not just a name match, so a shadowing `let` elsewhere with the same name is correctly excluded)
    across the whole program, plus the declaration's own name span if requested. Scanning the whole
    program rather than some more narrowly-computed "correct" subtree is deliberately simple and
    still exactly correct: an `Ident` genuinely out of `binding`'s scope can never resolve to it
    anyway (it either resolves to a different same-named binding, or to nothing)."""
    spans: list[Span] = []
    if include_declaration:
        spans.append(_def_span(name_spans, binding))
    for ident in _all_idents_in_program(program):
        if _resolve_ident(program, name_spans, ident) is binding:
            spans.append(ident.span)
    spans.sort(key=lambda s: s.start)
    return spans


def _is_valid_ident(name: str) -> bool:
    """The same identifier grammar `lexer.py`'s `_lex_ident` uses, plus rejecting anything that
    would actually lex as a keyword (renaming a binding to `let` or `fn` would silently break
    parsing, not just look unconventional)."""
    if not name or not _is_ident_start(name[0]):
        return False
    if any(not _is_ident_part(c) for c in name[1:]):
        return False
    return name not in KEYWORDS


class _InvalidRenameError(Exception):
    """Raised by `LspCore._rename` for an invalid `newName`, caught in `LspCore.handle` and turned
    into a JSON-RPC error response (code `-32602`, "invalid params") rather than a malformed
    edit -- `LspCore` otherwise never raises out of `handle` (see `_hover`'s `PeregrineError`
    handling for the same "never let a bad request 500" spirit, applied here to a bad parameter
    instead of a bad document)."""


# ---- the testable core ----------------------------------------------------------------------------


@dataclass(slots=True)
class _Document:
    uri: str
    text: str


@dataclass(slots=True)
class LspCore:
    """No IO. `handle` takes one decoded JSON-RPC message and returns the (possibly empty) list of
    JSON-RPC messages to send back — a request gets exactly one response; a notification
    (`didOpen`/`didChange`/`didClose`) gets zero or more notifications back (one
    `publishDiagnostics`, here)."""

    documents: dict[str, _Document] = field(default_factory=dict)
    shutdown_requested: bool = False

    def handle(self, message: JsonRpcMessage) -> list[JsonRpcMessage]:
        method = message.get("method")
        msg_id = message.get("id")
        params: dict[str, Any] = message.get("params") or {}

        if method == "initialize":
            return [
                self._response(
                    msg_id,
                    {
                        "capabilities": {
                            "textDocumentSync": 1,
                            "hoverProvider": True,
                            "definitionProvider": True,
                            "referencesProvider": True,
                            "renameProvider": True,
                            "completionProvider": {},
                        },
                        "serverInfo": _SERVER_INFO,
                    },
                )
            ]
        if method == "initialized":
            return []
        if method == "textDocument/didOpen":
            doc = params["textDocument"]
            self.documents[doc["uri"]] = _Document(doc["uri"], doc["text"])
            return [self._publish(doc["uri"])]
        if method == "textDocument/didChange":
            uri = params["textDocument"]["uri"]
            changes = params.get("contentChanges", [])
            if changes:
                self.documents[uri] = _Document(uri, changes[-1]["text"])
            return [self._publish(uri)]
        if method == "textDocument/didClose":
            uri = params["textDocument"]["uri"]
            self.documents.pop(uri, None)
            return [self._notification("textDocument/publishDiagnostics", {"uri": uri, "diagnostics": []})]
        if method == "textDocument/hover":
            return [self._response(msg_id, self._hover(params))]
        if method == "textDocument/definition":
            return [self._response(msg_id, self._definition(params))]
        if method == "textDocument/references":
            return [self._response(msg_id, self._references(params))]
        if method == "textDocument/rename":
            try:
                return [self._response(msg_id, self._rename(params))]
            except _InvalidRenameError as exc:
                return [self._error(msg_id, -32602, str(exc))]
        if method == "textDocument/completion":
            return [self._response(msg_id, self._completion(params))]
        if method == "shutdown":
            self.shutdown_requested = True
            return [self._response(msg_id, None)]
        if method == "exit":
            return []
        if msg_id is not None:
            return [self._error(msg_id, -32601, f"method not found: {method}")]
        return []

    def _publish(self, uri: str) -> JsonRpcMessage:
        doc = self.documents[uri]
        return self._notification(
            "textDocument/publishDiagnostics", {"uri": uri, "diagnostics": compute_diagnostics(doc.text)}
        )

    def _hover(self, params: dict[str, Any]) -> dict[str, Any] | None:
        uri = params.get("textDocument", {}).get("uri")
        doc = self.documents.get(uri)
        if doc is None:
            return None
        position = params["position"]
        offset = _position_to_offset(doc.text, position["line"], position["character"])
        try:
            program = parse_program(doc.text)
            top_level_types = infer_program(program)
        except PeregrineError:
            return None
        ident = _find_ident_at(program, offset)
        if ident is not None and ident.name in top_level_types:
            return {
                "contents": {
                    "kind": "plaintext",
                    "value": f"{ident.name} : {type_to_str(top_level_types[ident.name])}",
                },
                "range": _span_to_range(doc.text, ident.span),
            }
        return self._hover_local(program, doc.text, offset)

    @staticmethod
    def _hover_local(program: ast.Program, source: str, offset: int) -> dict[str, Any] | None:
        """The fallback path for a name `top_level_types` has no entry for — a parameter, a `let`
        inside a function body, or a lambda parameter (see the module docstring's hover section).
        Lowers to `ir.py`'s span-carrying IR and finds the innermost node at `offset`; only an
        `IrName` (a use) or `IrLet` (the bound name itself) produces a message, matching the
        existing top-level path's "only an identifier" scope."""
        try:
            ir_program = ir.lower_program(program)
        except PeregrineError:
            return None
        target = _find_ir_node_at(ir_program, offset)
        if isinstance(target, ir.IrLet):
            name_spans = _name_spans(source)
            span = name_spans.get(target.span.start, target.span)
            return {
                "contents": {"kind": "plaintext", "value": f"{target.name} : {type_to_str(target.value.ty)}"},
                "range": _span_to_range(source, span),
            }
        if isinstance(target, ir.IrName):
            return {
                "contents": {"kind": "plaintext", "value": f"{target.name} : {type_to_str(target.ty)}"},
                "range": _span_to_range(source, target.span),
            }
        return None

    def _definition(self, params: dict[str, Any]) -> dict[str, Any] | None:
        doc, offset = self._doc_and_offset(params)
        if doc is None or offset is None:
            return None
        try:
            program = parse_program(doc.text)
        except PeregrineError:
            return None
        name_spans = _name_spans(doc.text)
        binding = _binding_at_cursor(program, name_spans, offset)
        if binding is None:
            return None
        return {"uri": doc.uri, "range": _span_to_range(doc.text, _def_span(name_spans, binding))}

    def _references(self, params: dict[str, Any]) -> list[dict[str, Any]] | None:
        doc, offset = self._doc_and_offset(params)
        if doc is None or offset is None:
            return None
        try:
            program = parse_program(doc.text)
        except PeregrineError:
            return None
        name_spans = _name_spans(doc.text)
        binding = _binding_at_cursor(program, name_spans, offset)
        if binding is None:
            return None
        include_declaration = params.get("context", {}).get("includeDeclaration", True)
        spans = _all_reference_spans(program, name_spans, binding, include_declaration=include_declaration)
        return [{"uri": doc.uri, "range": _span_to_range(doc.text, span)} for span in spans]

    def _rename(self, params: dict[str, Any]) -> dict[str, Any] | None:
        new_name = params["newName"]
        if not _is_valid_ident(new_name):
            raise _InvalidRenameError(f"{new_name!r} is not a valid Peregrine identifier")
        doc, offset = self._doc_and_offset(params)
        if doc is None or offset is None:
            return None
        try:
            program = parse_program(doc.text)
        except PeregrineError:
            return None
        name_spans = _name_spans(doc.text)
        binding = _binding_at_cursor(program, name_spans, offset)
        if binding is None:
            return None
        spans = _all_reference_spans(program, name_spans, binding, include_declaration=True)
        edits = [{"range": _span_to_range(doc.text, span), "newText": new_name} for span in spans]
        return {"changes": {doc.uri: edits}}

    def _completion(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = [{"label": kw, "kind": 14} for kw in KEYWORDS]
        doc, offset = self._doc_and_offset(params)
        if doc is None or offset is None:
            return items
        try:
            program = parse_program(doc.text)
        except PeregrineError:
            return items
        name_spans = _name_spans(doc.text)
        scope, _ = _locate_in_program(program, name_spans, offset)
        seen: set[str] = set()
        for frame in reversed(scope):
            for name, binding in frame.items():
                if name in seen:
                    continue
                seen.add(name)
                items.append({"label": name, "kind": 3 if isinstance(binding, ast.FnDecl) else 6})
        return items

    def _doc_and_offset(self, params: dict[str, Any]) -> tuple[_Document | None, int | None]:
        """Shared by every position-based request (`definition`/`references`/`rename`/`completion`)
        — resolves `params["textDocument"]["uri"]` to an open document and its `params["position"]`
        to a byte offset, or `(None, None)` if the document isn't open (matching `_hover`'s own
        "unopened document -> no result" handling)."""
        uri = params.get("textDocument", {}).get("uri")
        doc = self.documents.get(uri)
        if doc is None:
            return None, None
        position = params["position"]
        offset = _position_to_offset(doc.text, position["line"], position["character"])
        return doc, offset

    @staticmethod
    def _response(msg_id: Any, result: Any) -> JsonRpcMessage:
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    @staticmethod
    def _error(msg_id: Any, code: int, message: str) -> JsonRpcMessage:
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}

    @staticmethod
    def _notification(method: str, params: dict[str, Any]) -> JsonRpcMessage:
        return {"jsonrpc": "2.0", "method": method, "params": params}


# ---- stdio message framing ------------------------------------------------------------------------


def read_message(stream: BinaryIO) -> JsonRpcMessage | None:
    """Reads one `Content-Length: N\\r\\n\\r\\n<json>`-framed message (the LSP wire format — headers
    terminated by a blank line, then exactly `N` bytes of UTF-8-encoded JSON body). Returns `None`
    at EOF so `serve()` can loop on `while (msg := read_message(stream)) is not None`."""
    headers: dict[str, str] = {}
    while True:
        line = stream.readline()
        if not line:
            return None  # EOF before a complete header block — the client hung up
        decoded = line.decode("ascii").rstrip("\r\n")
        if decoded == "":
            break
        name, _, value = decoded.partition(":")
        headers[name.strip().lower()] = value.strip()
    length = int(headers.get("content-length", "0"))
    body = stream.read(length)
    if len(body) < length:
        return None
    result: JsonRpcMessage = json.loads(body.decode("utf-8"))
    return result


def write_message(stream: BinaryIO, message: JsonRpcMessage) -> None:
    body = json.dumps(message).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
    stream.write(header)
    stream.write(body)
    stream.flush()


def serve(input_stream: BinaryIO, output_stream: BinaryIO) -> None:
    """Thin IO wiring: read a framed message, hand it to `LspCore`, write back whatever it
    returns, repeat until EOF or an `exit` notification."""
    core = LspCore()
    while True:
        message = read_message(input_stream)
        if message is None:
            return
        if message.get("method") == "exit":
            return
        for response in core.handle(message):
            write_message(output_stream, response)


def main() -> None:
    serve(sys.stdin.buffer, sys.stdout.buffer)


if __name__ == "__main__":
    main()
