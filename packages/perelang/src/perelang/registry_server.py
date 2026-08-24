"""A real HTTP package registry server for Peregrine — the network-mode counterpart to `pkg.py`'s
local-directory registry mode (see that module's docstring for how the two modes relate; both are
fully supported).

Hand-rolled on `http.server.BaseHTTPRequestHandler`/`ThreadingHTTPServer` (both stdlib) rather than
pulling in `flask`/`fastapi` — matching `lsp_server.py`'s precedent of hand-writing a protocol layer
directly rather than putting a framework between the code and what it does; this API is small enough
(four routes) that a framework would buy little.

**Storage.** A directory tree on disk, in exactly the layout `pkg.py`'s local-directory mode already
uses: `<root>/<name>/<version>/`, each holding a `peregrine.toml` plus the package's source files.
The server just serves/accepts that same shape over HTTP instead of a client reading it directly off
a shared filesystem path — publishing writes into `<root>` exactly where a local-directory-mode
client would have expected to find it directly, and the two modes could in principle share a root
(not something this project relies on, but not accidental either).

**Endpoints:**

- `GET /packages/<name>/<version>` — the package's `peregrine.toml`, parsed and returned as JSON.
  404 if that exact name/version was never published.
- `GET /packages/<name>/<version>/archive` — the package's manifest plus source files, as a single
  zip archive (`pkg.build_archive`'s format) — one request instead of file-by-file. 404 if unknown.
- `PUT /packages/<name>/<version>` — publish: the request body is a zip archive in the same format,
  extracted into `<root>/<name>/<version>/`.
- `GET /packages/<name>` — every published version of `name`, as a sorted JSON list. Not currently
  consumed by `pkg.py`'s resolver (which always resolves an exact pinned version — there is no
  "latest" concept anywhere else in this package manager), but it's a small, natural addition for a
  registry API to have, and exactly the kind of endpoint a future "resolve to latest" feature would
  need without any server-side change.

**Immutable versions — a real design choice, not just an HTTP status code.** `PUT` on an exact
`name@version` that's already published is rejected with `409 Conflict`, never silently overwritten.
Registries that let you overwrite an already-published version create a correctness hazard no
lockfile can protect against: two installs of the exact same pinned `name@version`, minutes apart,
could resolve to different bytes — which is precisely what a lockfile pinning a version number is
supposed to rule out. Publishing something new means bumping the version number, the same as every
mainstream registry (npm, crates.io, PyPI) already enforces.

**Lifecycle.** `serve(root, host, port)` is the blocking entry point `pere registry serve` uses.
`make_server(root, host, port)` builds a not-yet-serving `ThreadingHTTPServer` for callers (tests,
mainly) that want to run it on a background thread and `.shutdown()`/`.server_close()` it themselves
— see `test_registry_server.py`'s `running_server` context manager.
"""

from __future__ import annotations

import json
import shutil
import threading
import tomllib
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from perelang import pkg

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 8765


def _package_dir(root: Path, name: str, version: str) -> Path:
    return root / name / version


