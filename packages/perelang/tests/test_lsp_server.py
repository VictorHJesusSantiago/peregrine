"""Drives `LspCore` directly with constructed request dicts — no subprocess, no socket, matching
the house style (`repl.py`'s `Repl`, driven the same way by `test_repl.py`)."""

from __future__ import annotations

import io
import subprocess
import sys
from typing import Any, BinaryIO, cast

from perelang.lsp_server import LspCore, read_message, write_message

_URI = "file:///doc.pgr"


def _did_open(text: str, uri: str = _URI) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "method": "textDocument/didOpen",
        "params": {"textDocument": {"uri": uri, "languageId": "peregrine", "version": 1, "text": text}},
    }


def _did_change(text: str, uri: str = _URI) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "method": "textDocument/didChange",
        "params": {"textDocument": {"uri": uri, "version": 2}, "contentChanges": [{"text": text}]},
    }


class TestInitialize:
    def test_initialize_responds_with_capabilities(self) -> None:
        core = LspCore()
        responses = core.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        assert len(responses) == 1
        assert responses[0]["id"] == 1
        caps = responses[0]["result"]["capabilities"]
        assert caps["textDocumentSync"] == 1
        assert caps["hoverProvider"] is True
        assert caps["definitionProvider"] is True
        assert caps["referencesProvider"] is True
        assert caps["renameProvider"] is True
        assert caps["completionProvider"] == {}

    def test_initialized_notification_has_no_response(self) -> None:
        core = LspCore()
        assert core.handle({"jsonrpc": "2.0", "method": "initialized", "params": {}}) == []


class TestDiagnostics:
    def test_syntax_error_publishes_a_diagnostic_with_correct_range(self) -> None:
        core = LspCore()
        # `1 +` on line 0 (0-based): the unexpected '}' is the 24th character (0-based) of
        # "fn main() -> Int { 1 + }".
        source = "fn main() -> Int { 1 + }"
        responses = core.handle(_did_open(source))
        assert len(responses) == 1
        notif = responses[0]
        assert notif["method"] == "textDocument/publishDiagnostics"
        assert notif["params"]["uri"] == _URI
        diags = notif["params"]["diagnostics"]
        assert len(diags) == 1
        assert diags[0]["severity"] == 1
        assert "range" in diags[0] and "start" in diags[0]["range"] and "end" in diags[0]["range"]
        assert diags[0]["range"]["start"]["line"] == 0

    def test_fixing_the_error_via_didchange_clears_diagnostics(self) -> None:
        core = LspCore()
        core.handle(_did_open("fn main() -> Int { 1 + }"))
        responses = core.handle(_did_change("fn main() -> Int { 1 + 2 }"))
        assert len(responses) == 1
        diags = responses[0]["params"]["diagnostics"]
        assert diags == []

    def test_type_error_is_reported_as_an_error_diagnostic(self) -> None:
        core = LspCore()
        responses = core.handle(_did_open("fn main() -> Int { true }"))
        diags = responses[0]["params"]["diagnostics"]
        assert len(diags) == 1
        assert diags[0]["severity"] == 1

    def test_lint_finding_is_reported_as_a_warning_diagnostic(self) -> None:
        core = LspCore()
        responses = core.handle(_did_open("fn f() -> Int { let x = 1; 2 }"))
        diags = responses[0]["params"]["diagnostics"]
        assert len(diags) == 1
        assert diags[0]["severity"] == 2
        assert "unused binding" in diags[0]["message"]

    def test_valid_clean_document_has_no_diagnostics(self) -> None:
        core = LspCore()
        responses = core.handle(_did_open("fn main() -> Int { 1 + 2 }"))
        assert responses[0]["params"]["diagnostics"] == []

    def test_did_close_clears_diagnostics_and_forgets_the_document(self) -> None:
        core = LspCore()
        core.handle(_did_open("fn main() -> Int { 1 + }"))
        assert _URI in core.documents
        responses = core.handle(
            {"jsonrpc": "2.0", "method": "textDocument/didClose", "params": {"textDocument": {"uri": _URI}}}
        )
        assert responses[0]["params"]["diagnostics"] == []
        assert _URI not in core.documents


