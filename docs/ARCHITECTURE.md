# Architecture

## Monorepo shape

Four independently `pip install -e`-able packages under `packages/`, sharing one root
`pyproject.toml` for `pytest`/`ruff`/`mypy` configuration (`testpaths=["packages"]`,
`ruff.select=["E","F","I","UP","B","SIM","N"]`, `mypy --strict` with `mypy_path` pointing at all
four `src/` trees). This is a Python-idiomatic choice, deliberately different from a
many-small-packages layout: four large, cohesive packages rather than dozens of thin ones, matching
how the Python packaging ecosystem (setuptools `src`-layout, single-package installs) actually
expects to be used. See [ADR 0001](adr/0001-four-large-packages-not-many-small-ones.md).

Every package follows the same house style regardless of domain: hand-written core algorithms (no
parser-generator, no orchestration framework, no GIS library doing the actual spatial math),
frozen dataclasses for immutable values, plain classes for genuinely mutable state, `match`
statements for dispatch over union types, `mypy --strict` and `ruff` clean throughout, and narrow
exception hierarchies (one class per failure category, always ending in `Error`, never shadowing a
Python builtin — `TypeCheckError` not `TypeError`, `PeregrineRuntimeError` not `RuntimeError`).

---

## `perelang` — the language

### Pipeline

```
source text
  -> lexer.py       tokenize()          -> list[Token]
  -> parser.py       parse_program()     -> ast_nodes.Program
  -> ir.py            lower_program()     -> ir.IrProgram   (runs Algorithm W as part of lowering)
  -> bytecode.py      compile_program()   -> bytecode.BytecodeProgram
  -> vm.py             VM.run_program()    -> a runtime Value
```

with `llvm_backend.py` as an alternative to the last two stages: `ir.IrProgram -> LLVM IR -> MCJIT
-> machine code`, for the subset of the language it covers (see below).

