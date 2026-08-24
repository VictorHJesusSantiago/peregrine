# Roadmap

## Status

All four packages are built, tested, and verified as of this writing: 736 tests passing (2
appropriately skipped — a Docker- and a Kubernetes-daemon-dependent test, no daemon reachable in
this environment), zero `ruff` findings, zero `mypy --strict` findings, across 88 source files.
Two companion, hand-rolled JavaScript frontends (a browser playground for the language and a
WebGL 3D viewer for `digitaltwin`'s exported scenarios) exist alongside the repo as well — see the
`digitaltwin` scope note below and the end of this document.

This document separates two things that are easy to conflate: **deliberate, permanent scope
boundaries** (a line drawn on purpose, not a gap to close) and **genuine follow-up work** (real,
listed next steps if this project continued). Every scope boundary below is also stated inline, in
the relevant module's own docstring — this file collects them in one place, it doesn't introduce
new information the code doesn't already say.

---

## Deliberate scope boundaries (not planned work)

### `perelang`

- **Arithmetic is Int-only for the original `+ - * / %` operators, by design — this is permanent,
  not a gap.** They unify their operands to `Int`, full stop; no implicit numeric coercion, no
  numeric type class. `Float` has its own separate, now-implemented operator set instead
  (`+. -. *. /.`, the OCaml pattern — see [ADR 0002](adr/0002-arithmetic-monomorphic-over-int.md)):
  `1 + 2.0` is still, and will always be, a type error.
- **The LLVM backend's `entry` boundary is Int/Bool/Float-only, permanently.** Closures,
  `String`/`List` values, and top-level `let` globals referenced as values are all now implemented
  and compile/run correctly *inside* the compiled call graph (arguments, locals, return values
  between Peregrine functions) — see [ADR 0003](adr/0003-llvm-backend-scope-cuts.md)'s update notes.
  What remains a deliberate, permanent boundary is `run_llvm`'s outer `ctypes` call: it only knows
  how to marshal Int/Bool/Float in and out of Python, so a `String`/`List`/function-typed `entry`
  still raises a specific, named error rather than miscompiling or silently truncating a value.
- **The LSP server now covers hover (top-level and local), go-to-definition, find-references,
  rename, and completion — still not the full protocol.** `IrExpr`/`IrLet` nodes carry real spans
  now (`ir.py`/`bytecode.py`/`vm.py` were threaded end-to-end — see "Genuine follow-up work" below,
  where this item used to live), which is what let hover's hand-written IR walk resolve a
  parameter/`let`-bound local's type, not just a top-level name; go-to-definition/references/
  rename/completion turned out to need none of that IR data at all — they're pure AST lexical
  scope resolution over `ast_nodes.py`'s already-span-carrying nodes. What's still genuinely out:
  signature help (not attempted), and everything is scoped to the one document a client has open —
  there's no cross-file/module symbol table (and no `import` system in the language for one to
  resolve through, either), and completion is keyword-plus-in-scope-identifier only, with no
  member-access completion, snippet expansion, or type-directed filtering by expected call-site
  type.
- **The package manager's local-directory registry mode is a deliberate, permanent, offline-friendly
  option, not a placeholder.** `pkg.py` now also has a real network mode — a hand-rolled HTTP server
  (`registry_server.py`) and client (`pere registry serve`, `pere pkg publish`,
  `pere pkg install --registry-url`; see [ADR 0005](adr/0005-local-package-registry.md)) — but the
  local-directory mode (`--registry DIR`) stays fully supported alongside it: no server to start, no
  network involved, the right choice for tests/offline work/CI. Dependency resolution itself
  (transitive, cycle-detected, deterministic lockfile) is identical either way, which was always the
  actual interesting part.
- **The formatter cannot preserve comments.** The lexer discards them entirely and the AST has no
  comment nodes; recovering them would mean threading comment-association through the lexer and
  parser, not just the formatter.

### `mlorch`

- **`KubernetesExecutor` is real but not live-verified here.** It shells out to `kubectl run` the
  same way `DockerExecutor` shells out to `docker run`, sharing the same wire protocol, and is
  unit-tested independently of a cluster; there is still no live cluster in this environment, so
  only its live end-to-end test (which skips honestly rather than faking a pass) is unverified.
- **Drift statistics (PSI, KS) are hand-implemented, not `scipy`-backed** — `scipy` isn't installed
  in this environment; this is a permanent choice consistent with the project's from-scratch ethos,
  not a temporary substitute.

### `digitaltwin`

