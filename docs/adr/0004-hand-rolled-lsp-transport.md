# ADR 0004: Hand-rolled JSON-RPC/stdio transport for the LSP server, not `pygls`

## Status

Accepted.

## Context

`perelang`'s `pyproject.toml` originally declared an `lsp = ["pygls>=1.3"]` optional-dependency
group, anticipating that the language server would be built on `pygls` (the standard Python LSP
framework — it handles JSON-RPC framing, request/notification dispatch, and the LSP type
definitions for you). `pygls` is not installed in this development environment, and installing new
dependencies mid-project was avoided throughout this repo's build (every package's agent-driven
work was explicitly constrained to already-available dependencies, to keep the environment
reproducible and to avoid quietly growing the dependency surface of a portfolio-style project).

Separately from the installation constraint, there's a house-style argument that applies regardless:
every other "framework-shaped" piece of this project was built by hand on purpose — the parser
instead of a parser-generator, the bytecode VM instead of an existing bytecode-interpreter
framework, the LLVM backend's codegen instead of a higher-level codegen library. An LSP server built
on `pygls` would be the one place in `perelang` where the interesting transport-and-protocol layer
was outsourced rather than demonstrated.

## Decision

Implement `lsp_server.py`'s JSON-RPC-over-stdio transport by hand: `Content-Length`-framed message
parsing/writing, a request/notification dispatch table, and the specific subset of the LSP message
types this server needs (`initialize`, `initialized`, `textDocument/didOpen`,
`textDocument/didChange`, `textDocument/didClose`, `textDocument/publishDiagnostics`,
`textDocument/hover`, `shutdown`, `exit`) as plain dicts, not a generated/imported protocol schema.
Remove the now-inaccurate `lsp` optional-dependency group from `pyproject.toml` rather than leaving
stale metadata pointing at a dependency the code no longer uses.

## Consequences

- Zero new dependencies for the LSP server — it works in any environment `perelang` itself works
  in, with no optional extra to install.
- The transport layer (`Content-Length` framing, JSON-RPC request/response/notification shapes) is
  verified by a real subprocess test that exchanges actual framed messages over real stdio, not
  just an in-process call into a testable core — proving the framing code itself, not only the
  message-handling logic behind it.
- The tradeoff: this server implements a genuine but narrow slice of the LSP specification
  (diagnostics + a scoped hover), not the full protocol `pygls` would make easier to grow into
  (completion, code actions, rename, workspace symbols, ...). That gap is listed explicitly in
  `docs/ROADMAP.md`, not implied away by pointing at `pygls` as if it were already wired up.
- If this project's LSP needs grew substantially beyond diagnostics/hover, revisiting this decision
  (adopting `pygls` once its dependency is actually justified by real added surface) would be
  reasonable — this ADR records why the current, narrower scope didn't need it yet, not a
  permanent rejection of the library.
