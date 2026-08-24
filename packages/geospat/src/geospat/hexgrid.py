"""A hierarchical spatial grid in the spirit of Uber's H3, with one deliberate simplification
spelled out up front so it is never mistaken for the real thing:

**This is NOT true H3.** Real H3 tiles the sphere in hexagons derived from a projected
icosahedron, which makes cells nearly uniform in area and gives every cell exactly six neighbors
(except twelve pentagon cells per resolution). That cell geometry is a substantial piece of
computational geometry on its own — icosahedral projection, hexagon-vs-pentagon handling at the
twelve singular vertices — well beyond what this package's other four subsystems leave room for.

What is implemented instead is a **quadtree over an equirectangular lon/lat world**, indexed with
a **quadkey** (the same style of path-encoded tile id Bing Maps / slippy-map tiling uses):
resolution 0 is the single cell covering the whole world `[-180, 180] x [-90, 90]`; each step down
in resolution splits a cell into four quadrants, and a cell's id is the string of quadrant digits
(`"0"`=SW, `"1"`=SE, `"2"`=NW, `"3"`=NE) from the root down to it. This gives the same *interface*
H3 is used for here — assign a point to a cell at a resolution, walk to parent/child resolutions,
find neighbors — without pretending to be hexagonal or sphere-aware. Cell area is not uniform in
true surface terms either, for the same equirectangular-vs-sphere reason `geospat.tiles` documents
for tile bounding boxes.
"""

from __future__ import annotations

from dataclasses import dataclass

from geospat.errors import InvalidGeometryError
from geospat.geometry import BBox, Point

WORLD_BBOX = BBox(-180.0, -90.0, 180.0, 90.0)

_QUADRANT_ORDER = ("0", "1", "2", "3")  # SW, SE, NW, NE


@dataclass(frozen=True, slots=True)
class Cell:
    """`id` is the empty string at resolution 0 (the whole world), and one quadrant digit longer
    per resolution below that; `resolution == len(id)` always holds."""

    id: str

    @property
    def resolution(self) -> int:
        return len(self.id)


def _quadrant_bbox(parent: BBox, digit: str) -> BBox:
    midx = (parent.minx + parent.maxx) / 2
    midy = (parent.miny + parent.maxy) / 2
    match digit:
        case "0":
            return BBox(parent.minx, parent.miny, midx, midy)
        case "1":
            return BBox(midx, parent.miny, parent.maxx, midy)
        case "2":
            return BBox(parent.minx, midy, midx, parent.maxy)
        case "3":
            return BBox(midx, midy, parent.maxx, parent.maxy)
        case _:
            raise InvalidGeometryError(f"invalid quadrant digit {digit!r}")


def cell_bbox(cell: Cell) -> BBox:
    box = WORLD_BBOX
    for digit in cell.id:
        box = _quadrant_bbox(box, digit)
    return box


def cell_for_point(point: Point, resolution: int) -> Cell:
    if resolution < 0:
        raise InvalidGeometryError("resolution must be >= 0")
    if not WORLD_BBOX.contains_point(point):
        raise InvalidGeometryError(f"point {point} is outside the world bbox {WORLD_BBOX}")
    digits: list[str] = []
    box = WORLD_BBOX
    for _ in range(resolution):
        midx = (box.minx + box.maxx) / 2
        midy = (box.miny + box.maxy) / 2
        west = point.x <= midx
        south = point.y <= midy
        digit = "0" if (west and south) else "1" if (not west and south) else "2" if (west and not south) else "3"
        digits.append(digit)
        box = _quadrant_bbox(box, digit)
    return Cell("".join(digits))


def parent(cell: Cell) -> Cell:
    if cell.resolution == 0:
        raise InvalidGeometryError("the resolution-0 root cell has no parent")
    return Cell(cell.id[:-1])


def children(cell: Cell) -> tuple[Cell, Cell, Cell, Cell]:
    sw, se, nw, ne = (Cell(cell.id + d) for d in _QUADRANT_ORDER)
    return (sw, se, nw, ne)


def ancestor(cell: Cell, resolution: int) -> Cell:
    if not 0 <= resolution <= cell.resolution:
        raise InvalidGeometryError(f"resolution {resolution} is not an ancestor level of {cell}")
    return Cell(cell.id[:resolution])


def neighbors(cell: Cell) -> list[Cell]:
    """The up-to-8 cells sharing an edge or corner with `cell`, found by nudging a point just past
    each side/corner of `cell`'s own bbox and re-deriving the cell id at the same resolution —
    simple and correct for a quadtree grid, unlike true H3 where neighbor lookup needs dedicated
    direction tables because hexagon adjacency isn't representable as a coordinate offset.
    Neighbors that would fall outside the world bbox (at the poles/antimeridian) are omitted
    rather than wrapped, since this grid does not model the sphere's wraparound topology.
    """
    box = cell_bbox(cell)
    eps_x = box.width / 2
    eps_y = box.height / 2
    offsets = [
        (-eps_x, -eps_y), (0.0, -eps_y), (eps_x, -eps_y),
        (-eps_x, 0.0), (eps_x, 0.0),
        (-eps_x, eps_y), (0.0, eps_y), (eps_x, eps_y),
    ]  # fmt: skip
    center = box.center
    result: list[Cell] = []
    seen: set[str] = {cell.id}
    for dx, dy in offsets:
        candidate = Point(center.x + dx, center.y + dy)
        if not WORLD_BBOX.contains_point(candidate):
            continue
        neighbor_cell = cell_for_point(candidate, cell.resolution)
        if neighbor_cell.id not in seen:
            seen.add(neighbor_cell.id)
            result.append(neighbor_cell)
    return result
