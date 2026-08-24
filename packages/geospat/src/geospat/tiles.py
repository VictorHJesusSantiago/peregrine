"""On-the-fly vector tiles: given an indexed set of geometries and a standard slippy-map `z/x/y`
tile coordinate, produce the subset of geometry actually visible in that tile, clipped to its
bounds.

Two scope notes, stated up front rather than implied:

- **Output format**: tiles are produced as this module's own `VectorTile`/`TileFeature`
  dataclasses (JSON/GeoJSON-shaped via `.to_dict()`/`.to_json()`), not the Mapbox Vector Tile
  protobuf wire format. Same information a real MVT tile would carry, different (much simpler,
  human-readable) encoding.
- **Projection**: tile bounds are computed with a *linear* split of `[-180, 180] x [-90, 90]` at
  each zoom level (an equirectangular grid), not the spherical Web Mercator projection real
  slippy-map tiles use. Web Mercator warps tile height by latitude (tiles get geographically
  smaller near the poles); this grid's tiles are uniform in degrees instead. The z/x/y addressing
  scheme and quadrant-doubling-per-zoom structure match the real thing — only the underlying
  projection math is simplified, consistent with not depending on a projection library anywhere in
  this package.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from geospat.errors import TileError
from geospat.geometry import BBox, Geometry, LineString, Point, Polygon
from geospat.index import FeatureIndex

WORLD_WIDTH = 360.0
WORLD_HEIGHT = 180.0


@dataclass(frozen=True, slots=True)
class TileCoord:
    z: int
    x: int
    y: int

    def __post_init__(self) -> None:
        if self.z < 0:
            raise TileError(f"zoom must be >= 0, got {self.z}")
        n = 2**self.z
        if not (0 <= self.x < n and 0 <= self.y < n):
            raise TileError(f"tile ({self.x}, {self.y}) out of range for zoom {self.z} (0..{n - 1})")


def tile_bbox(coord: TileCoord) -> BBox:
    n = 2**coord.z
    tile_w = WORLD_WIDTH / n
    tile_h = WORLD_HEIGHT / n
    minx = -180.0 + coord.x * tile_w
    maxy = 90.0 - coord.y * tile_h
    return BBox(minx, maxy - tile_h, minx + tile_w, maxy)


@dataclass(frozen=True, slots=True)
class TileFeature:
    id: int
    geom_type: str
    coordinates: object
    properties: Mapping[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "type": "Feature",
            "id": self.id,
            "geometry": {"type": self.geom_type, "coordinates": self.coordinates},
            "properties": dict(self.properties),
        }


@dataclass(frozen=True, slots=True)
class VectorTile:
    coord: TileCoord
    features: tuple[TileFeature, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "type": "FeatureCollection",
            "tile": {"z": self.coord.z, "x": self.coord.x, "y": self.coord.y},
            "features": [f.to_dict() for f in self.features],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict())


# -- clipping -----------------------------------------------------------------------------------


def clip_point(point: Point, bbox: BBox) -> Point | None:
    return point if bbox.contains_point(point) else None


def _clip_segment(p0: Point, p1: Point, bbox: BBox) -> tuple[Point, Point] | None:
    """Liang-Barsky parametric line clipping: walk the segment's parameter `t in [0, 1]` down to
    the sub-range that lies inside all four of the box's half-planes at once, which needs only
    four cheap divisions instead of Cohen-Sutherland's iterative region-code re-testing."""
    dx, dy = p1.x - p0.x, p1.y - p0.y
    t0, t1 = 0.0, 1.0
    for p, q in (
        (-dx, p0.x - bbox.minx),
        (dx, bbox.maxx - p0.x),
        (-dy, p0.y - bbox.miny),
        (dy, bbox.maxy - p0.y),
    ):
        if p == 0:
            if q < 0:
                return None
            continue
        t = q / p
        if p < 0:
            if t > t1:
                return None
            t0 = max(t0, t)
        else:
            if t < t0:
                return None
            t1 = min(t1, t)
    if t0 > t1:
        return None
    return (Point(p0.x + t0 * dx, p0.y + t0 * dy), Point(p0.x + t1 * dx, p0.y + t1 * dy))


def _points_close(a: Point, b: Point, eps: float = 1e-9) -> bool:
    return abs(a.x - b.x) < eps and abs(a.y - b.y) < eps


