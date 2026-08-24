import pytest

from perelang.errors import LexError
from perelang.lexer import tokenize
from perelang.tokens import TokenKind


def kinds(src: str) -> list[TokenKind]:
    return [t.kind for t in tokenize(src)]


def test_empty_source_is_just_eof() -> None:
    assert kinds("") == [TokenKind.EOF]


def test_keywords_recognized() -> None:
    assert kinds("let fn if else true false return") == [
        TokenKind.LET,
        TokenKind.FN,
        TokenKind.IF,
        TokenKind.ELSE,
        TokenKind.TRUE,
        TokenKind.FALSE,
        TokenKind.RETURN,
        TokenKind.EOF,
    ]


def test_identifier_not_confused_with_keyword_prefix() -> None:
    tokens = tokenize("letter")
    assert tokens[0].kind == TokenKind.IDENT
    assert tokens[0].text == "letter"


def test_integer_literal() -> None:
    tokens = tokenize("42")
    assert tokens[0].kind == TokenKind.INT
    assert tokens[0].text == "42"


def test_float_literal() -> None:
    tokens = tokenize("3.14")
    assert tokens[0].kind == TokenKind.FLOAT
    assert tokens[0].text == "3.14"


def test_integer_followed_by_bare_dot_is_a_lex_error() -> None:
    # A float literal requires a digit after the dot ("5.0"); Peregrine has no "5." shorthand
    # (unlike Python) and no "." operator (no field access syntax), so once "5" is lexed, a lone
    # trailing "." matches nothing this language defines — it's correctly a LexError, not a
    # silently-accepted token.
    with pytest.raises(LexError):
        tokenize("5.")


def test_string_literal_basic() -> None:
    tokens = tokenize('"hello world"')
    assert tokens[0].kind == TokenKind.STRING
    assert tokens[0].text == "hello world"


def test_string_literal_escapes() -> None:
    tokens = tokenize(r'"a\nb\t\"c\"\\d"')
    assert tokens[0].text == 'a\nb\t"c"\\d'


def test_unterminated_string_raises() -> None:
    with pytest.raises(LexError):
        tokenize('"unterminated')


def test_unknown_character_raises() -> None:
    with pytest.raises(LexError):
        tokenize("let x = @;")


def test_two_char_operators_not_split() -> None:
    tokens = tokenize("a == b != c <= d >= e && f || g -> h")
    ops = [t.kind for t in tokens if t.kind not in (TokenKind.IDENT, TokenKind.EOF)]
    assert ops == [
        TokenKind.EQEQ,
        TokenKind.NEQ,
        TokenKind.LTE,
        TokenKind.GTE,
        TokenKind.AND,
        TokenKind.OR,
        TokenKind.ARROW,
    ]


def test_float_arithmetic_operators_recognized() -> None:
    tokens = tokenize("a +. b -. c *. d /. e")
    ops = [t.kind for t in tokens if t.kind not in (TokenKind.IDENT, TokenKind.EOF)]
    assert ops == [TokenKind.PLUSDOT, TokenKind.MINUSDOT, TokenKind.STARDOT, TokenKind.SLASHDOT]
    texts = [t.text for t in tokens if t.kind not in (TokenKind.IDENT, TokenKind.EOF)]
    assert texts == ["+.", "-.", "*.", "/."]


def test_float_arithmetic_operator_does_not_collide_with_a_float_literal() -> None:
    # "1.0 +. 2.0": `_next_token` only ever enters `_lex_number` when the *current* character is a
    # digit — since `+`/`-`/`*`/`/` never are, "+." tokenizes as one two-char PLUSDOT token via the
    # normal `_TWO_CHAR` lookahead, entirely independent of the float-literal path; verified here
    # end to end rather than just by reading the source.
    tokens = tokenize("1.0 +. 2.0")
    assert [t.kind for t in tokens] == [TokenKind.FLOAT, TokenKind.PLUSDOT, TokenKind.FLOAT, TokenKind.EOF]
    assert [t.text for t in tokens[:3]] == ["1.0", "+.", "2.0"]


def test_single_char_operators_after_two_char_lookahead_fails() -> None:
    # "=" alone must not be swallowed as half of "=="
    tokens = tokenize("a = b")
    assert tokens[1].kind == TokenKind.EQ


def test_line_comment_skipped_to_end_of_line() -> None:
    tokens = tokenize("let x = 1; // this is a comment\nlet y = 2;")
    texts = [t.text for t in tokens if t.kind == TokenKind.IDENT]
    assert texts == ["x", "y"]


def test_line_and_column_tracking_across_newlines() -> None:
    tokens = tokenize("let x =\n  5;")
    five = next(t for t in tokens if t.kind == TokenKind.INT)
    assert five.span.line == 2
    assert five.span.column == 3


def test_all_punctuation_recognized() -> None:
    src = "+ - * / % = ! ( ) { } [ ] , : ;"
    expected = [
        TokenKind.PLUS,
        TokenKind.MINUS,
        TokenKind.STAR,
        TokenKind.SLASH,
        TokenKind.PERCENT,
        TokenKind.EQ,
        TokenKind.BANG,
        TokenKind.LPAREN,
        TokenKind.RPAREN,
        TokenKind.LBRACE,
        TokenKind.RBRACE,
        TokenKind.LBRACKET,
        TokenKind.RBRACKET,
        TokenKind.COMMA,
        TokenKind.COLON,
        TokenKind.SEMI,
    ]
    assert kinds(src) == [*expected, TokenKind.EOF]


def test_span_covers_exact_token_extent() -> None:
    tokens = tokenize("  hello")
    tok = tokens[0]
    assert tok.span.start == 2
    assert tok.span.end == 7
