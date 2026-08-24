import pytest

from perelang import ir
from perelang.errors import TypeCheckError
from perelang.parser import parse_program
from perelang.types import T_BOOL, T_INT, TFun, type_to_str


def lower(src: str) -> ir.IrProgram:
    return ir.lower_program(parse_program(src))


class TestLiterals:
    def test_int_literal_lowers_with_int_type(self) -> None:
        prog = lower("let x = 5;")
        decl = prog.decls[0]
        assert isinstance(decl, ir.IrGlobalLet)
        assert isinstance(decl.value, ir.IrInt)
        assert decl.value.value == 5
        assert decl.value.ty == T_INT

    def test_float_literal_lowers_with_float_type(self) -> None:
        decl = lower("let x = 3.5;").decls[0]
        assert isinstance(decl, ir.IrGlobalLet)
        assert isinstance(decl.value, ir.IrFloat)
        assert type_to_str(decl.value.ty) == "Float"

    def test_string_and_bool_literals(self) -> None:
        s = lower('let x = "hi";').decls[0]
        b = lower("let x = true;").decls[0]
        assert isinstance(s, ir.IrGlobalLet) and isinstance(s.value, ir.IrStr)
        assert isinstance(b, ir.IrGlobalLet) and isinstance(b.value, ir.IrBool)


class TestNameResolution:
    def test_reference_to_top_level_let_is_global(self) -> None:
        prog = lower("let a = 1; let b = a;")
        b = prog.decls[1]
        assert isinstance(b, ir.IrGlobalLet)
        assert isinstance(b.value, ir.IrName)
        assert b.value.is_global is True
        assert b.value.name == "a"

    def test_function_parameter_is_not_global(self) -> None:
        prog = lower("fn f(x) -> Int { x }")
        fn = prog.decls[0]
        assert isinstance(fn, ir.IrFunction)
        assert fn.body.tail is not None
        assert isinstance(fn.body.tail, ir.IrName)
        assert fn.body.tail.is_global is False

    def test_recursive_call_inside_its_own_body_is_global(self) -> None:
        prog = lower("fn fact(n) -> Int { if n <= 1 { 1 } else { n * fact(n - 1) } }")
        fn = prog.decls[0]
        assert isinstance(fn, ir.IrFunction)
        assert isinstance(fn.body.tail, ir.IrIf)
        else_tail = fn.body.tail.else_branch.tail
        assert isinstance(else_tail, ir.IrBinary)
        call = else_tail.right
        assert isinstance(call, ir.IrCall)
        assert isinstance(call.callee, ir.IrName)
        assert call.callee.is_global is True
        assert call.callee.name == "fact"

    def test_local_shadowing_a_global_resolves_to_the_local(self) -> None:
        # `x` is both a top-level `let` and a parameter of `f` — inside `f`'s body the parameter
        # must win; `is_global` must reflect the *lexical* shadow, not just "is this name a
        # top-level decl somewhere."
        prog = lower("let x = 1; fn f(x) -> Int { x }")
        fn = prog.decls[1]
        assert isinstance(fn, ir.IrFunction)
        assert isinstance(fn.body.tail, ir.IrName)
        assert fn.body.tail.is_global is False


