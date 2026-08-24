"""Generates Markdown API documentation for a `.pgr` file (or a directory of them): one section per
top-level `fn`/`let` declaration, its inferred type signature (via `types.infer_program` — the
same source of truth `repl.py` uses to describe a binding), and its doc comment if it has one.

**Why doc-comment extraction is a raw-source line scan, not an AST/token thing.** `lexer.py`
discards `//` comments during tokenization on purpose (see its module docstring) — there is no
`COMMENT` token and the AST has no comment nodes, so nothing downstream of the lexer has ever seen
one. Recovering "the comment lines immediately above this declaration" therefore can't be done by
walking the AST at all; it has to look at the original source text directly, independently of
parsing it. `_doc_comment_for` does exactly that and nothing more: given the 1-based line number a
declaration's `Span` starts on (already computed by the lexer/parser for every node — see
`errors.Span`), it walks upward through the raw source lines collecting a contiguous run of `//`
lines, stopping at the first non-comment (including a *blank*) line. "Contiguous, no blank line
before the declaration" is the pairing rule the task asked for, and it's also the same rule collides
with nothing the parser does — this scan runs entirely independently of, and after, parsing
succeeds.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from perelang import ast_nodes as ast
from perelang.parser import parse_program
from perelang.types import TFun, Type, infer_program, type_to_str

_PGR_GLOB = "*.pgr"


@dataclass(frozen=True, slots=True)
class DeclDoc:
    name: str
    kind: str  # "fn" | "let"
    signature: str
    doc_comment: str | None
    source_file: str


def _doc_comment_for(source_lines: list[str], decl_start_line: int) -> str | None:
    """`decl_start_line` is 1-based (straight from `Span.line`). Walks upward from the line just
    above the declaration, collecting `//`-prefixed lines until a non-comment line breaks the run;
    returns them re-joined top-to-bottom with each line's `//` and exactly one following space
    stripped, or `None` if the line directly above isn't a comment at all."""
    comment_lines: list[str] = []
    i = decl_start_line - 2  # 0-based index of the line directly above the declaration
    while i >= 0:
        stripped = source_lines[i].strip()
        if not stripped.startswith("//"):
            break
        text = stripped[2:]
        if text.startswith(" "):
            text = text[1:]
        comment_lines.append(text)
        i -= 1
    if not comment_lines:
        return None
    comment_lines.reverse()
    return "\n".join(comment_lines)


def _signature_for(decl: ast.Decl, ty: Type) -> str:
    if isinstance(decl, ast.FnDecl) and isinstance(ty, TFun):
        params = ", ".join(
            f"{p.name}: {type_to_str(t)}" for p, t in zip(decl.params, ty.params, strict=True)
        )
        return f"fn {decl.name}({params}) -> {type_to_str(ty.ret)}"
    return f"let {decl.name}: {type_to_str(ty)}"


def extract_docs(source: str, source_file: str = "<source>") -> list[DeclDoc]:
    """Every top-level declaration in one file, in source order. Raises whatever
    `parser.parse_program`/`types.infer_program` raise on invalid input — there's nothing
    meaningful to document for a file that doesn't parse or type-check."""
    program = parse_program(source)
    types_by_name = infer_program(program)
    source_lines = source.splitlines()

    docs: list[DeclDoc] = []
    for decl in program.decls:
        kind = "fn" if isinstance(decl, ast.FnDecl) else "let"
        ty = types_by_name[decl.name]
        docs.append(
            DeclDoc(
                name=decl.name,
                kind=kind,
                signature=_signature_for(decl, ty),
                doc_comment=_doc_comment_for(source_lines, decl.span.line),
                source_file=source_file,
            )
        )
    return docs


def extract_docs_from_path(path: Path) -> list[DeclDoc]:
    """`path` may be a single `.pgr` file or a directory — a directory is scanned (non-recursively)
    for every `*.pgr` file, sorted by name for deterministic output, and every file's declarations
    are concatenated in that order."""
    if path.is_dir():
        docs: list[DeclDoc] = []
        for file in sorted(path.glob(_PGR_GLOB)):
            docs += extract_docs(file.read_text(encoding="utf-8"), source_file=file.name)
        return docs
    return extract_docs(path.read_text(encoding="utf-8"), source_file=path.name)


def render_markdown(docs: list[DeclDoc], title: str = "Peregrine API Documentation") -> str:
    lines = [f"# {title}", ""]
    if not docs:
        lines.append("_No top-level declarations found._")
        return "\n".join(lines) + "\n"

    by_file: dict[str, list[DeclDoc]] = {}
    for doc in docs:
        by_file.setdefault(doc.source_file, []).append(doc)

    for source_file, file_docs in by_file.items():
        lines.append(f"## {source_file}")
        lines.append("")
        for doc in file_docs:
            lines.append(f"### `{doc.name}`")
            lines.append("")
            lines.append(f"```peregrine\n{doc.signature}\n```")
            lines.append("")
            if doc.doc_comment is not None:
                lines.append(doc.doc_comment)
                lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def generate_docs(path: Path, title: str = "Peregrine API Documentation") -> str:
    return render_markdown(extract_docs_from_path(path), title=title)
