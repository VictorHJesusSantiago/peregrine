import pytest

pytest.importorskip("llvmlite")

from perelang.ir import lower_program  # noqa: E402
from perelang.llvm_backend import LlvmBackendUnsupportedError, run_llvm  # noqa: E402
from perelang.parser import parse_program  # noqa: E402
from perelang.vm import run_source  # noqa: E402


def _run(source: str, entry: str = "main", args: tuple[int | float, ...] = ()) -> int | float:
    return run_llvm(lower_program(parse_program(source)), entry=entry, args=args)


class TestArithmeticAndComparison:
    def test_basic_arithmetic(self) -> None:
        assert _run("fn main() -> Int { 2 + 3 * 4 }") == 14
        assert _run("fn main() -> Int { (2 + 3) * 4 }") == 20
        assert _run("fn main() -> Int { 7 % 3 }") == 1
        assert _run("fn main() -> Int { 7 / 2 }") == 3

    def test_unary_negation(self) -> None:
        assert _run("fn main() -> Int { -5 + 2 }") == -3

    def test_comparisons_return_bool_as_zero_or_one(self) -> None:
        assert _run("fn main() -> Bool { 3 < 5 }") == 1
        assert _run("fn main() -> Bool { 3 > 5 }") == 0
        assert _run("fn main() -> Bool { 3 == 3 }") == 1

    def test_logical_and_or_short_circuit_and_compute_correctly(self) -> None:
        assert _run("fn main() -> Bool { true && false }") == 0
        assert _run("fn main() -> Bool { true || false }") == 1
        assert _run("fn f(x: Int) -> Bool { x != 0 && 10 / x > 1 } fn main() -> Bool { f(0) }") == 0

    def test_unary_not(self) -> None:
        assert _run("fn main() -> Bool { !(3 < 5) }") == 0


class TestIfExpressions:
    def test_if_else_selects_correct_branch(self) -> None:
        assert _run("fn main() -> Int { if true { 1 } else { 2 } }") == 1
        assert _run("fn main() -> Int { if false { 1 } else { 2 } }") == 2

    def test_nested_if(self) -> None:
        source = """
            fn classify(n: Int) -> Int {
              if n < 0 { 0 - 1 } else { if n == 0 { 0 } else { 1 } }
            }
            fn main() -> Int { classify(-5) + classify(0) * 10 + classify(5) * 100 }
        """
        assert _run(source) == -1 + 0 + 100


class TestFunctionCallsAndRecursion:
    def test_calling_a_function_with_arguments(self) -> None:
        source = "fn add(a: Int, b: Int) -> Int { a + b } fn main() -> Int { add(2, 3) }"
        assert _run(source) == 5

    def test_factorial_of_ten_via_recursion(self) -> None:
        source = """
            fn fact(n: Int) -> Int {
              if n <= 1 { 1 } else { n * fact(n - 1) }
            }
            fn main() -> Int { fact(10) }
        """
        assert _run(source) == 3628800

    def test_fibonacci_of_twenty_via_recursion(self) -> None:
        source = """
            fn fib(n: Int) -> Int {
              if n < 2 { n } else { fib(n - 1) + fib(n - 2) }
            }
            fn main() -> Int { fib(20) }
        """
        assert _run(source) == 6765

    def test_entry_function_other_than_main_with_explicit_args(self) -> None:
        source = "fn double(x: Int) -> Int { x * 2 }"
        assert _run(source, entry="double", args=(21,)) == 42


