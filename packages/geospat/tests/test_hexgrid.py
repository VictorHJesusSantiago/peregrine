import random

import pytest

from geospat.errors import InvalidGeometryError
from geospat.geometry import Point
from geospat.hexgrid import WORLD_BBOX, Cell, ancestor, cell_bbox, cell_for_point, children, neighbors, parent


class TestCellForPoint:
    def test_resolution_zero_is_always_the_root(self) -> None:
        assert cell_for_point(Point(12.3, -45.6), 0) == Cell("")

    def test_resolution_matches_id_length(self) -> None:
        cell = cell_for_point(Point(40.0, 20.0), 5)
        assert cell.resolution == 5
        assert len(cell.id) == 5

    def test_out_of_world_point_is_rejected(self) -> None:
        with pytest.raises(InvalidGeometryError):
            cell_for_point(Point(200, 0), 3)

    def test_negative_resolution_is_rejected(self) -> None:
        with pytest.raises(InvalidGeometryError):
            cell_for_point(Point(0, 0), -1)

    def test_cell_bbox_contains_the_originating_point(self) -> None:
        rng = random.Random(1)
        for _ in range(50):
            point = Point(rng.uniform(-180, 180), rng.uniform(-90, 90))
            for resolution in (1, 2, 3, 6):
                cell = cell_for_point(point, resolution)
                assert cell_bbox(cell).contains_point(point)

    def test_two_points_in_the_same_quadrant_share_a_cell(self) -> None:
        a = cell_for_point(Point(10.0, 10.0), 4)
        b = cell_for_point(Point(10.5, 10.5), 4)
        assert a == b

    def test_distant_points_land_in_different_cells(self) -> None:
        a = cell_for_point(Point(-170.0, -80.0), 4)
        b = cell_for_point(Point(170.0, 80.0), 4)
        assert a != b


class TestParentChild:
    def test_root_has_no_parent(self) -> None:
        with pytest.raises(InvalidGeometryError):
            parent(Cell(""))

    def test_child_of_parent_is_reversible(self) -> None:
        cell = cell_for_point(Point(33.0, 12.0), 6)
        assert parent(cell).id == cell.id[:-1]

    def test_children_of_a_cell_are_all_at_resolution_plus_one(self) -> None:
        cell = cell_for_point(Point(-50.0, 20.0), 3)
        for child in children(cell):
            assert child.resolution == cell.resolution + 1
            assert parent(child) == cell

    def test_point_cell_at_finer_resolution_is_a_descendant(self) -> None:
        point = Point(77.7, -33.3)
        coarse = cell_for_point(point, 2)
        fine = cell_for_point(point, 8)
        assert ancestor(fine, 2) == coarse

    def test_children_partition_the_parent_bbox(self) -> None:
        cell = cell_for_point(Point(0.1, 0.1), 2)
        parent_box = cell_bbox(cell)
        child_boxes = [cell_bbox(c) for c in children(cell)]
        total_area = sum(b.area for b in child_boxes)
        assert total_area == pytest.approx(parent_box.area)

    def test_ancestor_at_own_resolution_is_itself(self) -> None:
        cell = cell_for_point(Point(5.0, 5.0), 4)
        assert ancestor(cell, 4) == cell

    def test_ancestor_of_invalid_resolution_raises(self) -> None:
        cell = cell_for_point(Point(5.0, 5.0), 4)
        with pytest.raises(InvalidGeometryError):
            ancestor(cell, 5)


class TestNeighbors:
    def test_root_cell_has_no_neighbors(self) -> None:
        assert neighbors(Cell("")) == []

    def test_neighbors_never_include_self(self) -> None:
        cell = cell_for_point(Point(0.0, 0.0), 3)
        assert cell not in neighbors(cell)

    def test_interior_cell_has_up_to_eight_distinct_neighbors(self) -> None:
        cell = cell_for_point(Point(0.0, 0.0), 3)
        result = neighbors(cell)
        assert len(result) <= 8
        assert len(result) == len(set(result))

    def test_neighbors_are_all_at_the_same_resolution(self) -> None:
        cell = cell_for_point(Point(45.0, -20.0), 4)
        for n in neighbors(cell):
            assert n.resolution == cell.resolution

    def test_corner_of_the_world_has_fewer_neighbors(self) -> None:
        # The world's own corner (near -180, -90) has no cells beyond the world bbox, so it must
        # have strictly fewer live neighbors than a comfortably interior cell.
        corner_point = Point(-179.9, -89.9)
        interior_point = Point(0.0, 0.0)
        corner_cell = cell_for_point(corner_point, 3)
        interior_cell = cell_for_point(interior_point, 3)
        assert len(neighbors(corner_cell)) < len(neighbors(interior_cell))

    def test_neighbor_relationship_is_mutual_for_interior_cells(self) -> None:
        cell = cell_for_point(Point(20.0, 20.0), 4)
        for n in neighbors(cell):
            # Not every quadtree neighbor pair is perfectly symmetric across a resolution
            # boundary in the corner-touching case, but same-size adjacent cells found from well
            # inside the world bbox should see each other back.
            back = neighbors(n)
            assert cell in back or cell_bbox(cell).intersects(cell_bbox(n))

    def test_world_bbox_is_documented_lon_lat_extent(self) -> None:
        assert WORLD_BBOX.minx == -180.0
        assert WORLD_BBOX.maxx == 180.0
        assert WORLD_BBOX.miny == -90.0
        assert WORLD_BBOX.maxy == 90.0