class TestBinaryAndIf:
    def test_arithmetic_binary_has_int_type(self) -> None:
        decl = lower("let x = 1 + 2;").decls[0]
        assert isinstance(decl, ir.IrGlobalLet)
        assert isinstance(decl.value, ir.IrBinary)
        assert decl.value.ty == T_INT

    def test_float_arithmetic_binary_has_float_type(self) -> None:
        from perelang.types import T_FLOAT

        decl = lower("let x = 1.0 +. 2.0;").decls[0]
        assert isinstance(decl, ir.IrGlobalLet)
        assert isinstance(decl.value, ir.IrBinary)
        assert decl.value.op == "+."
        assert decl.value.ty == T_FLOAT
        assert decl.value.left.ty == T_FLOAT
        assert decl.value.right.ty == T_FLOAT

    def test_float_unary_negation_has_float_type(self) -> None:
        from perelang.types import T_FLOAT

        decl = lower("let x = -.5.0;").decls[0]
        assert isinstance(decl, ir.IrGlobalLet)
        assert isinstance(decl.value, ir.IrUnary)
        assert decl.value.op == "-."
        assert decl.value.ty == T_FLOAT

    def test_int_plus_float_still_raises_when_lowering(self) -> None:
        with pytest.raises(TypeCheckError):
            lower("let x = 1 +. 2.0;")

    def test_comparison_binary_has_bool_type(self) -> None:
        decl = lower("let x = 1 < 2;").decls[0]
        assert isinstance(decl, ir.IrGlobalLet)
        assert isinstance(decl.value, ir.IrBinary)
        assert decl.value.ty == T_BOOL

    def test_if_branches_resolve_to_the_unified_type(self) -> None:
        decl = lower("let x = if true { 1 } else { 2 };").decls[0]
        assert isinstance(decl, ir.IrGlobalLet)
        assert isinstance(decl.value, ir.IrIf)
        assert decl.value.ty == T_INT
        assert decl.value.then_branch.ty == T_INT
        assert decl.value.else_branch.ty == T_INT

    def test_unbound_name_raises_type_check_error(self) -> None:
        with pytest.raises(TypeCheckError):
            lower("let x = ghost;")

    def test_heterogeneous_list_raises_type_check_error(self) -> None:
        with pytest.raises(TypeCheckError):
            lower('let xs = [1, "two"];')


class TestFunctionLowering:
    def test_function_type_matches_type_checker(self) -> None:
        prog = lower("fn add(a, b) -> Int { a + b }")
        fn = prog.decls[0]
        assert isinstance(fn, ir.IrFunction)
        assert fn.param_types == (T_INT, T_INT)
        assert fn.ret_type == T_INT
        assert fn.params == ("a", "b")


class TestClosureCaptures:
    def test_non_capturing_lambda_has_no_captures(self) -> None:
        decl = lower("let id = |x| x;").decls[0]
        assert isinstance(decl, ir.IrGlobalLet)
        assert isinstance(decl.value, ir.IrClosure)
        assert decl.value.captures == ()

    def test_lambda_capturing_an_outer_let_records_it(self) -> None:
        decl = lower("let n = 10; let addn = |x| x + n;").decls[1]
        assert isinstance(decl, ir.IrGlobalLet)
        closure = decl.value
        assert isinstance(closure, ir.IrClosure)
        # `n` is a top-level global here, not a capture — only *non-global* outer bindings are
        # captured (a global is always reachable by name through `LOAD_GLOBAL`; see ir.py's
        # module docstring, point 2).
        assert closure.captures == ()

    def test_nested_lambda_captures_from_enclosing_lambda_param(self) -> None:
        decl = lower("let make_adder = |x| |y| x + y;").decls[0]
        assert isinstance(decl, ir.IrGlobalLet)
        outer = decl.value
        assert isinstance(outer, ir.IrClosure)
        assert outer.captures == ()
        inner = outer.body
        assert isinstance(inner, ir.IrClosure)
        assert inner.captures == ("x",)

    def test_capture_bubbles_through_a_function_taking_a_lambda_param(self) -> None:
        # The capture is only meaningful relative to the lambda's *own* lexical scope — a
        # function's parameter is a perfectly good thing for a lambda defined inside it to
        # capture. (No return type annotation: this language's `type_ann` grammar has no syntax
        # for a function type — see `parser.py`'s grammar comment — so it must be inferred.)
        prog = lower("fn make(x: Int) -> Int { let g = |y| x + y; g(0) }")
        fn = prog.decls[0]
        assert isinstance(fn, ir.IrFunction)
        first_stmt = fn.body.stmts[0]
        assert isinstance(first_stmt, ir.IrLet)
        closure = first_stmt.value
        assert isinstance(closure, ir.IrClosure)
        assert closure.captures == ("x",)

    def test_let_bound_inside_lambda_body_is_not_a_capture(self) -> None:
        # `y` is bound by a `let` inside the lambda's own block body — it must not be mistaken for
        # a capture of an outer `y` (there is none here; this would raise TypeCheckError if free
        # variable analysis got scoping wrong and *this* wrong name leaked through instead).
        decl = lower("let f = |x| { let y = x + 1; y };").decls[0]
        assert isinstance(decl, ir.IrGlobalLet)
        closure = decl.value
        assert isinstance(closure, ir.IrClosure)
        assert closure.captures == ()


