# Peregrine

A monorepo of four independent Python systems, each built from scratch with no framework doing
the load-bearing work: a programming language with a full toolchain, an ML pipeline orchestrator,
an urban-logistics simulation engine, and a geospatial analysis engine.

736 tests passing (2 appropriately skipped — a Docker- and a Kubernetes-daemon-dependent test, no
daemon reachable in this environment), zero `ruff` findings, zero `mypy --strict` findings, across
88 source files and four `pip install -e`-able packages under `packages/`. Two companion,
hand-rolled JavaScript frontends — a browser playground for the language and a WebGL 3D scenario
viewer for `digitaltwin` — are published as standalone pages alongside the repo (see below).

## Why these four

Each package demonstrates a different kind of systems work end-to-end rather than gluing together
existing libraries:

| Package | What it is | What's hand-built |
|---|---|---|
| [`perelang`](packages/perelang) | A programming language | Lexer, recursive-descent parser, Hindley-Milner type inference (Algorithm W), a lowering IR, a stack-based bytecode VM, an LLVM JIT backend, a REPL, a formatter, a linter, an LSP server, a package manager, a docs generator |
| [`mlorch`](packages/mlorch) | An ML pipeline orchestrator | A DAG scheduler (not Airflow), pluggable executors (local/Docker/Kubernetes), content-addressed dataset versioning, an online/offline feature store, a model registry, drift detection, incremental backfill |
| [`digitaltwin`](packages/digitaltwin) | An urban-logistics simulation | A discrete-event engine (`heapq`-backed, not fixed-timestep polling), closed-form vehicle kinematics, two routing heuristics (nearest-neighbor and multi-vehicle Clarke-Wright), golden-section parameter calibration |
| [`geospat`](packages/geospat) | A geospatial analysis engine | An R-tree, a hierarchical spatial grid, vector tile generation, raster processing, contraction-hierarchy shortest-path routing |

`perelang` is the flagship: it's the one where the toolchain around the core compiler (REPL,
formatter, linter, LSP, package manager, docs generator) is itself most of the engineering effort,
not an afterthought.

## Quickstart

```bash
# From the repo root — installs all four packages in editable mode.
python -m pip install -e packages/perelang -e packages/mlorch -e packages/digitaltwin -e packages/geospat

# Run everything.
python -m pytest packages
python -m ruff check packages
python -m mypy --strict packages
```

### Peregrine, the language

```bash
pere run packages/perelang/examples/fibonacci.pgr        # bytecode VM
pere run packages/perelang/examples/fibonacci.pgr --backend llvm   # LLVM JIT
pere repl                                                  # interactive
pere check some_file.pgr                                   # type-check only
pere fmt some_file.pgr                                      # reformat
pere lint some_file.pgr                                     # static checks
pere docs packages/perelang/examples -o api.md              # generate docs
pere lsp                                                    # language server, stdio — hover, go-to-
                                                              # definition, references, rename, completion
pere pkg publish --registry-url http://localhost:8000       # publish to a running registry
pere registry serve                                          # run a real HTTP package registry
```

The language itself is documented inline via each module's docstring; start at
[`packages/perelang/src/perelang/parser.py`](packages/perelang/src/perelang/parser.py) for the
grammar, or [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the compiler pipeline as a whole.

### The other three

Each is a library, not a CLI — import it and use its public API:

```python
from mlorch.scheduler import Scheduler
from digitaltwin.scenario import Scenario, run_scenario
from geospat.rtree import RTree
```

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for each package's module layout and design,
and each package's `tests/` directory for concrete usage examples — every public entry point is
exercised there.

## Companion frontends

Two standalone pages exist alongside the repo, not inside any installable package:

- **A browser playground** for the language — a hand-written JavaScript port of the lexer, parser,
  and a tree-walking evaluator, so anyone can run Peregrine source without installing anything.
  It deliberately skips porting the Hindley-Milner type checker (a separate, larger undertaking);
  its own footer states exactly where it diverges from the real toolchain, which stays
  authoritative.
- **A WebGL 3D scenario viewer** for `digitaltwin` — an orbit-camera scene (no Three.js) that
  animates delivery agents along their routes over the road network, scrubbable by simulated time,
  consuming exactly the JSON shape `digitaltwin.viz.export_network()`/`export_trajectories()`
  produce (with a "paste your own scenario" mode, not just the bundled sample).

## Repo layout

```
peregrine/
  pyproject.toml          # shared pytest/ruff/mypy config for all four packages
  packages/
    perelang/              # the flagship: language + toolchain
      src/perelang/
      tests/
      examples/            # runnable .pgr programs
      syntax/              # TextMate grammar for editor highlighting
    mlorch/                 # ML pipeline orchestrator
    digitaltwin/            # urban-logistics simulation
    geospat/                 # geospatial analysis engine
  docs/
    ARCHITECTURE.md
    ROADMAP.md
    adr/                    # architecture decision records
```

## Honesty about scope

Every package states its own scope cuts in its module docstrings and in
[`docs/ROADMAP.md`](docs/ROADMAP.md), and that file is kept current rather than aspirational — most
of what originally shipped there as "genuine follow-up work" has since been built and moved into
"deliberate scope boundaries" instead: `perelang` grew a second, `Float`-typed operator set
(`+. -. *. /.`), real source spans threaded end-to-end so runtime errors point at the exact
faulting operation, an LLVM backend that now JIT-compiles closures and `String`/`List` values (heap
struct + malloc-and-leak, no GC — stated plainly, not hidden) alongside its original Int/Bool/Float
support, an LSP server with real go-to-definition/find-references/rename/completion alongside
hover, and a real HTTP package registry (`pere registry serve`) alongside the original
local-directory mode; `mlorch` grew a genuine `kubectl`-based Kubernetes executor; `digitaltwin`
grew a real multi-vehicle Clarke-Wright savings heuristic alongside its original single-route
nearest-neighbor one; `geospat`'s contraction hierarchy now uses the real dynamically-recomputed
edge-difference node ordering instead of a one-shot degree sort (measurably fewer shortcuts, fewer
nodes visited per query — see `docs/ROADMAP.md`).

The LLVM backend went on to gain closures, `String`/`List` values, and top-level `let` globals too
(a synthesized module constructor wired into `@llvm.global_ctors`, mirroring the bytecode VM's own
`<init>`-chunk semantics) — the one thing that remains a permanent, structural limit there is the
`ctypes` boundary at the JIT's outer `entry` point, which only knows how to marshal Int/Bool/Float
across the Python/machine-code line; a `String`/`List`/function-typed `entry` still raises a named
error rather than silently truncating a value. And the hosted `pere` playground originally deferred
for needing external hosting turned out not to: the two companion frontends above are published,
self-contained pages, no deploy target or domain of this project's own required.

What's left, stated plainly rather than hidden behind confident-sounding names: a simplified H3
analog instead of true hexagonal cell math (`geospat`), a hop-limited-vs-unbounded simplification
in that same contraction hierarchy's witness search, and a golden-section search instead of a
general optimizer (`digitaltwin`'s calibration) — each a deliberate, permanent line, not a gap
still waiting on effort.
