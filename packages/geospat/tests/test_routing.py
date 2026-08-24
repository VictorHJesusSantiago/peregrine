import heapq
import random

import pytest

from geospat.errors import RoutingError
from geospat.routing import (
    ContractionHierarchy,
    Graph,
    NodeId,
    _ContractedEdge,
    dijkstra,
    shortest_path_distance,
)


def _random_connected_graph(rng: random.Random, n_nodes: int, extra_edges: int) -> Graph:
    """A random graph guaranteed connected: start with a random spanning tree (so every node is
    reachable from every other), then sprinkle in extra random edges for more interesting
    shortest paths and shortcut opportunities during contraction."""
    graph = Graph()
    nodes = list(range(n_nodes))
    rng.shuffle(nodes)
    graph.add_node(nodes[0])
    for i in range(1, n_nodes):
        parent = nodes[rng.randrange(i)]
        graph.add_edge(parent, nodes[i], rng.uniform(1.0, 20.0))
    for _ in range(extra_edges):
        u, v = rng.sample(nodes, 2)
        graph.add_edge(u, v, rng.uniform(1.0, 20.0))
    return graph


def _grid_graph(rng: random.Random, rows: int, cols: int) -> Graph:
    """A `rows` x `cols` 4-neighbor grid lattice with random positive weights — a toy road network
    with real spatial/hierarchical structure, unlike `_random_connected_graph`'s Erdos-Renyi-ish
    tree-plus-random-edges shape. Contraction hierarchies are specifically designed to exploit that
    kind of structure (queries funnel through a small set of "highway" nodes near the top of the
    hierarchy), so this is the graph shape that makes the edge-difference ordering's query-speed
    advantage over one-shot degree-sort show up clearly and reproducibly — see
    `TestContractionHierarchyOrderingQuality`."""
    graph = Graph()

    def node_id(r: int, c: int) -> int:
        return r * cols + c

    for r in range(rows):
        for c in range(cols):
            graph.add_node(node_id(r, c))
    for r in range(rows):
        for c in range(cols):
            if c + 1 < cols:
                graph.add_edge(node_id(r, c), node_id(r, c + 1), rng.uniform(1.0, 5.0))
            if r + 1 < rows:
                graph.add_edge(node_id(r, c), node_id(r + 1, c), rng.uniform(1.0, 5.0))
    return graph


class TestGraph:
    def test_add_edge_is_bidirectional_by_default(self) -> None:
        g = Graph()
        g.add_edge(1, 2, 5.0)
        assert any(e.to == 2 for e in g.neighbors(1))
        assert any(e.to == 1 for e in g.neighbors(2))

    def test_one_way_edge_is_not_traversable_in_reverse(self) -> None:
        g = Graph()
        g.add_edge(1, 2, 5.0, bidirectional=False)
        assert any(e.to == 2 for e in g.neighbors(1))
        assert not any(e.to == 1 for e in g.neighbors(2))

    def test_rejects_negative_weight(self) -> None:
        g = Graph()
        with pytest.raises(RoutingError):
            g.add_edge(1, 2, -1.0)


class TestDijkstra:
    def test_simple_known_distance(self) -> None:
        g = Graph()
        g.add_edge(0, 1, 1.0)
        g.add_edge(1, 2, 1.0)
        g.add_edge(0, 2, 5.0)
        assert shortest_path_distance(g, 0, 2) == pytest.approx(2.0)

    def test_unreachable_target_raises(self) -> None:
        g = Graph()
        g.add_node(0)
        g.add_node(1)
        with pytest.raises(RoutingError):
            shortest_path_distance(g, 0, 1)

    def test_unknown_source_raises(self) -> None:
        g = Graph()
        g.add_node(0)
        with pytest.raises(RoutingError):
            dijkstra(g, 99)

    def test_distance_to_self_is_zero(self) -> None:
        g = Graph()
        g.add_edge(0, 1, 3.0)
        assert shortest_path_distance(g, 0, 0) == 0.0


class TestContractionHierarchyMatchesDijkstra:
    @pytest.mark.parametrize("seed", range(8))
    def test_random_graph_distances_agree_with_dijkstra(self, seed: int) -> None:
        rng = random.Random(seed)
        graph = _random_connected_graph(rng, n_nodes=30, extra_edges=25)
        ch = ContractionHierarchy(graph)

        nodes = graph.nodes()
        for _ in range(15):
            source, target = rng.sample(nodes, 2)
            expected = shortest_path_distance(graph, source, target)
            actual = ch.distance(source, target)
            assert actual == pytest.approx(expected, rel=1e-9, abs=1e-9)

    def test_distance_to_self_is_zero(self) -> None:
        rng = random.Random(123)
        graph = _random_connected_graph(rng, n_nodes=15, extra_edges=10)
        ch = ContractionHierarchy(graph)
        for node in graph.nodes():
            assert ch.distance(node, node) == 0.0

    def test_unknown_node_raises(self) -> None:
        rng = random.Random(1)
        graph = _random_connected_graph(rng, n_nodes=10, extra_edges=5)
        ch = ContractionHierarchy(graph)
        with pytest.raises(RoutingError):
            ch.distance(0, 9999)

    def test_small_hand_built_graph(self) -> None:
        # A -1- B -1- C -1- D, plus a slow direct A-D edge, so the true shortest path must go
        # through the whole chain rather than the tempting direct edge.
        g = Graph()
        g.add_edge("A", "B", 1.0)
        g.add_edge("B", "C", 1.0)
        g.add_edge("C", "D", 1.0)
        g.add_edge("A", "D", 10.0)
        ch = ContractionHierarchy(g)
        assert ch.distance("A", "D") == pytest.approx(3.0)
        assert shortest_path_distance(g, "A", "D") == pytest.approx(3.0)

    @pytest.mark.parametrize("seed", range(4))
    def test_disconnected_components_raise_on_both_baseline_and_ch(self, seed: int) -> None:
        rng = random.Random(seed)
        g1 = _random_connected_graph(rng, n_nodes=8, extra_edges=4)
        g2 = _random_connected_graph(rng, n_nodes=8, extra_edges=4)
        combined = Graph()
        for node in g1.nodes():
            combined.add_node(f"a_{node}")
        for node in g2.nodes():
            combined.add_node(f"b_{node}")
        for node in g1.nodes():
            for edge in g1.neighbors(node):
                combined.add_edge(f"a_{node}", f"a_{edge.to}", edge.weight, bidirectional=False)
        for node in g2.nodes():
            for edge in g2.neighbors(node):
                combined.add_edge(f"b_{node}", f"b_{edge.to}", edge.weight, bidirectional=False)

        ch = ContractionHierarchy(combined)
        with pytest.raises(RoutingError):
            shortest_path_distance(combined, "a_0", "b_0")
        with pytest.raises(RoutingError):
            ch.distance("a_0", "b_0")


