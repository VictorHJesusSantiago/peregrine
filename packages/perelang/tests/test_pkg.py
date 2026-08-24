from pathlib import Path

import pytest

# Sibling test module, not a package under `mypy_path` (only the src/ trees are) — `running_server`
# is documented at its own definition in test_registry_server.py and reused here rather than
# duplicated, per this task's own instructions.
from test_registry_server import running_server  # type: ignore[import-not-found]
from typer.testing import CliRunner

from perelang import pkg
from perelang.cli import app

runner = CliRunner()


class TestManifestRoundTrip:
    def test_render_then_parse_recovers_the_manifest(self) -> None:
        manifest = pkg.Manifest(
            "demo",
            "1.2.3",
            {
                "left_pad": pkg.PathDependency("../left_pad"),
                "json": pkg.RegistryDependency("json", "2.0.0"),
            },
        )
        recovered = pkg.parse_manifest(pkg.render_manifest(manifest))
        assert recovered == manifest

    def test_save_then_load_round_trips_through_disk(self, tmp_path: Path) -> None:
        manifest = pkg.Manifest("demo", "0.1.0", {"a": pkg.PathDependency("../a")})
        pkg.save_manifest(tmp_path, manifest)
        assert pkg.load_manifest(tmp_path) == manifest

    def test_manifest_missing_package_table_raises_pkg_error(self) -> None:
        with pytest.raises(pkg.PkgError):
            pkg.parse_manifest("[dependencies]\n")

    def test_dependency_missing_path_and_version_raises_pkg_error(self) -> None:
        with pytest.raises(pkg.PkgError):
            pkg.parse_manifest('[package]\nname = "x"\nversion = "0.1.0"\n\n[dependencies]\nfoo = {}\n')

    def test_init_manifest_defaults_name_to_directory_name(self, tmp_path: Path) -> None:
        pkg_dir = tmp_path / "my_pkg"
        pkg_dir.mkdir()
        manifest = pkg.init_manifest(pkg_dir)
        assert manifest.name == "my_pkg"
        assert manifest.version == "0.1.0"

    def test_init_manifest_twice_raises(self, tmp_path: Path) -> None:
        pkg.init_manifest(tmp_path, name="a")
        with pytest.raises(pkg.PkgError):
            pkg.init_manifest(tmp_path, name="a")

    def test_add_dependency_persists_to_disk(self, tmp_path: Path) -> None:
        pkg.init_manifest(tmp_path, name="a")
        pkg.add_dependency(tmp_path, "b", pkg.PathDependency("../b"))
        assert pkg.load_manifest(tmp_path).dependencies == {"b": pkg.PathDependency("../b")}


def _make_pkg(root: Path, name: str, deps: dict[str, pkg.Dependency] | None = None) -> Path:
    directory = root / name
    pkg.init_manifest(directory, name=name)
    for dep_name, dep in (deps or {}).items():
        pkg.add_dependency(directory, dep_name, dep)
    return directory


