"""Recursive descent for declarations/statements, precedence climbing for expressions — same
house style as every other hand-written parser in this author's other projects, for the same
reason: the precedence table *is* the call chain (`_or` calls `_and` calls `_equality` calls
`_comparison` ...), readable as ordinary control flow with no generated code and no grammar DSL
between the grammar and what actually runs.

Grammar (informal)::

    program    := decl* EOF
    decl       := let_decl | fn_decl
    let_decl   := 'let' IDENT (':' type_ann)? '=' expr ';'
    fn_decl    := 'fn' IDENT '(' params ')' ('->' type_ann)? block_expr
    params     := (param (',' param)*)?
    param      := IDENT (':' type_ann)?
    type_ann   := IDENT ('<' type_ann (',' type_ann)* '>')?
    block_expr := '{' stmt* tail? '}'
    stmt       := let_decl | expr ';'
    expr       := or_expr
    or_expr    := and_expr ('||' and_expr)*
    and_expr   := equality ('&&' equality)*
    equality   := comparison (('==' | '!=') comparison)*
    comparison := additive (('<' | '<=' | '>' | '>=') additive)*
    additive   := multiplicative (('+' | '-' | '+.' | '-.') multiplicative)*
    multiplicative := unary (('*' | '/' | '%' | '*.' | '/.') unary)*
    unary      := ('-' | '-.' | '!') unary | call
    call       := primary ('(' args ')')*
    primary    := INT | FLOAT | STRING | 'true' | 'false' | IDENT
                | '(' expr ')' | '[' (expr (',' expr)*)? ']'
                | block_expr | if_expr | lambda_expr
    if_expr    := 'if' expr block_expr 'else' block_expr
    lambda_expr:= '|' params '|' expr
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from perelang import ast_nodes as ast
from perelang.errors import ParseError, Span
from perelang.lexer import tokenize
from perelang.tokens import Token, TokenKind


@runtime_checkable
class _Spanned(Protocol):
    # A `@property` here (rather than a plain `span: Span` attribute) is what tells mypy this
    # protocol only needs *read* access — every AST node's `span` is a `frozen=True` dataclass
    # field, so treating the protocol as requiring a settable attribute would reject all of them.
    @property
    def span(self) -> Span: ...

_COMPARISON_OPS = {TokenKind.LT, TokenKind.LTE, TokenKind.GT, TokenKind.GTE}
_ADDITIVE_OPS = {TokenKind.PLUS, TokenKind.MINUS, TokenKind.PLUSDOT, TokenKind.MINUSDOT}
_MULTIPLICATIVE_OPS = {TokenKind.STAR, TokenKind.SLASH, TokenKind.PERCENT, TokenKind.STARDOT, TokenKind.SLASHDOT}


class Parser:
    def __init__(self, tokens: list[Token]) -> None:
        self._tokens = tokens
        self._pos = 0

    # ---- token cursor helpers -----------------------------------------------------------

    def _peek(self, offset: int = 0) -> Token:
        i = min(self._pos + offset, len(self._tokens) - 1)
        return self._tokens[i]

    def _advance(self) -> Token:
        tok = self._peek()
        if tok.kind is not TokenKind.EOF:
            self._pos += 1
        return tok

    def _check(self, kind: TokenKind) -> bool:
        return self._peek().kind is kind

    def _expect(self, kind: TokenKind) -> Token:
        if not self._check(kind):
            tok = self._peek()
            raise ParseError(f"expected {kind.value!r}, got {tok.kind.value!r}", tok.span)
        return self._advance()

    def at_end(self) -> bool:
        return self._check(TokenKind.EOF)

    def current_token(self) -> Token:
        return self._peek()

    # ---- top level ------------------------------------------------------------------------

    def parse_program(self) -> ast.Program:
        decls: list[ast.Decl] = []
        while not self.at_end():
            decls.append(self.parse_decl())
        return ast.Program(tuple(decls))

    def parse_decl(self) -> ast.Decl:
        if self._check(TokenKind.LET):
            return self._parse_let_decl()
        if self._check(TokenKind.FN):
            return self._parse_fn_decl()
        tok = self._peek()
        raise ParseError(f"expected a declaration ('let' or 'fn'), got {tok.kind.value!r}", tok.span)

    def _parse_let_decl(self) -> ast.LetDecl:
        start = self._expect(TokenKind.LET)
        name = self._expect(TokenKind.IDENT).text
        type_ann = self._parse_optional_type_ann()
        self._expect(TokenKind.EQ)
        value = self.parse_expr()
        end = self._expect(TokenKind.SEMI)
        return ast.LetDecl(name, type_ann, value, _span(start, end))

    def _parse_fn_decl(self) -> ast.FnDecl:
        start = self._expect(TokenKind.FN)
        name = self._expect(TokenKind.IDENT).text
        self._expect(TokenKind.LPAREN)
        params = self._parse_params()
        self._expect(TokenKind.RPAREN)
        return_type_ann = None
        if self._check(TokenKind.ARROW):
            self._advance()
            return_type_ann = self._parse_type_ann()
        body = self._parse_block_expr()
        return ast.FnDecl(name, params, return_type_ann, body, _span(start, body))

    def _parse_params(self) -> tuple[ast.Param, ...]:
        params: list[ast.Param] = []
        if self._check(TokenKind.RPAREN):
            return ()
        while True:
            name_tok = self._expect(TokenKind.IDENT)
            type_ann = self._parse_optional_type_ann()
            params.append(ast.Param(name_tok.text, type_ann, name_tok.span))
            if self._check(TokenKind.COMMA):
                self._advance()
                continue
            break
        return tuple(params)

    def _parse_optional_type_ann(self) -> ast.TypeAnn | None:
        if not self._check(TokenKind.COLON):
            return None
        self._advance()
        return self._parse_type_ann()

    def _parse_type_ann(self) -> ast.TypeAnn:
        name_tok = self._expect(TokenKind.IDENT)
        args: list[ast.TypeAnn] = []
        end = name_tok
        if self._check(TokenKind.LT):
            self._advance()
            while True:
                args.append(self._parse_type_ann())
                if self._check(TokenKind.COMMA):
                    self._advance()
                    continue
                break
            end = self._expect(TokenKind.GT)
        return ast.TypeAnn(name_tok.text, tuple(args), _span(name_tok, end))

    # ---- blocks and statements -------------------------------------------------------------

    def _parse_block_expr(self) -> ast.BlockExpr:
        start = self._expect(TokenKind.LBRACE)
        stmts: list[ast.Stmt] = []
        tail: ast.Expr | None = None
        while not self._check(TokenKind.RBRACE):
            if self._check(TokenKind.LET):
                stmts.append(self._parse_let_decl())
                continue
            expr = self.parse_expr()
            if self._check(TokenKind.SEMI):
                semi = self._advance()
                stmts.append(ast.ExprStmt(expr, _span(expr, semi)))
                continue
            # No semicolon: this expression must be the block's tail, so the very next token has
            # to close the block — anything else is a missing `;` between two statements.
            if not self._check(TokenKind.RBRACE):
                tok = self._peek()
                raise ParseError("expected ';' after expression statement", tok.span)
            tail = expr
            break
        end = self._expect(TokenKind.RBRACE)
        return ast.BlockExpr(tuple(stmts), tail, _span(start, end))

    # ---- expressions: precedence climbing ---------------------------------------------------

    def parse_expr(self) -> ast.Expr:
        return self._parse_or()

    def _parse_or(self) -> ast.Expr:
        left = self._parse_and()
        while self._check(TokenKind.OR):
            self._advance()
            right = self._parse_and()
            left = ast.Binary("||", left, right, _span(left, right))
        return left

    def _parse_and(self) -> ast.Expr:
        left = self._parse_equality()
        while self._check(TokenKind.AND):
            self._advance()
            right = self._parse_equality()
            left = ast.Binary("&&", left, right, _span(left, right))
        return left

    def _parse_equality(self) -> ast.Expr:
        left = self._parse_comparison()
        while self._check(TokenKind.EQEQ) or self._check(TokenKind.NEQ):
            op_tok = self._advance()
            right = self._parse_comparison()
            left = ast.Binary(op_tok.text, left, right, _span(left, right))
        return left

    def _parse_comparison(self) -> ast.Expr:
        left = self._parse_additive()
        while self._peek().kind in _COMPARISON_OPS:
            op_tok = self._advance()
            right = self._parse_additive()
            left = ast.Binary(op_tok.text, left, right, _span(left, right))
        return left

    def _parse_additive(self) -> ast.Expr:
        left = self._parse_multiplicative()
        while self._peek().kind in _ADDITIVE_OPS:
            op_tok = self._advance()
            right = self._parse_multiplicative()
            left = ast.Binary(op_tok.text, left, right, _span(left, right))
        return left

    def _parse_multiplicative(self) -> ast.Expr:
        left = self._parse_unary()
        while self._peek().kind in _MULTIPLICATIVE_OPS:
            op_tok = self._advance()
            right = self._parse_unary()
            left = ast.Binary(op_tok.text, left, right, _span(left, right))
        return left

    def _parse_unary(self) -> ast.Expr:
        if self._check(TokenKind.MINUS) or self._check(TokenKind.MINUSDOT) or self._check(TokenKind.BANG):
            op_tok = self._advance()
            operand = self._parse_unary()
            return ast.Unary(op_tok.text, operand, _span(op_tok, operand))
        return self._parse_call()

    def _parse_call(self) -> ast.Expr:
        expr = self._parse_primary()
        while self._check(TokenKind.LPAREN):
            self._advance()
            args = self._parse_args()
            end = self._expect(TokenKind.RPAREN)
            expr = ast.Call(expr, args, _span(expr, end))
        return expr

    def _parse_args(self) -> tuple[ast.Expr, ...]:
        args: list[ast.Expr] = []
        if self._check(TokenKind.RPAREN):
            return ()
        while True:
            args.append(self.parse_expr())
            if self._check(TokenKind.COMMA):
                self._advance()
                continue
            break
        return tuple(args)

    def _parse_primary(self) -> ast.Expr:
        tok = self._peek()

        if tok.kind is TokenKind.INT:
            self._advance()
            return ast.IntLit(int(tok.text), tok.span)
        if tok.kind is TokenKind.FLOAT:
            self._advance()
            return ast.FloatLit(float(tok.text), tok.span)
        if tok.kind is TokenKind.STRING:
            self._advance()
            return ast.StringLit(tok.text, tok.span)
        if tok.kind is TokenKind.TRUE:
            self._advance()
            return ast.BoolLit(True, tok.span)
        if tok.kind is TokenKind.FALSE:
            self._advance()
            return ast.BoolLit(False, tok.span)
        if tok.kind is TokenKind.IDENT:
            self._advance()
            return ast.Ident(tok.text, tok.span)
        if tok.kind is TokenKind.LPAREN:
            self._advance()
            inner = self.parse_expr()
            self._expect(TokenKind.RPAREN)
            return inner
        if tok.kind is TokenKind.LBRACKET:
            return self._parse_list_lit()
        if tok.kind is TokenKind.LBRACE:
            return self._parse_block_expr()
        if tok.kind is TokenKind.IF:
            return self._parse_if_expr()
        if tok.kind is TokenKind.PIPE:
            return self._parse_lambda_expr()
        if tok.kind is TokenKind.OR:
            # `||` in a position where an *expression* is expected can only be a zero-param
            # lambda's fused opening+closing pipes (a binary `||` can never start an expression —
            # its left operand would have to precede it) — see `_parse_lambda_expr`'s own doc for
            # why the lexer can't tell these apart itself.
            return self._parse_lambda_expr()

        raise ParseError(f"unexpected token {tok.kind.value!r}", tok.span)

    def _parse_list_lit(self) -> ast.ListLit:
        start = self._expect(TokenKind.LBRACKET)
        items: list[ast.Expr] = []
        if not self._check(TokenKind.RBRACKET):
            while True:
                items.append(self.parse_expr())
                if self._check(TokenKind.COMMA):
                    self._advance()
                    continue
                break
        end = self._expect(TokenKind.RBRACKET)
        return ast.ListLit(tuple(items), _span(start, end))

    def _parse_if_expr(self) -> ast.If:
        start = self._expect(TokenKind.IF)
        cond = self.parse_expr()
        then_branch = self._parse_block_expr()
        self._expect(TokenKind.ELSE)
        else_branch = self._parse_block_expr()
        return ast.If(cond, then_branch, else_branch, _span(start, else_branch))

    def _parse_lambda_expr(self) -> ast.Lambda:
        # A zero-param lambda's `||` and the logical-or operator are lexically identical — the
        # lexer's two-char-operator lookahead (see `lexer.py`) always merges adjacent `|` `|`
        # into one `OR` token, greedily and correctly, with no idea a lambda's parameter list
        # might be what's actually starting here. So the *parser*, not the lexer, is where this
        # ambiguity gets resolved: entering this method on an `OR` token (rather than the usual
        # single opening `PIPE`) means that token *is* the fused opening-and-closing pipe pair
        # around an empty parameter list — consumed once, standing for both at once.
        if self._check(TokenKind.OR):
            start = self._advance()
            body = self.parse_expr()
            return ast.Lambda((), body, _span(start, body))
        start = self._expect(TokenKind.PIPE)
        params = self._parse_lambda_params()
        self._expect(TokenKind.PIPE)
        body = self.parse_expr()
        return ast.Lambda(params, body, _span(start, body))

    def _parse_lambda_params(self) -> tuple[ast.Param, ...]:
        params: list[ast.Param] = []
        if self._check(TokenKind.PIPE):
            return ()
        while True:
            name_tok = self._expect(TokenKind.IDENT)
            type_ann = self._parse_optional_type_ann()
            params.append(ast.Param(name_tok.text, type_ann, name_tok.span))
            if self._check(TokenKind.COMMA):
                self._advance()
                continue
            break
        return tuple(params)


def _span(start: _Spanned, end: _Spanned) -> Span:
    """Merges two nodes'/tokens' spans into one covering both — takes anything with a `.span`
    attribute (a `Token` or any AST node), since callers pass a mix of both."""
    return Span(start=start.span.start, end=end.span.end, line=start.span.line, column=start.span.column)


def parse_program(source: str) -> ast.Program:
    return Parser(tokenize(source)).parse_program()


def parse_expr(source: str) -> ast.Expr:
    parser = Parser(tokenize(source))
    expr = parser.parse_expr()
    if not parser.at_end():
        tok = parser.current_token()
        raise ParseError(f"unexpected trailing input: {tok.kind.value!r}", tok.span)
    return expr