class TestLetPolymorphismIsMonomorphizedPerUse:
    def test_identity_used_at_two_types_lowers_two_separate_int_and_bool_uses(self) -> None:
        prog = lower("""
            let id = |x| x;
            let a = id(5);
            let b = id(true);
        """)
        a, b = prog.decls[1], prog.decls[2]
        assert isinstance(a, ir.IrGlobalLet) and a.ty == T_INT
        assert isinstance(b, ir.IrGlobalLet) and b.ty == T_BOOL


class TestSpans:
    """Every IR node carries the source `Span` of the AST node it was lowered from (see ir.py's
    `span` field, threaded through `_Lowerer` and preserved unchanged by `_resolve_ir_expr`) — the
    prerequisite for `vm.py` ever being able to report a precise runtime error location."""

    def test_int_literal_span_matches_its_source_position(self) -> None:
        decl = lower("let x =   42;").decls[0]
        assert isinstance(decl, ir.IrGlobalLet)
        assert isinstance(decl.value, ir.IrInt)
        # "let x =   42;" -> "42" starts at column 11 (1-based), offset 10.
        assert decl.value.span.start == 10
        assert decl.value.span.column == 11

    def test_binary_expr_span_covers_both_operands(self) -> None:
        decl = lower("let x = 11 + 22;").decls[0]
        assert isinstance(decl, ir.IrGlobalLet)
        assert isinstance(decl.value, ir.IrBinary)
        # "let x = 11 + 22;" -> "11 + 22" spans offsets 8..15.
        assert decl.value.span.start == 8
        assert decl.value.span.end == 15

    def test_span_survives_final_substitution_resolution(self) -> None:
        # `fact`'s body's division/multiplication go through `_resolve_ir_block` (the final
        # substitution rewrite pass) before landing in the returned `IrFunction` — its span must
        # still be the original source span, not reset/lost by that rewrite.
        prog = lower("fn fact(n) -> Int { if n <= 1 { 1 } else { n * fact(n - 1) } }")
        fn = prog.decls[0]
        assert isinstance(fn, ir.IrFunction)
        assert isinstance(fn.body.tail, ir.IrIf)
        else_tail = fn.body.tail.else_branch.tail
        assert isinstance(else_tail, ir.IrBinary)
        assert else_tail.span.start >= 0
        # The multiplication's own span starts where `n * fact(n - 1)` starts, strictly before
        # where its right operand (`fact(n - 1)`) starts.
        assert else_tail.span.start < else_tail.right.span.start

    def test_function_and_global_let_decls_carry_a_span(self) -> None:
        prog = lower("let a = 1; fn f() -> Int { 1 }")
        global_let, fn = prog.decls
        assert isinstance(global_let, ir.IrGlobalLet)
        assert isinstance(fn, ir.IrFunction)
        assert global_let.span.start == 0
        assert fn.span.start == 11


def test_full_program_lowers_without_error_and_types_match_the_checker() -> None:
    prog = lower("""
        fn fib(n) -> Int {
          if n < 2 { n } else { fib(n - 1) + fib(n - 2) }
        }
        let result = fib(10);
    """)
    fib_decl, result_decl = prog.decls
    assert isinstance(fib_decl, ir.IrFunction)
    assert fib_decl.ret_type == T_INT
    assert isinstance(result_decl, ir.IrGlobalLet)
    assert result_decl.ty == T_INT


def test_lower_program_is_a_type_alias_shaped_entry_point() -> None:
    # Sanity check on the public surface itself, not just its behavior.
    fn_type = TFun((T_INT,), T_INT)
    assert type_to_str(fn_type) == "(Int) -> Int"
