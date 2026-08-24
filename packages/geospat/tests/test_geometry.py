import pytest

from geospat.errors import InvalidGeometryError
from geospat.geometry import (
    BBox,
    LineString,
    Point,
    Polygon,
    distance_point_to_geometry,
    geometries_intersect,
    point_in_polygon,
    segments_intersect,
)

SQUARE = Polygon(((Point(0, 0), Point(4, 0), Point(4, 4), Point(0, 4)),))


class TestBBox:
    def test_rejects_degenerate_box(self) -> None:
        with pytest.raises(InvalidGeometryError):
            BBox(1, 1, 0, 1)

    def test_intersects_is_symmetric_and_inclusive_of_touching_edges(self) -> None:
        a = BBox(0, 0, 1, 1)
        b = BBox(1, 0, 2, 1)
        assert a.intersects(b)
        assert b.intersects(a)

    def test_disjoint_boxes_do_not_intersect(self) -> None:
        assert not BBox(0, 0, 1, 1).intersects(BBox(2, 2, 3, 3))

    def test_union_covers_both(self) -> None:
        u = BBox(0, 0, 1, 1).union(BBox(2, 2, 3, 3))
        assert u == BBox(0, 0, 3, 3)

    def test_intersection_of_overlapping_boxes(self) -> None:
        i = BBox(0, 0, 2, 2).intersection(BBox(1, 1, 3, 3))
        assert i == BBox(1, 1, 2, 2)

    def test_intersection_of_disjoint_boxes_is_none(self) -> None:
        assert BBox(0, 0, 1, 1).intersection(BBox(5, 5, 6, 6)) is None

    def test_center(self) -> None:
        assert BBox(0, 0, 4, 2).center == Point(2, 1)


class TestPointInPolygon:
    def test_interior_point(self) -> None:
        assert point_in_polygon(Point(2, 2), SQUARE)

    def test_exterior_point(self) -> None:
        assert not point_in_polygon(Point(10, 10), SQUARE)

    def test_point_in_hole_is_outside(self) -> None:
        donut = Polygon(
            (
                (Point(0, 0), Point(10, 0), Point(10, 10), Point(0, 10)),
                (Point(4, 4), Point(6, 4), Point(6, 6), Point(4, 6)),
            )
        )
        assert point_in_polygon(Point(5, 5), donut) is False
        assert point_in_polygon(Point(1, 1), donut) is True


class TestSegmentsIntersect:
    def test_crossing_segments(self) -> None:
        assert segments_intersect(Point(0, 0), Point(2, 2), Point(0, 2), Point(2, 0))

    def test_parallel_non_touching_segments(self) -> None:
        assert not segments_intersect(Point(0, 0), Point(1, 0), Point(0, 1), Point(1, 1))

    def test_collinear_overlap(self) -> None:
        assert segments_intersect(Point(0, 0), Point(2, 0), Point(1, 0), Point(3, 0))


class TestGeometriesIntersect:
    def test_point_inside_polygon(self) -> None:
        assert geometries_intersect(Point(1, 1), SQUARE)

    def test_point_outside_polygon(self) -> None:
        assert not geometries_intersect(Point(100, 100), SQUARE)

    def test_line_crossing_polygon(self) -> None:
        line = LineString((Point(-1, 2), Point(5, 2)))
        assert geometries_intersect(line, SQUARE)

    def test_line_missing_polygon(self) -> None:
        line = LineString((Point(10, 10), Point(20, 20)))
        assert not geometries_intersect(line, SQUARE)

    def test_overlapping_polygons(self) -> None:
        other = Polygon(((Point(2, 2), Point(6, 2), Point(6, 6), Point(2, 6)),))
        assert geometries_intersect(SQUARE, other)

    def test_disjoint_polygons(self) -> None:
        other = Polygon(((Point(100, 100), Point(101, 100), Point(101, 101), Point(100, 101)),))
        assert not geometries_intersect(SQUARE, other)

    def test_is_symmetric_for_mixed_types(self) -> None:
        line = LineString((Point(-1, 2), Point(5, 2)))
        assert geometries_intersect(line, SQUARE) == geometries_intersect(SQUARE, line)


class TestDistancePointToGeometry:
    def test_distance_to_point(self) -> None:
        assert distance_point_to_geometry(Point(0, 0), Point(3, 4)) == pytest.approx(5.0)

    def test_distance_to_line(self) -> None:
        line = LineString((Point(0, 0), Point(10, 0)))
        assert distance_point_to_geometry(Point(5, 3), line) == pytest.approx(3.0)

    def test_distance_to_polygon_interior_is_zero(self) -> None:
        assert distance_point_to_geometry(Point(2, 2), SQUARE) == 0.0

    def test_distance_to_polygon_exterior(self) -> None:
        assert distance_point_to_geometry(Point(8, 0), SQUARE) == pytest.approx(4.0)
