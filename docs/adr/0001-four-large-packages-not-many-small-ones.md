# ADR 0001: Four large packages, not many small ones

## Status

Accepted.

## Context

This monorepo needed a top-level structure before any code was written. A prior TypeScript
monorepo in this same environment used the opposite shape: ~22 small `packages/*` directories, one
per cohesive concern (a parser, a planner, a storage engine, a UI component library), each
independently versioned and depended-upon via workspace references.

Python's packaging tooling doesn't reward that shape the same way. There's no equivalent of a fast,
native workspace-linking package manager that npm/pnpm provide; `pip install -e` across dozens of
interdependent local packages is workable but adds real friction (each needs its own
`pyproject.toml`, its own `src/` layout, and explicit cross-package dependency declarations) for a
benefit — independent versioning and publishing of, say, "the lexer" separately from "the parser"
— that this project will never actually need. `perelang`'s lexer is never going to be published or
versioned separately from its parser.

## Decision

Structure this repo as four large, cohesive packages — one per "flagship system" — each internally
organized as a flat `src/<name>/*.py` module tree, not as a nested tree of sub-packages. Within
`perelang`, for instance, `lexer.py`, `parser.py`, `types.py`, `ir.py`, `bytecode.py`, `vm.py`, and
the toolchain modules are all siblings in one flat directory, not four nested packages.

Shared tooling config (`pytest`, `ruff`, `mypy`) lives once, in the root `pyproject.toml`, with each
package's `src` directory added to `ruff.src` / `mypy_path` — one source of truth for lint/type
rules across all four packages, rather than four copies that could drift.

## Consequences

- Adding a new module inside a package is just adding a file — no new `pyproject.toml`, no new
  workspace reference to declare.
- Cross-module imports within a package are plain `from perelang.foo import bar` — no package
  boundary to cross, no circular-workspace-dependency risk.
- The tradeoff: a module inside `perelang` cannot be `pip install`ed or versioned independently of
  the rest of `perelang`. This was never a requirement — nothing about the "the toolchain is what
  multiplies files and credibility" goal this project scoped itself around depends on independent
  sub-package publishing.
- Four packages, not one single package for the whole repo, because the four systems genuinely
  don't share code or a domain — `geospat` importing from `mlorch` would be a smell, not a
  convenience. The boundary is drawn at "does this code have any reason to know about the other
  system," and the answer is no in every pair.
