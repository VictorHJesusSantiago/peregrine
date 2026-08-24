from pathlib import Path

from typer.testing import CliRunner

from perelang.cli import app
from perelang.linter import lint_source

runner = CliRunner()

_EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


class TestExamplesAreClean:
    """False-positive avoidance matters as much as detection — every shipped example must lint
    clean, or the checks are too eager to trust."""

    def test_factorial_example_has_no_findings(self) -> None:
        assert lint_source((_EXAMPLES / "factorial.pgr").read_text()) == []

    def test_fibonacci_example_has_no_findings(self) -> None:
        assert lint_source((_EXAMPLES / "fibonacci.pgr").read_text()) == []

    def test_closures_example_has_no_findings(self) -> None:
        assert lint_source((_EXAMPLES / "closures.pgr").read_text()) == []


class TestUnusedBinding:
    def test_unused_let_is_flagged(self) -> None:
        findings = lint_source("fn f() -> Int { let x = 1; 2 }")
        assert len(findings) == 1
        assert "unused binding 'x'" in findings[0].message

    def test_let_used_in_tail_is_not_flagged(self) -> None:
        assert lint_source("fn f() -> Int { let x = 1; x }") == []

    def test_let_used_in_later_stmt_is_not_flagged(self) -> None:
        assert lint_source("fn f() -> Int { let x = 1; let y = x + 1; y }") == []

    def test_let_captured_by_later_lambda_is_not_flagged(self) -> None:
        assert lint_source("fn f() -> Int { let x = 1; let g = |y| x + y; g(2) }") == []


class TestUnusedParameter:
    def test_unused_parameter_is_flagged(self) -> None:
        findings = lint_source("fn f(x: Int) -> Int { 1 }")
        assert len(findings) == 1
        assert "unused parameter 'x'" in findings[0].message

    def test_used_parameter_is_not_flagged(self) -> None:
        assert lint_source("fn f(x: Int) -> Int { x + 1 }") == []

    def test_parameter_used_only_in_nested_if_is_not_flagged(self) -> None:
        assert lint_source("fn f(x: Bool) -> Int { if x { 1 } else { 2 } }") == []


class TestIdenticalBranches:
    def test_identical_branches_are_flagged(self) -> None:
        findings = lint_source("fn f(x: Bool) -> Int { if x { 1 } else { 1 } }")
        assert len(findings) == 1
        assert "identical" in findings[0].message

    def test_different_branches_are_not_flagged(self) -> None:
        assert lint_source("fn f(x: Bool) -> Int { if x { 1 } else { 2 } }") == []

    def test_branches_differing_only_in_source_position_are_still_flagged(self) -> None:
        # Same shape, deliberately written with different internal spacing/newlines so the two
        # branches don't share a single Span — the check must compare structure, not source text.
        findings = lint_source(
            "fn f(x: Bool) -> Int {\n  if x {\n    1 + 1\n  } else {\n    1 +\n      1\n  }\n}"
        )
        assert len(findings) == 1


class TestShadowing:
    def test_shadowing_a_parameter_is_flagged(self) -> None:
        findings = lint_source("fn f(x: Int) -> Int { let x = 2; x }")
        assert len(findings) == 1
        assert "shadows" in findings[0].message

    def test_shadowing_an_outer_let_is_flagged(self) -> None:
        findings = lint_source("fn f() -> Int { let x = 1; let y = { let x = 2; x }; y }")
        assert any("shadows" in f.message for f in findings)

    def test_sibling_top_level_functions_reusing_a_param_name_is_not_flagged(self) -> None:
        # Every top-level fn/let is visible everywhere (mutual recursion), so two functions each
        # using `x` as a parameter name is completely ordinary, not shadowing.
        assert lint_source("fn f(x: Int) -> Int { x } fn g(x: Int) -> Int { x }") == []

    def test_distinct_names_in_nested_scopes_are_not_flagged(self) -> None:
        assert lint_source("fn f(x: Int) -> Int { let y = x + 1; y }") == []


class TestCli:
    def test_lint_clean_file_reports_ok_and_exits_zero(self) -> None:
        result = runner.invoke(app, ["lint", str(_EXAMPLES / "factorial.pgr")])
        assert result.exit_code == 0
        assert result.stdout.strip() == "ok"

    def test_lint_file_with_findings_exits_nonzero_and_lists_them(self, tmp_path: Path) -> None:
        src = tmp_path / "unused.pgr"
        src.write_text("fn f() -> Int { let x = 1; 2 }")
        result = runner.invoke(app, ["lint", str(src)])
        assert result.exit_code == 1
        assert "unused binding 'x'" in result.stdout

    def test_lint_reports_parse_error(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.pgr"
        bad.write_text("fn f( {")
        result = runner.invoke(app, ["lint", str(bad)])
        assert result.exit_code == 1
        assert "error:" in result.stderr