class TestHover:
    def test_hover_over_a_call_to_a_top_level_function_shows_its_signature(self) -> None:
        core = LspCore()
        source = "fn add(a: Int, b: Int) -> Int { a + b }\nfn main() -> Int { add(1, 2) }\n"
        core.handle(_did_open(source))
        col = source.split("\n")[1].index("add")
        responses = core.handle(
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "textDocument/hover",
                "params": {"textDocument": {"uri": _URI}, "position": {"line": 1, "character": col + 1}},
            }
        )
        assert len(responses) == 1
        result = responses[0]["result"]
        assert result is not None
        assert result["contents"]["value"] == "add : (Int, Int) -> Int"

    def test_hover_over_whitespace_returns_no_result(self) -> None:
        core = LspCore()
        core.handle(_did_open("fn main() -> Int { 1 + 2 }"))
        responses = core.handle(
            {
                "jsonrpc": "2.0",
                "id": 8,
                "method": "textDocument/hover",
                "params": {"textDocument": {"uri": _URI}, "position": {"line": 0, "character": 0}},
            }
        )
        assert responses[0]["result"] is None

    def test_hover_on_unopened_document_returns_no_result(self) -> None:
        core = LspCore()
        responses = core.handle(
            {
                "jsonrpc": "2.0",
                "id": 9,
                "method": "textDocument/hover",
                "params": {"textDocument": {"uri": "file:///never-opened.pgr"}, "position": {"line": 0, "character": 0}},
            }
        )
        assert responses[0]["result"] is None

    def test_hover_over_a_parameter_use_shows_its_type(self) -> None:
        # `ir.py`'s IR nodes now carry real spans; this exercises the new `_hover_local` fallback,
        # which the old top-level-only hover couldn't reach at all (see the module docstring).
        core = LspCore()
        source = "fn add(a: Int, b: Int) -> Int {\n    let sum = a + b;\n    sum\n}\n"
        core.handle(_did_open(source))
        line = source.split("\n")[1]
        col = line.index("a + b")
        responses = core.handle(
            {
                "jsonrpc": "2.0",
                "id": 10,
                "method": "textDocument/hover",
                "params": {"textDocument": {"uri": _URI}, "position": {"line": 1, "character": col}},
            }
        )
        result = responses[0]["result"]
        assert result is not None
        assert result["contents"]["value"] == "a : Int"

    def test_hover_over_a_let_bound_local_name_shows_its_type(self) -> None:
        core = LspCore()
        source = "fn add(a: Int, b: Int) -> Int {\n    let sum = a + b;\n    sum\n}\n"
        core.handle(_did_open(source))
        line = source.split("\n")[1]
        col = line.index("sum")
        responses = core.handle(
            {
                "jsonrpc": "2.0",
                "id": 11,
                "method": "textDocument/hover",
                "params": {"textDocument": {"uri": _URI}, "position": {"line": 1, "character": col}},
            }
        )
        result = responses[0]["result"]
        assert result is not None
        assert result["contents"]["value"] == "sum : Int"

    def test_hover_over_a_use_of_a_let_bound_local_shows_its_type(self) -> None:
        core = LspCore()
        source = "fn add(a: Int, b: Int) -> Int {\n    let sum = a + b;\n    sum\n}\n"
        core.handle(_did_open(source))
        line = source.split("\n")[2]
        col = line.index("sum")
        responses = core.handle(
            {
                "jsonrpc": "2.0",
                "id": 12,
                "method": "textDocument/hover",
                "params": {"textDocument": {"uri": _URI}, "position": {"line": 2, "character": col}},
            }
        )
        result = responses[0]["result"]
        assert result is not None
        assert result["contents"]["value"] == "sum : Int"


_SHADOW_SOURCE = (
    "fn f() -> Int {\n"
    "    let x = 1;\n"
    "    let y = {\n"
    "        let x = 2;\n"
    "        x\n"
    "    };\n"
    "    x + y\n"
    "}\n"
)