class TestContractionHierarchyOrderingQuality:
    """`ContractionHierarchy`'s real, shipped node ordering is the standard dynamically-recomputed
    edge-difference priority queue. `_ordering="degree"` (see `routing._Ordering`) is the one-shot
    static sort it replaced, kept only as an injectable comparison baseline so this class can prove
    the replacement is actually *better* — the `TestContractionHierarchyMatchesDijkstra` tests above
    already prove both orderings are equally *correct* (any total contraction order is), which on
    its own says nothing about whether this change accomplished anything."""

    @staticmethod
    def _count_shortcuts(ch: ContractionHierarchy) -> int:
        return sum(1 for edges in ch._up.values() for e in edges if e.via is not None) + sum(
            1 for edges in ch._down.values() for e in edges if e.via is not None
        )

    @staticmethod
    def _count_settled(adjacency: dict[NodeId, list[_ContractedEdge]], source: NodeId) -> int:
        """Mirrors `ContractionHierarchy._search`'s traversal exactly (same heap, same relaxation),
        but reports how many nodes it actually settles — pops off the heap and finalizes — rather
        than the distances themselves. That settle count is how many nodes a query actually has to
        touch, which is the thing a better node ordering is supposed to shrink."""
        dist: dict[NodeId, float] = {source: 0.0}
        visited: set[NodeId] = set()
        heap: list[tuple[float, NodeId]] = [(0.0, source)]
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
        return len(visited)

    def test_produces_fewer_shortcuts_than_degree_sort(self) -> None:
        # Same graph shape and parameters as `TestContractionHierarchyMatchesDijkstra`'s main
        # correctness test (30 nodes, 25 extra edges, seeds 0-7): dense enough that contraction
        # order visibly affects how many shortcuts get created.
        total_new = 0
        total_old = 0
        for seed in range(8):
            rng = random.Random(seed)
            graph = _random_connected_graph(rng, n_nodes=30, extra_edges=25)
            total_new += self._count_shortcuts(ContractionHierarchy(graph))
            total_old += self._count_shortcuts(ContractionHierarchy(graph, _ordering="degree"))

        # Individual seeds can be close or even tie (any valid order is still just a heuristic),
        # but summed across seeds the edge-difference ordering should win, and by a real margin —
        # not by one or two shortcuts.
        assert total_new < total_old
        assert total_new <= total_old * 0.85

    def test_visits_fewer_nodes_per_query_than_degree_sort(self) -> None:
        # A grid, not `_random_connected_graph`: contraction hierarchies are built to exploit
        # spatial/hierarchical structure (see `_grid_graph`'s docstring), and that structure is
        # what makes the query-speed advantage of a good node ordering show up reliably rather
        # than getting lost in the noise of an unstructured random graph.
        total_new = 0
        total_old = 0
        n_queries = 0
        for seed in range(8):
            rng = random.Random(seed)
            graph = _grid_graph(rng, rows=10, cols=10)
            ch_new = ContractionHierarchy(graph)
            ch_old = ContractionHierarchy(graph, _ordering="degree")

            nodes = graph.nodes()
            qrng = random.Random(seed + 1000)
            seed_new = 0
            seed_old = 0
            for _ in range(15):
                source, target = qrng.sample(nodes, 2)
                seed_new += self._count_settled(ch_new._up, source) + self._count_settled(
                    ch_new._down, target
                )
                seed_old += self._count_settled(ch_old._up, source) + self._count_settled(
                    ch_old._down, target
                )
            # Holds for every seed tested here, not just on average — the edge-difference ordering
            # never needs to touch more nodes than the degree-sort one on this graph shape.
            assert seed_new <= seed_old
            total_new += seed_new
            total_old += seed_old
            n_queries += 15

        avg_new = total_new / n_queries
        avg_old = total_old / n_queries
        assert avg_new < avg_old
        # Not a marginal win: on this graph shape the edge-difference ordering visits well under
        # two-thirds as many nodes per query as the degree-sort ordering does.
        assert avg_new <= avg_old * 0.65
