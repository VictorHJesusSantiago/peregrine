import pytest

from perelang.bytecode import FunctionProto, Instr, OpCode
from perelang.errors import PeregrineRuntimeError, Span, TypeCheckError
from perelang.vm import UNIT, VM, Closure, run_source, value_to_str


class TestEndToEndRecursion:
    def test_factorial_of_ten(self) -> None:
        result = run_source("""
            fn fact(n) -> Int {
              if n <= 1 { 1 } else { n * fact(n - 1) }
            }
            fn main() -> Int { fact(10) }
        """)
        assert result == 3628800

    def test_fibonacci_of_fifteen(self) -> None:
        result = run_source("""
            fn fib(n) -> Int {
              if n < 2 { n } else { fib(n - 1) + fib(n - 2) }
            }
            fn main() -> Int { fib(15) }
        """)
        assert result == 610

    def test_mutual_style_mono_recursion_deep_enough_to_prove_real_recursion(self) -> None:
        # fact(12) forces 12 nested Python-level calls to `VM.call_closure` — enough to prove this
        # isn't accidentally memoized or unrolled, without approaching Python's recursion limit.
        result = run_source("""
            fn fact(n) -> Int { if n <= 1 { 1 } else { n * fact(n - 1) } }
            fn main() -> Int { fact(12) }
        """)
        assert result == 479001600


class TestClosures:
    def test_adder_closure_captures_outer_binding(self) -> None:
        result = run_source("""
            let make_adder = |x| |y| x + y;
            let add5 = make_adder(5);
            let result = add5(10);
        """)
        assert result == 15

    def test_two_closures_from_the_same_maker_capture_independently(self) -> None:
        result = run_source("""
            let make_adder = |x| |y| x + y;
            let add5 = make_adder(5);
            let add100 = make_adder(100);
            let result = add5(1) + add100(1);
        """)
        assert result == 107

    def test_closure_over_a_function_parameter(self) -> None:
        result = run_source("""
            fn make_multiplier(factor: Int) -> Int {
              let mul = |x| x * factor;
              mul(6)
            }
            fn main() -> Int { make_multiplier(7) }
        """)
        assert result == 42

    def test_plain_top_level_function_is_a_first_class_value(self) -> None:
        # A bare `fn` name used as a value is the zero-capture case of the same `Closure` runtime
        # representation a lambda uses (see vm.py's module docstring) — no special-casing needed.
        result = run_source("""
            fn add(a, b) -> Int { a + b }
            let g = add;
            let result = g(2, 3);
        """)
        assert result == 5


class TestArithmeticAndComparison:
    def test_basic_arithmetic(self) -> None:
        assert run_source("let x = 2 + 3 * 4;") == 14
        assert run_source("let x = (2 + 3) * 4;") == 20
        assert run_source("let x = 7 % 3;") == 1
        assert run_source("let x = -5;") == -5

    def test_integer_division_truncates(self) -> None:
        assert run_source("let x = 7 / 2;") == 3

    def test_division_by_zero_raises_peregrine_runtime_error(self) -> None:
        with pytest.raises(PeregrineRuntimeError):
            run_source("let x = 1 / 0;")

    def test_modulo_by_zero_raises_peregrine_runtime_error(self) -> None:
        with pytest.raises(PeregrineRuntimeError):
            run_source("let x = 1 % 0;")

    def test_comparisons(self) -> None:
        assert run_source("let x = 1 < 2;") is True
        assert run_source("let x = 2 <= 2;") is True
        assert run_source('let x = "a" == "a";') is True
        assert run_source("let x = 1 == 2;") is False

    def test_logical_operators(self) -> None:
        assert run_source("let x = true && false;") is False
        assert run_source("let x = true || false;") is True
        assert run_source("let x = !true;") is False

    def test_short_circuit_and_skips_a_division_by_zero_on_the_right(self) -> None:
        # `boom()` is never called: if `&&` evaluated its right operand eagerly this would raise.
        result = run_source("""
            fn boom() -> Bool { 1 / 0 == 0 }
            let x = false && boom();
        """)
        assert result is False

    def test_short_circuit_or_skips_a_division_by_zero_on_the_right(self) -> None:
        result = run_source("""
            fn boom() -> Bool { 1 / 0 == 0 }
            let x = true || boom();
        """)
        assert result is True


