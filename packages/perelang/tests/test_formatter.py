import dataclasses
from pathlib import Path

from typer.testing import CliRunner

from perelang import ast_nodes as ast
from perelang.cli import app
from perelang.errors import Span
from perelang.formatter import format_source
from perelang.parser import parse_program

runner = CliRunner()

_EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def _strip_spans(node: object) -> object:
    """Structural equality helper shared by every re-parse-equivalence test below: two ASTs
    "mean the same program" iff they're equal after every `Span` is erased — spans necessarily
    differ between the original source and the reformatted source (different column positions,
    different line numbers for a reflowed block), but nothing else should."""
    if isinstance(node, Span):
        return None
    if isinstance(node, tuple):
        return tuple(_strip_spans(x) for x in node)
    if dataclasses.is_dataclass(node) and not isinstance(node, type):
        return (
            type(node).__name__,
            tuple(
                (f.name, _strip_spans(getattr(node, f.name)))
                for f in dataclasses.fields(node)
                if f.name != "span"
            ),
        )
    return node


def _assert_reparse_equivalent(source: str) -> None:
    original = parse_program(source)
    formatted = format_source(source)
    reparsed = parse_program(formatted)
    assert _strip_spans(original) == _strip_spans(reparsed)


class TestExamples:
    def test_factorial_example_is_already_canonical(self) -> None:
        src = (_EXAMPLES / "factorial.pgr").read_text()
        assert format_source(src) == src

    def test_fibonacci_example_is_already_canonical(self) -> None:
        src = (_EXAMPLES / "fibonacci.pgr").read_text()
        assert format_source(src) == src

    def test_closures_example_reparses_equivalently(self) -> None:
        # Not byte-identical: this example has a `//` comment, which the formatter can never
        # reproduce (see formatter.py's module docstring) — but the *code* it emits must still
        # mean the same thing.
        src = (_EXAMPLES / "closures.pgr").read_text()
        _assert_reparse_equivalent(src)


class TestIdempotency:
    def test_formatting_canonical_output_is_a_no_op(self) -> None:
        for name in ("factorial.pgr", "fibonacci.pgr", "closures.pgr"):
            src = (_EXAMPLES / name).read_text()
            once = format_source(src)
            twice = format_source(once)
            assert once == twice

    def test_idempotent_on_deeply_nested_expression(self) -> None:
        src = "fn f(a: Int, b: Int, c: Int) -> Int { a + b * c - (a - b) - c }"
        once = format_source(src)
        twice = format_source(once)
        assert once == twice

    def test_idempotent_on_multi_stmt_block(self) -> None:
        src = "fn f() -> Int { let a = 1; let b = 2; a + b }"
        once = format_source(src)
        twice = format_source(once)
        assert once == twice


class TestReparseEquivalence:
    def test_precedence_preserved_for_grouped_addition(self) -> None:
        _assert_reparse_equivalent("fn f() -> Int { (1 + 2) * 3 }")

    def test_associativity_preserved_for_repeated_subtraction(self) -> None:
        _assert_reparse_equivalent("fn f() -> Int { 1 - (2 - 3) }")

    def test_left_associative_subtraction_needs_no_parens_added(self) -> None:
        # `(1 - 2) - 3` and `1 - 2 - 3` are the *same* tree (left-assoc) — formatting must not
        # add spurious parens here even though it must add them for the mirror-image case above.
        _assert_reparse_equivalent("fn f() -> Int { (1 - 2) - 3 }")

    def test_lambda_as_call_callee_keeps_parens(self) -> None:
        _assert_reparse_equivalent("fn f() -> Int { (|x| x + 1)(5) }")

    def test_binary_as_call_callee_keeps_parens(self) -> None:
        # Constructed by hand (the parser itself can never produce this shape from source without
        # parens, since Binary/Unary aren't `primary` productions) to exercise the formatter's own
        # defensive callee-parenthesization independent of what the parser happens to emit.
        expr = ast.Call(
            callee=ast.Binary("+", ast.IntLit(1, _span()), ast.IntLit(2, _span()), _span()),
            args=(ast.IntLit(3, _span()),),
            span=_span(),
        )
        program = ast.Program((ast.FnDecl("f", (), None, _wrap_block(expr), _span()),))
        from perelang.formatter import format_program

        text = format_program(program)
        reparsed = parse_program(text)
        fn = reparsed.decls[0]
        assert isinstance(fn, ast.FnDecl)
        assert isinstance(fn.body, ast.BlockExpr)
        assert isinstance(fn.body.tail, ast.Call)
        assert isinstance(fn.body.tail.callee, ast.Binary)

    def test_nested_if_inside_call_args_still_reparses(self) -> None:
        _assert_reparse_equivalent("fn f(x: Bool) -> Int { g(if x { 1 } else { 2 }, 3) }")

    def test_string_escapes_round_trip(self) -> None:
        _assert_reparse_equivalent('fn f() -> String { "line1\\nline2\\ttabbed\\"quoted\\"" }')

    def test_float_literal_round_trips(self) -> None:
        _assert_reparse_equivalent("let x: Float = 3.14;")

    def test_list_literal_round_trips(self) -> None:
        _assert_reparse_equivalent("let xs: List<Int> = [1, 2, 3];")

    def test_unary_and_logical_operators_round_trip(self) -> None:
        _assert_reparse_equivalent("fn f(a: Bool, b: Bool) -> Bool { !a && b || !(a && b) }")

    def test_float_arithmetic_operators_round_trip(self) -> None:
        _assert_reparse_equivalent("fn f(a: Float, b: Float) -> Float { a +. b *. 2.0 -. b /. a }")

    def test_float_arithmetic_operators_format_with_the_same_precedence_as_int(self) -> None:
        assert format_source("let x: Float = 1.0 +. 2.0 *. 3.0;") == "let x: Float = 1.0 +. 2.0 *. 3.0;\n"

    def test_unary_float_negation_round_trips(self) -> None:
        _assert_reparse_equivalent("fn f(a: Float) -> Float { -.a +. 1.0 }")

    def test_circle_area_example_is_already_canonical(self) -> None:
        src = (_EXAMPLES / "circle_area.pgr").read_text()
        assert format_source(src) == src


def _span() -> Span:
    return Span(start=0, end=0, line=1, column=1)


def _wrap_block(tail: ast.Expr) -> ast.BlockExpr:
    return ast.BlockExpr((), tail, _span())


class TestCli:
    def test_fmt_rewrites_file_in_place(self, tmp_path: Path) -> None:
        src = tmp_path / "unformatted.pgr"
        src.write_text("fn   main( )  ->   Int   {    1   +   2   }")
        result = runner.invoke(app, ["fmt", str(src)])
        assert result.exit_code == 0
        assert src.read_text() == "fn main() -> Int { 1 + 2 }\n"

    def test_fmt_check_passes_on_canonical_file(self) -> None:
        result = runner.invoke(app, ["fmt", "--check", str(_EXAMPLES / "factorial.pgr")])
        assert result.exit_code == 0

    def test_fmt_check_fails_on_non_canonical_file_without_writing(self, tmp_path: Path) -> None:
        src = tmp_path / "messy.pgr"
        original = "fn   main( )  ->   Int   {    1   +   2   }"
        src.write_text(original)
        result = runner.invoke(app, ["fmt", "--check", str(src)])
        assert result.exit_code == 1
        assert src.read_text() == original  # --check must never write

    def test_fmt_reports_parse_error(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.pgr"
        bad.write_text("fn f( {")
        result = runner.invoke(app, ["fmt", str(bad)])
        assert result.exit_code == 1
        assert "error:" in result.stderr