**Lexer** (`lexer.py`, `tokens.py`): hand-written single-pass tokenizer, line/column tracking,
`//` comments, escaped string literals, int/float literals. No comment-preserving token kind —
comments are fully discarded at this stage (the docs generator recovers them separately via its
own raw-source line scan, precisely because the lexer doesn't carry them forward).

**Parser** (`parser.py`): recursive descent with precedence climbing for the full binary-operator
chain (`||` → `&&` → equality → comparison → additive → multiplicative → unary → call/primary).
Every AST node (`ast_nodes.py`) is a frozen dataclass carrying a `Span`; `Expr` and `Decl` are
`Union` type aliases rather than a sealed class hierarchy — combined with `match` and a final
`case _: assert_never(...)` at every consumer, this gets the same "add a variant, mypy flags every
unhandled match" property a real discriminated union gives, checked by `mypy --strict`, not just
convention.

**Type checker** (`types.py`): a genuine Hindley-Milner implementation — Algorithm W, with
`Substitution`, `unify` (with an occurs-check), `TypeScheme`/`generalize`/`instantiate` for
let-polymorphism, and letrec-style recursion (monomorphic within a function's own body, fully
polymorphic at every other use site — provable directly: see `test_types.py`'s
`TestLetPolymorphism`, which generalizes `id` and uses it at two different types in one program).
**Stated scope limit, not hidden**: arithmetic operators (`+ - * / %`) are typed monomorphically
over `Int` only — no implicit numeric coercion, ever. `Float` gets its own separate operator set
instead (`+. -. *. /.`, `Inferencer._FLOAT_ARITHMETIC`), the same line early ML dialects (OCaml,
with its separate `+.`) draw. See [ADR 0002](adr/0002-arithmetic-monomorphic-over-int.md).

**IR** (`ir.py`): a second, genuinely distinct representation from the surface AST — every
expression node carries its *resolved* `Type` (the AST has none; a node's `.ty` is what Algorithm W
inferred, computed by an `_Lowerer` that structurally mirrors `Inferencer`'s own traversal), name
resolution is finished (`IrName.is_global` distinguishes a global lookup from a local/capture slot,
decided once here rather than re-derived at every downstream phase), and closures are made explicit
(`IrClosure.captures` is the exact free-variable list a lambda's body reads, computed once via
free-variable analysis). Capture is always by value — sound because this language has no
reassignment (`let` only ever introduces a fresh binding). Every IR node also carries the source
`Span` of the AST node it was lowered from, threaded mechanically through to `bytecode.py`'s
compiled `Instr`s and from there into every runtime-fault `PeregrineRuntimeError` `vm.py` raises.

**Bytecode + VM** (`bytecode.py`, `vm.py`): a small stack-machine instruction set (arithmetic,
comparisons, jumps for `if` and short-circuit `&&`/`||`, `CALL`, `MAKE_CLOSURE`) and a
straightforward stack-based interpreter. Runtime values are native Python types
(`int | float | bool | str | tuple | Closure | Unit`) rather than a hand-rolled tagged union —
every Peregrine type already has an exact native counterpart, so a wrapper would cost an allocation
per operation for zero extra information. Verified correct on real recursion (factorial, Fibonacci,
deep enough to prove it isn't accidentally memoized) and closures (independently-capturing
closures from the same factory function). Every compiled `Instr` carries a real source `Span` (set
via `_FunctionCompiler._current_span`, one per IR node visited), so a `PeregrineRuntimeError` —
division by zero, wrong arity, a non-Bool `if` condition, ... — points at the real operation that
faulted rather than a placeholder.

**LLVM backend** (`llvm_backend.py`): `llvmlite`-based JIT compilation of the same IR to real LLVM
IR, executed via MCJIT and called back into Python through `ctypes`. Covers Int/Bool/Float
arithmetic, comparisons, `if`/`else`, function calls including recursion, closures (a heap-allocated
`{ fn_ptr, env_ptr }` struct shared by capturing lambdas and top-level functions used as values),
`String`/`List` values (plain 2-word-struct values, no GC needed since neither type has a mutating
operator), and top-level `let` globals referenced as values (initialized once by a synthesized
module-constructor function wired into the standard `@llvm.global_ctors` mechanism, mirroring
`vm.py`'s own `<init>`-chunk semantics) — verified with the same factorial/Fibonacci tests the
bytecode VM uses, producing identical results, plus recursive Float-typed and closure-returning
functions, String/List values passed between functions, and top-level globals (including one
initialized from a real function call) all cross-checked against `vm.py`. Codegen is type-directed
(`_llvm_type_for` maps `Int`/`Bool` -> `i64`, `Float` -> LLVM `double`, `String`/`List<T>` -> a
2-word struct, a function type -> a pointer to the closure struct; every binary/unary/`if` codegen
path branches on the operand's resolved type to pick the matching instruction family). The one
remaining boundary: the `ctypes` call at the outer `entry` boundary only knows how to marshal
Int/Bool/Float, so a `String`/`List`/function-typed `entry` still raises a specific
`LlvmBackendUnsupportedError` rather than silently miscompiling, even though such values compile and
run correctly *inside* the compiled call graph. See [ADR 0003](adr/0003-llvm-backend-scope-cuts.md).

**REPL** (`repl.py`): a testable core (`Repl.submit`/`run_lines`, no I/O) separated from a thin
`main()` doing real `input()`/`print()` — the same split used throughout this codebase to keep
interactive/stdio-bound code testable. Multi-line input is accumulated by bracket-depth balancing.
Each submitted chunk re-runs the *entire* session's accumulated source through the full pipeline
rather than incrementally extending one VM's state — correct specifically because this language has
no observable side effects beyond a returned value, so re-evaluating an already-accepted `let` can
never produce a different result.

**Toolchain** (`formatter.py`, `linter.py`, `lsp_server.py`, `pkg.py`, `registry_server.py`,
`docs_gen.py`):
- *Formatter*: an AST-driven pretty-printer that re-derives required parentheses (the parser
  discards grouping info once it's built the tree), idempotent, matching the 2-space/brace-on-line
  convention used throughout `examples/*.pgr`.
- *Linter*: unused `let` bindings, unused parameters, identical if/else branches, shadowing —
  each with both a triggering test and a false-positive-avoidance test.
- *LSP server*: hand-written JSON-RPC-over-stdio (`Content-Length`-framed), not built on `pygls` —
  see [ADR 0004](adr/0004-hand-rolled-lsp-transport.md). Diagnostics from lex/parse/type errors and
  linter findings; `textDocument/hover` for both top-level declarations (via `types.infer_program`)
  and locals (via `ir.py`'s now-span-carrying IR); `textDocument/definition`/`references`/`rename`/
  `completion`, all pure AST lexical scope resolution (`_locate_in_program`) needing no type
  information at all, with shadowing handled by walking the same enclosing-scope chain
  `linter.py`'s own shadowing check reasons about. Scoped to one open document at a time — no
  cross-file symbol resolution, no signature help.
- *Package manager*: a real dependency resolver (DFS, cycle detection, diamond dependencies,
  deterministic lockfile) over a manifest format, with two interchangeable registry modes behind
  it — a local directory (`--registry DIR`, no network) or a real HTTP registry server
  (`registry_server.py`, hand-rolled on `http.server` the same way `lsp_server.py` hand-rolls its
  own transport; `--registry-url URL`) — see [ADR 0005](adr/0005-local-package-registry.md). Only
  the resolver's registry-lookup branch knows which mode is active; graph traversal, cycle
  detection, and the lockfile format are identical either way.
- *Docs generator*: extracts every top-level declaration's inferred signature plus any immediately
  preceding `//`-comment run, emitting Markdown.

All wired into one `pere` CLI (`cli.py`, built on `typer`): `run`, `check`, `repl`, `fmt`, `lint`,
`docs`, `lsp`, `pkg init`/`add`/`install`/`publish`, `registry serve`.

**Companion browser playground.** A standalone page — not part of this package, no Python involved
— runs a hand-written JavaScript port of the lexer, recursive-descent parser, and a tree-walking
evaluator, so a visitor can run Peregrine source in-browser without installing anything. It
deliberately does not port the Hindley-Milner type checker (a much larger undertaking to reimplement
faithfully in JS) — the page's own footer states this plainly, and the Python toolchain remains the
authoritative implementation for anything the two disagree on.

---

## `mlorch` — ML pipeline orchestrator

- `dag.py`: `Task`/`DAG`. Graph storage, topological sort, and cycle detection delegate to
  `networkx` (a reasonable use of an already-available graph library); the *scheduling policy* —
  parallel-eligible grouping via longest-path levels, failure blocking only transitive
  downstream — is hand-written.
- `executors.py`: an `Executor` protocol; `LocalExecutor` (real `ThreadPoolExecutor` parallelism),
  `DockerExecutor` (genuine `docker run`-over-stdin wire protocol, refusing loudly rather than
  no-opping when no daemon is reachable), and `KubernetesExecutor` (the same wire protocol over
  `kubectl run --rm -i`, refusing loudly when no cluster is reachable through the current context).
- `scheduler.py`: level-by-level execution, per-task retries, failure containment.
- `versioning.py`: SHA-256 content-addressed dataset storage with a JSON manifest, deduping both
  on-disk objects and consecutive-duplicate history entries.
- `features.py`: offline (pandas-backed, entity+timestamp keyed) and online (in-memory dict)
  feature stores, with a `materialize()` step between them.
- `registry.py`: model versioning with metadata (params, metrics, dataset lineage) and stage
  promotion.
- `drift.py`: hand-implemented PSI and a two-sample KS statistic (no `scipy` in this environment,
  confirmed before deciding to hand-roll rather than depend on it).
- `backfill.py`: incremental partition computation, reusing `versioning.py`'s own history as the
  "already done" manifest.

`KubernetesExecutor` is built and unit-tested the same way `DockerExecutor` is; there is still no
live cluster in this environment, so only its live end-to-end test is unverified (it skips
honestly rather than faking a pass).

## `digitaltwin` — urban-logistics simulation

- `network.py`: a `networkx`-backed road graph with two seeded, deterministic synthetic generators
  (grid and random-geometric) — no dependency on real map data, so tests are fully self-contained.
- `events.py`: a `heapq`-backed event queue and simulation clock — the load-bearing piece; movement
  is event-driven, not fixed-timestep polling.
- `kinematics.py`: closed-form trapezoidal/triangular constant-acceleration motion per road
  segment, cross-checked against hand-computed values in tests.
- `agents.py`: route planning over the road network (shortest path via `networkx`'s Dijkstra), with
  two routing heuristics: single-route nearest-neighbor ordering, and a multi-vehicle Clarke-Wright
  savings heuristic (`clarke_wright_routes`, with an optional per-route stop-count capacity) that
  decides both the grouping of demand points into routes and their order — a genuine, if still
  heuristic, VRP solver, not an exact optimizer.
- `calibration.py`: golden-section search (no `scipy` available) recovering a known-correct speed
  parameter from synthetic observed data.
- `scenario.py`: the public API — `Scenario` + `run_scenario()`, with `Scenario.routing_strategy`
  selecting between the two `agents.py` heuristics above.
- `viz.py`: structured trajectory/network export (JSON-serializable, plus a `pandas.DataFrame`
  form) for a downstream visualization frontend, with an optional `matplotlib` 2D plot as a bonus.
  A companion hand-rolled WebGL 3D viewer (orbit camera, animated agents, scrubbable timeline, no
  Three.js) consumes exactly this module's `export_network()`/`export_trajectories()` JSON shape —
  delivered as a standalone page alongside the repo, not inside this package.

## `geospat` — geospatial analysis engine

- `rtree.py`: a hand-written R-tree with Guttman quadratic-split node overflow handling, verified
  against brute-force range queries on randomized data.
- `hexgrid.py`: **not real H3** — a quadtree over an equirectangular lon/lat plane with
  quadkey-style cell IDs, explicitly documented as a simplified stand-in, not hexagonal cell math.
- `tiles.py`: slippy `z/x/y` vector tile generation (Liang-Barsky line clipping, Sutherland-Hodgman
  polygon clipping) into custom JSON-shaped tile dataclasses, not Mapbox Vector Tile protobuf.
- `raster.py`: `numpy`-backed rasters with georeferencing metadata, resampling, and real analytical
  operations (slope/aspect, focal mean) checked against hand-computed values.
- `routing.py`: a real Dijkstra baseline plus a contraction-hierarchy implementation — node ordering
  is the genuine, dynamically-recomputed edge-difference priority queue, and the bidirectional
  upward query and shortcut creation are the genuine CH algorithm too; only witness search (a full,
  unbounded Dijkstra rather than a hop-limited local one) is simplified. Verified via exact distance
  agreement with Dijkstra across many randomized queries, and via a direct shortcut-count/query-size
  comparison against the one-shot degree-sort ordering this replaced (`test_routing.py`).
- `query.py`: a fluent builder-pattern spatial query API (`Query(index).within(bbox)...execute()`),
  not a separately parsed textual DSL.

See each package's module docstrings for the full, precise statement of what's faithful vs.
simplified — this document summarizes; the code is the source of truth.