class TestFloatArithmetic:
    """`+. -. *. /.` end-to-end, over real `run_source` programs (not the hand-built `FunctionProto`
    opcode-level tests below, which predate `types.py` actually being able to type-check Float
    arithmetic at all — see `TestOpcodeLevelBehaviorBeyondWhatSourceCanReach`'s updated docstring)."""

    def test_float_addition_subtraction_multiplication_division(self) -> None:
        assert run_source("let x = 1.5 +. 2.5;") == 4.0
        assert run_source("let x = 5.5 -. 2.0;") == 3.5
        assert run_source("let x = 2.5 *. 4.0;") == 10.0
        assert run_source("let x = 7.0 /. 2.0;") == 3.5

    def test_float_division_is_true_division_not_truncating(self) -> None:
        # Unlike Int `/` (which truncates), Float `/.` always does real division — 5.0 /. 2.0 must
        # be 2.5, not 2.0.
        assert run_source("let x = 5.0 /. 2.0;") == 2.5

    def test_float_division_by_zero_raises_peregrine_runtime_error(self) -> None:
        with pytest.raises(PeregrineRuntimeError):
            run_source("let x = 1.0 /. 0.0;")

    def test_float_unary_negation(self) -> None:
        assert run_source("let x = -.5.0;") == -5.0

    def test_float_arithmetic_in_a_recursive_function(self) -> None:
        result = run_source("""
            fn sum_to(n: Int, acc: Float) -> Float {
              if n <= 0 { acc } else { sum_to(n - 1, acc +. 2.5) }
            }
            fn main() -> Float { sum_to(4, 0.0) }
        """)
        assert result == 10.0

    def test_mixing_int_and_float_with_a_float_operator_is_a_type_error_before_the_vm_ever_runs(self) -> None:
        with pytest.raises(TypeCheckError):
            run_source("let x = 1 +. 2.0;")

    def test_circle_area_example_runs_correctly(self) -> None:
        # Mirrors examples/circle_area.pgr — pi * r^2 for r=2.0 and r=3.0, summed.
        result = run_source("""
            fn circle_area(radius: Float) -> Float {
              let pi = 3.14159;
              pi *. radius *. radius
            }
            fn main() -> Float { circle_area(2.0) +. circle_area(3.0) }
        """)
        assert result == pytest.approx(3.14159 * 4.0 + 3.14159 * 9.0)


class TestRuntimeErrorSpansPointAtTheFaultingOperation:
    """The concrete, checkable proof that spans are threaded end-to-end through `ir.py` ->
    `bytecode.py` -> `vm.py`: a `PeregrineRuntimeError` raised from deep inside `call_closure`'s
    execution loop carries the *real* source location of the operation that faulted, not
    `vm._RUNTIME_SPAN`'s placeholder `(0, 0)`."""

    def test_division_by_zero_span_points_at_the_division_expression(self) -> None:
        # Line 2, "let x = 1 / 0;" -> "1 / 0" starts at column 9 on that line.
        source = "let a = 1;\nlet x = 1 / 0;\n"
        with pytest.raises(PeregrineRuntimeError) as exc_info:
            run_source(source)
        span = exc_info.value.span
        assert span.line == 2
        assert span.column == 9
        assert span != Span(start=0, end=0, line=0, column=0)

    def test_division_by_zero_deep_inside_a_function_still_points_at_the_real_division(self) -> None:
        source = """
            fn boom(n: Int) -> Int { 10 / n }
            fn main() -> Int { boom(0) }
        """
        with pytest.raises(PeregrineRuntimeError) as exc_info:
            run_source(source)
        span = exc_info.value.span
        # "10 / n" is on line 2; the DIV instruction's span must point there, not at the call site
        # on line 3 and not at the module's line-0 placeholder.
        assert span.line == 2
        assert span != Span(start=0, end=0, line=0, column=0)

    def test_wrong_arity_call_span_points_at_the_call_expression(self) -> None:
        # A genuine wrong-arity call cannot arise from well-typed source at all — `types.py` always
        # rejects it first (exactly as it does for the hand-built-only "call a non-function" case
        # in `TestOpcodeLevelBehaviorBeyondWhatSourceCanReach` below), so this proves the same thing
        # `bytecode.py`/`vm.py` actually promise at the opcode level: a `CALL` instruction's own
        # span (not the callee's, and not the placeholder) is what a runtime arity error reports.
        call_span = Span(start=20, end=32, line=4, column=15)
        callee_proto = FunctionProto(
            name="callee", arity=0, num_locals=0, code=(Instr(OpCode.PUSH_UNIT),), constants=(), capture_names=()
        )
        caller_proto = FunctionProto(
            name="caller",
            arity=0,
            num_locals=0,
            code=(
                Instr(OpCode.MAKE_CLOSURE, 0),
                Instr(OpCode.CONST, 1),
                Instr(OpCode.CONST, 2),
                Instr(OpCode.CONST, 3),
                Instr(OpCode.CALL, 3, call_span),
            ),
            constants=(callee_proto, 10, 20, 30),
            capture_names=(),
        )
        with pytest.raises(PeregrineRuntimeError) as exc_info:
            VM().call_closure(Closure(caller_proto, ()), ())
        assert exc_info.value.span == call_span

    def test_unbound_global_span_points_at_the_reference(self) -> None:
        proto = FunctionProto(
            name="unbound",
            arity=0,
            num_locals=0,
            code=(Instr(OpCode.LOAD_GLOBAL, 0, Span(start=5, end=10, line=3, column=7)),),
            constants=("ghost",),
            capture_names=(),
        )
        try:
            VM().call_closure(Closure(proto, ()), ())
            pytest.fail("expected PeregrineRuntimeError")
        except PeregrineRuntimeError as e:
            assert e.span == Span(start=5, end=10, line=3, column=7)

    def test_top_level_arity_check_with_no_caller_falls_back_to_the_placeholder(self) -> None:
        # `run_program`'s own initial call to `main` has no calling instruction to attribute an
        # arity mismatch to — this is the one documented exception (see vm.py's module docstring)
        # that still uses the placeholder span.
        proto = FunctionProto(name="needs_one_arg", arity=1, num_locals=1, code=(), constants=(), capture_names=())
        with pytest.raises(PeregrineRuntimeError) as exc_info:
            VM().call_closure(Closure(proto, ()), ())
        assert exc_info.value.span == Span(start=0, end=0, line=0, column=0)