class _RegistryRequestHandler(BaseHTTPRequestHandler):
    """One instance per request (stdlib `http.server` behavior). `root` and `lock` are bound once,
    as class attributes, by `_build_handler_class`'s closure below — every instance handling
    requests for one server shares the same storage root and the same lock guarding a publish's
    check-then-write (two concurrent `PUT`s to the same `name@version`, on `ThreadingHTTPServer`'s
    separate threads, must not both observe "not yet published" and both proceed)."""

    root: Path
    lock: threading.Lock

    server_version = "PeregrineRegistry/0.1"

    def log_message(self, format: str, *args: Any) -> None:
        pass  # quiet by default — nothing here is worth stdout/stderr noise in tests or the CLI

    # ---- response helpers ---------------------------------------------------------------------

    def _write_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _write_bytes(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _write_error(self, status: int, message: str) -> None:
        self._write_json(status, {"error": message})

    # ---- routing --------------------------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler's required method name)
        parts = [p for p in urlsplit(self.path).path.split("/") if p]
        if len(parts) == 2 and parts[0] == "packages":
            self._get_versions(parts[1])
        elif len(parts) == 3 and parts[0] == "packages":
            self._get_metadata(parts[1], parts[2])
        elif len(parts) == 4 and parts[0] == "packages" and parts[3] == "archive":
            self._get_archive(parts[1], parts[2])
        else:
            self._write_error(404, f"no such route: {self.path!r}")

    def do_PUT(self) -> None:  # noqa: N802
        parts = [p for p in urlsplit(self.path).path.split("/") if p]
        if len(parts) == 3 and parts[0] == "packages":
            self._put_package(parts[1], parts[2])
        else:
            self._write_error(404, f"no such route: {self.path!r}")

    # ---- handlers -------------------------------------------------------------------------------

    def _get_versions(self, name: str) -> None:
        name_dir = self.root / name
        if not name_dir.is_dir():
            self._write_error(404, f"no such package: {name!r}")
            return
        versions = sorted(
            p.name for p in name_dir.iterdir() if p.is_dir() and (p / pkg.MANIFEST_FILENAME).is_file()
        )
        if not versions:
            self._write_error(404, f"no such package: {name!r}")
            return
        self._write_json(200, {"name": name, "versions": versions})

    def _get_metadata(self, name: str, version: str) -> None:
        manifest_path = _package_dir(self.root, name, version) / pkg.MANIFEST_FILENAME
        if not manifest_path.is_file():
            self._write_error(404, f"no such package version: {name}@{version}")
            return
        with manifest_path.open("rb") as fh:
            data = tomllib.load(fh)
        self._write_json(200, data)

    def _get_archive(self, name: str, version: str) -> None:
        package_dir = _package_dir(self.root, name, version)
        if not (package_dir / pkg.MANIFEST_FILENAME).is_file():
            self._write_error(404, f"no such package version: {name}@{version}")
            return
        self._write_bytes(200, "application/zip", pkg.build_archive(package_dir))

    def _put_package(self, name: str, version: str) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(content_length)
        package_dir = _package_dir(self.root, name, version)

        with self.lock:
            if (package_dir / pkg.MANIFEST_FILENAME).is_file():
                self._write_error(
                    409, f"{name}@{version} is already published — registry versions are immutable"
                )
                return

            try:
                pkg.extract_archive(body, package_dir)
                published = pkg.load_manifest(package_dir)
            except (pkg.PkgError, zipfile.BadZipFile, tomllib.TOMLDecodeError) as exc:
                shutil.rmtree(package_dir, ignore_errors=True)
                self._write_error(400, f"invalid package archive: {exc}")
                return

            if published.name != name or published.version != version:
                shutil.rmtree(package_dir, ignore_errors=True)
                self._write_error(
                    400,
                    f"archive's manifest declares {published.name}@{published.version}, which "
                    f"doesn't match the published route /packages/{name}/{version}",
                )
                return

        self._write_json(201, {"name": name, "version": version})


def _build_handler_class(root: Path) -> type[_RegistryRequestHandler]:
    """A fresh `_RegistryRequestHandler` subclass per call, with `root`/`lock` bound as class
    attributes via this closure — `ThreadingHTTPServer` instantiates the handler class itself (one
    instance per request, with no way to pass extra constructor arguments through), so binding the
    storage root has to happen on the class, not on an instance."""

    class _BoundHandler(_RegistryRequestHandler):
        pass

    _BoundHandler.root = root
    _BoundHandler.lock = threading.Lock()
    return _BoundHandler


def make_server(root: Path, host: str = _DEFAULT_HOST, port: int = 0) -> ThreadingHTTPServer:
    """Build a `ThreadingHTTPServer` bound to `root`, not yet serving. `port=0` (the default) asks
    the OS for an ephemeral free port — read it back from the returned server's `server_address`
    before handing a base URL to a client; this lets many independent server instances (one per
    test) run back-to-back without ever colliding on a fixed port number. Callers own the
    lifecycle: `.serve_forever()` (typically on a background thread) to start serving, then
    `.shutdown()` followed by `.server_close()` to stop cleanly — see `test_registry_server.py`'s
    `running_server` for the pattern this project's own tests use."""
    root.mkdir(parents=True, exist_ok=True)
    return ThreadingHTTPServer((host, port), _build_handler_class(root))


def serve(root: Path, host: str = _DEFAULT_HOST, port: int = _DEFAULT_PORT) -> None:
    """Blocking entry point for `pere registry serve`: build a server and run it until interrupted
    (Ctrl+C / SIGINT, which `serve_forever` surfaces as `KeyboardInterrupt`)."""
    httpd = make_server(root, host, port)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
