import pytest

from perelang.errors import TypeCheckError
from perelang.parser import parse_program
from perelang.types import T_BOOL, T_INT, TCon, TFun, TVar, infer_program, type_to_str


def infer(src: str) -> dict[str, object]:
    return infer_program(parse_program(src))  # type: ignore[return-value]


class TestLiterals:
    def test_int_literal(self) -> None:
        assert infer("let x = 5;")["x"] == T_INT

    def test_float_literal(self) -> None:
        result = infer("let x = 5.0;")["x"]
        assert type_to_str(result) == "Float"  # type: ignore[arg-type]

    def test_bool_literal(self) -> None:
        assert infer("let x = true;")["x"] == T_BOOL

    def test_string_literal(self) -> None:
        result = infer('let x = "hi";')["x"]
        assert type_to_str(result) == "String"  # type: ignore[arg-type]


class TestArithmeticAndComparison:
    def test_addition_is_int(self) -> None:
        assert infer("let x = 1 + 2;")["x"] == T_INT

    def test_mixed_int_bool_arithmetic_is_a_type_error(self) -> None:
        with pytest.raises(TypeCheckError):
            infer("let x = 1 + true;")

    def test_comparison_yields_bool(self) -> None:
        assert infer("let x = 1 < 2;")["x"] == T_BOOL

    def test_equality_requires_matching_types(self) -> None:
        with pytest.raises(TypeCheckError):
            infer('let x = 1 == "1";')

    def test_equality_works_for_matching_types(self) -> None:
        assert infer("let x = 1 == 1;")["x"] == T_BOOL

    def test_logical_and_or_require_bool_operands(self) -> None:
        assert infer("let x = true && false;")["x"] == T_BOOL
        with pytest.raises(TypeCheckError):
            infer("let x = 1 && true;")

    def test_unary_negation_requires_int(self) -> None:
        assert infer("let x = -5;")["x"] == T_INT
        with pytest.raises(TypeCheckError):
            infer("let x = -true;")

    def test_unary_not_requires_bool(self) -> None:
        assert infer("let x = !true;")["x"] == T_BOOL


class TestFloatArithmetic:
    """`+. -. *. /.` (see `docs/adr/0002-arithmetic-monomorphic-over-int.md`) mirror `_ARITHMETIC`'s
    tests one-for-one, but unify to `T_FLOAT`, and — critically — never implicitly coerce with
    `Int`: `1 +. 2.0` is a type error exactly like `1 + 2.0` already was."""

    def test_float_addition_is_float(self) -> None:
        result = infer("let x = 1.0 +. 2.0;")["x"]
        assert type_to_str(result) == "Float"  # type: ignore[arg-type]

    def test_all_four_float_operators_type_check(self) -> None:
        for op in ("+.", "-.", "*.", "/."):
            result = infer(f"let x = 1.0 {op} 2.0;")["x"]
            assert type_to_str(result) == "Float"  # type: ignore[arg-type]

    def test_mixing_int_and_float_with_a_float_operator_is_a_type_error(self) -> None:
        with pytest.raises(TypeCheckError):
            infer("let x = 1 +. 2.0;")

    def test_plain_plus_still_rejects_float_operands(self) -> None:
        # `+` itself was never made polymorphic — only a *second*, separate set of operators was
        # added (see the ADR); `1 + 2.0` is still exactly as much of an error as it always was.
        with pytest.raises(TypeCheckError):
            infer("let x = 1 + 2.0;")

    def test_float_operator_on_two_ints_is_a_type_error(self) -> None:
        with pytest.raises(TypeCheckError):
            infer("let x = 1 +. 2;")

    def test_unary_float_negation_requires_float(self) -> None:
        result = infer("let x = -.5.0;")["x"]
        assert type_to_str(result) == "Float"  # type: ignore[arg-type]
        with pytest.raises(TypeCheckError):
            infer("let x = -.5;")

    def test_float_arithmetic_in_a_function(self) -> None:
        result = infer("fn scale(x: Float, factor: Float) -> Float { x *. factor }")["scale"]
        assert isinstance(result, TFun)
        assert type_to_str(result.ret) == "Float"


class TestIfExpressions:
    def test_condition_must_be_bool(self) -> None:
        with pytest.raises(TypeCheckError):
            infer("let x = if 1 { 2 } else { 3 };")

    def test_branches_must_unify(self) -> None:
        with pytest.raises(TypeCheckError):
            infer('let x = if true { 1 } else { "s" };')

    def test_branches_unify_to_common_type(self) -> None:
        assert infer("let x = if true { 1 } else { 2 };")["x"] == T_INT

    def test_if_as_a_pure_side_effect_block_is_unit(self) -> None:
        result = infer("let x = if true { let y = 1; } else { let z = 2; };")["x"]
        assert type_to_str(result) == "Unit"  # type: ignore[arg-type]