class TestIfExpressions:
    def test_if_selects_the_taken_branch_value(self) -> None:
        assert run_source("let x = if 3 > 2 { 100 } else { 200 };") == 100
        assert run_source("let x = if 3 < 2 { 100 } else { 200 };") == 200

    def test_if_with_unit_branches(self) -> None:
        assert run_source("let x = if true { let y = 1; } else { let z = 2; };") is UNIT


class TestLists:
    def test_list_literal_evaluates_to_a_tuple_of_values(self) -> None:
        assert run_source("let xs = [1, 2, 3];") == (1, 2, 3)

    def test_empty_list(self) -> None:
        assert run_source("let xs = [];") == ()

    def test_list_equality_is_structural(self) -> None:
        assert run_source("let x = [1, 2, 3] == [1, 2, 3];") is True
        assert run_source("let x = [1, 2, 3] == [1, 2, 4];") is False

    def test_lists_pass_through_function_calls(self) -> None:
        result = run_source("""
            fn first_plus_len(xs: List<Int>) -> Int { 0 }
            fn identity(xs: List<Int>) -> List<Int> { xs }
            fn main() -> List<Int> { identity([1, 2, 3]) }
        """)
        assert result == (1, 2, 3)


class TestBlocksAndScoping:
    def test_let_shadowing_within_a_block(self) -> None:
        result = run_source("""
            fn f() -> Int {
              let x = 1;
              let x = x + 1;
              x
            }
            fn main() -> Int { f() }
        """)
        assert result == 2

    def test_nested_block_expression_value(self) -> None:
        assert run_source("let x = { let a = 1; let b = 2; a + b };") == 3


class TestProgramValueConvention:
    def test_main_functions_return_value_is_the_program_value(self) -> None:
        assert run_source("fn main() -> Int { 41 + 1 }") == 42

    def test_no_main_falls_back_to_last_top_level_let(self) -> None:
        assert run_source("let a = 1; let b = 2; let c = a + b;") == 3

    def test_no_main_and_no_let_yields_unit(self) -> None:
        assert run_source("fn helper() -> Int { 1 }") is UNIT


class TestTypeCheckingHappensBeforeExecution:
    def test_type_error_prevents_the_vm_from_running_at_all(self) -> None:
        with pytest.raises(TypeCheckError):
            run_source("let x = 1 + true;")


