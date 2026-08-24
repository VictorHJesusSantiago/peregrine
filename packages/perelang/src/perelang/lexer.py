"""Hand-written, single-pass tokenizer. Tracks line/column incrementally (rather than
recomputing from an absolute offset on demand) because every token gets one, not just the ones
that end up in an error — recomputing per-token from scratch would be the same total work done
lazily for no benefit, since nothing here is ever *not* going to need its position.
"""

from __future__ import annotations

from perelang.errors import LexError, Span
from perelang.tokens import KEYWORDS, Token, TokenKind

_TWO_CHAR: dict[str, TokenKind] = {
    "==": TokenKind.EQEQ,
    "!=": TokenKind.NEQ,
    "<=": TokenKind.LTE,
    ">=": TokenKind.GTE,
    "&&": TokenKind.AND,
    "||": TokenKind.OR,
    "->": TokenKind.ARROW,
    "+.": TokenKind.PLUSDOT,
    "-.": TokenKind.MINUSDOT,
    "*.": TokenKind.STARDOT,
    "/.": TokenKind.SLASHDOT,
}

_ONE_CHAR: dict[str, TokenKind] = {
    "+": TokenKind.PLUS,
    "-": TokenKind.MINUS,
    "*": TokenKind.STAR,
    "/": TokenKind.SLASH,
    "%": TokenKind.PERCENT,
    "=": TokenKind.EQ,
    "<": TokenKind.LT,
    ">": TokenKind.GT,
    "!": TokenKind.BANG,
    "|": TokenKind.PIPE,
    "(": TokenKind.LPAREN,
    ")": TokenKind.RPAREN,
    "{": TokenKind.LBRACE,
    "}": TokenKind.RBRACE,
    "[": TokenKind.LBRACKET,
    "]": TokenKind.RBRACKET,
    ",": TokenKind.COMMA,
    ":": TokenKind.COLON,
    ";": TokenKind.SEMI,
}


def _is_ident_start(c: str) -> bool:
    return c.isalpha() or c == "_"


def _is_ident_part(c: str) -> bool:
    return c.isalnum() or c == "_"


class Lexer:
    def __init__(self, source: str) -> None:
        self._src = source
        self._pos = 0
        self._line = 1
        self._col = 1

    def _peek(self, offset: int = 0) -> str:
        i = self._pos + offset
        return self._src[i] if i < len(self._src) else "\0"

    def _advance(self) -> str:
        c = self._peek()
        self._pos += 1
        if c == "\n":
            self._line += 1
            self._col = 1
        else:
            self._col += 1
        return c

    def _span(self, start: int, start_line: int, start_col: int) -> Span:
        return Span(start=start, end=self._pos, line=start_line, column=start_col)

    def tokenize(self) -> list[Token]:
        tokens: list[Token] = []
        while True:
            self._skip_trivia()
            if self._pos >= len(self._src):
                tokens.append(Token(TokenKind.EOF, "", self._span(self._pos, self._line, self._col)))
                return tokens
            tokens.append(self._next_token())

    def _skip_trivia(self) -> None:
        while self._pos < len(self._src):
            c = self._peek()
            if c in " \t\r\n":
                self._advance()
            elif c == "/" and self._peek(1) == "/":
                while self._pos < len(self._src) and self._peek() != "\n":
                    self._advance()
            else:
                return

    def _next_token(self) -> Token:
        start, start_line, start_col = self._pos, self._line, self._col
        c = self._peek()

        if c.isdigit():
            return self._lex_number(start, start_line, start_col)
        if c == '"':
            return self._lex_string(start, start_line, start_col)
        if _is_ident_start(c):
            return self._lex_ident(start, start_line, start_col)

        two = c + self._peek(1)
        if two in _TWO_CHAR:
            self._advance()
            self._advance()
            return Token(_TWO_CHAR[two], two, self._span(start, start_line, start_col))

        if c in _ONE_CHAR:
            self._advance()
            return Token(_ONE_CHAR[c], c, self._span(start, start_line, start_col))

        raise LexError(f'unexpected character "{c}"', self._span(start, start_line, start_col))

    def _lex_number(self, start: int, start_line: int, start_col: int) -> Token:
        while self._peek().isdigit():
            self._advance()
        is_float = False
        if self._peek() == "." and self._peek(1).isdigit():
            is_float = True
            self._advance()
            while self._peek().isdigit():
                self._advance()
        text = self._src[start : self._pos]
        kind = TokenKind.FLOAT if is_float else TokenKind.INT
        return Token(kind, text, self._span(start, start_line, start_col))

    def _lex_string(self, start: int, start_line: int, start_col: int) -> Token:
        self._advance()  # opening quote
        chars: list[str] = []
        while self._peek() != '"':
            if self._pos >= len(self._src):
                raise LexError("unterminated string literal", self._span(start, start_line, start_col))
            if self._peek() == "\\":
                self._advance()
                escaped = self._advance()
                chars.append({"n": "\n", "t": "\t", '"': '"', "\\": "\\"}.get(escaped, escaped))
            else:
                chars.append(self._advance())
        self._advance()  # closing quote
        return Token(TokenKind.STRING, "".join(chars), self._span(start, start_line, start_col))

    def _lex_ident(self, start: int, start_line: int, start_col: int) -> Token:
        while _is_ident_part(self._peek()):
            self._advance()
        text = self._src[start : self._pos]
        kind = KEYWORDS.get(text, TokenKind.IDENT)
        return Token(kind, text, self._span(start, start_line, start_col))


def tokenize(source: str) -> list[Token]:
    return Lexer(source).tokenize()
