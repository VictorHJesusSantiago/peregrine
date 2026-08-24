"""Token kinds as a plain `enum.Enum`, and `Token` itself as a frozen dataclass carrying a `Span`
(see `errors.py`) — every downstream phase's error messages point at real source positions, not
"somewhere in your program."
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

from perelang.errors import Span


class TokenKind(enum.Enum):
    INT = "INT"
    FLOAT = "FLOAT"
    STRING = "STRING"
    IDENT = "IDENT"

    # Keywords
    LET = "let"
    FN = "fn"
    IF = "if"
    ELSE = "else"
    TRUE = "true"
    FALSE = "false"
    RETURN = "return"

    # Punctuation / operators
    PLUS = "+"
    MINUS = "-"
    STAR = "*"
    SLASH = "/"
    PERCENT = "%"
    PLUSDOT = "+."
    MINUSDOT = "-."
    STARDOT = "*."
    SLASHDOT = "/."
    EQ = "="
    EQEQ = "=="
    NEQ = "!="
    LT = "<"
    LTE = "<="
    GT = ">"
    GTE = ">="
    AND = "&&"
    OR = "||"
    BANG = "!"
    ARROW = "->"
    PIPE = "|"
    LPAREN = "("
    RPAREN = ")"
    LBRACE = "{"
    RBRACE = "}"
    LBRACKET = "["
    RBRACKET = "]"
    COMMA = ","
    COLON = ":"
    SEMI = ";"

    EOF = "EOF"


KEYWORDS: dict[str, TokenKind] = {
    "let": TokenKind.LET,
    "fn": TokenKind.FN,
    "if": TokenKind.IF,
    "else": TokenKind.ELSE,
    "true": TokenKind.TRUE,
    "false": TokenKind.FALSE,
    "return": TokenKind.RETURN,
}


@dataclass(frozen=True, slots=True)
class Token:
    kind: TokenKind
    text: str
    span: Span
