"""`FeatureIndex`: the everyday entry point for indexing geometries. It pairs the bbox-only
`RTree` with the `id -> Feature` mapping every caller needs anyway, so vector tile generation, the
query DSL, and tests all share one obvious way to load a dataset instead of each reinventing an id
scheme around a bare `RTree`.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field

from geospat.geometry import BBox, Geometry, bbox_of
from geospat.rtree import RTree


@dataclass(frozen=True, slots=True)
class Feature:
    id: int
    geom: Geometry
    properties: Mapping[str, object] = field(default_factory=dict)


class FeatureIndex:
    def __init__(self, max_entries: int = 8) -> None:
        self._rtree = RTree(max_entries=max_entries)
        self._features: dict[int, Feature] = {}
        self._next_id = 0

    def add(self, geom: Geometry, properties: Mapping[str, object] | None = None) -> int:
        fid = self._next_id
        self._next_id += 1
        self._features[fid] = Feature(fid, geom, properties or {})
        self._rtree.insert(fid, bbox_of(geom))
        return fid

    def get(self, feature_id: int) -> Feature:
        return self._features[feature_id]

    def query_bbox(self, bbox: BBox) -> list[Feature]:
        return [self._features[fid] for fid in self._rtree.range_query(bbox)]

    def __len__(self) -> int:
        return len(self._features)

    def __iter__(self) -> Iterator[Feature]:
        return iter(self._features.values())
