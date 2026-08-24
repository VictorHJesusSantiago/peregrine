"""The `pere` command (see `project.scripts` in `pyproject.toml`). Thin IO wiring only — every
subcommand delegates straight to an already-tested pipeline function (`vm.run_ast`,
`llvm_backend.run_llvm`, `types.infer_program`, `repl.main`) rather than re-implementing anything.

Conventionally, Peregrine source files use the `.pgr` extension — this is a convention, not an
enforced rule; `run`/`check` read whatever path they're given regardless of suffix.
"""

from __future__ import annotations

from pathlib import Path

import typer

from perelang import pkg, registry_server
from perelang.docs_gen import generate_docs
from perelang.errors import PeregrineError
from perelang.formatter import format_source
from perelang.ir import lower_program
from perelang.linter import lint_source
from perelang.llvm_backend import LlvmBackendUnsupportedError, run_llvm
from perelang.lsp_server import main as _lsp_main
from perelang.parser import parse_program
from perelang.repl import main as _repl_main
from perelang.types import infer_program
from perelang.vm import run_ast, value_to_str

app = typer.Typer(add_completion=False, no_args_is_help=True, help="The Peregrine language toolchain.")
pkg_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Package management: a local-directory registry (--registry DIR) or a real network "
    "registry (--registry-url URL, talking to `pere registry serve`) — see pkg.py.",
)
app.add_typer(pkg_app, name="pkg")
registry_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Run a real network package registry server (see registry_server.py).",
)
app.add_typer(registry_app, name="registry")


def _read_source(path: Path) -> str:
    if not path.is_file():
        typer.echo(f"error: no such file: {path}", err=True)
        raise typer.Exit(code=1)
    return path.read_text(encoding="utf-8")


@app.command()
def run(
    file: Path = typer.Argument(..., help="A Peregrine source file (conventionally *.pgr)"),  # noqa: B008
    backend: str = typer.Option(
        "bytecode", "--backend", help="Execution backend: 'bytecode' (vm.py) or 'llvm' (llvm_backend.py)"
    ),
    entry: str = typer.Option(
        "main", "--entry", help="Entry function to JIT and call (only meaningful for --backend llvm)"
    ),
) -> None:
    """Run a Peregrine source file end-to-end and print its final value."""
    source = _read_source(file)
    try:
        program = parse_program(source)
        if backend == "bytecode":
            typer.echo(value_to_str(run_ast(program)))
        elif backend == "llvm":
            typer.echo(str(run_llvm(lower_program(program), entry=entry)))
        else:
            typer.echo(f"error: unknown backend {backend!r} (expected 'bytecode' or 'llvm')", err=True)
            raise typer.Exit(code=1)
    except (PeregrineError, LlvmBackendUnsupportedError, ImportError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc


@app.command()
def check(file: Path = typer.Argument(..., help="A Peregrine source file (conventionally *.pgr)")) -> None:  # noqa: B008
    """Lex, parse, and type-check a file without running it — reports errors or 'ok'."""
    source = _read_source(file)
    try:
        infer_program(parse_program(source))
    except PeregrineError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo("ok")


@app.command()
def repl() -> None:
    """Launch the interactive REPL."""
    _repl_main()


@app.command()
def fmt(
    file: Path = typer.Argument(..., help="A Peregrine source file (conventionally *.pgr)"),  # noqa: B008
    check: bool = typer.Option(
        False, "--check", help="Report whether the file is already canonically formatted; write nothing"
    ),
) -> None:
    """Reformat a Peregrine source file to canonical style (see formatter.py)."""
    source = _read_source(file)
    try:
        formatted = format_source(source)
    except PeregrineError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    if check:
        if formatted != source:
            typer.echo(f"{file} is not formatted", err=True)
            raise typer.Exit(code=1)
        typer.echo(f"{file} is already formatted")
        return

    if formatted != source:
        file.write_text(formatted, encoding="utf-8")
    typer.echo(f"formatted {file}")


@app.command()
def lint(file: Path = typer.Argument(..., help="A Peregrine source file (conventionally *.pgr)")) -> None:  # noqa: B008
    """Run static checks beyond type-checking (see linter.py): unused bindings/parameters,
    identical if/else branches, shadowing. Exits nonzero if there are any findings."""
    source = _read_source(file)
    try:
        findings = lint_source(source)
    except PeregrineError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    if not findings:
        typer.echo("ok")
        return
    for finding in findings:
        typer.echo(f"{file}:{finding}")
    raise typer.Exit(code=1)


@app.command()
def docs(
    path: Path = typer.Argument(..., help="A Peregrine source file or a directory of *.pgr files"),  # noqa: B008
    output: Path = typer.Option(..., "-o", "--output", help="Where to write the generated Markdown"),  # noqa: B008
) -> None:
    """Generate Markdown API docs for every top-level fn/let declaration (see docs_gen.py)."""
    if not path.exists():
        typer.echo(f"error: no such file or directory: {path}", err=True)
        raise typer.Exit(code=1)
    try:
        markdown = generate_docs(path)
    except PeregrineError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    output.write_text(markdown, encoding="utf-8")
    typer.echo(f"wrote {output}")


@app.command()
def lsp() -> None:
    """Launch the Peregrine language server (JSON-RPC over stdio — see lsp_server.py)."""
    _lsp_main()


@pkg_app.command("init")
def pkg_init(
    directory: Path = typer.Argument(Path("."), help="Directory to create peregrine.toml in"),  # noqa: B008
    name: str | None = typer.Option(None, "--name", help="Package name (defaults to the directory name)"),
) -> None:
    """Create a new peregrine.toml manifest."""
    try:
        manifest = pkg.init_manifest(directory, name)
    except pkg.PkgError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"created {directory / pkg.MANIFEST_FILENAME} for package {manifest.name!r}")