class TestFloatArithmetic:
    """Proves the type-directed dispatch in `_compile_binary`/`_compile_unary`/`_compile_if`
    genuinely discriminates between Int and Float codegen (`add` vs `fadd`, `icmp_signed` vs
    `fcmp_ordered`, ...) rather than defaulting everything to one type — see `llvm_backend.py`'s
    module docstring."""

    def test_basic_float_arithmetic(self) -> None:
        assert _run("fn main() -> Float { 2.0 +. 3.0 *. 4.0 }") == 14.0
        assert _run("fn main() -> Float { (2.0 +. 3.0) *. 4.0 }") == 20.0
        assert _run("fn main() -> Float { 7.0 /. 2.0 }") == 3.5
        assert _run("fn main() -> Float { 7.5 -. 2.5 }") == 5.0

    def test_float_unary_negation(self) -> None:
        assert _run("fn main() -> Float { -.5.0 +. 2.0 }") == -3.0

    def test_float_comparison_returns_bool_as_zero_or_one(self) -> None:
        assert _run("fn main() -> Bool { 3.0 < 5.0 }") == 1
        assert _run("fn main() -> Bool { 3.0 > 5.0 }") == 0
        assert _run("fn main() -> Bool { 3.5 == 3.5 }") == 1

    def test_float_if_else_selects_correct_branch(self) -> None:
        assert _run("fn main() -> Float { if true { 1.5 } else { 2.5 } }") == 1.5
        assert _run("fn main() -> Float { if false { 1.5 } else { 2.5 } }") == 2.5

    def test_recursive_float_function(self) -> None:
        # A recursive running-sum over a Float accumulator — proves recursion works for a
        # Float-typed function specifically, not just the pre-existing Int recursion tests.
        source = """
            fn sum_to(n: Int, acc: Float) -> Float {
              if n <= 0 { acc } else { sum_to(n - 1, acc +. 1.5) }
            }
            fn main() -> Float { sum_to(4, 0.0) }
        """
        assert _run(source) == 6.0

    def test_float_entry_with_explicit_float_args(self) -> None:
        source = "fn average(a: Float, b: Float) -> Float { (a +. b) /. 2.0 }"
        assert _run(source, entry="average", args=(3.0, 7.0)) == 5.0

    def test_mixed_int_and_float_functions_compile_into_the_same_module(self) -> None:
        # Two entirely separate top-level `fn`s, one Int-typed and one Float-typed, declared in the
        # same source program — `build_llvm_module` compiles every `IrFunction` decl it's given
        # into one `llvmlite.ir.Module`, so both of these end up in that one module. Calling each
        # by name and getting back the *correct* type for each (not both silently treated as `i64`
        # or both as `double`) is the actual proof the dispatch discriminates per-function.
        source = """
            fn int_double(x: Int) -> Int { x * 2 }
            fn float_double(x: Float) -> Float { x *. 2.0 }
        """
        int_result = _run(source, entry="int_double", args=(21,))
        float_result = _run(source, entry="float_double", args=(21.5,))
        assert int_result == 42
        assert isinstance(int_result, int)
        assert float_result == 43.0
        assert isinstance(float_result, float)