class TestResolution:
    def test_single_path_dependency_resolves(self, tmp_path: Path) -> None:
        _make_pkg(tmp_path, "b")
        a = _make_pkg(tmp_path, "a", {"b": pkg.PathDependency("../b")})
        graph = pkg.resolve_dependencies(a)
        assert set(graph.packages) == {"b"}
        assert graph.packages["b"].source_kind == "path"

    def test_multi_level_transitive_dependency_resolves(self, tmp_path: Path) -> None:
        _make_pkg(tmp_path, "c")
        _make_pkg(tmp_path, "b", {"c": pkg.PathDependency("../c")})
        a = _make_pkg(tmp_path, "a", {"b": pkg.PathDependency("../b")})
        graph = pkg.resolve_dependencies(a)
        assert set(graph.packages) == {"b", "c"}
        assert graph.packages["b"].dependencies == ("c",)
        assert graph.packages["c"].dependencies == ()

    def test_diamond_dependency_resolves_once(self, tmp_path: Path) -> None:
        _make_pkg(tmp_path, "shared")
        _make_pkg(tmp_path, "left", {"shared": pkg.PathDependency("../shared")})
        _make_pkg(tmp_path, "right", {"shared": pkg.PathDependency("../shared")})
        root = _make_pkg(
            tmp_path, "root", {"left": pkg.PathDependency("../left"), "right": pkg.PathDependency("../right")}
        )
        graph = pkg.resolve_dependencies(root)
        assert set(graph.packages) == {"left", "right", "shared"}

    def test_two_node_cycle_is_detected(self, tmp_path: Path) -> None:
        _make_pkg(tmp_path, "a", {"b": pkg.PathDependency("../b")})
        b = _make_pkg(tmp_path, "b", {"a": pkg.PathDependency("../a")})
        with pytest.raises(pkg.PkgError, match="cycle"):
            pkg.resolve_dependencies(b)

    def test_three_node_cycle_is_detected(self, tmp_path: Path) -> None:
        a = _make_pkg(tmp_path, "a", {"b": pkg.PathDependency("../b")})
        _make_pkg(tmp_path, "b", {"c": pkg.PathDependency("../c")})
        _make_pkg(tmp_path, "c", {"a": pkg.PathDependency("../a")})
        with pytest.raises(pkg.PkgError, match="cycle"):
            pkg.resolve_dependencies(a)

    def test_missing_dependency_directory_raises(self, tmp_path: Path) -> None:
        a = _make_pkg(tmp_path, "a", {"ghost": pkg.PathDependency("../does_not_exist")})
        with pytest.raises(pkg.PkgError, match="not found"):
            pkg.resolve_dependencies(a)

    def test_registry_dependency_resolves(self, tmp_path: Path) -> None:
        registry = tmp_path / "registry"
        # Registry layout: <registry>/<name>/<version>/peregrine.toml (see pkg.py's docstring).
        pkg.init_manifest(registry / "leftpad" / "1.0.0", name="leftpad", version="1.0.0")
        a = _make_pkg(tmp_path, "a", {"leftpad": pkg.RegistryDependency("leftpad", "1.0.0")})
        graph = pkg.resolve_dependencies(a, registry_dir=registry)
        assert graph.packages["leftpad"].source_kind == "registry"
        assert graph.packages["leftpad"].source_spec == "leftpad@1.0.0"

    def test_registry_dependency_without_configured_registry_raises(self, tmp_path: Path) -> None:
        a = _make_pkg(tmp_path, "a", {"leftpad": pkg.RegistryDependency("leftpad", "1.0.0")})
        with pytest.raises(pkg.PkgError, match="registry"):
            pkg.resolve_dependencies(a)


class TestInstallAndLockfile:
    def test_install_writes_lockfile_and_pere_modules(self, tmp_path: Path) -> None:
        _make_pkg(tmp_path, "b")
        a = _make_pkg(tmp_path, "a", {"b": pkg.PathDependency("../b")})
        pkg.install(a)
        assert (a / pkg.LOCKFILE_FILENAME).is_file()
        assert (a / pkg.MODULES_DIRNAME / "b" / pkg.MANIFEST_FILENAME).is_file()

    def test_installing_twice_produces_identical_lockfile(self, tmp_path: Path) -> None:
        _make_pkg(tmp_path, "c")
        _make_pkg(tmp_path, "b", {"c": pkg.PathDependency("../c")})
        a = _make_pkg(tmp_path, "a", {"b": pkg.PathDependency("../b")})
        pkg.install(a)
        first = (a / pkg.LOCKFILE_FILENAME).read_text()
        pkg.install(a)
        second = (a / pkg.LOCKFILE_FILENAME).read_text()
        assert first == second

    def test_lockfile_lists_packages_sorted_by_name(self, tmp_path: Path) -> None:
        _make_pkg(tmp_path, "zeta")
        _make_pkg(tmp_path, "alpha")
        a = _make_pkg(
            tmp_path, "a", {"zeta": pkg.PathDependency("../zeta"), "alpha": pkg.PathDependency("../alpha")}
        )
        graph = pkg.install(a)
        lockfile = (a / pkg.LOCKFILE_FILENAME).read_text()
        assert lockfile.index('name = "alpha"') < lockfile.index('name = "zeta"')
        assert set(graph.packages) == {"alpha", "zeta"}


