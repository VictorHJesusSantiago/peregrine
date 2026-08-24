"""Shortest-path routing over a weighted graph: a hand-written Dijkstra baseline, and a
contraction hierarchy built on top of it.

**Contraction hierarchies (CH), and exactly how this implementation simplifies them.** Real CH
orders nodes by a running "edge difference" priority (shortcuts added minus edges removed if this
node were contracted next, recomputed lazily via a priority queue as neighbors get contracted) and
witness searches are hop-limited local Dijkstra runs for query-time speed. Node ordering here now
matches that: it's the standard dynamically-recomputed edge-difference priority queue, not a
simplification. Witness search is still simplified — that remains a performance-tuning choice, not
something that affects correctness:

- **Node ordering**: nodes are contracted in ascending order of edge difference (shortcuts that
  contracting the node would add, minus edges its contraction would remove), dynamically
  recomputed as contraction proceeds — the standard CH heuristic. This is a `heapq`-based priority
  queue with lazy deletion (heapq has no native decrease-key): a node is pushed as
  `(edge_difference, tiebreak, node)`; when popped, its edge difference is recomputed on the spot,
  and if that no longer matches what was popped, the fresh value is re-pushed and the pop is
  skipped rather than treated as a contraction. After a node is actually contracted, every one of
  its still-uncontracted live neighbors has its edge difference recomputed and re-pushed, since
  contracting a node changes what contracting its neighbors next would cost. This produces a
  better contraction order than the one-shot degree sort it replaced: fewer shortcuts, and faster
  bidirectional queries over the resulting graph — see `test_routing.py` for a direct before/after
  comparison on the same graphs.
- **Witness search**: to decide whether contracting node `v` needs a shortcut between neighbors
  `u` and `w`, this runs a full, unbounded Dijkstra from `u` (excluding `v`) rather than a
  hop-limited local search. Slower to build, but never wrong — it always finds the true shortest
  `u -> w` path excluding `v` if one exists, so the shortcut decision is always correct.

What is **not** simplified: shortcut creation is exact (a shortcut is added if and only if
`u -> v -> w` is a genuinely shortest `u -> w` path once `v` is removed), and the query algorithm
is the standard CH bidirectional search restricted to upward edges (rank increasing) in both
directions, meeting in the middle. That combination is why `test_routing.py` can assert *exact*
distance agreement with plain Dijkstra across randomized graphs, not just "close enough."

Path reconstruction (unpacking a shortcut back into the original edges it stands in for) is not
implemented — only distance queries are, since that is what this package needed and tested. Each
shortcut does record its `via` node, which is what a future unpacking pass would recurse on.
"""

from __future__ import annotations

import heapq
import itertools
from dataclasses import dataclass
from typing import Literal

from geospat.errors import RoutingError

# Any hashable label works as a node id (an OSM way id, an intersection name, a plain int index,
# ...); pinned to `int | str` rather than a fully generic `Hashable` so type errors on, say,
# passing a mutable/unhashable key still surface at the call site instead of only at runtime.
NodeId = int | str

# The node-ordering strategy `ContractionHierarchy` contracts nodes in. "edge_difference" is the
# real, shipped default (see module docstring). "degree" — the one-shot static sort this replaced
# — is kept solely as an injectable comparison baseline for `test_routing.py` to measure the
# edge-difference ordering's improvement against; no production code path selects it.
_Ordering = Literal["edge_difference", "degree"]


@dataclass(frozen=True, slots=True)
class Edge:
    to: NodeId
    weight: float