class TestValueToStr:
    def test_renders_lowercase_booleans(self) -> None:
        assert value_to_str(True) == "true"
        assert value_to_str(False) == "false"

    def test_renders_lists_with_bracket_syntax(self) -> None:
        assert value_to_str((1, 2, 3)) == "[1, 2, 3]"

    def test_renders_unit(self) -> None:
        assert value_to_str(UNIT) == "()"

    def test_renders_strings_unquoted(self) -> None:
        assert value_to_str("hi") == "hi"


class TestVmReuseAcrossCalls:
    def test_running_a_second_program_on_the_same_vm_keeps_earlier_globals(self) -> None:
        # A fresh `Program`'s static `TypeEnv` (built inside `ir.lower_program`, see that module)
        # starts empty every call — cross-line name resolution in a REPL therefore cannot rely on
        # `vm.globals` alone at the *type-checking* level, which is exactly why `repl.py`
        # re-lowers the whole accumulated session's source on every new line rather than
        # incrementally extending one `VM`'s state (see its module docstring). What a shared `VM`
        # *does* still guarantee, and what's worth covering here: running a second, independent
        # program against the same `VM` doesn't clear what an earlier `run_program` call stored.
        from perelang.bytecode import compile_program
        from perelang.ir import lower_program
        from perelang.parser import parse_program

        vm = VM()
        vm.run_program(compile_program(lower_program(parse_program("let x = 10;"))))
        assert vm.globals["x"] == 10
        result = vm.run_program(compile_program(lower_program(parse_program("let z = 1 + 1;"))))
        assert result == 2
        assert vm.globals["x"] == 10
        assert vm.globals["z"] == 2


class TestOpcodeLevelBehaviorBeyondWhatSourceCanReach:
    """These construct `FunctionProto`s by hand and call `VM.call_closure` directly, rather than
    going through the full source pipeline. Float *addition* specifically is no longer beyond what
    source can reach (`+. -. *. /.` — see `TestFloatArithmetic` above and `types.py`'s
    `_FLOAT_ARITHMETIC`); what remains genuinely opcode-only here is true (non-truncating) division
    applied to two *Int*-typed constants (`vm.py`'s `_div` supports it — see its own comment — but
    no source-level operator ever requests true division on two Ints; `/` truncates and `/.`
    requires Float operands) and calling a non-function value, which no well-typed source program
    can ever produce as a callee."""

    def test_float_arithmetic_at_the_opcode_level(self) -> None:
        proto = FunctionProto(
            name="add_floats",
            arity=0,
            num_locals=0,
            code=(
                Instr(OpCode.CONST, 0),
                Instr(OpCode.CONST, 1),
                Instr(OpCode.ADD),
            ),
            constants=(1.5, 2.5),
            capture_names=(),
        )
        result = VM().call_closure(Closure(proto, ()), ())
        assert result == 4.0

    def test_true_division_of_mixed_int_and_float_operands_at_the_opcode_level(self) -> None:
        # This is the one still genuinely unreachable from source: `_div`'s truncating branch only
        # fires when *both* operands are `int` — `/` guarantees both Int (truncating) and `/.`
        # guarantees both Float (already true division), so a well-typed program can never produce
        # a DIV with one Int and one Float constant. `_div` still handles it correctly (true
        # division, since not *both* are int) — only reachable by hand-building bytecode like this.
        proto = FunctionProto(
            name="div_mixed",
            arity=0,
            num_locals=0,
            code=(Instr(OpCode.CONST, 0), Instr(OpCode.CONST, 1), Instr(OpCode.DIV)),
            constants=(5, 2.0),
            capture_names=(),
        )
        assert VM().call_closure(Closure(proto, ()), ()) == 2.5

    def test_calling_a_non_function_value_raises_peregrine_runtime_error(self) -> None:
        proto = FunctionProto(
            name="call_a_number",
            arity=0,
            num_locals=0,
            code=(Instr(OpCode.CONST, 0), Instr(OpCode.CALL, 0)),
            constants=(7,),
            capture_names=(),
        )
        with pytest.raises(PeregrineRuntimeError):
            VM().call_closure(Closure(proto, ()), ())

    def test_calling_a_closure_directly_via_call_closure(self) -> None:
        proto = FunctionProto(
            name="double",
            arity=1,
            num_locals=1,
            code=(Instr(OpCode.LOAD_LOCAL, 0), Instr(OpCode.LOAD_LOCAL, 0), Instr(OpCode.ADD)),
            constants=(),
            capture_names=(),
        )
        result = VM().call_closure(Closure(proto, ()), (21,))
        assert result == 42
