"""A hand-written R-tree (Guttman 1984): a height-balanced tree of bounding boxes that answers
"which indexed items' bounding boxes intersect this query box" without scanning every item.

Only insertion and range query are implemented — no deletion. Deletion in an R-tree requires
underflow-driven re-insertion of a whole subtree, which is a meaningful chunk of extra machinery
this package does not need (nothing here ever removes a feature from an index); leaving it out is
a scope cut, not an oversight.

Node capacity is `max_entries` (default 8), with a minimum fill of `max_entries // 2` used only to
guide quadratic split's seed-vs-remaining distribution, not enforced by deletion (since there is
none).
"""

from __future__ import annotations

from dataclasses import dataclass

from geospat.errors import SpatialIndexError
from geospat.geometry import BBox


@dataclass
class _Entry:
    """One slot in a node. Leaf entries carry an `item_id` and no `child`; internal entries carry
    a `child` node and no `item_id`. `bbox` is always the tight bounding box of whatever the entry
    points at — kept in sync on every insert so a query never has to descend into a subtree whose
    box provably can't match."""

    bbox: BBox
    child: RTreeNode | None = None
    item_id: int | None = None


class RTreeNode:
    __slots__ = ("entries", "leaf", "parent")

    def __init__(self, leaf: bool) -> None:
        self.leaf = leaf
        self.entries: list[_Entry] = []
        self.parent: RTreeNode | None = None