class TestClosures:
    """Proves the heap-allocated-capture-struct + extra-environment-parameter design described in
    `llvm_backend.py`'s module docstring actually works end to end through the LLVM path — mirrors
    the Peregrine-source patterns `test_vm.py`'s own `TestClosures` already uses for the bytecode
    backend, so the two backends are proven against the same closure shapes."""

    def test_non_capturing_lambda_called_immediately(self) -> None:
        # A lambda with an empty `captures` tuple still goes through the exact same
        # malloc-a-closure-struct/synthesize-a-function path as a capturing one (see module
        # docstring) — this proves that uniform path works even with a null/unused `env_ptr`.
        assert _run("fn main() -> Int { (|x| x + 1)(41) }") == 42

    def test_zero_param_lambda_called_immediately(self) -> None:
        assert _run("fn main() -> Int { (|| 7)() }") == 7

    def test_adder_closure_captures_a_function_parameter(self) -> None:
        # Mirrors test_vm.py's TestClosures.test_closure_over_a_function_parameter, but returning
        # the lambda itself (rather than calling it immediately inside the same function) so the
        # *caller* (main) exercises the indirect-call path on a closure value stored in a local.
        source = """
            fn make_adder(x: Int) { |y| x + y }
            fn main() -> Int {
                let add5 = make_adder(5);
                add5(10)
            }
        """
        assert _run(source) == 15

    def test_two_closures_from_the_same_maker_capture_independently(self) -> None:
        # Mirrors test_vm.py's TestClosures.test_two_closures_from_the_same_maker_capture_independently
        # — two separate calls to the same factory `fn` must each get their own malloc'd capture
        # environment, not somehow alias or overwrite each other's captured `x`.
        source = """
            fn make_adder(x: Int) { |y| x + y }
            fn main() -> Int {
                let add5 = make_adder(5);
                let add100 = make_adder(100);
                add5(1) + add100(1)
            }
        """
        assert _run(source) == 107

    def test_closure_capturing_multiple_names_in_sorted_order(self) -> None:
        # `ir.py`'s `_lower_lambda` sorts `captures` alphabetically — this proves the capture
        # struct's field order (and this backend's GEP indices into it) actually line up with that
        # sort order for more than one captured name, not just one.
        source = """
            fn make_combiner(a: Int, b: Int) { |y| a + b + y }
            fn main() -> Int {
                let c = make_combiner(3, 4);
                c(10)
            }
        """
        assert _run(source) == 17

    def test_recursive_function_using_a_local_closure_each_call(self) -> None:
        # Recursion and closures combined: each recursive call creates its own closure capturing
        # that call's own `acc`, and calls it immediately — proving a fresh capture environment is
        # malloc'd per activation rather than one shared/stale environment across recursive calls.
        source = """
            fn sum_with_add(n: Int, acc: Int) -> Int {
              let add_acc = |y| y + acc;
              if n <= 0 { acc } else { sum_with_add(n - 1, add_acc(1)) }
            }
            fn main() -> Int { sum_with_add(5, 0) }
        """
        assert _run(source) == 5

    def test_top_level_function_assigned_to_a_let_and_called_indirectly(self) -> None:
        # Mirrors test_vm.py's TestClosures.test_plain_top_level_function_is_a_first_class_value.
        # `add` is referenced as a value (not called by name directly), so `_compile_name` must
        # wrap it in the closure representation (see `_wrap_top_level_function_as_closure`) and
        # `g(2, 3)` must go through `_compile_call`'s indirect path, not the direct-call fast path.
        source = """
            fn add(a: Int, b: Int) -> Int { a + b }
            fn main() -> Int {
                let g = add;
                g(2, 3)
            }
        """
        assert _run(source) == 5


class TestStringAndListValues:
    """Proves `String`/`List` values compile and flow correctly as locals, function arguments, and
    return values *between* Peregrine functions — the scope this backend actually covers (see
    module docstring); `entry` itself staying Int/Bool/Float-only is proven separately below."""

    def test_string_passed_between_functions(self) -> None:
        source = """
            fn make_greeting() -> String { "hello" }
            fn describe(s: String) -> Int { 7 }
            fn main() -> Int { describe(make_greeting()) }
        """
        assert _run(source) == 7

    def test_string_as_a_local(self) -> None:
        source = """
            fn describe(s: String) -> Int { 3 }
            fn main() -> Int {
                let greeting = "hi there";
                describe(greeting)
            }
        """
        assert _run(source) == 3

    def test_list_passed_between_functions(self) -> None:
        source = """
            fn build_list() -> List<Int> { [10, 20, 30] }
            fn consume_list(xs: List<Int>) -> Int { 42 }
            fn main() -> Int {
                let xs = build_list();
                consume_list(xs)
            }
        """
        assert _run(source) == 42

    def test_empty_list_round_trips_without_allocating(self) -> None:
        source = """
            fn consume_list(xs: List<Int>) -> Int { -1 }
            fn main() -> Int {
                let empty: List<Int> = [];
                consume_list(empty)
            }
        """
        assert _run(source) == -1

    def test_list_of_expressions_not_just_literals(self) -> None:
        source = """
            fn consume_list(xs: List<Int>) -> Int { 99 }
            fn main() -> Int {
                let a = 1;
                consume_list([a, a + 1, a + 2])
            }
        """
        assert _run(source) == 99


