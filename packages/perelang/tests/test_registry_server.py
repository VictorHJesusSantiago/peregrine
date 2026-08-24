from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from http.client import HTTPResponse
from io import BytesIO
from pathlib import Path

import pytest

from perelang import pkg, registry_server


@contextmanager
def running_server(root: Path) -> Iterator[str]:
    """Start a real `registry_server.py` server on an OS-assigned loopback port, in a background
    thread, and yield its base URL (`http://127.0.0.1:<port>`). Reused by `test_pkg.py` (and
    `test_cli.py`, indirectly) so server-startup/teardown boilerplate lives in exactly one place.
    Shuts the server down cleanly on exit — `.shutdown()` stops `serve_forever`'s loop,
    `.server_close()` releases the listening socket, and joining the thread makes sure both have
    actually finished before the `with` block exits, so nothing is ever leaked across tests."""
    httpd = registry_server.make_server(root, host="127.0.0.1", port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _make_package(root: Path, name: str, version: str, extra_files: dict[str, str] | None = None) -> Path:
    directory = root / name
    pkg.init_manifest(directory, name=name, version=version)
    for rel_path, content in (extra_files or {}).items():
        target = directory / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return directory


def _put_archive(base_url: str, name: str, version: str, archive: bytes) -> HTTPResponse:
    request = urllib.request.Request(f"{base_url}/packages/{name}/{version}", data=archive, method="PUT")
    response = urllib.request.urlopen(request)
    assert isinstance(response, HTTPResponse)
    return response


class TestRegistryServer:
    def test_get_missing_package_metadata_returns_404(self, tmp_path: Path) -> None:
        with running_server(tmp_path / "registry") as base_url:
            with pytest.raises(urllib.error.HTTPError) as excinfo:
                urllib.request.urlopen(f"{base_url}/packages/nope/1.0.0")
            assert excinfo.value.code == 404

    def test_get_missing_package_archive_returns_404(self, tmp_path: Path) -> None:
        with running_server(tmp_path / "registry") as base_url:
            with pytest.raises(urllib.error.HTTPError) as excinfo:
                urllib.request.urlopen(f"{base_url}/packages/nope/1.0.0/archive")
            assert excinfo.value.code == 404

    def test_get_versions_of_unknown_package_returns_404(self, tmp_path: Path) -> None:
        with running_server(tmp_path / "registry") as base_url:
            with pytest.raises(urllib.error.HTTPError) as excinfo:
                urllib.request.urlopen(f"{base_url}/packages/nope")
            assert excinfo.value.code == 404

    def test_unknown_route_returns_404(self, tmp_path: Path) -> None:
        with running_server(tmp_path / "registry") as base_url:
            with pytest.raises(urllib.error.HTTPError) as excinfo:
                urllib.request.urlopen(f"{base_url}/nonsense")
            assert excinfo.value.code == 404

    def test_publish_then_fetch_metadata(self, tmp_path: Path) -> None:
        package_dir = _make_package(tmp_path, "leftpad", "1.0.0")
        with running_server(tmp_path / "registry") as base_url:
            archive = pkg.build_archive(package_dir)
            with _put_archive(base_url, "leftpad", "1.0.0", archive) as response:
                assert response.status == 201

            with urllib.request.urlopen(f"{base_url}/packages/leftpad/1.0.0") as response:
                metadata = json.loads(response.read())
        assert metadata["package"]["name"] == "leftpad"
        assert metadata["package"]["version"] == "1.0.0"

    def test_publish_then_fetch_archive_contents(self, tmp_path: Path) -> None:
        package_dir = _make_package(
            tmp_path, "leftpad", "1.0.0", {"src/main.pgr": "fn main() -> Int { 0 }"}
        )
        with running_server(tmp_path / "registry") as base_url:
            archive = pkg.build_archive(package_dir)
            _put_archive(base_url, "leftpad", "1.0.0", archive).close()

            with urllib.request.urlopen(f"{base_url}/packages/leftpad/1.0.0/archive") as response:
                fetched = response.read()

        with zipfile.ZipFile(BytesIO(fetched)) as zf:
            names = set(zf.namelist())
            assert "peregrine.toml" in names
            assert "src/main.pgr" in names
            assert zf.read("src/main.pgr").decode("utf-8") == "fn main() -> Int { 0 }"

    def test_archive_never_contains_pere_modules_or_lockfile(self, tmp_path: Path) -> None:
        package_dir = _make_package(tmp_path, "leftpad", "1.0.0")
        (package_dir / pkg.MODULES_DIRNAME).mkdir()
        (package_dir / pkg.MODULES_DIRNAME / "junk.txt").write_text("nope", encoding="utf-8")
        (package_dir / pkg.LOCKFILE_FILENAME).write_text("# stale lockfile\n", encoding="utf-8")

        with running_server(tmp_path / "registry") as base_url:
            _put_archive(base_url, "leftpad", "1.0.0", pkg.build_archive(package_dir)).close()
            with urllib.request.urlopen(f"{base_url}/packages/leftpad/1.0.0/archive") as response:
                fetched = response.read()

        with zipfile.ZipFile(BytesIO(fetched)) as zf:
            names = set(zf.namelist())
        assert not any(pkg.MODULES_DIRNAME in name for name in names)
        assert pkg.LOCKFILE_FILENAME not in names

    def test_list_versions_returns_all_published_versions_sorted(self, tmp_path: Path) -> None:
        with running_server(tmp_path / "registry") as base_url:
            for version in ["2.0.0", "1.0.0"]:
                package_dir = _make_package(tmp_path / version, "leftpad", version)
                _put_archive(base_url, "leftpad", version, pkg.build_archive(package_dir)).close()

            with urllib.request.urlopen(f"{base_url}/packages/leftpad") as response:
                payload = json.loads(response.read())
        assert payload["versions"] == ["1.0.0", "2.0.0"]

    def test_republishing_same_name_and_version_is_rejected_with_409(self, tmp_path: Path) -> None:
        package_dir = _make_package(tmp_path, "leftpad", "1.0.0")
        with running_server(tmp_path / "registry") as base_url:
            archive = pkg.build_archive(package_dir)
            _put_archive(base_url, "leftpad", "1.0.0", archive).close()

            with pytest.raises(urllib.error.HTTPError) as excinfo:
                _put_archive(base_url, "leftpad", "1.0.0", archive)
        assert excinfo.value.code == 409

    def test_publishing_a_new_version_after_an_existing_one_is_allowed(self, tmp_path: Path) -> None:
        with running_server(tmp_path / "registry") as base_url:
            v1_dir = _make_package(tmp_path / "v1", "leftpad", "1.0.0")
            v2_dir = _make_package(tmp_path / "v2", "leftpad", "2.0.0")
            _put_archive(base_url, "leftpad", "1.0.0", pkg.build_archive(v1_dir)).close()
            with _put_archive(base_url, "leftpad", "2.0.0", pkg.build_archive(v2_dir)) as response:
                assert response.status == 201

    def test_archive_manifest_mismatching_published_route_is_rejected(self, tmp_path: Path) -> None:
        package_dir = _make_package(tmp_path, "leftpad", "1.0.0")
        with running_server(tmp_path / "registry") as base_url:
            archive = pkg.build_archive(package_dir)
            with pytest.raises(urllib.error.HTTPError) as excinfo:
                _put_archive(base_url, "other", "9.9.9", archive)
        assert excinfo.value.code == 400

    def test_publishing_garbage_bytes_is_rejected_not_crashed(self, tmp_path: Path) -> None:
        with running_server(tmp_path / "registry") as base_url, pytest.raises(urllib.error.HTTPError) as excinfo:
            _put_archive(base_url, "leftpad", "1.0.0", b"not a zip file")
        assert excinfo.value.code == 400

    def test_two_servers_on_ephemeral_ports_do_not_collide(self, tmp_path: Path) -> None:
        with running_server(tmp_path / "registry_a") as base_url_a, running_server(tmp_path / "registry_b") as base_url_b:
            assert base_url_a != base_url_b
            with pytest.raises(urllib.error.HTTPError):
                urllib.request.urlopen(f"{base_url_a}/packages/nope/1.0.0")
            with pytest.raises(urllib.error.HTTPError):
                urllib.request.urlopen(f"{base_url_b}/packages/nope/1.0.0")
