"""A package manager for Peregrine, with two real, fully-supported registry modes.

**Scope, stated plainly, up front.** This language has no real ecosystem and nothing resembling a
public package index — building a fake npm/PyPI clone (hosted infrastructure, accounts, a public
index) would be pure theater with nothing real behind it. What's built instead is the part of a
package manager that's genuinely load-bearing regardless of what a "registry" turns out to be: a
manifest format, a dependency *graph resolver* with cycle detection, and a reproducible lockfile —
plus, now, two genuinely different ways for a `RegistryDependency` to actually be resolved:

- **Local-directory mode** (`--registry DIR`, laid out as `DIR/<name>/<version>/`, each holding its
  own `peregrine.toml`): a configurable local directory standing in for a registry, with no network
  involved at all. This remains a fully supported mode, not a deprecated fallback — it's genuinely
  useful for tests, offline work, and CI, where spinning up a server is pure overhead.
- **Network mode** (`--registry-url URL`, e.g. `http://127.0.0.1:8765`): a real HTTP client (stdlib
  `urllib.request`, no new dependency) that talks to an actual `registry_server.py` server over the
  network (loopback in this repo's tests, but genuinely HTTP request/response — not a disguised
  filesystem read) — see that module for the server side and its wire format. Fetched packages are
  cached under `<root_dir>/.pere_registry_cache/<name>/<version>/` (see `REGISTRY_CACHE_DIRNAME`)
  so a repeat resolution of an unchanged manifest doesn't repeat the round-trip; this is safe
  because a published `name@version` is immutable on the server (see `registry_server.py`'s module
  docstring), so a cache hit can never be stale.

Both modes were, from the start, exactly the isolated extension point the previous version of this
docstring predicted: `resolve_dependencies`'s registry-lookup branch is the only place that knows
*how* a `RegistryDependency` gets turned into a directory on disk. The graph algorithm, cycle
detection, and lockfile format below are completely unaware of, and unchanged by, which mode is in
use.

**Manifest** (`peregrine.toml`, read with the stdlib's `tomllib` — read-only, so writing one back
out is hand-rolled string templating in `render_manifest` rather than pulling in a TOML-writing
dependency this project doesn't otherwise need)::

    [package]
    name = "my_pkg"
    version = "0.1.0"

    [dependencies]
    left_pad = { path = "../left_pad" }
    json = { registry = "json", version = "2.0.0" }

A dependency is either a `path` (resolved relative to the directory holding the manifest that
declares it — so a transitive path dependency's own path is relative to *its* declaring package,
not the root) or a `registry` name + `version` (looked up in the configured registry, whichever
mode is in use).

**Resolution** (`resolve_dependencies`) walks the graph depth-first from the root manifest,
recording each dependency's resolved directory once (a diamond — two packages both depending on the
same third package — resolves to one entry, *as long as* every edge into it points at the exact
same directory; a genuine version conflict is reported as an error rather than silently picking
one, since there's no version-solver here to pick correctly). A repeated name already on the
current path back to the root is a cycle, reported with the full chain.

**Install** (`install`) resolves the graph, copies each resolved package's directory into
`<root>/pere_modules/<name>/`, and writes `<root>/peregrine.lock` — an array-of-tables TOML
document (valid TOML, though nothing here re-parses it; recorded for a human or a future tool to
read) listing every resolved package's name, version, exact source, and direct dependency names.
Resolution is a pure function of the manifest tree on disk (plus, in network mode, whatever is
already published and cached), so installing twice from an unchanged manifest produces
byte-identical lockfile content — there's no hidden clock or random ID in the loop to make two runs
disagree.

**Publish** (`publish`) is network mode's write path: it packages a directory's manifest and source
files into the same archive format `registry_server.py`'s `GET .../archive` endpoint returns (see
`build_archive`/`extract_archive`, shared by both the client here and the server) and `PUT`s it to
the registry, reading the package's own `name`/`version` from its manifest. A registry rejects
publishing over an already-published exact `name@version` — versions are immutable by design (see
`registry_server.py`) — surfaced here as a `PkgError` with the server's own error message included.
"""

