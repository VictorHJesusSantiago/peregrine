import pytest

from perelang import ast_nodes as ast
from perelang.errors import ParseError
from perelang.parser import parse_expr, parse_program


def test_let_decl_basic() -> None:
    prog = parse_program("let x = 5;")
    assert len(prog.decls) == 1
    decl = prog.decls[0]
    assert isinstance(decl, ast.LetDecl)
    assert decl.name == "x"
    assert decl.type_ann is None
    assert isinstance(decl.value, ast.IntLit)
    assert decl.value.value == 5


def test_let_decl_with_type_annotation() -> None:
    decl = parse_program("let x: Int = 5;").decls[0]
    assert isinstance(decl, ast.LetDecl)
    assert decl.type_ann is not None
    assert decl.type_ann.name == "Int"


def test_let_decl_with_generic_type_annotation() -> None:
    decl = parse_program("let xs: List<Int> = [1, 2];").decls[0]
    assert isinstance(decl, ast.LetDecl)
    assert decl.type_ann is not None
    assert decl.type_ann.name == "List"
    assert decl.type_ann.args[0].name == "Int"


def test_fn_decl_with_params_and_return_type() -> None:
    decl = parse_program("fn add(a, b) -> Int { a + b }").decls[0]
    assert isinstance(decl, ast.FnDecl)
    assert decl.name == "add"
    assert [p.name for p in decl.params] == ["a", "b"]
    assert decl.return_type_ann is not None
    assert decl.return_type_ann.name == "Int"


def test_fn_decl_no_params() -> None:
    decl = parse_program("fn main() { 1 }").decls[0]
    assert isinstance(decl, ast.FnDecl)
    assert decl.params == ()


def test_fn_decl_typed_params() -> None:
    decl = parse_program("fn add(a: Int, b: Int) -> Int { a + b }").decls[0]
    assert isinstance(decl, ast.FnDecl)
    assert decl.params[0].type_ann is not None
    assert decl.params[0].type_ann.name == "Int"


def test_block_tail_expression_no_semicolon() -> None:
    decl = parse_program("fn f() { 1; 2; 3 }").decls[0]
    assert isinstance(decl, ast.FnDecl)
    body = decl.body
    assert isinstance(body, ast.BlockExpr)
    assert len(body.stmts) == 2
    assert isinstance(body.tail, ast.IntLit)
    assert body.tail.value == 3


def test_block_with_no_tail_has_none() -> None:
    decl = parse_program("fn f() { 1; }").decls[0]
    assert isinstance(decl, ast.FnDecl)
    body = decl.body
    assert isinstance(body, ast.BlockExpr)
    assert body.tail is None
    assert len(body.stmts) == 1


def test_block_missing_semicolon_between_statements_is_an_error() -> None:
    with pytest.raises(ParseError):
        parse_program("fn f() { 1 2 }")


def test_let_inside_block() -> None:
    decl = parse_program("fn f() { let y = 1; y }").decls[0]
    assert isinstance(decl, ast.FnDecl)
    body = decl.body
    assert isinstance(body, ast.BlockExpr)
    stmt = body.stmts[0]
    assert isinstance(stmt, ast.LetDecl)
    assert stmt.name == "y"