class Graph:
    """A directed, weighted graph. `add_edge(u, v, w, bidirectional=True)` (the default) adds the
    reverse edge too, since most spatial routing graphs (road segments open both ways) are
    naturally undirected; pass `bidirectional=False` for one-way edges."""

    def __init__(self) -> None:
        self._out: dict[NodeId, list[Edge]] = {}

    def add_node(self, node: NodeId) -> None:
        self._out.setdefault(node, [])

    def add_edge(self, u: NodeId, v: NodeId, weight: float, bidirectional: bool = True) -> None:
        if weight < 0:
            raise RoutingError("edge weights must be non-negative (Dijkstra requires it)")
        self.add_node(u)
        self.add_node(v)
        self._out[u].append(Edge(v, weight))
        if bidirectional:
            self._out[v].append(Edge(u, weight))

    def has_node(self, node: NodeId) -> bool:
        return node in self._out

    def nodes(self) -> list[NodeId]:
        return list(self._out.keys())

    def neighbors(self, node: NodeId) -> list[Edge]:
        return self._out.get(node, [])

    def __len__(self) -> int:
        return len(self._out)


def dijkstra(graph: Graph, source: NodeId, target: NodeId | None = None) -> dict[NodeId, float]:
    """Plain Dijkstra from `source`. If `target` is given, search stops as soon as it is settled
    (a small, standard early-exit optimization); the returned dict otherwise holds every reachable
    node's distance."""
    if not graph.has_node(source):
        raise RoutingError(f"unknown source node {source}")
    dist: dict[NodeId, float] = {source: 0.0}
    visited: set[NodeId] = set()
    heap: list[tuple[float, NodeId]] = [(0.0, source)]
    while heap:
        d, node = heapq.heappop(heap)
        if node in visited:
            continue
        visited.add(node)
        if node == target:
            break
        for edge in graph.neighbors(node):
            nd = d + edge.weight
            if edge.to not in dist or nd < dist[edge.to]:
                dist[edge.to] = nd
                heapq.heappush(heap, (nd, edge.to))
    return dist


def shortest_path_distance(graph: Graph, source: NodeId, target: NodeId) -> float:
    dist = dijkstra(graph, source, target)
    if target not in dist:
        raise RoutingError(f"no path from {source} to {target}")
    return dist[target]


@dataclass
class _ContractedEdge:
    """Mutable (not frozen) because building the augmented graph repeatedly finds a cheaper
    shortcut for the same `(u, w)` pair and updates it in place rather than growing duplicate
    entries — see `ContractionHierarchy._add_live_edge`."""

    to: NodeId
    weight: float
    via: NodeId | None = None  # the contracted node this shortcut stands in for, if any