class TestNetworkRegistry:
    """`resolve_dependencies`/`install` against a real `registry_server.py` instance, over real
    HTTP (via `running_server`, imported from `test_registry_server.py` rather than duplicated
    here) — proving the cycle-detection/diamond-dependency/lockfile logic in `TestResolution` and
    `TestInstallAndLockfile` above needs no changes at all when a `RegistryDependency` is resolved
    over the network instead of against a local `--registry DIR`, exactly as `pkg.py`'s module
    docstring claims."""

    def test_registry_dependency_resolves_over_http(self, tmp_path: Path) -> None:
        leftpad_dir = _make_pkg(tmp_path / "src", "leftpad")
        with running_server(tmp_path / "registry") as base_url:
            pkg.publish(leftpad_dir, base_url)
            a = _make_pkg(tmp_path, "a", {"leftpad": pkg.RegistryDependency("leftpad", "0.1.0")})
            graph = pkg.resolve_dependencies(a, registry_url=base_url)
        assert graph.packages["leftpad"].source_kind == "registry"
        assert graph.packages["leftpad"].source_spec == "leftpad@0.1.0"

    def test_transitive_dependency_resolves_over_http(self, tmp_path: Path) -> None:
        with running_server(tmp_path / "registry") as base_url:
            base_dir = _make_pkg(tmp_path / "src1", "base")
            pkg.publish(base_dir, base_url)

            leftpad_dir = _make_pkg(
                tmp_path / "src2", "leftpad", {"base": pkg.RegistryDependency("base", "0.1.0")}
            )
            pkg.publish(leftpad_dir, base_url)

            a = _make_pkg(tmp_path, "a", {"leftpad": pkg.RegistryDependency("leftpad", "0.1.0")})
            graph = pkg.resolve_dependencies(a, registry_url=base_url)

        assert set(graph.packages) == {"leftpad", "base"}
        assert graph.packages["leftpad"].dependencies == ("base",)

    def test_diamond_dependency_resolves_once_over_http(self, tmp_path: Path) -> None:
        with running_server(tmp_path / "registry") as base_url:
            shared_dir = _make_pkg(tmp_path / "src", "shared")
            pkg.publish(shared_dir, base_url)

            _make_pkg(tmp_path, "left", {"shared": pkg.RegistryDependency("shared", "0.1.0")})
            _make_pkg(tmp_path, "right", {"shared": pkg.RegistryDependency("shared", "0.1.0")})
            root = _make_pkg(
                tmp_path,
                "root",
                {"left": pkg.PathDependency("../left"), "right": pkg.PathDependency("../right")},
            )
            graph = pkg.resolve_dependencies(root, registry_url=base_url)

        assert set(graph.packages) == {"left", "right", "shared"}

    def test_cycle_through_a_network_registry_dependency_is_detected(self, tmp_path: Path) -> None:
        with running_server(tmp_path / "registry") as base_url:
            x_src = _make_pkg(tmp_path / "src_x", "x", {"y": pkg.RegistryDependency("y", "0.1.0")})
            pkg.publish(x_src, base_url)
            y_src = _make_pkg(tmp_path / "src_y", "y", {"x": pkg.RegistryDependency("x", "0.1.0")})
            pkg.publish(y_src, base_url)

            a = _make_pkg(tmp_path, "a", {"x": pkg.RegistryDependency("x", "0.1.0")})
            with pytest.raises(pkg.PkgError, match="cycle"):
                pkg.resolve_dependencies(a, registry_url=base_url)

    def test_registry_dir_and_registry_url_are_mutually_exclusive(self, tmp_path: Path) -> None:
        a = _make_pkg(tmp_path, "a", {"dep": pkg.RegistryDependency("dep", "1.0.0")})
        with pytest.raises(pkg.PkgError, match="at most one"):
            pkg.resolve_dependencies(a, registry_dir=tmp_path, registry_url="http://127.0.0.1:1")

    def test_missing_network_dependency_raises_pkg_error(self, tmp_path: Path) -> None:
        a = _make_pkg(tmp_path, "a", {"ghost": pkg.RegistryDependency("ghost", "1.0.0")})
        with running_server(tmp_path / "registry") as base_url, pytest.raises(pkg.PkgError, match="not found"):
            pkg.resolve_dependencies(a, registry_url=base_url)

    def test_cached_network_dependency_is_reused_without_repeat_fetch(self, tmp_path: Path) -> None:
        dep_dir = _make_pkg(tmp_path / "src", "dep")
        with running_server(tmp_path / "registry") as base_url:
            pkg.publish(dep_dir, base_url)
            a = _make_pkg(tmp_path, "a", {"dep": pkg.RegistryDependency("dep", "0.1.0")})
            pkg.resolve_dependencies(a, registry_url=base_url)

        # The server is now shut down; a second resolution must still succeed purely from the
        # local cache under <root>/.pere_registry_cache/, proving a cache hit needs no network.
        graph = pkg.resolve_dependencies(a, registry_url=base_url)
        assert graph.packages["dep"].source_kind == "registry"

    def test_install_end_to_end_over_http(self, tmp_path: Path) -> None:
        dep_dir = _make_pkg(tmp_path / "src", "dep")
        (dep_dir / "greet.pgr").write_text("fn greet() -> Int { 1 }", encoding="utf-8")
        with running_server(tmp_path / "registry") as base_url:
            pkg.publish(dep_dir, base_url)
            root = _make_pkg(tmp_path, "root", {"dep": pkg.RegistryDependency("dep", "0.1.0")})
            graph = pkg.install(root, registry_url=base_url)

        assert (root / pkg.MODULES_DIRNAME / "dep" / "greet.pgr").is_file()
        assert (root / pkg.MODULES_DIRNAME / "dep" / pkg.MANIFEST_FILENAME).is_file()
        assert (root / pkg.LOCKFILE_FILENAME).is_file()
        assert graph.packages["dep"].source_kind == "registry"

    def test_publish_returns_the_published_manifest(self, tmp_path: Path) -> None:
        package_dir = _make_pkg(tmp_path / "src", "dep")
        with running_server(tmp_path / "registry") as base_url:
            manifest = pkg.publish(package_dir, base_url)
        assert manifest.name == "dep"
        assert manifest.version == "0.1.0"

    def test_publish_rejects_a_duplicate_version(self, tmp_path: Path) -> None:
        package_dir = _make_pkg(tmp_path / "src", "dup")
        with running_server(tmp_path / "registry") as base_url:
            pkg.publish(package_dir, base_url)
            with pytest.raises(pkg.PkgError, match="already published"):
                pkg.publish(package_dir, base_url)

    def test_publish_then_install_round_trips_source_files(self, tmp_path: Path) -> None:
        package_dir = _make_pkg(tmp_path / "src", "widgets")
        (package_dir / "widgets.pgr").write_text("fn make() -> Int { 7 }", encoding="utf-8")
        with running_server(tmp_path / "registry") as base_url:
            pkg.publish(package_dir, base_url)
            root = _make_pkg(tmp_path, "root", {"widgets": pkg.RegistryDependency("widgets", "0.1.0")})
            pkg.install(root, registry_url=base_url)

        installed = root / pkg.MODULES_DIRNAME / "widgets" / "widgets.pgr"
        assert installed.read_text(encoding="utf-8") == "fn make() -> Int { 7 }"