def clip_linestring(line: LineString, bbox: BBox) -> list[LineString]:
    """Clips each segment independently and merges consecutive surviving segments into chains
    where their endpoints line up. Segments that fall entirely outside the box break the chain.
    Not merged across a gap, and not simplified beyond that — a production tile clipper might also
    collapse near-duplicate points, which this does not do."""
    chains: list[list[Point]] = []
    current: list[Point] = []
    for a, b in line.segments:
        clipped = _clip_segment(a, b, bbox)
        if clipped is None:
            if current:
                chains.append(current)
                current = []
            continue
        ca, cb = clipped
        if current and _points_close(current[-1], ca):
            current.append(cb)
        else:
            if current:
                chains.append(current)
            current = [ca, cb]
    if current:
        chains.append(current)
    return [LineString(tuple(chain)) for chain in chains if len(chain) >= 2]


def _lerp_x(a: Point, b: Point, x: float) -> Point:
    t = (x - a.x) / (b.x - a.x)
    return Point(x, a.y + t * (b.y - a.y))


def _lerp_y(a: Point, b: Point, y: float) -> Point:
    t = (y - a.y) / (b.y - a.y)
    return Point(a.x + t * (b.x - a.x), y)


def _sutherland_hodgman_edge(
    points: list[Point],
    inside: Callable[[Point], bool],
    intersect: Callable[[Point, Point], Point],
) -> list[Point]:
    if not points:
        return []
    output: list[Point] = []
    prev = points[-1]
    prev_inside = inside(prev)
    for curr in points:
        curr_inside = inside(curr)
        if curr_inside:
            if not prev_inside:
                output.append(intersect(prev, curr))
            output.append(curr)
        elif prev_inside:
            output.append(intersect(prev, curr))
        prev, prev_inside = curr, curr_inside
    return output


def _clip_ring(ring: tuple[Point, ...], bbox: BBox) -> list[Point]:
    """Sutherland-Hodgman: clip the polygon ring against each of the box's four half-planes in
    turn, each pass consuming the previous pass's output ring. Only correct for a convex clip
    window, which a rectangle always is."""
    pts = list(ring)
    pts = _sutherland_hodgman_edge(pts, lambda p: p.x >= bbox.minx, lambda a, b: _lerp_x(a, b, bbox.minx))
    pts = _sutherland_hodgman_edge(pts, lambda p: p.x <= bbox.maxx, lambda a, b: _lerp_x(a, b, bbox.maxx))
    pts = _sutherland_hodgman_edge(pts, lambda p: p.y >= bbox.miny, lambda a, b: _lerp_y(a, b, bbox.miny))
    pts = _sutherland_hodgman_edge(pts, lambda p: p.y <= bbox.maxy, lambda a, b: _lerp_y(a, b, bbox.maxy))
    return pts


def clip_polygon(polygon: Polygon, bbox: BBox) -> Polygon | None:
    exterior = _clip_ring(polygon.exterior, bbox)
    if len(exterior) < 3:
        return None
    holes = tuple(
        clipped for hole in polygon.holes if len(clipped := tuple(_clip_ring(hole, bbox))) >= 3
    )
    return Polygon((tuple(exterior), *holes))


def _clip_geometry(geom: Geometry, bbox: BBox) -> list[Geometry]:
    match geom:
        case Point():
            clipped_point = clip_point(geom, bbox)
            return [clipped_point] if clipped_point is not None else []
        case LineString():
            lines: list[Geometry] = list(clip_linestring(geom, bbox))
            return lines
        case Polygon():
            clipped_polygon = clip_polygon(geom, bbox)
            return [clipped_polygon] if clipped_polygon is not None else []


def _coordinates_of(geom: Geometry) -> tuple[str, object]:
    match geom:
        case Point():
            return "Point", [geom.x, geom.y]
        case LineString():
            return "LineString", [[p.x, p.y] for p in geom.coords]
        case Polygon():
            return "Polygon", [[[p.x, p.y] for p in ring] for ring in geom.rings]


def generate_tile(index: FeatureIndex, coord: TileCoord) -> VectorTile:
    box = tile_bbox(coord)
    features: list[TileFeature] = []
    for feature in index.query_bbox(box):
        for clipped in _clip_geometry(feature.geom, box):
            geom_type, coordinates = _coordinates_of(clipped)
            features.append(TileFeature(feature.id, geom_type, coordinates, feature.properties))
    return VectorTile(coord, tuple(features))
