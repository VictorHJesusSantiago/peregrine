"""Model registry.

Registers arbitrary trained model objects (anything picklable — kept generic rather than
sklearn-specific, since mlorch itself has no opinion on which ML framework produced the model)
under a `(name, version)` pair, with metadata: training params, metrics, a timestamp, and
lineage back to the dataset content address(es) — see `mlorch.versioning` — that produced the
training data. Versions are auto-incrementing integers per name, and `stage` promotion follows
the common staging -> production lifecycle, with "production" treated as a singleton per model
name (promoting a new version to production automatically demotes whichever version held it
before).

Deliberately in-memory only: the spec for this piece has no persistence requirement (unlike
the content store, which explicitly needs a local directory), and keeping model objects in a
plain dict avoids taking on pickle-to-disk lifecycle concerns for something not asked for.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any

from mlorch.errors import ModelRegistryError
from mlorch.versioning import ContentAddress


class Stage(Enum):
    NONE = "none"
    STAGING = "staging"
    PRODUCTION = "production"


@dataclass(frozen=True, slots=True)
class ModelMetadata:
    name: str
    version: int
    created_at: float
    params: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, float] = field(default_factory=dict)
    dataset_addresses: tuple[ContentAddress, ...] = ()
    stage: Stage = Stage.NONE


class ModelRegistry:
    def __init__(self) -> None:
        self._versions: dict[str, list[ModelMetadata]] = {}
        self._objects: dict[tuple[str, int], Any] = {}

    def register(
        self,
        name: str,
        model: Any,
        *,
        params: dict[str, Any] | None = None,
        metrics: dict[str, float] | None = None,
        dataset_addresses: tuple[ContentAddress, ...] = (),
    ) -> ModelMetadata:
        history = self._versions.setdefault(name, [])
        version = len(history) + 1
        meta = ModelMetadata(
            name=name,
            version=version,
            created_at=time.time(),
            params=dict(params or {}),
            metrics=dict(metrics or {}),
            dataset_addresses=tuple(dataset_addresses),
        )
        history.append(meta)
        self._objects[(name, version)] = model
        return meta

    def get_metadata(self, name: str, version: int | None = None) -> ModelMetadata:
        history = self._versions.get(name)
        if not history:
            raise ModelRegistryError(f"no model registered under name {name!r}")
        if version is None:
            return history[-1]
        for meta in history:
            if meta.version == version:
                return meta
        raise ModelRegistryError(f"model {name!r} has no version {version}")

    def get(self, name: str, version: int | None = None) -> Any:
        meta = self.get_metadata(name, version)
        return self._objects[(name, meta.version)]

    def history(self, name: str) -> tuple[ModelMetadata, ...]:
        return tuple(self._versions.get(name, ()))

    def promote(self, name: str, version: int, stage: Stage) -> ModelMetadata:
        history = self._versions.get(name)
        if not history:
            raise ModelRegistryError(f"no model registered under name {name!r}")

        if stage is Stage.PRODUCTION:
            # "production" is a singleton pointer per model name, not a label many versions
            # can hold at once -- promoting a new version demotes whichever one held it,
            # mirroring how real model registries treat the production slot.
            for i, m in enumerate(history):
                if m.stage is Stage.PRODUCTION:
                    history[i] = replace(m, stage=Stage.STAGING)

        for i, m in enumerate(history):
            if m.version == version:
                history[i] = replace(m, stage=stage)
                return history[i]
        raise ModelRegistryError(f"model {name!r} has no version {version}")

    def get_by_stage(self, name: str, stage: Stage) -> ModelMetadata:
        for m in reversed(self._versions.get(name, ())):
            if m.stage is stage:
                return m
        raise ModelRegistryError(f"no version of {name!r} is currently in stage {stage.value!r}")