class TestCli:
    def test_pkg_init_creates_manifest(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["pkg", "init", str(tmp_path), "--name", "demo"])
        assert result.exit_code == 0
        assert (tmp_path / pkg.MANIFEST_FILENAME).is_file()

    def test_pkg_add_then_install(self, tmp_path: Path) -> None:
        dep_dir = tmp_path / "dep"
        runner.invoke(app, ["pkg", "init", str(dep_dir), "--name", "dep"])
        root_dir = tmp_path / "root"
        runner.invoke(app, ["pkg", "init", str(root_dir), "--name", "root"])
        add_result = runner.invoke(app, ["pkg", "add", "dep", "--path", "../dep", "--dir", str(root_dir)])
        assert add_result.exit_code == 0
        install_result = runner.invoke(app, ["pkg", "install", "--dir", str(root_dir)])
        assert install_result.exit_code == 0
        assert (root_dir / pkg.MODULES_DIRNAME / "dep").is_dir()
        assert (root_dir / pkg.LOCKFILE_FILENAME).is_file()

    def test_pkg_add_requires_exactly_one_of_path_or_version(self, tmp_path: Path) -> None:
        runner.invoke(app, ["pkg", "init", str(tmp_path), "--name", "demo"])
        result = runner.invoke(app, ["pkg", "add", "dep", "--dir", str(tmp_path)])
        assert result.exit_code == 1