_RECURSIVE_SOURCE = (
    "fn fact(n: Int) -> Int {\n"
    "    if n <= 1 { 1 } else { n * fact(n - 1) }\n"
    "}\n"
    "fn main() -> Int { fact(5) }\n"
)


class TestDefinition:
    def test_definition_of_a_use_resolves_to_its_declaration_respecting_shadowing(self) -> None:
        core = LspCore()
        core.handle(_did_open(_SHADOW_SOURCE))
        lines = _SHADOW_SOURCE.split("\n")
        # The `x` used as the inner block's tail (line 4) must resolve to the *inner* `let x = 2;`
        # (line 3), not the outer `let x = 1;` (line 1) it shadows.
        use_col = lines[4].index("x")
        responses = core.handle(
            {
                "jsonrpc": "2.0",
                "id": 20,
                "method": "textDocument/definition",
                "params": {"textDocument": {"uri": _URI}, "position": {"line": 4, "character": use_col}},
            }
        )
        result = responses[0]["result"]
        assert result is not None
        decl_col = lines[3].index("x")
        assert result["range"]["start"]["line"] == 3
        assert result["range"]["start"]["character"] == decl_col
        assert result["range"]["end"]["character"] == decl_col + 1

    def test_definition_of_a_top_level_call_resolves_to_its_fn_decl_name(self) -> None:
        core = LspCore()
        core.handle(_did_open(_RECURSIVE_SOURCE))
        lines = _RECURSIVE_SOURCE.split("\n")
        use_col = lines[3].index("fact")
        responses = core.handle(
            {
                "jsonrpc": "2.0",
                "id": 21,
                "method": "textDocument/definition",
                "params": {"textDocument": {"uri": _URI}, "position": {"line": 3, "character": use_col}},
            }
        )
        result = responses[0]["result"]
        assert result is not None
        decl_col = lines[0].index("fact")
        assert result["range"]["start"] == {"line": 0, "character": decl_col}

    def test_definition_on_whitespace_returns_no_result(self) -> None:
        core = LspCore()
        core.handle(_did_open("fn main() -> Int { 1 + 2 }"))
        responses = core.handle(
            {
                "jsonrpc": "2.0",
                "id": 22,
                "method": "textDocument/definition",
                "params": {"textDocument": {"uri": _URI}, "position": {"line": 0, "character": 0}},
            }
        )
        assert responses[0]["result"] is None


class TestReferences:
    def test_references_to_a_recursively_used_name_includes_every_use_and_the_declaration(self) -> None:
        core = LspCore()
        core.handle(_did_open(_RECURSIVE_SOURCE))
        lines = _RECURSIVE_SOURCE.split("\n")
        use_col = lines[3].index("fact")  # trigger from the use in `main`, not the declaration
        responses = core.handle(
            {
                "jsonrpc": "2.0",
                "id": 30,
                "method": "textDocument/references",
                "params": {
                    "textDocument": {"uri": _URI},
                    "position": {"line": 3, "character": use_col},
                    "context": {"includeDeclaration": True},
                },
            }
        )
        result = responses[0]["result"]
        assert result is not None
        # declaration (line 0) + the recursive self-call (line 1) + the call from `main` (line 3).
        assert len(result) == 3
        lines_found = {loc["range"]["start"]["line"] for loc in result}
        assert lines_found == {0, 1, 3}

    def test_references_excludes_declaration_when_not_requested(self) -> None:
        core = LspCore()
        core.handle(_did_open(_RECURSIVE_SOURCE))
        lines = _RECURSIVE_SOURCE.split("\n")
        use_col = lines[3].index("fact")
        responses = core.handle(
            {
                "jsonrpc": "2.0",
                "id": 31,
                "method": "textDocument/references",
                "params": {
                    "textDocument": {"uri": _URI},
                    "position": {"line": 3, "character": use_col},
                    "context": {"includeDeclaration": False},
                },
            }
        )
        result = responses[0]["result"]
        assert result is not None
        assert len(result) == 2

    def test_references_respects_shadowing(self) -> None:
        core = LspCore()
        core.handle(_did_open(_SHADOW_SOURCE))
        lines = _SHADOW_SOURCE.split("\n")
        use_col = lines[4].index("x")  # the inner `x`
        responses = core.handle(
            {
                "jsonrpc": "2.0",
                "id": 32,
                "method": "textDocument/references",
                "params": {"textDocument": {"uri": _URI}, "position": {"line": 4, "character": use_col}},
            }
        )
        result = responses[0]["result"]
        assert result is not None
        # Only the inner declaration (line 3) and its one use (line 4) — never the outer `x`
        # (line 1) or the final `x + y` (line 6), which refer to the *outer* binding.
        lines_found = {loc["range"]["start"]["line"] for loc in result}
        assert lines_found == {3, 4}


