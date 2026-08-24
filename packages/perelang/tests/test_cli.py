from __future__ import annotations

import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from typer.testing import CliRunner

from perelang import pkg, registry_server
from perelang.cli import app

runner = CliRunner()

_EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


class TestRun:
    def test_run_factorial_example_bytecode_backend(self) -> None:
        result = runner.invoke(app, ["run", str(_EXAMPLES / "factorial.pgr")])
        assert result.exit_code == 0
        assert result.stdout.strip() == "3628800"

    def test_run_fibonacci_example(self) -> None:
        result = runner.invoke(app, ["run", str(_EXAMPLES / "fibonacci.pgr")])
        assert result.exit_code == 0
        assert result.stdout.strip() == "6765"

    def test_run_closures_example(self) -> None:
        result = runner.invoke(app, ["run", str(_EXAMPLES / "closures.pgr")])
        assert result.exit_code == 0
        assert result.stdout.strip() == "42"

    def test_run_factorial_example_llvm_backend(self) -> None:
        result = runner.invoke(app, ["run", str(_EXAMPLES / "factorial.pgr"), "--backend", "llvm"])
        assert result.exit_code == 0
        assert result.stdout.strip() == "3628800"

    def test_run_closures_example_llvm_backend(self) -> None:
        # Closures are now supported by the LLVM backend (see llvm_backend.py and
        # docs/adr/0003-llvm-backend-scope-cuts.md's "Update, implemented" note) — this mirrors
        # test_run_closures_example above, just on the other backend.
        result = runner.invoke(app, ["run", str(_EXAMPLES / "closures.pgr"), "--backend", "llvm"])
        assert result.exit_code == 0
        assert result.stdout.strip() == "42"

    def test_run_missing_file_reports_error_and_exits_nonzero(self) -> None:
        result = runner.invoke(app, ["run", str(_EXAMPLES / "does_not_exist.pgr")])
        assert result.exit_code == 1
        assert "no such file" in result.stderr

    def test_run_unknown_backend_reports_error(self) -> None:
        result = runner.invoke(app, ["run", str(_EXAMPLES / "factorial.pgr"), "--backend", "nonsense"])
        assert result.exit_code == 1
        assert "unknown backend" in result.stderr

    def test_run_source_with_type_error_reports_it(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.pgr"
        bad.write_text("fn main() -> Int { true }")
        result = runner.invoke(app, ["run", str(bad)])
        assert result.exit_code == 1
        assert "error:" in result.stderr

    def test_run_llvm_entry_argument_selects_a_non_main_function(self, tmp_path: Path) -> None:
        src = tmp_path / "answer.pgr"
        src.write_text("fn answer() -> Int { 42 } fn main() -> Int { 0 }")
        result = runner.invoke(app, ["run", str(src), "--backend", "llvm", "--entry", "answer"])
        assert result.exit_code == 0
        assert result.stdout.strip() == "42"


class TestCheck:
    def test_check_valid_program_reports_ok(self) -> None:
        result = runner.invoke(app, ["check", str(_EXAMPLES / "factorial.pgr")])
        assert result.exit_code == 0
        assert result.stdout.strip() == "ok"

    def test_check_type_error_reports_it_and_does_not_run(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.pgr"
        bad.write_text("fn main() -> Int { true }")
        result = runner.invoke(app, ["check", str(bad)])
        assert result.exit_code == 1
        assert "error:" in result.stderr

    def test_check_missing_file(self) -> None:
        result = runner.invoke(app, ["check", str(_EXAMPLES / "does_not_exist.pgr")])
        assert result.exit_code == 1


class TestPkgPublishAndNetworkInstall:
    """`pere pkg publish` / `pere pkg install --registry-url` against a real, in-process
    `registry_server.py` instance (background thread, ephemeral loopback port) — the CLI wiring
    on top of `pkg.publish`/`pkg.install`'s already-tested network mode (see `test_pkg.py`'s
    `TestNetworkRegistry`)."""

    def test_pkg_publish_then_install_via_registry_url(self, tmp_path: Path) -> None:
        httpd = registry_server.make_server(tmp_path / "registry", host="127.0.0.1", port=0)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            base_url = f"http://127.0.0.1:{httpd.server_address[1]}"

            dep_dir = tmp_path / "dep"
            runner.invoke(app, ["pkg", "init", str(dep_dir), "--name", "dep"])
            publish_result = runner.invoke(
                app, ["pkg", "publish", "--dir", str(dep_dir), "--registry-url", base_url]
            )
            assert publish_result.exit_code == 0
            assert "published dep@0.1.0" in publish_result.stdout

            root_dir = tmp_path / "root"
            runner.invoke(app, ["pkg", "init", str(root_dir), "--name", "root"])
            runner.invoke(app, ["pkg", "add", "dep", "--version", "0.1.0", "--dir", str(root_dir)])
            install_result = runner.invoke(
                app, ["pkg", "install", "--dir", str(root_dir), "--registry-url", base_url]
            )
            assert install_result.exit_code == 0
            assert (root_dir / pkg.MODULES_DIRNAME / "dep").is_dir()
            assert (root_dir / pkg.LOCKFILE_FILENAME).is_file()
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

    def test_pkg_publish_rejects_duplicate_version(self, tmp_path: Path) -> None:
        httpd = registry_server.make_server(tmp_path / "registry", host="127.0.0.1", port=0)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            base_url = f"http://127.0.0.1:{httpd.server_address[1]}"
            dep_dir = tmp_path / "dep"
            runner.invoke(app, ["pkg", "init", str(dep_dir), "--name", "dep"])
            runner.invoke(app, ["pkg", "publish", "--dir", str(dep_dir), "--registry-url", base_url])

            second = runner.invoke(app, ["pkg", "publish", "--dir", str(dep_dir), "--registry-url", base_url])
            assert second.exit_code == 1
            assert "already published" in second.stderr
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

    def test_pkg_install_rejects_both_registry_and_registry_url(self, tmp_path: Path) -> None:
        runner.invoke(app, ["pkg", "init", str(tmp_path), "--name", "demo"])
        result = runner.invoke(
            app,
            [
                "pkg",
                "install",
                "--dir",
                str(tmp_path),
                "--registry",
                str(tmp_path),
                "--registry-url",
                "http://127.0.0.1:1",
            ],
        )
        assert result.exit_code == 1
        assert "at most one" in result.stderr


class TestRegistryServeCommand:
    def test_registry_serve_actually_listens_and_responds(self, tmp_path: Path) -> None:
        """`pere registry serve` blocks forever by design, so it's exercised as a real subprocess
        (not `CliRunner.invoke`, which would hang the test) — started, polled with real HTTP
        requests until it responds, asserted on, then terminated. Proves the CLI's
        `--root`/`--host`/`--port` options actually reach `registry_server.serve`."""
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()

        root = tmp_path / "registry_data"
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "perelang.cli",
                "registry",
                "serve",
                "--root",
                str(root),
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            base_url = f"http://127.0.0.1:{port}"
            deadline = time.monotonic() + 15
            listening = False
            while time.monotonic() < deadline and not listening:
                try:
                    urllib.request.urlopen(f"{base_url}/packages/nope/1.0.0", timeout=1)
                except urllib.error.HTTPError as exc:
                    listening = exc.code == 404
                except (urllib.error.URLError, OSError):
                    time.sleep(0.2)
            assert listening, "pere registry serve did not start listening in time"
            assert root.is_dir()
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
