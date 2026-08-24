"""Hand-written geometric value types and the predicates the rest of the package is built on.

Everything here is planar (a flat x/y plane, not a spherical or ellipsoidal earth model) — when
`x`/`y` are used to mean longitude/latitude elsewhere in this package (the hex grid, vector
tiles), that is an explicit, documented simplification, never a claim of geodesic correctness.

Value types (`Point`, `BBox`, `LineString`, `Polygon`) are frozen dataclasses: once built they are
never mutated in place, which is what lets them be shared as dictionary keys, stashed in an R-tree
leaf, or handed to multiple callers without defensive copying.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from geospat.errors import InvalidGeometryError


@dataclass(frozen=True, slots=True)
class Point:
    x: float
    y: float

    def distance_to(self, other: Point) -> float:
        return math.hypot(self.x - other.x, self.y - other.y)

    @property
    def bbox(self) -> BBox:
        return BBox(self.x, self.y, self.x, self.y)


@dataclass(frozen=True, slots=True)
class BBox:
    """An axis-aligned bounding box, `[minx, maxx] x [miny, maxy]`, inclusive on both ends."""

    minx: float
    miny: float
    maxx: float
    maxy: float

    def __post_init__(self) -> None:
        if self.minx > self.maxx or self.miny > self.maxy:
            raise InvalidGeometryError(
                f"degenerate bbox: min ({self.minx}, {self.miny}) exceeds max "
                f"({self.maxx}, {self.maxy})"
            )

    @property
    def width(self) -> float:
        return self.maxx - self.minx

    @property
    def height(self) -> float:
        return self.maxy - self.miny

    @property
    def area(self) -> float:
        return self.width * self.height

    @property
    def center(self) -> Point:
        return Point((self.minx + self.maxx) / 2, (self.miny + self.maxy) / 2)

    def intersects(self, other: BBox) -> bool:
        return (
            self.minx <= other.maxx
            and self.maxx >= other.minx
            and self.miny <= other.maxy
            and self.maxy >= other.miny
        )

    def contains_point(self, point: Point) -> bool:
        return self.minx <= point.x <= self.maxx and self.miny <= point.y <= self.maxy

    def contains_bbox(self, other: BBox) -> bool:
        return (
            self.minx <= other.minx
            and self.miny <= other.miny
            and self.maxx >= other.maxx
            and self.maxy >= other.maxy
        )

    def intersection(self, other: BBox) -> BBox | None:
        if not self.intersects(other):
            return None
        return BBox(
            max(self.minx, other.minx),
            max(self.miny, other.miny),
            min(self.maxx, other.maxx),
            min(self.maxy, other.maxy),
        )

    def union(self, other: BBox) -> BBox:
        return BBox(
            min(self.minx, other.minx),
            min(self.miny, other.miny),
            max(self.maxx, other.maxx),
            max(self.maxy, other.maxy),
        )

    def enlargement(self, other: BBox) -> float:
        """How much this box's area would grow to also cover `other`. Used by the R-tree to pick
        the cheapest subtree to insert into (Guttman's "least enlargement" heuristic)."""
        return self.union(other).area - self.area

    def expand(self, margin: float) -> BBox:
        return BBox(self.minx - margin, self.miny - margin, self.maxx + margin, self.maxy + margin)


@dataclass(frozen=True, slots=True)
class LineString:
    coords: tuple[Point, ...]

    def __post_init__(self) -> None:
        if len(self.coords) < 2:
            raise InvalidGeometryError("a LineString needs at least two points")

    @property
    def bbox(self) -> BBox:
        xs = [p.x for p in self.coords]
        ys = [p.y for p in self.coords]
        return BBox(min(xs), min(ys), max(xs), max(ys))

    @property
    def segments(self) -> list[tuple[Point, Point]]:
        # Deliberately not `strict=True`: `coords[1:]` is always exactly one shorter than
        # `coords`, so this zip always stops one pair early by construction (N points -> N-1
        # segments) — that is the correct, intended truncation, not a length mismatch bug.
        return list(zip(self.coords, self.coords[1:], strict=False))


@dataclass(frozen=True, slots=True)
class Polygon:
    """A simple polygon: `rings[0]` is the exterior ring, `rings[1:]` are holes. Rings are plain
    coordinate lists (no enforced closure, no topology validation against self-intersection) —
    this is the "simple polygon, no full topology" scope explicitly allowed for this package."""

    rings: tuple[tuple[Point, ...], ...]

    def __post_init__(self) -> None:
        if not self.rings or len(self.rings[0]) < 3:
            raise InvalidGeometryError("a Polygon needs an exterior ring of at least three points")

    @property
    def exterior(self) -> tuple[Point, ...]:
        return self.rings[0]

    @property
    def holes(self) -> tuple[tuple[Point, ...], ...]:
        return self.rings[1:]

    @property
    def bbox(self) -> BBox:
        xs = [p.x for p in self.exterior]
        ys = [p.y for p in self.exterior]
        return BBox(min(xs), min(ys), max(xs), max(ys))


type Geometry = Point | LineString | Polygon


def bbox_of(geom: Geometry) -> BBox:
    match geom:
        case Point():
            return geom.bbox
        case LineString():
            return geom.bbox
        case Polygon():
            return geom.bbox


def _ring_edges(ring: tuple[Point, ...]) -> list[tuple[Point, Point]]:
    return list(zip(ring, ring[1:] + ring[:1], strict=True))


def point_in_ring(point: Point, ring: tuple[Point, ...]) -> bool:
    """Standard even-odd ray-casting test: cast a ray in the +x direction from `point` and count
    how many ring edges it crosses. An odd count means the point is inside."""
    inside = False
    for a, b in _ring_edges(ring):
        crosses = (a.y > point.y) != (b.y > point.y)
        if crosses:
            x_at_y = a.x + (point.y - a.y) * (b.x - a.x) / (b.y - a.y)
            if point.x < x_at_y:
                inside = not inside
    return inside


def point_in_polygon(point: Point, polygon: Polygon) -> bool:
    if not point_in_ring(point, polygon.exterior):
        return False
    return not any(point_in_ring(point, hole) for hole in polygon.holes)


def _on_segment(point: Point, a: Point, b: Point, eps: float = 1e-9) -> bool:
    cross = (b.x - a.x) * (point.y - a.y) - (b.y - a.y) * (point.x - a.x)
    if abs(cross) > eps:
        return False
    return min(a.x, b.x) - eps <= point.x <= max(a.x, b.x) + eps and (
        min(a.y, b.y) - eps <= point.y <= max(a.y, b.y) + eps
    )


def _orientation(a: Point, b: Point, c: Point) -> int:
    val = (b.x - a.x) * (c.y - a.y) - (b.y - a.y) * (c.x - a.x)
    if abs(val) < 1e-12:
        return 0
    return 1 if val > 0 else -1


def segments_intersect(p1: Point, p2: Point, p3: Point, p4: Point) -> bool:
    """Classic orientation-based segment intersection test, including the collinear-overlap
    special cases."""
    o1, o2 = _orientation(p1, p2, p3), _orientation(p1, p2, p4)
    o3, o4 = _orientation(p3, p4, p1), _orientation(p3, p4, p2)
    if o1 != o2 and o3 != o4:
        return True
    if o1 == 0 and _on_segment(p3, p1, p2):
        return True
    if o2 == 0 and _on_segment(p4, p1, p2):
        return True
    if o3 == 0 and _on_segment(p1, p3, p4):
        return True
    return bool(o4 == 0 and _on_segment(p2, p3, p4))


def point_on_linestring(point: Point, line: LineString) -> bool:
    return any(_on_segment(point, a, b) for a, b in line.segments)


def linestrings_intersect(a: LineString, b: LineString) -> bool:
    if not a.bbox.intersects(b.bbox):
        return False
    return any(
        segments_intersect(p1, p2, p3, p4) for p1, p2 in a.segments for p3, p4 in b.segments
    )


def linestring_intersects_polygon(line: LineString, polygon: Polygon) -> bool:
    if not line.bbox.intersects(polygon.bbox):
        return False
    if any(point_in_polygon(p, polygon) for p in line.coords):
        return True
    rings = (polygon.exterior, *polygon.holes)
    return any(
        segments_intersect(p1, p2, ra, rb)
        for p1, p2 in line.segments
        for ring in rings
        for ra, rb in _ring_edges(ring)
    )


def polygons_intersect(a: Polygon, b: Polygon) -> bool:
    """Real, but not exhaustive: true for edge crossings and for either polygon's vertices lying
    inside the other, which covers every case except two polygons that overlap with none of their
    edges crossing and none of their vertices inside one another (e.g. a plus-sign through a
    square with no vertex containment) — a rare configuration for the simple road/parcel-style
    polygons this package targets, but a real gap worth naming rather than hiding."""
    if not a.bbox.intersects(b.bbox):
        return False
    if any(point_in_polygon(p, b) for p in a.exterior):
        return True
    if any(point_in_polygon(p, a) for p in b.exterior):
        return True
    a_edges = [e for ring in (a.exterior, *a.holes) for e in _ring_edges(ring)]
    b_edges = [e for ring in (b.exterior, *b.holes) for e in _ring_edges(ring)]
    return any(segments_intersect(p1, p2, p3, p4) for p1, p2 in a_edges for p3, p4 in b_edges)


def _point_to_segment_distance(point: Point, a: Point, b: Point) -> float:
    dx, dy = b.x - a.x, b.y - a.y
    if dx == 0 and dy == 0:
        return point.distance_to(a)
    t = ((point.x - a.x) * dx + (point.y - a.y) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return point.distance_to(Point(a.x + t * dx, a.y + t * dy))


def distance_point_to_geometry(point: Point, geom: Geometry) -> float:
    """Euclidean distance from `point` to the nearest part of `geom` — `0.0` whenever `point` is
    inside a `Polygon`, since "distance to a filled region" means distance to its interior, not
    just its boundary. Used by the query DSL's `near(...)` predicate."""
    match geom:
        case Point():
            return point.distance_to(geom)
        case LineString():
            return min(_point_to_segment_distance(point, a, b) for a, b in geom.segments)
        case Polygon():
            if point_in_polygon(point, geom):
                return 0.0
            rings = (geom.exterior, *geom.holes)
            return min(
                _point_to_segment_distance(point, a, b) for ring in rings for a, b in _ring_edges(ring)
            )


def geometries_intersect(a: Geometry, b: Geometry) -> bool:
    """Dispatches on the runtime shape of both operands. `match` on a tuple of the two geometries
    keeps every pairing explicit instead of routing through a matrix of `isinstance` checks."""
    match (a, b):
        case (Point(), Point()):
            return a == b
        case (Point(), LineString()):
            return point_on_linestring(a, b)
        case (LineString(), Point()):
            return point_on_linestring(b, a)
        case (Point(), Polygon()):
            return point_in_polygon(a, b)
        case (Polygon(), Point()):
            return point_in_polygon(b, a)
        case (LineString(), LineString()):
            return linestrings_intersect(a, b)
        case (LineString(), Polygon()):
            return linestring_intersects_polygon(a, b)
        case (Polygon(), LineString()):
            return linestring_intersects_polygon(b, a)
        case (Polygon(), Polygon()):
            return polygons_intersect(a, b)
        case _:  # pragma: no cover - exhaustive over Geometry
            raise InvalidGeometryError(f"unsupported geometry pair: {type(a)}, {type(b)}")