class TestRename:
    def test_rename_computes_a_workspace_edit_covering_every_reference(self) -> None:
        core = LspCore()
        core.handle(_did_open(_RECURSIVE_SOURCE))
        lines = _RECURSIVE_SOURCE.split("\n")
        use_col = lines[3].index("fact")
        responses = core.handle(
            {
                "jsonrpc": "2.0",
                "id": 40,
                "method": "textDocument/rename",
                "params": {
                    "textDocument": {"uri": _URI},
                    "position": {"line": 3, "character": use_col},
                    "newName": "factorial",
                },
            }
        )
        result = responses[0]["result"]
        assert result is not None
        edits = result["changes"][_URI]
        assert len(edits) == 3
        assert all(edit["newText"] == "factorial" for edit in edits)
        lines_found = {edit["range"]["start"]["line"] for edit in edits}
        assert lines_found == {0, 1, 3}

    def test_rename_rejects_an_invalid_identifier(self) -> None:
        core = LspCore()
        core.handle(_did_open(_RECURSIVE_SOURCE))
        lines = _RECURSIVE_SOURCE.split("\n")
        use_col = lines[3].index("fact")
        responses = core.handle(
            {
                "jsonrpc": "2.0",
                "id": 41,
                "method": "textDocument/rename",
                "params": {
                    "textDocument": {"uri": _URI},
                    "position": {"line": 3, "character": use_col},
                    "newName": "1bad",
                },
            }
        )
        assert "result" not in responses[0]
        assert responses[0]["error"]["code"] == -32602

    def test_rename_rejects_a_keyword(self) -> None:
        core = LspCore()
        core.handle(_did_open(_RECURSIVE_SOURCE))
        lines = _RECURSIVE_SOURCE.split("\n")
        use_col = lines[3].index("fact")
        responses = core.handle(
            {
                "jsonrpc": "2.0",
                "id": 42,
                "method": "textDocument/rename",
                "params": {
                    "textDocument": {"uri": _URI},
                    "position": {"line": 3, "character": use_col},
                    "newName": "let",
                },
            }
        )
        assert responses[0]["error"]["code"] == -32602


class TestCompletion:
    def test_completion_inside_a_function_body_respects_sequential_block_scoping(self) -> None:
        core = LspCore()
        source = (
            "fn helper(a: Int) -> Int { a }\n"
            "fn main(x: Int) -> Int {\n"
            "    let y = 1;\n"
            "    let z = 2;\n"
            "    x + y + z\n"
            "}\n"
        )
        core.handle(_did_open(source))
        # Position right at the start of the `let z = 2;` line, before that `let` is reached — `z`
        # must not be visible yet, even though it's textually later in the same block.
        responses = core.handle(
            {
                "jsonrpc": "2.0",
                "id": 50,
                "method": "textDocument/completion",
                "params": {"textDocument": {"uri": _URI}, "position": {"line": 3, "character": 0}},
            }
        )
        result = responses[0]["result"]
        assert result is not None
        by_label = {item["label"]: item["kind"] for item in result}
        for kw in ("let", "fn", "if", "else", "true", "false", "return"):
            assert by_label[kw] == 14
        assert by_label["helper"] == 3
        assert by_label["main"] == 3
        assert by_label["x"] == 6
        assert by_label["y"] == 6
        assert "z" not in by_label