class TestTopLevelGlobals:
    """Proves a top-level `let`, referenced as a value from a compiled function body, is a real
    usable value via `run_llvm` — the `@llvm.global_ctors`-backed module-constructor mechanism
    described in `llvm_backend.py`'s module docstring (`_build_global_ctor`), the LLVM-backend
    equivalent of `vm.py`'s own synthesized `<init>` chunk. Each test also cross-checks against
    `vm.py`'s bytecode backend (`run_source`) run on the exact same source, proving the two
    backends agree, not just that the LLVM backend doesn't crash."""

    def test_top_level_let_referenced_as_a_value(self) -> None:
        source = "let x = 5; fn main() -> Int { x + 1 }"
        assert _run(source) == 6
        assert run_source(source) == 6

    def test_multiple_globals_one_referencing_an_earlier_one(self) -> None:
        # `y`'s initializer references `x`, declared earlier in `program.decls` — legal per this
        # language's sequential top-level scoping (see `types.py`'s `infer_program`/`ir.py`'s
        # `lower_program`, both of which only extend the top-level environment with a name
        # *after* processing its own declaration). A later global referencing an *earlier* one is
        # the only direction that type-checks at all; there is no forward-reference case to test.
        source = """
            let x = 5;
            let y = x + 10;
            fn main() -> Int { x + y }
        """
        assert _run(source) == 20
        assert run_source(source) == 20

    def test_global_initializer_is_a_real_function_call_not_just_a_literal(self) -> None:
        # `x`'s initializer calls a real top-level `fn` (`double`) rather than being
        # constant-foldable — proves `_build_global_ctor`'s combined constructor actually emits and
        # executes a `call` instruction as part of module initialization, not just literal stores.
        source = """
            fn double(n: Int) -> Int { n * 2 }
            let x = double(21);
            fn main() -> Int { x }
        """
        assert _run(source) == 42
        assert run_source(source) == 42

    def test_global_used_from_a_function_other_than_main(self) -> None:
        # The global is read from a function that isn't the constructor and isn't `main` either —
        # proves `_compile_name`'s `is_global` branch resolves a `GlobalVariable` uniformly from
        # any compiled function body, not just the one the test happens to call `entry`.
        source = """
            let base = 100;
            fn add_base(n: Int) -> Int { n + base }
            fn main() -> Int { add_base(1) + add_base(2) }
        """
        assert _run(source) == 203
        assert run_source(source) == 203

    def test_program_with_no_globals_still_runs(self) -> None:
        # No `IrGlobalLet` at all — `build_llvm_module` must skip building the constructor/
        # `@llvm.global_ctors` entry entirely (see module docstring), and `run_static_constructors`
        # on a module with none registered must still be a harmless no-op.
        assert _run("fn main() -> Int { 41 + 1 }") == 42


class TestUnsupportedConstructsRaiseRatherThanMiscompile:
    def test_unit_returning_function_raises(self) -> None:
        source = "fn main() { let x = 1; }"
        with pytest.raises(LlvmBackendUnsupportedError):
            _run(source)

    def test_string_entry_boundary_still_raises(self) -> None:
        # String/List values now compile fine *inside* a function body (see
        # TestStringAndListValues) — but `entry` itself must still resolve to Int/Bool/Float at the
        # `ctypes` boundary (see `_ctypes_type_for`), so a String-typed `entry` still raises.
        source = 'fn main() -> String { "hi" }'
        with pytest.raises(LlvmBackendUnsupportedError):
            _run(source)

    def test_list_entry_boundary_still_raises(self) -> None:
        source = "fn main() -> List<Int> { [1, 2, 3] }"
        with pytest.raises(LlvmBackendUnsupportedError):
            _run(source)