from __future__ import annotations

import io
import json
import shutil
import tomllib
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MANIFEST_FILENAME = "peregrine.toml"
LOCKFILE_FILENAME = "peregrine.lock"
MODULES_DIRNAME = "pere_modules"
REGISTRY_CACHE_DIRNAME = ".pere_registry_cache"  # network-mode fetch cache, under the installing
# project's own root_dir — deliberately *not* under pere_modules/, since install() rmtree's and
# recreates that directory on every run; living outside it means a cached fetch survives reinstalls.


class PkgError(Exception):
    """Not a `PeregrineError` (see `errors.py`): every `PeregrineError` carries a `Span` into one
    Peregrine *source file*, but a package-manager failure (a missing manifest, a dependency
    cycle, an unresolvable registry entry) is about the filesystem and a dependency graph, not a
    position in a `.pgr` file — there's nothing to attach a `Span` to. Still named to end in
    `Error` and not shadow a builtin, matching every other exception in this codebase."""


# ---- manifest ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PathDependency:
    path: str  # relative to the directory holding the manifest that declares it, not the root


@dataclass(frozen=True, slots=True)
class RegistryDependency:
    name: str
    version: str


Dependency = PathDependency | RegistryDependency


@dataclass(frozen=True, slots=True)
class Manifest:
    name: str
    version: str
    dependencies: dict[str, Dependency] = field(default_factory=dict)


def parse_manifest(text: str) -> Manifest:
    data = tomllib.loads(text)
    try:
        package = data["package"]
        name = package["name"]
        version = package["version"]
    except KeyError as exc:
        raise PkgError(f"manifest is missing a required [package] field: {exc}") from exc

    deps: dict[str, Dependency] = {}
    for dep_name, spec in data.get("dependencies", {}).items():
        if not isinstance(spec, dict):
            raise PkgError(f"dependency {dep_name!r} must be a table, got {type(spec).__name__}")
        if "path" in spec:
            deps[dep_name] = PathDependency(spec["path"])
        elif "version" in spec:
            deps[dep_name] = RegistryDependency(spec.get("registry", dep_name), spec["version"])
        else:
            raise PkgError(f"dependency {dep_name!r} needs either a 'path' or a 'version'")
    return Manifest(name, version, deps)


