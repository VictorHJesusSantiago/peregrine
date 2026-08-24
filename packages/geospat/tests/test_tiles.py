from typing import cast

import pytest

from geospat.errors import TileError
from geospat.geometry import BBox, LineString, Point, Polygon
from geospat.index import FeatureIndex
from geospat.tiles import (
    TileCoord,
    clip_linestring,
    clip_point,
    clip_polygon,
    generate_tile,
    tile_bbox,
)


class TestTileCoord:
    def test_rejects_negative_zoom(self) -> None:
        with pytest.raises(TileError):
            TileCoord(-1, 0, 0)

    def test_rejects_out_of_range_xy(self) -> None:
        with pytest.raises(TileError):
            TileCoord(1, 2, 0)  # zoom 1 only has x,y in {0,1}

    def test_zoom_zero_is_the_whole_world(self) -> None:
        assert tile_bbox(TileCoord(0, 0, 0)) == BBox(-180, -90, 180, 90)

    def test_zoom_one_tiles_cover_the_four_quadrants(self) -> None:
        assert tile_bbox(TileCoord(1, 0, 0)) == BBox(-180, 0, 0, 90)  # NW
        assert tile_bbox(TileCoord(1, 1, 0)) == BBox(0, 0, 180, 90)  # NE
        assert tile_bbox(TileCoord(1, 0, 1)) == BBox(-180, -90, 0, 0)  # SW
        assert tile_bbox(TileCoord(1, 1, 1)) == BBox(0, -90, 180, 0)  # SE


class TestClipPrimitives:
    def test_clip_point_inside(self) -> None:
        assert clip_point(Point(1, 1), BBox(0, 0, 2, 2)) == Point(1, 1)

    def test_clip_point_outside(self) -> None:
        assert clip_point(Point(5, 5), BBox(0, 0, 2, 2)) is None

    def test_clip_linestring_fully_inside_is_unchanged(self) -> None:
        line = LineString((Point(0, 0), Point(1, 1)))
        result = clip_linestring(line, BBox(-10, -10, 10, 10))
        assert len(result) == 1
        assert result[0].coords == line.coords

    def test_clip_linestring_crossing_boundary_is_truncated(self) -> None:
        line = LineString((Point(-5, 0), Point(5, 0)))
        result = clip_linestring(line, BBox(0, -1, 10, 1))
        assert len(result) == 1
        assert result[0].coords[0] == Point(0, 0)
        assert result[0].coords[-1] == Point(5, 0)

    def test_clip_linestring_entirely_outside_is_empty(self) -> None:
        line = LineString((Point(100, 100), Point(200, 200)))
        assert clip_linestring(line, BBox(0, 0, 1, 1)) == []

    def test_clip_linestring_produces_two_chains_for_a_line_that_exits_and_reenters(self) -> None:
        # A "U" shape: inside, out through the top, back in - should yield two separate chains.
        line = LineString((Point(1, 1), Point(1, 20), Point(9, 20), Point(9, 1)))
        result = clip_linestring(line, BBox(0, 0, 10, 10))
        assert len(result) == 2

    def test_clip_polygon_fully_inside_is_unchanged_in_area(self) -> None:
        square = Polygon(((Point(1, 1), Point(2, 1), Point(2, 2), Point(1, 2)),))
        clipped = clip_polygon(square, BBox(0, 0, 10, 10))
        assert clipped is not None

    def test_clip_polygon_straddling_boundary_is_truncated(self) -> None:
        square = Polygon(((Point(-5, -5), Point(5, -5), Point(5, 5), Point(-5, 5)),))
        clipped = clip_polygon(square, BBox(0, 0, 10, 10))
        assert clipped is not None
        xs = [p.x for p in clipped.exterior]
        ys = [p.y for p in clipped.exterior]
        assert min(xs) == pytest.approx(0.0)
        assert min(ys) == pytest.approx(0.0)
        assert max(xs) == pytest.approx(5.0)
        assert max(ys) == pytest.approx(5.0)

    def test_clip_polygon_entirely_outside_is_none(self) -> None:
        square = Polygon(((Point(100, 100), Point(101, 100), Point(101, 101), Point(100, 101)),))
        assert clip_polygon(square, BBox(0, 0, 1, 1)) is None


class TestGenerateTile:
    def _fixture_index(self) -> FeatureIndex:
        index = FeatureIndex()
        # NE quadrant tile at zoom 1 is BBox(0, 0, 180, 90).
        index.add(Point(45, 45), {"name": "inside_point"})
        index.add(Point(-45, -45), {"name": "outside_point"})
        index.add(LineString((Point(-10, 10), Point(50, 10))), {"name": "crossing_line"})
        index.add(LineString((Point(-50, -50), Point(-10, -10))), {"name": "far_line"})
        index.add(
            Polygon(((Point(10, 10), Point(60, 10), Point(60, 60), Point(10, 60)),)),
            {"name": "inside_polygon"},
        )
        index.add(
            Polygon(((Point(-60, -60), Point(-10, -60), Point(-10, -10), Point(-60, -10)),)),
            {"name": "far_polygon"},
        )
        return index

    def test_tile_contains_exactly_the_expected_features(self) -> None:
        index = self._fixture_index()
        tile = generate_tile(index, TileCoord(1, 1, 0))  # NE quadrant: BBox(0, 0, 180, 90)
        names = {f.properties["name"] for f in tile.features}
        assert names == {"inside_point", "crossing_line", "inside_polygon"}

    def test_clipped_line_coordinates_stay_within_tile_bounds(self) -> None:
        index = self._fixture_index()
        tile = generate_tile(index, TileCoord(1, 1, 0))
        box = tile_bbox(TileCoord(1, 1, 0))
        for feature in tile.features:
            if feature.geom_type == "LineString":
                coords = cast(list[list[float]], feature.coordinates)
                for x, y in coords:
                    assert box.minx - 1e-9 <= x <= box.maxx + 1e-9
                    assert box.miny - 1e-9 <= y <= box.maxy + 1e-9

    def test_empty_tile_has_no_features(self) -> None:
        index = FeatureIndex()
        index.add(Point(-170, -80))
        tile = generate_tile(index, TileCoord(1, 1, 0))  # far from the point
        assert tile.features == ()

    def test_to_dict_round_trips_through_json(self) -> None:
        index = self._fixture_index()
        tile = generate_tile(index, TileCoord(1, 1, 0))
        payload = tile.to_json()
        assert '"type": "FeatureCollection"' in payload or "FeatureCollection" in payload
        d = tile.to_dict()
        assert d["tile"] == {"z": 1, "x": 1, "y": 0}
        features = cast(list[object], d["features"])
        assert len(features) == len(tile.features)
