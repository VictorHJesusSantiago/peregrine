"""Online/offline feature store.

A `FeatureDefinition` names a feature, the entity-key column it's computed against, its
declared dtype, and a pure function computing it from a raw-data `DataFrame` — one definition
drives both offline batch computation and (after materialization) online lookups, so there is
exactly one place a feature's logic lives.

`OfflineFeatureStore` holds batch-computed history: one row per (entity, timestamp) pair, for
every feature in a named "feature view". This is the store you'd train a model against — it
can answer "what was this feature at this time".

`OnlineFeatureStore` is an in-memory, dict-backed point-lookup store: entity key ->
{feature_name: latest_value}. This is the store you'd serve predictions against — it only ever
knows the *latest* value per entity, by design (a real online store like Redis has the same
shape; this just keeps it in-process rather than reaching for an external dependency, per the
spec's own suggestion that this doesn't need Redis).

`materialize()` is the bridge: it reads the latest offline value per entity for a feature view
and pushes it into an online store, which is the standard offline-batch -> online-serving
feature store pattern.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pandas as pd

from mlorch.errors import FeatureStoreError


@dataclass(frozen=True, slots=True)
class FeatureDefinition:
    name: str
    entity_key: str
    dtype: type
    compute: Callable[[pd.DataFrame], pd.Series]


class OfflineFeatureStore:
    """Batch-computed feature history, grouped into named "feature views"."""

    def __init__(self) -> None:
        self._tables: dict[str, pd.DataFrame] = {}

    def write(
        self,
        view_name: str,
        raw: pd.DataFrame,
        features: list[FeatureDefinition],
        *,
        timestamp_col: str = "timestamp",
    ) -> pd.DataFrame:
        """Compute every feature in `features` over `raw` and append the result to
        `view_name`'s history. All features in one call must share an entity key column, since
        the resulting rows are keyed by a single `entity` column."""
        if not features:
            raise FeatureStoreError("write() requires at least one feature definition")
        if timestamp_col not in raw.columns:
            raise FeatureStoreError(f"raw data is missing timestamp column {timestamp_col!r}")

        entity_key = features[0].entity_key
        out = pd.DataFrame({"entity": raw[entity_key], "timestamp": raw[timestamp_col]})
        for feature in features:
            if feature.entity_key not in raw.columns:
                raise FeatureStoreError(
                    f"raw data is missing entity key column {feature.entity_key!r} "
                    f"required by feature {feature.name!r}"
                )
            out[feature.name] = feature.compute(raw)

        existing = self._tables.get(view_name)
        self._tables[view_name] = (
            pd.concat([existing, out], ignore_index=True) if existing is not None else out
        )
        return out

    def read(self, view_name: str) -> pd.DataFrame:
        table = self._tables.get(view_name)
        if table is None:
            raise FeatureStoreError(f"no feature view named {view_name!r} has been written")
        return table

    def latest_per_entity(self, view_name: str) -> pd.DataFrame:
        """The most recent row (by `timestamp`) for each distinct entity in `view_name`."""
        table = self.read(view_name).sort_values("timestamp")
        result: pd.DataFrame = table.groupby("entity", as_index=False).tail(1)
        return result.reset_index(drop=True)


class OnlineFeatureStore:
    """In-memory point-lookup store: entity key -> {feature_name: latest value}."""

    def __init__(self) -> None:
        self._data: dict[Any, dict[str, Any]] = {}

    def set_many(self, entity: Any, values: dict[str, Any]) -> None:
        self._data.setdefault(entity, {}).update(values)

    def get(self, entity: Any, feature_name: str) -> Any:
        try:
            return self._data[entity][feature_name]
        except KeyError:
            raise FeatureStoreError(
                f"no online value for feature {feature_name!r} on entity {entity!r}"
            ) from None

    def get_all(self, entity: Any) -> dict[str, Any]:
        return dict(self._data.get(entity, {}))

    def entities(self) -> tuple[Any, ...]:
        return tuple(self._data.keys())


def materialize(
    offline: OfflineFeatureStore,
    online: OnlineFeatureStore,
    view_name: str,
    feature_names: list[str],
) -> int:
    """Push the latest offline value of each named feature, per entity, into `online`.

    Returns the number of entities materialized. This is intentionally a full snapshot
    overwrite of the given features per entity (not an incremental diff) — offline batch runs
    are the source of truth here, so "materialize" means "make online match offline's latest",
    not "append online history".
    """
    latest = offline.latest_per_entity(view_name)
    count = 0
    for _, row in latest.iterrows():
        online.set_many(row["entity"], {name: row[name] for name in feature_names})
        count += 1
    return count
