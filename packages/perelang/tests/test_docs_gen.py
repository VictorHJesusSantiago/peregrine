from pathlib import Path

from typer.testing import CliRunner

from perelang.cli import app
from perelang.docs_gen import extract_docs, generate_docs, render_markdown

runner = CliRunner()

_WITH_COMMENTS = """\
// Adds two integers together.
// Simple, but a good doc-comment example.
fn add(a: Int, b: Int) -> Int {
  a + b
}

let pi: Float = 3.14;

// Not attached to add -- separated by a blank line below.

fn sub(a: Int, b: Int) -> Int {
  a - b
}
"""

_WITHOUT_COMMENTS = """\
fn add(a: Int, b: Int) -> Int {
  a + b
}

let pi: Float = 3.14;
"""


class TestExtractDocs:
    def test_doc_comment_is_attached_to_the_following_fn(self) -> None:
        docs = extract_docs(_WITH_COMMENTS)
        add_doc = next(d for d in docs if d.name == "add")
        assert add_doc.doc_comment == "Adds two integers together.\nSimple, but a good doc-comment example."

    def test_blank_line_breaks_the_comment_association(self) -> None:
        docs = extract_docs(_WITH_COMMENTS)
        sub_doc = next(d for d in docs if d.name == "sub")
        assert sub_doc.doc_comment is None

    def test_declaration_without_a_preceding_comment_has_none(self) -> None:
        docs = extract_docs(_WITHOUT_COMMENTS)
        assert all(d.doc_comment is None for d in docs)

    def test_fn_signature_includes_param_names_and_return_type(self) -> None:
        docs = extract_docs(_WITHOUT_COMMENTS)
        add_doc = next(d for d in docs if d.name == "add")
        assert add_doc.signature == "fn add(a: Int, b: Int) -> Int"
        assert add_doc.kind == "fn"

    def test_let_signature_includes_inferred_type(self) -> None:
        docs = extract_docs(_WITHOUT_COMMENTS)
        pi_doc = next(d for d in docs if d.name == "pi")
        assert pi_doc.signature == "let pi: Float"
        assert pi_doc.kind == "let"

    def test_declarations_are_returned_in_source_order(self) -> None:
        docs = extract_docs(_WITH_COMMENTS)
        assert [d.name for d in docs] == ["add", "pi", "sub"]


class TestRenderMarkdown:
    def test_markdown_contains_a_section_per_declaration(self) -> None:
        docs = extract_docs(_WITH_COMMENTS, source_file="demo.pgr")
        markdown = render_markdown(docs)
        assert "### `add`" in markdown
        assert "### `sub`" in markdown
        assert "### `pi`" in markdown

    def test_markdown_includes_the_signature_in_a_code_block(self) -> None:
        docs = extract_docs(_WITHOUT_COMMENTS, source_file="demo.pgr")
        markdown = render_markdown(docs)
        assert "fn add(a: Int, b: Int) -> Int" in markdown

    def test_markdown_includes_doc_comment_text(self) -> None:
        docs = extract_docs(_WITH_COMMENTS, source_file="demo.pgr")
        markdown = render_markdown(docs)
        assert "Adds two integers together." in markdown

    def test_empty_program_still_renders_a_title(self) -> None:
        markdown = render_markdown([])
        assert markdown.startswith("# ")


class TestGenerateDocsFromPath:
    def test_single_file(self, tmp_path: Path) -> None:
        src = tmp_path / "demo.pgr"
        src.write_text(_WITH_COMMENTS)
        markdown = generate_docs(src)
        assert "### `add`" in markdown
        assert "demo.pgr" in markdown

    def test_directory_of_files_sorted_by_name(self, tmp_path: Path) -> None:
        (tmp_path / "b_file.pgr").write_text("fn only_in_b() -> Int { 1 }")
        (tmp_path / "a_file.pgr").write_text("fn only_in_a() -> Int { 2 }")
        markdown = generate_docs(tmp_path)
        assert markdown.index("a_file.pgr") < markdown.index("b_file.pgr")
        assert "only_in_a" in markdown
        assert "only_in_b" in markdown


class TestCli:
    def test_docs_command_writes_output_file(self, tmp_path: Path) -> None:
        src = tmp_path / "demo.pgr"
        src.write_text(_WITH_COMMENTS)
        out = tmp_path / "out.md"
        result = runner.invoke(app, ["docs", str(src), "-o", str(out)])
        assert result.exit_code == 0
        assert out.is_file()
        assert "### `add`" in out.read_text()

    def test_docs_command_missing_path(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["docs", str(tmp_path / "nope.pgr"), "-o", str(tmp_path / "out.md")])
        assert result.exit_code == 1
        assert "no such file" in result.stderr

    def test_docs_command_reports_parse_error(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.pgr"
        bad.write_text("fn f( {")
        result = runner.invoke(app, ["docs", str(bad), "-o", str(tmp_path / "out.md")])
        assert result.exit_code == 1
        assert "error:" in result.stderr