class TestLifecycle:
    def test_shutdown_then_exit(self) -> None:
        core = LspCore()
        responses = core.handle({"jsonrpc": "2.0", "id": 3, "method": "shutdown", "params": {}})
        assert responses == [{"jsonrpc": "2.0", "id": 3, "result": None}]
        assert core.shutdown_requested is True
        assert core.handle({"jsonrpc": "2.0", "method": "exit", "params": {}}) == []

    def test_unknown_method_with_id_returns_method_not_found_error(self) -> None:
        core = LspCore()
        # `textDocument/signatureHelp` is a real LSP method this server genuinely doesn't implement
        # (see `lsp_server.py`'s module docstring) — a plausible-but-unbuilt method, not a made-up one.
        responses = core.handle({"jsonrpc": "2.0", "id": 4, "method": "textDocument/signatureHelp", "params": {}})
        assert len(responses) == 1
        assert responses[0]["error"]["code"] == -32601

    def test_unknown_notification_is_silently_ignored(self) -> None:
        core = LspCore()
        assert core.handle({"jsonrpc": "2.0", "method": "$/someNotification", "params": {}}) == []


class TestWireFraming:
    def test_write_then_read_message_round_trips(self) -> None:
        buf = io.BytesIO()
        message = {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}
        write_message(buf, message)
        buf.seek(0)
        assert read_message(buf) == message

    def test_read_message_returns_none_at_eof(self) -> None:
        assert read_message(io.BytesIO(b"")) is None

    def test_definition_round_trips_through_a_real_subprocess(self) -> None:
        """Everything else in this file drives `LspCore` directly (see the module docstring); this
        one test proves the actual `Content-Length`-framed wire protocol works end to end for one
        of the new methods too — through a real `perelang.lsp_server` child process's real stdin/
        stdout, not a constructed dict, for the same "prove the framing, not just the logic" reason
        `test_write_then_read_message_round_trips` exists (see the module docstring)."""
        proc = subprocess.Popen(
            [sys.executable, "-m", "perelang.lsp_server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert proc.stdin is not None
        assert proc.stdout is not None
        # `Popen.stdin`/`.stdout` are typed as `IO[bytes]`, not the (structurally identical, but
        # not implicitly compatible under `mypy --strict`) `BinaryIO` that `read_message`/
        # `write_message` accept — a real subprocess pipe genuinely supports every method either
        # signature needs, so this narrowing is honest, not a workaround for an actual mismatch.
        stdin = cast(BinaryIO, proc.stdin)
        stdout = cast(BinaryIO, proc.stdout)
        try:
            source = "fn add(a: Int, b: Int) -> Int { a + b }\nfn main() -> Int { add(1, 2) }\n"

            write_message(stdin, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
            init_response = read_message(stdout)
            assert init_response is not None
            assert init_response["result"]["capabilities"]["definitionProvider"] is True

            write_message(stdin, _did_open(source))
            published = read_message(stdout)
            assert published is not None
            assert published["params"]["diagnostics"] == []

            col = source.split("\n")[1].index("add")
            write_message(
                stdin,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "textDocument/definition",
                    "params": {"textDocument": {"uri": _URI}, "position": {"line": 1, "character": col + 1}},
                },
            )
            def_response = read_message(stdout)
            assert def_response is not None
            assert def_response["result"]["range"]["start"] == {"line": 0, "character": 3}

            write_message(stdin, {"jsonrpc": "2.0", "method": "exit", "params": {}})
        finally:
            proc.stdin.close()
            proc.stdout.close()
            proc.wait(timeout=10)

    def test_two_messages_back_to_back(self) -> None:
        buf = io.BytesIO()
        write_message(buf, {"jsonrpc": "2.0", "id": 1, "result": 1})
        write_message(buf, {"jsonrpc": "2.0", "id": 2, "result": 2})
        buf.seek(0)
        first = read_message(buf)
        second = read_message(buf)
        assert first is not None and first["id"] == 1
        assert second is not None and second["id"] == 2