class RTree:
    """Maps integer item ids to bounding boxes. The tree stores only ids and boxes — callers keep
    their own `id -> geometry` mapping (see `geospat.index.FeatureIndex` for a ready-made one),
    the same division of responsibility as `libspatialindex`'s `rtree.index.Index`."""

    def __init__(self, max_entries: int = 8) -> None:
        if max_entries < 4:
            raise SpatialIndexError("max_entries must be at least 4 for quadratic split to work")
        self.max_entries = max_entries
        self.min_entries = max(2, max_entries // 2)
        self.root = RTreeNode(leaf=True)

    def insert(self, item_id: int, bbox: BBox) -> None:
        leaf = self._choose_leaf(bbox)
        self._insert_entry(leaf, _Entry(bbox=bbox, item_id=item_id))

    def range_query(self, query_bbox: BBox) -> list[int]:
        results: list[int] = []
        self._search(self.root, query_bbox, results)
        return results

    def __len__(self) -> int:
        count = 0
        stack = [self.root]
        while stack:
            node = stack.pop()
            if node.leaf:
                count += len(node.entries)
            else:
                stack.extend(e.child for e in node.entries if e.child is not None)
        return count

    # -- search -------------------------------------------------------------

    def _search(self, node: RTreeNode, query_bbox: BBox, results: list[int]) -> None:
        for entry in node.entries:
            if not entry.bbox.intersects(query_bbox):
                continue
            if node.leaf:
                assert entry.item_id is not None
                results.append(entry.item_id)
            else:
                assert entry.child is not None
                self._search(entry.child, query_bbox, results)

    # -- insertion ------------------------------------------------------------

    def _choose_leaf(self, bbox: BBox) -> RTreeNode:
        """Descend picking, at each level, the child whose box would enlarge least to also cover
        `bbox` (Guttman's `ChooseLeaf`) — the cheap greedy proxy for "which subtree is this new
        item most naturally part of," ties broken by the child with the smaller box."""
        node = self.root
        while not node.leaf:
            best = min(node.entries, key=lambda e: (e.bbox.enlargement(bbox), e.bbox.area))
            assert best.child is not None
            node = best.child
        return node

    def _insert_entry(self, node: RTreeNode, entry: _Entry) -> None:
        node.entries.append(entry)
        if entry.child is not None:
            entry.child.parent = node
        if len(node.entries) <= self.max_entries:
            self._propagate_bbox(node)
            return

        sibling = self._quadratic_split(node)
        if node.parent is None:
            new_root = RTreeNode(leaf=False)
            self._attach(new_root, node)
            self._attach(new_root, sibling)
            self.root = new_root
        else:
            parent = node.parent
            self._sync_child_bbox(parent, node)
            # `_insert_entry` itself sets `sibling.parent = parent` as it appends this entry, and
            # may then split `parent` again, which can reassign `sibling.parent` a second time to
            # a *new* sibling-of-parent — do not touch `sibling.parent` again after this call, or
            # it clobbers that correct reassignment with the now-stale `parent` reference.
            self._insert_entry(parent, _Entry(bbox=self._node_bbox(sibling), child=sibling))

    def _attach(self, parent: RTreeNode, child: RTreeNode) -> None:
        parent.entries.append(_Entry(bbox=self._node_bbox(child), child=child))
        child.parent = parent

    def _node_bbox(self, node: RTreeNode) -> BBox:
        boxes = [e.bbox for e in node.entries]
        result = boxes[0]
        for b in boxes[1:]:
            result = result.union(b)
        return result

    def _sync_child_bbox(self, parent: RTreeNode, child: RTreeNode) -> None:
        entry = next(e for e in parent.entries if e.child is child)
        entry.bbox = self._node_bbox(child)

    def _propagate_bbox(self, node: RTreeNode) -> None:
        current = node
        while current.parent is not None:
            parent = current.parent
            self._sync_child_bbox(parent, current)
            current = parent

    # -- quadratic split --------------------------------------------------

    def _quadratic_split(self, node: RTreeNode) -> RTreeNode:
        """Guttman's quadratic-cost split. Chosen over the cheaper linear split because with a
        small `max_entries` (the common case for an in-memory index like this one) quadratic
        split's O(n^2) seed selection is negligible in absolute time, and it produces visibly
        tighter, less-overlapping node boxes than linear split's single left-to-right scan — which
        is what keeps range queries fast. Linear split would be the better trade only at node
        fanouts large enough that O(n^2) actually shows up in a profile.
        """
        entries = node.entries
        seed_a, seed_b = self._pick_seeds(entries)
        group1 = [entries[seed_a]]
        group2 = [entries[seed_b]]
        remaining = [e for i, e in enumerate(entries) if i not in (seed_a, seed_b)]

        while remaining:
            if len(group1) + len(remaining) == self.min_entries:
                group1.extend(remaining)
                remaining = []
                break
            if len(group2) + len(remaining) == self.min_entries:
                group2.extend(remaining)
                remaining = []
                break

            bbox1 = self._union_all(group1)
            bbox2 = self._union_all(group2)
            # PickNext: the remaining entry with the strongest preference for one group over the
            # other (biggest gap between how much it would enlarge each), resolved greedily first.
            next_entry, prefer_group1 = max(
                (
                    (e, bbox1.enlargement(e.bbox) < bbox2.enlargement(e.bbox))
                    for e in remaining
                ),
                key=lambda pair: abs(bbox1.enlargement(pair[0].bbox) - bbox2.enlargement(pair[0].bbox)),
            )
            remaining.remove(next_entry)
            if prefer_group1:
                group1.append(next_entry)
            else:
                group2.append(next_entry)

        node.entries = group1
        sibling = RTreeNode(leaf=node.leaf)
        sibling.entries = group2
        for e in group2:
            if e.child is not None:
                e.child.parent = sibling
        return sibling

    def _union_all(self, entries: list[_Entry]) -> BBox:
        result = entries[0].bbox
        for e in entries[1:]:
            result = result.union(e.bbox)
        return result

    def _pick_seeds(self, entries: list[_Entry]) -> tuple[int, int]:
        """Guttman's `PickSeeds`: the pair of entries that would waste the most area if forced
        into the same group — the pair "most worth" separating."""
        best_waste = -1.0
        best_pair = (0, 1)
        for i in range(len(entries)):
            for j in range(i + 1, len(entries)):
                combined = entries[i].bbox.union(entries[j].bbox)
                waste = combined.area - entries[i].bbox.area - entries[j].bbox.area
                if waste > best_waste:
                    best_waste = waste
                    best_pair = (i, j)
        return best_pair
