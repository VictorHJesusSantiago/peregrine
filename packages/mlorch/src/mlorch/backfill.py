"""Incremental backfill for time-partitioned datasets.

Given a list of partition keys (e.g. date strings) and a function that computes one
partition's output DataFrame, `run_backfill` computes only the partitions that don't already
have committed output, skipping the rest — unless `force=True`, which recomputes everything.

Rather than inventing a second manifest format, "does partition P already have output" is
answered by asking the `mlorch.versioning.ContentStore` (see that module) whether the logical
dataset name `f"{dataset_name}/{partition}"` has any version history: if it does, that
partition is done and its latest content address is the recorded output. This reuses the
content store's existing name -> history mapping instead of tracking a second, parallel source
of truth that could drift out of sync with it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import pandas as pd

from mlorch.errors import ContentStoreError
from mlorch.versioning import ContentAddress, ContentStore


@dataclass(frozen=True, slots=True)
class BackfillResult:
    computed: tuple[str, ...]
    skipped: tuple[str, ...]
    addresses: dict[str, ContentAddress]


class BackfillManifest:
    """Answers "is this partition of this dataset already done" by querying a `ContentStore`."""

    def __init__(self, store: ContentStore, dataset_name: str) -> None:
        self._store = store
        self._dataset_name = dataset_name

    def logical_name(self, partition: str) -> str:
        return f"{self._dataset_name}/{partition}"

    def is_done(self, partition: str) -> ContentAddress | None:
        try:
            return self._store.latest(self.logical_name(partition)).address
        except ContentStoreError:
            return None


def run_backfill(
    partitions: list[str],
    compute_fn: Callable[[str], pd.DataFrame],
    store: ContentStore,
    dataset_name: str,
    *,
    force: bool = False,
) -> BackfillResult:
    """Compute and version each partition in `partitions` that isn't already done.

    `compute_fn(partition)` is only called for partitions actually being (re)computed --
    with `force=False` (the default) and every partition already done, `compute_fn` is never
    called at all, which is the whole point of incremental backfill.
    """
    manifest = BackfillManifest(store, dataset_name)
    computed: list[str] = []
    skipped: list[str] = []
    addresses: dict[str, ContentAddress] = {}

    for partition in partitions:
        existing = None if force else manifest.is_done(partition)
        if existing is not None:
            skipped.append(partition)
            addresses[partition] = existing
            continue
        df = compute_fn(partition)
        version = store.put_dataframe(manifest.logical_name(partition), df)
        addresses[partition] = version.address
        computed.append(partition)

    return BackfillResult(computed=tuple(computed), skipped=tuple(skipped), addresses=addresses)