@pkg_app.command("add")
def pkg_add(
    name: str = typer.Argument(..., help="Dependency name"),
    path: Path | None = typer.Option(None, "--path", help="Resolve this dependency to a local directory"),  # noqa: B008
    version: str | None = typer.Option(None, "--version", help="Resolve this dependency via the registry"),
    registry_name: str | None = typer.Option(
        None, "--registry-name", help="Name to look up in the registry (defaults to <name>)"
    ),
    directory: Path = typer.Option(Path("."), "--dir", help="Directory holding the manifest to edit"),  # noqa: B008
) -> None:
    """Add a dependency (either --path or --version, not both) to peregrine.toml."""
    if (path is None) == (version is None):
        typer.echo("error: specify exactly one of --path or --version", err=True)
        raise typer.Exit(code=1)
    dependency: pkg.Dependency
    if path is not None:
        # `.as_posix()`, not `str(path)`: manifests are meant to be portable text files, and a
        # bare `str()` on Windows would render backslashes — valid in a `Path`, but requiring
        # doubled-backslash escaping to round-trip through TOML (see pkg.py's `_toml_string`).
        # Forward slashes parse identically on every platform `pathlib.Path` runs on.
        dependency = pkg.PathDependency(path.as_posix())
    else:
        assert version is not None
        dependency = pkg.RegistryDependency(registry_name or name, version)
    try:
        pkg.add_dependency(directory, name, dependency)
    except pkg.PkgError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"added dependency {name!r} to {directory / pkg.MANIFEST_FILENAME}")


@pkg_app.command("install")
def pkg_install(
    directory: Path = typer.Option(Path("."), "--dir", help="Directory holding the manifest to install for"),  # noqa: B008
    registry: Path | None = typer.Option(  # noqa: B008
        None, "--registry", help="Local directory standing in for a package registry (see pkg.py)"
    ),
    registry_url: str | None = typer.Option(
        None, "--registry-url", help="Base URL of a running `pere registry serve` instance (network mode)"
    ),
) -> None:
    """Resolve the full dependency graph (with cycle detection) into pere_modules/ and write a lockfile."""
    if registry is not None and registry_url is not None:
        typer.echo("error: specify at most one of --registry or --registry-url", err=True)
        raise typer.Exit(code=1)
    try:
        graph = pkg.install(directory, registry_dir=registry, registry_url=registry_url)
    except pkg.PkgError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"installed {len(graph.packages)} package(s) into {directory / pkg.MODULES_DIRNAME}")


@pkg_app.command("publish")
def pkg_publish(
    directory: Path = typer.Option(Path("."), "--dir", help="Directory holding the manifest+package to publish"),  # noqa: B008
    registry_url: str = typer.Option(
        ..., "--registry-url", help="Base URL of a running `pere registry serve` instance"
    ),
) -> None:
    """Publish a package (its peregrine.toml + source files) to a network registry."""
    try:
        manifest = pkg.publish(directory, registry_url)
    except pkg.PkgError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"published {manifest.name}@{manifest.version} to {registry_url}")


@registry_app.command("serve")
def registry_serve(
    root: Path = typer.Option(Path("./registry_data"), "--root", help="Directory to store published packages in"),  # noqa: B008
    host: str = typer.Option("127.0.0.1", "--host", help="Host/interface to bind to"),
    port: int = typer.Option(8765, "--port", help="Port to listen on"),
) -> None:
    """Run a network package registry server (blocks until interrupted — Ctrl+C to stop)."""
    typer.echo(f"serving registry from {root} on http://{host}:{port}")
    registry_server.serve(root, host, port)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
