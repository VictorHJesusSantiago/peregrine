import pytest

from geospat.errors import QueryError
from geospat.geometry import BBox, LineString, Point, Polygon
from geospat.index import FeatureIndex
from geospat.query import Query


def _index_with_grid_of_points() -> FeatureIndex:
    index = FeatureIndex()
    for x in range(0, 10):
        for y in range(0, 10):
            index.add(Point(float(x), float(y)), {"label": f"{x},{y}"})
    return index


class TestWithin:
    def test_within_returns_only_points_inside_bbox(self) -> None:
        index = _index_with_grid_of_points()
        results = Query(index).within(BBox(2, 2, 4, 4)).execute()
        labels = {f.properties["label"] for f in results}
        assert labels == {f"{x},{y}" for x in range(2, 5) for y in range(2, 5)}

    def test_within_disjoint_from_everything_is_empty(self) -> None:
        index = _index_with_grid_of_points()
        results = Query(index).within(BBox(1000, 1000, 1001, 1001)).execute()
        assert results == []


class TestIntersects:
    def test_intersects_polygon_keeps_only_geometries_that_actually_overlap(self) -> None:
        index = FeatureIndex()
        inside_id = index.add(Point(1, 1))
        outside_id = index.add(Point(50, 50))
        triangle = Polygon(((Point(0, 0), Point(3, 0), Point(0, 3)),))
        results = Query(index).intersects(triangle).execute()
        ids = {f.id for f in results}
        assert inside_id in ids
        assert outside_id not in ids

    def test_intersects_is_exact_not_just_bbox_overlap(self) -> None:
        # A line whose bbox overlaps the triangle's bbox but which never actually crosses it.
        index = FeatureIndex()
        far_corner_line = index.add(LineString((Point(2.9, 2.9), Point(3.0, 3.0))))
        triangle = Polygon(((Point(0, 0), Point(1, 0), Point(0, 1)),))
        results = Query(index).intersects(triangle).execute()
        assert far_corner_line not in {f.id for f in results}


class TestNear:
    def test_near_returns_nearest_first(self) -> None:
        index = FeatureIndex()
        index.add(Point(0, 0), {"name": "far"})
        index.add(Point(1, 0), {"name": "near"})
        index.add(Point(0.5, 0), {"name": "mid"})
        results = Query(index).near(Point(1, 0), radius=2.0).execute()
        names = [f.properties["name"] for f in results]
        assert names == ["near", "mid", "far"]

    def test_near_excludes_points_outside_radius(self) -> None:
        index = FeatureIndex()
        index.add(Point(0, 0), {"name": "close"})
        index.add(Point(100, 100), {"name": "distant"})
        results = Query(index).near(Point(0, 0), radius=5.0).execute()
        assert [f.properties["name"] for f in results] == ["close"]

    def test_negative_radius_rejected(self) -> None:
        index = FeatureIndex()
        with pytest.raises(QueryError):
            Query(index).near(Point(0, 0), radius=-1.0)


class TestAtResolution:
    def test_requires_an_anchor(self) -> None:
        index = _index_with_grid_of_points()
        with pytest.raises(QueryError):
            Query(index).at_resolution(3).execute()

    def test_restricts_results_to_the_anchor_cell(self) -> None:
        index = FeatureIndex()
        index.add(Point(1.0, 1.0), {"name": "same_cell"})
        index.add(Point(-170.0, -80.0), {"name": "far_away_cell"})
        results = Query(index).within(BBox(0, 0, 2, 2)).at_resolution(2).execute()
        names = {f.properties["name"] for f in results}
        assert names == {"same_cell"}


class TestLimit:
    def test_limit_caps_results_after_near_sort(self) -> None:
        index = FeatureIndex()
        for i in range(10):
            index.add(Point(float(i), 0.0))
        results = Query(index).near(Point(0, 0), radius=100.0).limit(3).execute()
        assert len(results) == 3
        assert [f.geom.x for f in results] == [0.0, 1.0, 2.0]  # type: ignore[union-attr]


class TestComposition:
    def test_within_and_intersects_and_near_compose_with_and_semantics(self) -> None:
        index = _index_with_grid_of_points()
        triangle = Polygon(((Point(0, 0), Point(9, 0), Point(0, 9)),))
        results = (
            Query(index)
            .within(BBox(0, 0, 5, 5))
            .intersects(triangle)
            .near(Point(0, 0), radius=3.0)
            .execute()
        )
        for feature in results:
            p = feature.geom
            assert isinstance(p, Point)
            assert 0 <= p.x <= 5 and 0 <= p.y <= 5
            assert p.x + p.y <= 9
            assert (p.x**2 + p.y**2) ** 0.5 <= 3.0

    def test_mutually_disjoint_constraints_raise(self) -> None:
        index = _index_with_grid_of_points()
        with pytest.raises(QueryError):
            Query(index).within(BBox(0, 0, 1, 1)).near(Point(100, 100), radius=1.0).execute()

    def test_empty_query_returns_everything(self) -> None:
        index = _index_with_grid_of_points()
        results = Query(index).execute()
        assert len(results) == len(index)