class ContractionHierarchy:
    """Builds an augmented graph (original edges + shortcuts) and a node ranking, then answers
    shortest-path *distance* queries with a bidirectional search over upward edges only."""

    def __init__(self, graph: Graph, _ordering: _Ordering = "edge_difference") -> None:
        self._rank: dict[NodeId, int] = {}
        self._up: dict[NodeId, list[_ContractedEdge]] = {n: [] for n in graph.nodes()}
        self._down: dict[NodeId, list[_ContractedEdge]] = {n: [] for n in graph.nodes()}
        self._build(graph, _ordering)

    def _build(self, graph: Graph, ordering: _Ordering = "edge_difference") -> None:
        # `live` is the working adjacency (original edges + shortcuts added so far). Contracted
        # nodes stay as keys but get excluded by the `contracted` set rather than removed, which
        # is simpler than deleting them out of every other node's edge list.
        live: dict[NodeId, list[_ContractedEdge]] = {
            n: [_ContractedEdge(e.to, e.weight) for e in graph.neighbors(n)] for n in graph.nodes()
        }
        contracted: set[NodeId] = set()

        if ordering == "degree":
            order = self._contract_by_degree(graph, live, contracted)
        else:
            order = self._contract_by_edge_difference(graph, live, contracted)

        for rank, v in enumerate(order):
            self._rank[v] = rank

        # Now that every node has a rank, split the final live graph (original edges + every
        # shortcut ever added) into the up-graph (edges from the lower-rank endpoint) and
        # down-graph (edges from the higher-rank endpoint). Restricting both search directions of
        # a query to upward edges is what makes the bidirectional search correct and terminating.
        for u, edges in live.items():
            for e in edges:
                if self._rank[u] < self._rank[e.to]:
                    self._up[u].append(_ContractedEdge(e.to, e.weight, e.via))
                else:
                    self._down[e.to].append(_ContractedEdge(u, e.weight, e.via))

    def _contract_by_degree(
        self,
        graph: Graph,
        live: dict[NodeId, list[_ContractedEdge]],
        contracted: set[NodeId],
    ) -> list[NodeId]:
        """The one-shot static ordering this package used before: nodes sorted ascending by their
        degree in the *original* graph, computed once up front and never revisited. Kept only as
        an injectable comparison baseline (see `_Ordering`) — not the shipped default."""
        order = sorted(graph.nodes(), key=lambda n: len(graph.neighbors(n)))
        for v in order:
            self._contract_node(live, contracted, v)
        return order

    def _contract_by_edge_difference(
        self,
        graph: Graph,
        live: dict[NodeId, list[_ContractedEdge]],
        contracted: set[NodeId],
    ) -> list[NodeId]:
        """The standard CH node ordering: a `heapq`-based priority queue keyed on edge difference
        (lower = cheaper to contract next), dynamically recomputed as contraction proceeds. `heapq`
        has no native decrease-key, so this uses the standard lazy-deletion pattern: push a fresh
        entry rather than mutating one in place, and when a popped entry's edge difference doesn't
        match a freshly recomputed value for that node, discard it (re-pushing the current value)
        instead of contracting. A monotonic tiebreaker is threaded through every heap entry so
        `heapq` never needs to compare two `NodeId`s directly (which could be an `int` and a `str`,
        an unsupported comparison).
        """
        tiebreak = itertools.count()
        heap: list[tuple[int, int, NodeId]] = [
            (self._edge_difference(live, contracted, n), next(tiebreak), n) for n in graph.nodes()
        ]
        heapq.heapify(heap)

        order: list[NodeId] = []
        while heap:
            edge_diff, _, v = heapq.heappop(heap)
            if v in contracted:
                continue
            current_edge_diff = self._edge_difference(live, contracted, v)
            if current_edge_diff != edge_diff:
                heapq.heappush(heap, (current_edge_diff, next(tiebreak), v))
                continue

            neighbors = {u for u, _ in self._in_neighbors(live, contracted, v)} | {
                w for w, _ in self._out_neighbors(live, contracted, v)
            }
            self._contract_node(live, contracted, v)
            order.append(v)
            for u in neighbors:
                if u not in contracted:
                    heapq.heappush(heap, (self._edge_difference(live, contracted, u), next(tiebreak), u))
        return order

    def _in_neighbors(
        self, live: dict[NodeId, list[_ContractedEdge]], contracted: set[NodeId], v: NodeId
    ) -> list[tuple[NodeId, float]]:
        return [(u, e.weight) for u in live for e in live[u] if e.to == v and u not in contracted]

    def _out_neighbors(
        self, live: dict[NodeId, list[_ContractedEdge]], contracted: set[NodeId], v: NodeId
    ) -> list[tuple[NodeId, float]]:
        return [(e.to, e.weight) for e in live[v] if e.to not in contracted]

    def _needed_shortcuts(
        self,
        live: dict[NodeId, list[_ContractedEdge]],
        contracted: set[NodeId],
        v: NodeId,
        in_neighbors: list[tuple[NodeId, float]],
        out_neighbors: list[tuple[NodeId, float]],
    ) -> list[tuple[NodeId, NodeId, float]]:
        """The `(u, w, weight)` shortcuts contracting `v` would need to add, given its current live
        in/out neighbors — i.e. every neighbor pair without a witness path avoiding `v`. Shared by
        `_edge_difference` (which only counts these, as a dry run) and `_contract_node` (which
        actually adds them), so the two can never disagree about what contracting `v` entails."""
        needed: list[tuple[NodeId, NodeId, float]] = []
        for u, w_uv in in_neighbors:
            if u == v:
                continue
            for w, w_vw in out_neighbors:
                if w in (v, u):
                    continue
                shortcut_weight = w_uv + w_vw
                if not self._has_witness(live, contracted | {v}, u, w, shortcut_weight):
                    needed.append((u, w, shortcut_weight))
        return needed

    def _edge_difference(
        self, live: dict[NodeId, list[_ContractedEdge]], contracted: set[NodeId], v: NodeId
    ) -> int:
        """Shortcuts contracting `v` would add, minus edges its contraction would remove — the
        standard CH priority: lower means `v` is cheaper to contract next."""
        in_neighbors = self._in_neighbors(live, contracted, v)
        out_neighbors = self._out_neighbors(live, contracted, v)
        removed = len(in_neighbors) + len(out_neighbors)
        added = len(self._needed_shortcuts(live, contracted, v, in_neighbors, out_neighbors))
        return added - removed

    def _contract_node(
        self, live: dict[NodeId, list[_ContractedEdge]], contracted: set[NodeId], v: NodeId
    ) -> None:
        in_neighbors = self._in_neighbors(live, contracted, v)
        out_neighbors = self._out_neighbors(live, contracted, v)
        for u, w, weight in self._needed_shortcuts(live, contracted, v, in_neighbors, out_neighbors):
            self._add_live_edge(live, u, w, weight, via=v)
        contracted.add(v)

    def _add_live_edge(
        self, live: dict[NodeId, list[_ContractedEdge]], u: NodeId, w: NodeId, weight: float, via: NodeId
    ) -> None:
        for e in live[u]:
            if e.to == w:
                if weight < e.weight:
                    e.weight = weight
                    e.via = via
                return
        live[u].append(_ContractedEdge(w, weight, via))

    def _has_witness(
        self,
        live: dict[NodeId, list[_ContractedEdge]],
        excluded: set[NodeId],
        source: NodeId,
        target: NodeId,
        max_weight: float,
    ) -> bool:
        """Unbounded Dijkstra from `source` to `target` over `live`, skipping `excluded` nodes
        (the node being contracted). Returns whether a path no longer than `max_weight` exists
        without going through it — if so, the direct shortcut `u -> v -> w` is redundant."""
        dist: dict[NodeId, float] = {source: 0.0}
        heap: list[tuple[float, NodeId]] = [(0.0, source)]
        visited: set[NodeId] = set()
        while heap:
            d, node = heapq.heappop(heap)
            if node in visited:
                continue
            visited.add(node)
            if node == target:
                return d <= max_weight + 1e-9
            for e in live.get(node, []):
                if e.to in excluded:
                    continue
                nd = d + e.weight
                if nd > max_weight + 1e-9:
                    continue
                if e.to not in dist or nd < dist[e.to]:
                    dist[e.to] = nd
                    heapq.heappush(heap, (nd, e.to))
        return False

    def distance(self, source: NodeId, target: NodeId) -> float:
        if source not in self._rank or target not in self._rank:
            raise RoutingError("unknown source or target node")
        forward = self._search(self._up, source)
        backward = self._search(self._down, target)
        best = float("inf")
        for node, d_fwd in forward.items():
            d_bwd = backward.get(node)
            if d_bwd is not None:
                best = min(best, d_fwd + d_bwd)
        if best == float("inf"):
            raise RoutingError(f"no path from {source} to {target}")
        return best

    def _search(self, adjacency: dict[NodeId, list[_ContractedEdge]], source: NodeId) -> dict[NodeId, float]:
        dist: dict[NodeId, float] = {source: 0.0}
        heap: list[tuple[float, NodeId]] = [(0.0, source)]
        visited: set[NodeId] = set()
        while heap:
            d, node = heapq.heappop(heap)
            if node in visited:
                continue
            visited.add(node)
            for e in adjacency.get(node, []):
                nd = d + e.weight
                if e.to not in dist or nd < dist[e.to]:
                    dist[e.to] = nd
                    heapq.heappush(heap, (nd, e.to))
        return dist
