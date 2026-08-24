# ADR 0005: A local directory stands in for a package registry

## Status

Accepted.

## Context

`perelang`'s package manager (`pkg.py`) needed a way for a dependency in `peregrine.toml` to
resolve to something installable. Real package managers (npm, cargo, pip itself) resolve most
dependencies against a network-hosted registry — a service this project has no reason to build or
operate. `perelang` is a language built for this repo, with no existing package ecosystem or user
base publishing to any registry, real or otherwise.

Two dishonest paths were available and rejected outright: pretending to talk to a network registry
with a fake HTTP client that only ever hits `localhost` or a bundled fixture (implying network
capability that doesn't exist), or silently only supporting path dependencies while the CLI help
text and manifest format implied full registry support.

## Decision

`pkg.py` supports two kinds of dependency: a `PathDependency` (a local filesystem path, resolved
directly) and a `RegistryDependency` (a name + version, resolved by looking it up in a configurable
local directory passed via `--registry`). That local directory is explicitly documented, in the
module docstring and in the CLI's own `--registry` help text, as "a local directory standing in for
a package registry" — not presented as if it were network-backed. The parts of the system that are
genuinely real and worth demonstrating — dependency graph resolution (DFS traversal), cycle
detection, correct handling of diamond dependencies (two packages depending on the same third
package without duplicate installation or conflict), and a deterministic, reproducible lockfile
(installing twice from the same manifest produces byte-identical lockfile output) — are fully
real and fully tested, independent of where a resolved package's bytes physically come from.

## Consequences

- `pere pkg install` is a genuinely working command against real local fixtures — the test suite
  (`test_pkg.py`) builds real multi-level transitive dependency graphs and a real cycle, and asserts
  correct resolution/rejection.
- The one deliberately-marked extension point, if a real registry were ever wanted, is the
  registry-lookup branch of the resolver in `pkg.py` — swapping a local-directory lookup for an
  HTTP call is a contained, well-isolated change specifically because the rest of the resolver
  (graph traversal, cycle detection, lockfile writing) never assumed anything about *how* a
  registry dependency's location was determined.
- This is listed as a deliberate, permanent scope boundary in `docs/ROADMAP.md`, not a temporary
  stand-in awaiting a "real" implementation as part of this project's own remaining work — building
  and operating an actual network registry was never in scope for a language built for one
  repository.

**Update, implemented:** a real network registry now exists — `registry_server.py`, a hand-rolled
HTTP server (`http.server.BaseHTTPRequestHandler`/`ThreadingHTTPServer`, stdlib only, matching
`lsp_server.py`'s precedent of a hand-written protocol layer over a framework) exposing
`GET /packages/<name>/<version>` (metadata), `GET /packages/<name>/<version>/archive` (the package
as a zip, via `pkg.build_archive`), `PUT /packages/<name>/<version>` (publish; rejects an
already-published exact version with `409` — versions are immutable), and
`GET /packages/<name>` (list published versions). `pkg.py`'s `resolve_dependencies` gained a
`registry_url` parameter (alongside, not replacing, `registry_dir`) that talks to a running server
over real `urllib.request` HTTP calls, exactly through the registry-lookup branch this ADR's
"Decision" section predicted would be the only necessary change — cycle detection, diamond
handling, and the lockfile format were untouched. `pkg.publish` is the write path. Wired into the
CLI as `pere registry serve` and `pere pkg publish`/`pere pkg install --registry-url`.
Local-directory mode (`--registry DIR`) remains fully supported, unchanged, and is still the
better choice for tests, offline work, and CI — this was additive, not a replacement.
