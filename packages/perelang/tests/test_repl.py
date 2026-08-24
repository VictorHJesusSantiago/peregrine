from perelang.repl import Repl, run_lines


def _run(lines: list[str]) -> list[str]:
    return list(run_lines(lines))


class TestBareExpressions:
    def test_bare_expression_is_echoed_with_its_type(self) -> None:
        assert _run(["1 + 2"]) == ["3 : Int"]

    def test_bare_boolean_expression(self) -> None:
        assert _run(["3 < 5"]) == ["true : Bool"]


class TestLetDeclarations:
    def test_let_prints_name_type_and_value(self) -> None:
        assert _run(["let x = 5 + 3;"]) == ["x : Int = 8"]

    def test_later_lines_see_earlier_lets(self) -> None:
        output = _run(["let x = 10;", "let y = x + 5;", "x + y"])
        assert output == ["x : Int = 10", "y : Int = 15", "25 : Int"]


class TestFunctionDeclarations:
    def test_fn_prints_its_inferred_signature(self) -> None:
        output = _run(["fn add(a: Int, b: Int) -> Int { a + b }"])
        assert output == ["fn add : (Int, Int) -> Int"]

    def test_previously_defined_function_can_be_called_on_a_later_line(self) -> None:
        output = _run(
            [
                "fn fact(n: Int) -> Int { if n <= 1 { 1 } else { n * fact(n - 1) } }",
                "fact(6)",
            ]
        )
        assert output == ["fn fact : (Int) -> Int", "720 : Int"]


class TestMultiLineChunks:
    def test_chunk_spanning_multiple_lines_via_open_brace(self) -> None:
        output = _run(
            [
                "fn square(x: Int) -> Int {",
                "  x * x",
                "}",
                "square(9)",
            ]
        )
        assert output == ["fn square : (Int) -> Int", "81 : Int"]

    def test_prompt_shows_continuation_while_buffering(self) -> None:
        repl = Repl()
        assert repl.prompt() == "pere> "
        assert repl.submit("fn f(x: Int) -> Int {") is None
        assert repl.prompt() == "...   "
        assert repl.submit("  x") is None  # still open: the closing brace hasn't arrived yet
        assert repl.prompt() == "...   "
        assert repl.submit("}") == "fn f : (Int) -> Int"
        assert repl.prompt() == "pere> "


class TestErrorRecoveryDoesNotCrashTheSession:
    def test_lex_error_is_reported_and_session_continues(self) -> None:
        output = _run(["let x = @;", "let y = 1;"])
        assert output[0].startswith("error:")
        assert output[1] == "y : Int = 1"

    def test_type_error_is_reported_and_session_continues(self) -> None:
        output = _run(["let x = 1 + true;", "let y = 2;"])
        assert output[0].startswith("error:")
        assert output[1] == "y : Int = 2"

    def test_unbound_name_is_reported(self) -> None:
        output = _run(["totally_unbound_name"])
        assert output[0].startswith("error:")

    def test_a_bad_line_does_not_poison_a_later_good_redefinition(self) -> None:
        output = _run(["let x = 1;", "let x = @;", "x + 1"])
        assert output == [
            "x : Int = 1",
            'error: unexpected character "@" (line 1, column 9)',
            "2 : Int",
        ]


class TestClosuresInRepl:
    def test_closure_defined_and_called_across_lines(self) -> None:
        output = _run(
            [
                "let make_adder = |x| |y| x + y;",
                "let add5 = make_adder(5);",
                "add5(10)",
            ]
        )
        # `+` is monomorphic over Int (see types.py's stated scope limit), so `make_adder`'s params
        # are forced to Int by its own body — not left generic despite being let-bound.
        assert output == [
            "make_adder : (Int) -> (Int) -> Int = <function <lambda>>",
            "add5 : (Int) -> Int = <function <lambda>>",
            "15 : Int",
        ]