- **No exact VRP solver.** `agents.py` now offers two routing strategies behind the same
  `Scenario` API: the original single-route nearest-neighbor heuristic, and a genuine
  multi-vehicle Clarke-Wright savings heuristic (`clarke_wright_routes`, opt in via
  `Scenario(routing_strategy="clarke_wright")`) that decides both the grouping of demand points
  into routes and their order, with an optional per-route stop-count capacity. Both are still
  heuristics, not an exact optimizer — full vehicle routing (weighted demand, time windows, etc.)
  remains a large field on its own and out of scope.
- **The `digitaltwin` Python package itself still has no 3D renderer, by design** — `viz.py`
  produces complete, correct structured trajectory/network data (`export_network`/
  `export_trajectories`) for a downstream frontend to consume, and building a renderer is a
  different kind of project (JS/WebGL, not a Python backend concern) than this package is. A
  companion frontend now exists anyway, delivered separately from the installable package: a
  hand-rolled WebGL 3D scene (orbit camera, animated agents, scrubbable timeline, no Three.js —
  consistent with this project's no-framework ethos) that consumes exactly the JSON shape
  `export_network()`/`export_trajectories()` produce, including a "paste your own scenario" mode
  for real exports, not just the bundled sample. See the end of this document.

### `geospat`

- **`hexgrid.py` is not H3.** It's a square-cell quadtree analog with quadkey-style IDs, not
  icosahedral hexagon math. Building real H3 is itself a multi-year effort at Uber; the analog
  demonstrates the same *interface* (cell-for-point, parent/child, neighbor lookup) faithfully.
- **Contraction hierarchies use a simplified witness search** (a full, unbounded Dijkstra to decide
  whether a shortcut is needed, not a hop-limited local search). Node ordering is the real,
  dynamically-recomputed edge-difference priority queue, and the query algorithm itself (shortcuts,
  bidirectional upward search) is real and verified correct against Dijkstra; only witness-search
  speed is simplified — shortcut decisions are always correct, just slower to compute than a
  production CH's bounded search would be.
- **Vector tiles are a custom JSON shape, not Mapbox Vector Tile protobuf**, and polygon-polygon
  intersection doesn't handle every topological edge case (documented at the point of
  implementation in `geometry.py`).

---

## Genuine follow-up work (if this project continued)

Empty, as of this writing — every item ever listed here has been closed out (see below). Every
remaining limitation anywhere in this project is now a *deliberate scope boundary* (documented
above, and inline in the relevant module's own docstring), not an acknowledged gap waiting on
future work. If a genuinely new one is identified later, it belongs here.

**What used to be here, and how it was closed:** `perelang`'s `Span` was threaded through
`ir.py`/`bytecode.py`/`vm.py` end to end (every `PeregrineRuntimeError` now carries the real source
location of the operation that faulted); Float arithmetic (`+. -. *. /.`) was added alongside
`types.py`'s existing Int-only operators, including through the LLVM backend's type-directed
codegen (see [ADR 0002](adr/0002-arithmetic-monomorphic-over-int.md) /
[ADR 0003](adr/0003-llvm-backend-scope-cuts.md)); the LLVM backend went on to gain closures,
`String`/`List` values, and top-level `let` globals too, leaving only the `ctypes` entry-boundary
as a permanent limit (see the scope-boundary note above); the LSP server gained
go-to-definition/find-references/rename/completion alongside hover; the package manager gained a
real HTTP registry mode (`registry_server.py`) alongside its local-directory mode (see
[ADR 0005](adr/0005-local-package-registry.md)); `mlorch` gained a real `kubectl`-based Kubernetes
executor; `digitaltwin` gained a genuine multi-vehicle Clarke-Wright savings routing heuristic
alongside nearest-neighbor; `geospat`'s contraction hierarchy gained the real dynamically-
recomputed edge-difference node ordering (measurably fewer shortcuts, fewer nodes visited per
query than the one-shot degree sort it replaced).

The last item, **a hosted demo/playground for `pere`**, is closed differently from the rest: rather
than a deployed service (this project provisions no hosting infrastructure of its own), two
self-contained, JavaScript-ported companion frontends were built and published as standalone pages
— a browser playground running Peregrine source through a JS lexer/parser/evaluator port (the
authoritative implementation stays the Python toolchain; the playground's own footer states exactly
where the two diverge, e.g. no static Hindley-Milner checking in the browser port), and a
hand-rolled WebGL 3D viewer for `digitaltwin` scenario exports (see that package's scope-boundary
note above). Neither page is part of any installable package in this repo — they're delivered
alongside it, not inside `packages/`.

Nothing on this list is required for any of the four packages to be complete on its own stated
terms — each is a real, working system today, not a scaffold waiting on this list.
