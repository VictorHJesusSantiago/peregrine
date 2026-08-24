import random

import pytest

from geospat.errors import SpatialIndexError
from geospat.geometry import BBox
from geospat.rtree import RTree


def _random_bbox(rng: random.Random, world: float = 1000.0, max_size: float = 20.0) -> BBox:
    minx = rng.uniform(0, world)
    miny = rng.uniform(0, world)
    return BBox(minx, miny, minx + rng.uniform(0.1, max_size), miny + rng.uniform(0.1, max_size))


def _brute_force_query(boxes: dict[int, BBox], query: BBox) -> set[int]:
    return {item_id for item_id, box in boxes.items() if box.intersects(query)}


class TestRTreeConstruction:
    def test_rejects_too_small_max_entries(self) -> None:
        with pytest.raises(SpatialIndexError):
            RTree(max_entries=2)

    def test_empty_tree_returns_no_results(self) -> None:
        tree = RTree()
        assert tree.range_query(BBox(0, 0, 10, 10)) == []

    def test_len_tracks_inserted_items(self) -> None:
        tree = RTree(max_entries=4)
        for i in range(10):
            tree.insert(i, BBox(i, i, i + 1, i + 1))
        assert len(tree) == 10


class TestRTreeRangeQueryAgainstBruteForce:
    @pytest.mark.parametrize("seed", [0, 1, 2, 3, 4])
    @pytest.mark.parametrize("max_entries", [4, 8, 16])
    def test_matches_brute_force_on_random_data(self, seed: int, max_entries: int) -> None:
        rng = random.Random(seed)
        tree = RTree(max_entries=max_entries)
        boxes: dict[int, BBox] = {}
        for item_id in range(300):
            box = _random_bbox(rng)
            boxes[item_id] = box
            tree.insert(item_id, box)

        for _ in range(30):
            query = _random_bbox(rng, max_size=100.0)
            expected = _brute_force_query(boxes, query)
            actual = set(tree.range_query(query))
            assert actual == expected

    def test_query_covering_whole_world_returns_everything(self) -> None:
        rng = random.Random(42)
        tree = RTree(max_entries=4)
        n = 150
        for item_id in range(n):
            tree.insert(item_id, _random_bbox(rng))
        assert set(tree.range_query(BBox(-1, -1, 1001, 1001))) == set(range(n))

    def test_query_disjoint_from_everything_returns_nothing(self) -> None:
        tree = RTree(max_entries=4)
        for item_id in range(50):
            tree.insert(item_id, BBox(item_id, item_id, item_id + 1, item_id + 1))
        assert tree.range_query(BBox(10_000, 10_000, 10_001, 10_001)) == []

    def test_exact_point_query_finds_only_overlapping_box(self) -> None:
        tree = RTree(max_entries=4)
        tree.insert(1, BBox(0, 0, 1, 1))
        tree.insert(2, BBox(5, 5, 6, 6))
        assert tree.range_query(BBox(0.5, 0.5, 0.5, 0.5)) == [1]


class TestRTreeForcesMultipleSplits:
    def test_many_inserts_still_produce_correct_results(self) -> None:
        # max_entries=4 with 500 items forces many rounds of quadratic split, including root
        # splits (tree growing taller) - this is really a correctness regression test for the
        # split/propagate-bbox bookkeeping, not just leaf-level insert.
        rng = random.Random(7)
        tree = RTree(max_entries=4)
        boxes: dict[int, BBox] = {}
        for item_id in range(500):
            box = _random_bbox(rng, world=200.0, max_size=5.0)
            boxes[item_id] = box
            tree.insert(item_id, box)

        for _ in range(20):
            query = _random_bbox(rng, world=200.0, max_size=30.0)
            assert set(tree.range_query(query)) == _brute_force_query(boxes, query)