class TestFunctionsAndCalls:
    def test_simple_function_type(self) -> None:
        result = infer("fn add(a, b) -> Int { a + b }")["add"]
        assert isinstance(result, TFun)
        assert result.params == (T_INT, T_INT)
        assert result.ret == T_INT

    def test_call_wrong_arg_type_is_an_error(self) -> None:
        with pytest.raises(TypeCheckError):
            infer('fn f(x) -> Int { x }  let y = f("s") + 1;')

    def test_calling_an_undeclared_function_is_unbound_name(self) -> None:
        with pytest.raises(TypeCheckError):
            infer("let x = ghost(1);")

    def test_return_type_annotation_is_checked_not_ignored(self) -> None:
        with pytest.raises(TypeCheckError):
            infer('fn f() -> Int { "not an int" }')

    def test_param_type_annotation_is_checked(self) -> None:
        with pytest.raises(TypeCheckError):
            infer("fn f(x: Int) -> Int { x }  let y = f(true);")


class TestRecursion:
    def test_recursive_factorial_type_checks(self) -> None:
        result = infer("""
            fn fact(n) -> Int {
              if n <= 1 { 1 } else { n * fact(n - 1) }
            }
        """)["fact"]
        assert isinstance(result, TFun)
        assert result.params == (T_INT,)
        assert result.ret == T_INT

    def test_recursion_is_monomorphic_within_its_own_body(self) -> None:
        # A recursive call must agree with the function's own signature — calling `f` with a
        # Bool inside its own body, when every other call site implies Int, is an error.
        with pytest.raises(TypeCheckError):
            infer("""
                fn f(n) -> Int {
                  if n <= 0 { 0 } else { f(true) }
                }
            """)


class TestLetPolymorphism:
    def test_identity_function_generalized_over_a_type_variable(self) -> None:
        result = infer("let id = |x| x;")["id"]
        assert isinstance(result, TFun)
        assert isinstance(result.params[0], TVar)
        assert result.ret == result.params[0]

    def test_identity_used_at_two_different_types_in_the_same_program(self) -> None:
        # The textbook let-polymorphism proof: one definition, two independently-typed uses.
        result = infer("""
            let id = |x| x;
            let a = id(5);
            let b = id(true);
        """)
        assert result["a"] == T_INT
        assert result["b"] == T_BOOL

    def test_lambda_parameters_are_not_polymorphic_within_their_own_body(self) -> None:
        # Unlike a `let`-bound name, a bare lambda parameter is monomorphic — using it at two
        # different types inside the same lambda body is a real type error.
        with pytest.raises(TypeCheckError):
            infer("let f = |x| if x { x } else { 1 };")


class TestLists:
    def test_empty_list_is_polymorphic_before_use(self) -> None:
        result = infer("let xs = [];")["xs"]
        assert isinstance(result, TCon)
        assert result.name == "List"
        assert isinstance(result.args[0], TVar)

    def test_homogeneous_list_infers_element_type(self) -> None:
        result = infer("let xs = [1, 2, 3];")["xs"]
        assert isinstance(result, TCon)
        assert result.name == "List"
        assert result.args[0] == T_INT

    def test_heterogeneous_list_is_a_type_error(self) -> None:
        with pytest.raises(TypeCheckError):
            infer('let xs = [1, "two", 3];')

    def test_list_type_annotation_is_checked(self) -> None:
        assert infer("let xs: List<Int> = [1, 2];")["xs"] is not None
        with pytest.raises(TypeCheckError):
            infer("let xs: List<Int> = [true];")


class TestBlocksAndScoping:
    def test_let_in_block_shadows_and_scopes_correctly(self) -> None:
        result = infer("""
            fn f() -> Int {
              let x = 1;
              let x = x + 1;
              x
            }
        """)["f"]
        assert isinstance(result, TFun)
        assert result.ret == T_INT

    def test_unbound_name_reports_a_span(self) -> None:
        try:
            infer("let x = totally_undefined;")
            pytest.fail("expected TypeCheckError")
        except TypeCheckError as e:
            assert e.span.start >= 0


class TestTypeToStr:
    def test_function_type_rendering(self) -> None:
        assert type_to_str(TFun((T_INT, T_INT), T_INT)) == "(Int, Int) -> Int"

    def test_generic_type_rendering(self) -> None:
        from perelang.types import t_list

        assert type_to_str(t_list(T_INT)) == "List<Int>"