def _toml_string(value: str) -> str:
    """Minimal TOML basic-string escaping (backslash and double-quote — the only two characters
    that can appear in a real path/name/version and would otherwise produce invalid TOML,
    notably a Windows-style path like `..\\dep`, which `tomllib` would reject as an "unescaped
    backslash" if written verbatim)."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def render_manifest(manifest: Manifest) -> str:
    lines = ["[package]", f'name = "{_toml_string(manifest.name)}"', f'version = "{_toml_string(manifest.version)}"']
    if manifest.dependencies:
        lines += ["", "[dependencies]"]
        for dep_name, dep in sorted(manifest.dependencies.items()):
            if isinstance(dep, PathDependency):
                lines.append(f'{dep_name} = {{ path = "{_toml_string(dep.path)}" }}')
            else:
                lines.append(
                    f'{dep_name} = {{ registry = "{_toml_string(dep.name)}", '
                    f'version = "{_toml_string(dep.version)}" }}'
                )
    return "\n".join(lines) + "\n"


def load_manifest(directory: Path) -> Manifest:
    manifest_path = directory / MANIFEST_FILENAME
    if not manifest_path.is_file():
        raise PkgError(f"no {MANIFEST_FILENAME} in {directory}")
    return parse_manifest(manifest_path.read_text(encoding="utf-8"))


def save_manifest(directory: Path, manifest: Manifest) -> None:
    (directory / MANIFEST_FILENAME).write_text(render_manifest(manifest), encoding="utf-8")


def init_manifest(directory: Path, name: str | None = None, version: str = "0.1.0") -> Manifest:
    directory.mkdir(parents=True, exist_ok=True)
    if (directory / MANIFEST_FILENAME).exists():
        raise PkgError(f"{MANIFEST_FILENAME} already exists in {directory}")
    manifest = Manifest(name or directory.resolve().name, version, {})
    save_manifest(directory, manifest)
    return manifest


def add_dependency(directory: Path, dep_name: str, dependency: Dependency) -> Manifest:
    manifest = load_manifest(directory)
    updated = Manifest(manifest.name, manifest.version, {**manifest.dependencies, dep_name: dependency})
    save_manifest(directory, updated)
    return updated


# ---- archive format (shared by the client here and registry_server.py) ------------------------


_ARCHIVE_EXCLUDED_DIRS = frozenset({MODULES_DIRNAME, "__pycache__"})
_ARCHIVE_EXCLUDED_FILES = frozenset({LOCKFILE_FILENAME})


def _archive_members(directory: Path) -> list[Path]:
    """Every file belonging in a package archive (a `publish` request body, or a registry's
    `.../archive` response), as paths relative to `directory` — excludes `pere_modules/` (a
    package's own already-resolved dependencies; a consumer resolves its own) and
    `peregrine.lock` (regenerated by whoever installs this package, never shipped), mirroring
    exactly what `install()` already excludes when copying a resolved package's directory locally
    (see its `shutil.ignore_patterns` call below) — publishing over HTTP should drop the same
    things copying over a local filesystem already does."""
    members: list[Path] = []
    for path in sorted(directory.rglob("*")):
        if path.is_dir():
            continue
        relative = path.relative_to(directory)
        if any(part in _ARCHIVE_EXCLUDED_DIRS for part in relative.parts[:-1]):
            continue
        if relative.name in _ARCHIVE_EXCLUDED_FILES:
            continue
        members.append(relative)
    return members


def build_archive(directory: Path) -> bytes:
    """Zip `directory` (a package: its manifest plus source files) into an in-memory archive —
    the format both `publish` (as a PUT request body) and `registry_server.py`'s
    `GET .../archive` endpoint use. Built in memory with the stdlib's `zipfile` rather than
    streamed to disk first, which is plenty for the intentionally-small example/test packages
    this project ever ships."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for relative in _archive_members(directory):
            zf.write(directory / relative, arcname=relative.as_posix())
    return buffer.getvalue()


def extract_archive(data: bytes, target: Path) -> None:
    """Extract a zip archive (in `build_archive`'s format) into `target`, refusing any member
    whose path would resolve outside `target` ("zip slip") rather than trusting
    `ZipFile.extractall` to validate that itself, since it doesn't. Used on both sides of the
    wire: `registry_server.py` extracting a publish's request body, and `_resolve_from_network`
    below extracting a fetched archive into the local registry cache."""
    target.mkdir(parents=True, exist_ok=True)
    target_resolved = target.resolve()
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for member in zf.namelist():
            member_path = (target / member).resolve()
            if member_path != target_resolved and target_resolved not in member_path.parents:
                raise PkgError(f"archive member {member!r} would extract outside the target directory")
        zf.extractall(target)


# ---- network registry client (urllib.request — stdlib only, no new dependency) ------------------


def _registry_url_join(base_url: str, *parts: str) -> str:
    return base_url.rstrip("/") + "".join(f"/{part}" for part in parts)


def _fetch_json(url: str) -> Any:
    """`GET url`, parsed as JSON. `None` on a 404 (a normal "not found," not an error condition
    the caller should be shown a traceback for); any other failure becomes a `PkgError` carrying
    the server's own status/reason so a resolution failure over the network is at least as
    diagnosable as one against a local directory."""
    try:
        with urllib.request.urlopen(url) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise PkgError(f"registry request to {url} failed: HTTP {exc.code} {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise PkgError(f"registry request to {url} failed: {exc.reason}") from exc


def _fetch_bytes(url: str) -> bytes:
    try:
        with urllib.request.urlopen(url) as response:
            return bytes(response.read())
    except urllib.error.HTTPError as exc:
        raise PkgError(f"registry request to {url} failed: HTTP {exc.code} {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise PkgError(f"registry request to {url} failed: {exc.reason}") from exc


def _resolve_from_network(root_dir: Path, registry_url: str, name: str, version: str) -> Path:
    """Fetch `name@version` from a network registry (`registry_server.py`) into a local cache
    under `<root_dir>/<REGISTRY_CACHE_DIRNAME>/<name>/<version>/`, then return that directory
    exactly as if it had been a local `--registry DIR` lookup all along — everything downstream
    (loading its manifest, recursing into its own dependencies, the lockfile) is unchanged, which
    is the whole point of keeping this the *only* registry-mode-aware branch (see module
    docstring). Already-cached packages are reused as-is, with no repeat network round-trip: a
    published version is immutable (the server rejects overwriting one), so a cache hit can never
    be stale."""
    cache_dir = root_dir / REGISTRY_CACHE_DIRNAME / name / version
    if (cache_dir / MANIFEST_FILENAME).is_file():
        return cache_dir

    metadata_url = _registry_url_join(registry_url, "packages", name, version)
    if _fetch_json(metadata_url) is None:
        raise PkgError(f"dependency {name!r}@{version} not found in registry at {registry_url}")

    archive_url = _registry_url_join(registry_url, "packages", name, version, "archive")
    extract_archive(_fetch_bytes(archive_url), cache_dir)

    if not (cache_dir / MANIFEST_FILENAME).is_file():
        raise PkgError(f"archive fetched for {name!r}@{version} from {registry_url} has no {MANIFEST_FILENAME}")
    return cache_dir


def publish(directory: Path, registry_url: str) -> Manifest:
    """Package `directory` (its manifest plus source files, via `build_archive` — the exact
    format `registry_server.py`'s `GET .../archive` endpoint returns) and `PUT` it to
    `<registry_url>/packages/<name>/<version>`, where `name`/`version` come from `directory`'s own
    manifest. Raises `PkgError` — with the server's own error message where one is available — on
    a 409 (already published; see `registry_server.py`'s module docstring for why versions are
    immutable) or any other failure."""
    manifest = load_manifest(directory)
    archive_bytes = build_archive(directory)
    url = _registry_url_join(registry_url, "packages", manifest.name, manifest.version)
    request = urllib.request.Request(
        url, data=archive_bytes, method="PUT", headers={"Content-Type": "application/zip"}
    )
    try:
        with urllib.request.urlopen(request):
            pass
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        if exc.code == 409:
            raise PkgError(
                f"{manifest.name}@{manifest.version} is already published at {registry_url}: {detail}"
            ) from exc
        raise PkgError(
            f"publishing {manifest.name}@{manifest.version} to {registry_url} failed: "
            f"HTTP {exc.code} {detail}"
        ) from exc
    except urllib.error.URLError as exc:
        raise PkgError(
            f"publishing {manifest.name}@{manifest.version} to {registry_url} failed: {exc.reason}"
        ) from exc
    return manifest


# ---- resolution ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ResolvedPackage:
    name: str
    version: str
    source_dir: Path
    source_kind: str  # "path" | "registry" — how it was located, recorded for the lockfile
    source_spec: str  # the literal path, or "registry_name@version"
    dependencies: tuple[str, ...]  # direct dependency names, sorted


@dataclass(frozen=True, slots=True)
class ResolvedGraph:
    root: Manifest
    packages: dict[str, ResolvedPackage]  # every dependency (transitively), keyed by name — not the root itself


def resolve_dependencies(
    root_dir: Path, registry_dir: Path | None = None, registry_url: str | None = None
) -> ResolvedGraph:
    """`registry_dir` (local-directory mode) and `registry_url` (network mode, talking to a
    running `registry_server.py`) are mutually exclusive — see the module docstring for what each
    means. Every existing caller passing `registry_dir` as a `Path` keeps working completely
    unchanged; `registry_url` is purely additive. This is the one function whose body knows *how*
    a `RegistryDependency` gets turned into a directory; everything below it (cycle detection,
    diamond handling, the `visit` recursion itself) has no idea which mode, if either, is active."""
    if registry_dir is not None and registry_url is not None:
        raise PkgError("specify at most one of registry_dir or registry_url, not both")

    root_manifest = load_manifest(root_dir)
    resolved: dict[str, ResolvedPackage] = {}

    def visit(name: str, declaring_dir: Path, dependency: Dependency, chain: tuple[str, ...]) -> None:
        if name in chain:
            raise PkgError(f"dependency cycle detected: {' -> '.join((*chain, name))}")

        if isinstance(dependency, PathDependency):
            dep_dir = (declaring_dir / dependency.path).resolve()
            source_kind, source_spec = "path", dependency.path
        elif registry_dir is not None:
            dep_dir = (registry_dir / dependency.name / dependency.version).resolve()
            source_kind, source_spec = "registry", f"{dependency.name}@{dependency.version}"
        elif registry_url is not None:
            dep_dir = _resolve_from_network(root_dir, registry_url, dependency.name, dependency.version)
            source_kind, source_spec = "registry", f"{dependency.name}@{dependency.version}"
        else:
            raise PkgError(f"dependency {name!r} needs a registry, but none was configured")

        if not dep_dir.is_dir():
            raise PkgError(f"dependency {name!r} not found at {dep_dir}")
        dep_manifest = load_manifest(dep_dir)

        if name in resolved:
            if resolved[name].source_dir != dep_dir:
                raise PkgError(
                    f"conflicting resolutions for dependency {name!r}: "
                    f"{resolved[name].source_dir} vs {dep_dir} — a real version-solver would be "
                    "needed to pick one, which is out of scope here (see module docstring)"
                )
            return

        dep_names = tuple(sorted(dep_manifest.dependencies))
        resolved[name] = ResolvedPackage(name, dep_manifest.version, dep_dir, source_kind, source_spec, dep_names)
        for sub_name, sub_dep in dep_manifest.dependencies.items():
            visit(sub_name, dep_dir, sub_dep, (*chain, name))

    for dep_name, dep in root_manifest.dependencies.items():
        visit(dep_name, root_dir, dep, (root_manifest.name,))

    return ResolvedGraph(root_manifest, resolved)


# ---- lockfile -----------------------------------------------------------------------------------


def render_lockfile(graph: ResolvedGraph) -> str:
    """An array-of-tables TOML document (valid TOML, parseable with `tomllib` even though nothing
    here re-reads it — recorded for a human, or a future tool, to inspect). Packages are emitted
    in sorted-by-name order specifically so two resolutions of the same manifest produce
    byte-identical text — dict/traversal order is otherwise an implementation detail this format
    should not leak."""
    lines = ["# generated by `pere pkg install` — do not edit by hand", ""]
    for name in sorted(graph.packages):
        pkg = graph.packages[name]
        deps = ", ".join(f'"{_toml_string(d)}"' for d in pkg.dependencies)
        lines += [
            "[[package]]",
            f'name = "{_toml_string(pkg.name)}"',
            f'version = "{_toml_string(pkg.version)}"',
            f'source = "{_toml_string(f"{pkg.source_kind}:{pkg.source_spec}")}"',
            f"dependencies = [{deps}]",
            "",
        ]
    return "\n".join(lines).rstrip("\n") + "\n"


# ---- install --------------------------------------------------------------------------------------


def install(root_dir: Path, registry_dir: Path | None = None, registry_url: str | None = None) -> ResolvedGraph:
    graph = resolve_dependencies(root_dir, registry_dir, registry_url)

    modules_dir = root_dir / MODULES_DIRNAME
    if modules_dir.exists():
        shutil.rmtree(modules_dir)
    modules_dir.mkdir(parents=True, exist_ok=True)

    ignore = shutil.ignore_patterns(MODULES_DIRNAME, LOCKFILE_FILENAME)
    for name, resolved_pkg in graph.packages.items():
        shutil.copytree(resolved_pkg.source_dir, modules_dir / name, ignore=ignore)

    (root_dir / LOCKFILE_FILENAME).write_text(render_lockfile(graph), encoding="utf-8")
    return graph
