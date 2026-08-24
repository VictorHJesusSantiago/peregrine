"""A spatial query DSL, built as a fluent/builder Python API rather than a parsed textual
language: `Query(index).within(bbox).intersects(other_geom).at_resolution(5).execute()`.

This is the explicitly-scoped alternative the package's brief allows in place of a full custom
parser: composing spatial predicates through chained method calls reads like a small query
language and is executed lazily (nothing runs until `.execute()`), but there is no tokenizer, no
grammar, and no textual query string anywhere — every "query" is just Python method calls
building up a `Query` object. If a real parsed DSL is wanted later, `perelang`'s hand-written
lexer/parser (`packages/perelang/src/perelang/lexer.py`, `parser.py`) is this monorepo's
established pattern for that; nothing here depends on it.
"""

from __future__ import annotations

from geospat.errors import QueryError
from geospat.geometry import BBox, Geometry, Point, bbox_of, distance_point_to_geometry, geometries_intersect
from geospat.hexgrid import cell_bbox, cell_for_point
from geospat.index import Feature, FeatureIndex


class Query:
    def __init__(self, index: FeatureIndex) -> None:
        self._index = index
        self._bbox: BBox | None = None
        self._intersects_geom: Geometry | None = None
        self._near: tuple[Point, float] | None = None
        self._resolution: int | None = None
        self._limit: int | None = None

    def within(self, bbox: BBox) -> Query:
        """Keep only features whose bounding box intersects `bbox`."""
        self._bbox = bbox
        return self

    def intersects(self, geom: Geometry) -> Query:
        """Keep only features that actually intersect `geom` (exact geometric test, not just a
        bounding-box overlap)."""
        self._intersects_geom = geom
        return self

    def near(self, point: Point, radius: float) -> Query:
        """Keep only features within `radius` of `point`, sorted nearest-first — the
        nearest-neighbor-within-radius predicate."""
        if radius < 0:
            raise QueryError("radius must be non-negative")
        self._near = (point, radius)
        return self

    def at_resolution(self, resolution: int) -> Query:
        """Restrict the search to the single hierarchical grid cell (see `geospat.hexgrid`) at
        `resolution` containing the query's spatial anchor — the center of `within(...)`'s bbox,
        or `near(...)`'s point if no bbox was given. Requires one of those to already be set,
        since a resolution alone has no location to anchor the cell to."""
        if resolution < 0:
            raise QueryError("resolution must be >= 0")
        self._resolution = resolution
        return self

    def limit(self, count: int) -> Query:
        """Cap the number of results (applied after `near(...)`'s nearest-first sort, if present,
        making this a top-k nearest-neighbor query)."""
        if count < 0:
            raise QueryError("limit must be non-negative")
        self._limit = count
        return self

    def _anchor_point(self) -> Point:
        if self._bbox is not None:
            return self._bbox.center
        if self._near is not None:
            return self._near[0]
        raise QueryError("at_resolution(...) needs a within(...) or near(...) anchor point")

    def _resolution_bbox(self) -> BBox | None:
        if self._resolution is None:
            return None
        cell = cell_for_point(self._anchor_point(), self._resolution)
        return cell_bbox(cell)

    def _candidate_bbox(self) -> BBox | None:
        """The tightest bounding box that could possibly contain a match, used to prune the
        R-tree search before any exact geometric predicate runs. `None` means "no constraint was
        given, scan everything"; a disjoint pair of constraints raises early rather than silently
        returning nothing, since a `within(...)` and `near(...)` that share no area is far more
        likely a caller bug than an intentionally empty query."""
        boxes: list[BBox] = []
        if self._bbox is not None:
            boxes.append(self._bbox)
        if self._intersects_geom is not None:
            boxes.append(bbox_of(self._intersects_geom))
        if self._near is not None:
            point, radius = self._near
            boxes.append(BBox(point.x - radius, point.y - radius, point.x + radius, point.y + radius))
        resolution_box = self._resolution_bbox()
        if resolution_box is not None:
            boxes.append(resolution_box)

        if not boxes:
            return None
        result = boxes[0]
        for box in boxes[1:]:
            intersection = result.intersection(box)
            if intersection is None:
                raise QueryError("query predicates describe mutually disjoint regions")
            result = intersection
        return result

    def execute(self) -> list[Feature]:
        candidate_bbox = self._candidate_bbox()
        candidates = self._index.query_bbox(candidate_bbox) if candidate_bbox is not None else list(self._index)

        results: list[Feature] = []
        for feature in candidates:
            if self._bbox is not None and not self._bbox.intersects(bbox_of(feature.geom)):
                continue
            if self._intersects_geom is not None and not geometries_intersect(
                feature.geom, self._intersects_geom
            ):
                continue
            resolution_box = self._resolution_bbox()
            if resolution_box is not None and not resolution_box.intersects(bbox_of(feature.geom)):
                continue
            results.append(feature)

        if self._near is not None:
            point, radius = self._near
            scored = [(distance_point_to_geometry(point, f.geom), f) for f in results]
            scored = [pair for pair in scored if pair[0] <= radius]
            scored.sort(key=lambda pair: pair[0])
            results = [f for _, f in scored]

        if self._limit is not None:
            results = results[: self._limit]
        return results
