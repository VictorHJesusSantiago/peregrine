"""Content-addressed dataset versioning.

A dataset's *content address* is the SHA-256 hash of a canonical byte serialization of its
content. Two datasets with byte-identical canonical content always hash to the same address —
this is what makes an address a trustworthy pointer to exact content (not just a label someone
attached), and it gives storage deduplication for free: writing the same content twice under
the same or different logical names stores the bytes on disk exactly once.

`ContentStore` is a local, directory-backed store: content objects live under
`<root>/objects/`, keyed by address, and a `<root>/manifest.json` maps each logical dataset
name to the ordered history of content addresses it has pointed at. This deliberately does not
attempt any cloud/remote backend — a local directory is what the spec calls for, and adding a
network layer here would be scope creep with nothing to verify it against.
"""

from __future__ import annotations

import hashlib
import io
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict, cast

import pandas as pd

from mlorch.errors import ContentStoreError


class _ManifestEntry(TypedDict):
    address: str
    created_at: float
    row_count: int
    columns: list[str]

ContentAddress = str
"""A string of the form 'sha256:<hex digest>'."""


def hash_bytes(data: bytes) -> ContentAddress:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def canonicalize_dataframe(df: pd.DataFrame) -> bytes:
    """Canonical byte serialization of a DataFrame's content.

    Columns are sorted by name so that column *order* (which carries no semantic meaning for
    a set of named features) doesn't change the address, but cell values and row order are
    preserved exactly — row order can be semantically meaningful (e.g. time-ordered data), so
    it is not normalized away. CSV is used over something like `to_json` because it is a
    stable, simple, dependency-free canonical form for tabular data.
    """
    ordered = df.reindex(sorted(df.columns), axis=1)
    csv_text: str = str(ordered.to_csv(index=False))
    return csv_text.encode("utf-8")


def hash_dataframe(df: pd.DataFrame) -> ContentAddress:
    return hash_bytes(canonicalize_dataframe(df))


@dataclass(frozen=True, slots=True)
class DatasetVersion:
    """One entry in a logical dataset's version history: a name pointing at a content
    address, plus enough metadata to inspect it without fetching the full content."""

    name: str
    address: ContentAddress
    created_at: float
    row_count: int
    columns: tuple[str, ...]


class ContentStore:
    """Local, directory-backed content-addressed store for `pandas.DataFrame` datasets."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._objects_dir = root / "objects"
        self._manifest_path = root / "manifest.json"
        self._objects_dir.mkdir(parents=True, exist_ok=True)
        if not self._manifest_path.exists():
            self._manifest_path.write_text("{}", encoding="utf-8")

    def _object_path(self, address: ContentAddress) -> Path:
        return self._objects_dir / address.replace(":", "_")

    def _read_manifest(self) -> dict[str, list[_ManifestEntry]]:
        raw = json.loads(self._manifest_path.read_text(encoding="utf-8"))
        return cast(dict[str, list[_ManifestEntry]], raw)

    def _write_manifest(self, manifest: dict[str, list[_ManifestEntry]]) -> None:
        self._manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    def put_dataframe(self, name: str, df: pd.DataFrame) -> DatasetVersion:
        """Store `df` under logical name `name`, returning the resulting version.

        If identical content was already stored (anywhere, under any name), the object on
        disk is not rewritten — that's the dedup guarantee. If the *immediately preceding*
        version for this logical name already points at the same address, no new history
        entry is appended either, so re-running a task that produces unchanged output doesn't
        bloat the version history with no-op duplicates.
        """
        content = canonicalize_dataframe(df)
        address = hash_bytes(content)
        obj_path = self._object_path(address)
        if not obj_path.exists():
            obj_path.write_bytes(content)

        version = DatasetVersion(
            name=name,
            address=address,
            created_at=time.time(),
            row_count=len(df),
            columns=tuple(sorted(df.columns.astype(str))),
        )

        manifest = self._read_manifest()
        history = manifest.setdefault(name, [])
        if not history or history[-1]["address"] != address:
            history.append(
                _ManifestEntry(
                    address=version.address,
                    created_at=version.created_at,
                    row_count=version.row_count,
                    columns=list(version.columns),
                )
            )
            self._write_manifest(manifest)
        return version

    def get_dataframe(self, address: ContentAddress) -> pd.DataFrame:
        obj_path = self._object_path(address)
        if not obj_path.exists():
            raise ContentStoreError(f"no object stored for address {address!r}")
        return pd.read_csv(io.StringIO(obj_path.read_text(encoding="utf-8")))

    def history(self, name: str) -> tuple[DatasetVersion, ...]:
        manifest = self._read_manifest()
        entries = manifest.get(name)
        if not entries:
            raise ContentStoreError(f"no dataset named {name!r} has ever been stored")
        return tuple(
            DatasetVersion(
                name=name,
                address=e["address"],
                created_at=e["created_at"],
                row_count=e["row_count"],
                columns=tuple(e["columns"]),
            )
            for e in entries
        )

    def latest(self, name: str) -> DatasetVersion:
        return self.history(name)[-1]

    def names(self) -> tuple[str, ...]:
        return tuple(self._read_manifest().keys())