class TestPrecedence:
    def test_multiplication_binds_tighter_than_addition(self) -> None:
        expr = parse_expr("1 + 2 * 3")
        assert isinstance(expr, ast.Binary)
        assert expr.op == "+"
        assert isinstance(expr.right, ast.Binary)
        assert expr.right.op == "*"

    def test_parentheses_override_precedence(self) -> None:
        expr = parse_expr("(1 + 2) * 3")
        assert isinstance(expr, ast.Binary)
        assert expr.op == "*"
        assert isinstance(expr.left, ast.Binary)
        assert expr.left.op == "+"

    def test_and_binds_tighter_than_or(self) -> None:
        expr = parse_expr("a || b && c")
        assert isinstance(expr, ast.Binary)
        assert expr.op == "||"
        assert isinstance(expr.right, ast.Binary)
        assert expr.right.op == "&&"

    def test_comparison_binds_tighter_than_equality(self) -> None:
        expr = parse_expr("a == b < c")
        assert isinstance(expr, ast.Binary)
        assert expr.op == "=="
        assert isinstance(expr.right, ast.Binary)
        assert expr.right.op == "<"

    def test_unary_minus_binds_tighter_than_binary_minus(self) -> None:
        expr = parse_expr("-a - b")
        assert isinstance(expr, ast.Binary)
        assert expr.op == "-"
        assert isinstance(expr.left, ast.Unary)

    def test_left_associativity_of_addition(self) -> None:
        expr = parse_expr("1 - 2 - 3")
        assert isinstance(expr, ast.Binary)
        assert expr.op == "-"
        assert isinstance(expr.left, ast.Binary)
        assert expr.left.op == "-"
        assert isinstance(expr.right, ast.IntLit)
        assert expr.right.value == 3

    def test_float_arithmetic_operators_parse_with_the_dotted_text(self) -> None:
        for src, op in (("1.0 +. 2.0", "+."), ("1.0 -. 2.0", "-."), ("1.0 *. 2.0", "*."), ("1.0 /. 2.0", "/.")):
            expr = parse_expr(src)
            assert isinstance(expr, ast.Binary)
            assert expr.op == op

    def test_float_multiplicative_binds_tighter_than_float_additive(self) -> None:
        expr = parse_expr("1.0 +. 2.0 *. 3.0")
        assert isinstance(expr, ast.Binary)
        assert expr.op == "+."
        assert isinstance(expr.right, ast.Binary)
        assert expr.right.op == "*."

    def test_unary_float_negation_parses(self) -> None:
        expr = parse_expr("-.1.5")
        assert isinstance(expr, ast.Unary)
        assert expr.op == "-."
        assert isinstance(expr.operand, ast.FloatLit)
        assert expr.operand.value == 1.5


def test_if_expression_requires_else() -> None:
    with pytest.raises(ParseError):
        parse_expr("if true { 1 }")


def test_if_expression_parses() -> None:
    expr = parse_expr("if a { 1 } else { 2 }")
    assert isinstance(expr, ast.If)
    assert expr.then_branch.tail is not None
    assert expr.else_branch.tail is not None


def test_call_expression() -> None:
    expr = parse_expr("f(1, 2, 3)")
    assert isinstance(expr, ast.Call)
    assert isinstance(expr.callee, ast.Ident)
    assert expr.callee.name == "f"
    assert len(expr.args) == 3


def test_curried_call_chain() -> None:
    expr = parse_expr("f(1)(2)")
    assert isinstance(expr, ast.Call)
    assert isinstance(expr.callee, ast.Call)


def test_lambda_expression() -> None:
    expr = parse_expr("|x, y| x + y")
    assert isinstance(expr, ast.Lambda)
    assert [p.name for p in expr.params] == ["x", "y"]
    assert isinstance(expr.body, ast.Binary)


def test_lambda_no_params() -> None:
    expr = parse_expr("|| 5")
    assert isinstance(expr, ast.Lambda)
    assert expr.params == ()


def test_list_literal() -> None:
    expr = parse_expr("[1, 2, 3]")
    assert isinstance(expr, ast.ListLit)
    assert len(expr.items) == 3


def test_empty_list_literal() -> None:
    expr = parse_expr("[]")
    assert isinstance(expr, ast.ListLit)
    assert expr.items == ()


def test_string_and_bool_literals() -> None:
    assert isinstance(parse_expr('"hi"'), ast.StringLit)
    assert isinstance(parse_expr("true"), ast.BoolLit)
    assert isinstance(parse_expr("false"), ast.BoolLit)


def test_nested_block_as_expression() -> None:
    expr = parse_expr("{ let x = 1; x + 1 }")
    assert isinstance(expr, ast.BlockExpr)


def test_unexpected_token_raises_parse_error_with_span() -> None:
    try:
        parse_expr("+")
        pytest.fail("expected ParseError")
    except ParseError as e:
        assert e.span.start == 0


def test_trailing_input_after_expression_is_an_error() -> None:
    with pytest.raises(ParseError):
        parse_expr("1 2")


def test_span_of_binary_expr_covers_both_operands() -> None:
    expr = parse_expr("aa + bb")
    assert isinstance(expr, ast.Binary)
    assert expr.span.start == 0
    assert expr.span.end == 7
