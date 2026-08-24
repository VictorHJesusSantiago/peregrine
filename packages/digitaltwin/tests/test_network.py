import networkx as nx
import pytest

from digitaltwin.errors import NetworkGenerationError, RoutingError
from digitaltwin.network import RoadNetwork, generate_grid_network, generate_random_network


class TestGridNetworkGeneration:
    def test_deterministic_for_same_seed(self) -> None:
        a = generate_grid_network(4, 5, seed=7)
        b = generate_grid_network(4, 5, seed=7)
        assert [(n.id, n.x, n.y) for n in a.nodes()] == [(n.id, n.x, n.y) for n in b.nodes()]
        edges_a = sorted((e.u, e.v, e.length, e.congestion_factor) for e in a.edges())
        edges_b = sorted((e.u, e.v, e.length, e.congestion_factor) for e in b.edges())
        assert edges_a == edges_b

    def test_different_seeds_yield_different_congestion(self) -> None:
        a = generate_grid_network(4, 4, seed=1)
        b = generate_grid_network(4, 4, seed=2)
        factors_a = sorted(e.congestion_factor for e in a.edges())
        factors_b = sorted(e.congestion_factor for e in b.edges())
        assert factors_a != factors_b

    def test_shape_and_node_count(self) -> None:
        net = generate_grid_network(3, 4, cell_size=50.0, seed=0)
        assert len(net) == 12
        # Interior connectivity: a 3x4 grid has 2*4*3 - 4 - 3 = 17 edges (horizontal + vertical).
        assert len(net.edges()) == 17

    def test_rejects_too_small_grid(self) -> None:
        with pytest.raises(NetworkGenerationError):
            generate_grid_network(1, 5, seed=0)

    def test_adjacent_nodes_are_cell_size_apart(self) -> None:
        net = generate_grid_network(2, 2, cell_size=75.0, seed=0)
        edge = net.get_edge(0, 1)
        assert edge.length == pytest.approx(75.0)


class TestRandomNetworkGeneration:
    def test_deterministic_for_same_seed(self) -> None:
        a = generate_random_network(20, seed=42)
        b = generate_random_network(20, seed=42)
        assert [(n.id, n.x, n.y) for n in a.nodes()] == [(n.id, n.x, n.y) for n in b.nodes()]

    def test_always_connected(self) -> None:
        # A small connect_radius forces the component-stitching path to kick in.
        net = generate_random_network(30, connect_radius=50.0, seed=3)
        assert nx.is_connected(net.graph)

    def test_rejects_too_few_nodes(self) -> None:
        with pytest.raises(NetworkGenerationError):
            generate_random_network(1, seed=0)


class TestShortestPathRouting:
    def _diamond_network(self) -> RoadNetwork:
        # Hand-built network with a known-optimal path, independent of the generators:
        #
        #        (len 10)
        #      0 -------- 1
        #      |          |
        # (len 1)     (len 10)
        #      |          |
        #      2 -------- 3
        #  (len 1)
        # and 2-1 also length 1, giving 0->2->1->3 a total of 1+1+10=12,
        # versus the direct 0->1->3 route at 10+10=20.
        g = nx.Graph()
        for n in (0, 1, 2, 3):
            g.add_node(n, x=float(n), y=0.0)
        g.add_edge(0, 1, length=10.0, speed_limit=10.0)
        g.add_edge(0, 2, length=1.0, speed_limit=10.0)
        g.add_edge(2, 1, length=1.0, speed_limit=10.0)
        g.add_edge(1, 3, length=10.0, speed_limit=10.0)
        g.add_edge(2, 3, length=20.0, speed_limit=10.0)
        return RoadNetwork(g)

    def test_shortest_path_picks_the_cheaper_route(self) -> None:
        net = self._diamond_network()
        path = net.shortest_path(0, 3)
        assert path == [0, 2, 1, 3]
        assert net.shortest_path_length(0, 3) == pytest.approx(12.0)

    def test_shortest_path_length_matches_sum_of_edges(self) -> None:
        net = self._diamond_network()
        path = net.shortest_path(0, 3)
        total = sum(net.get_edge(u, v).length for u, v in zip(path, path[1:], strict=False))
        assert total == pytest.approx(net.shortest_path_length(0, 3))

    def test_missing_node_raises_routing_error(self) -> None:
        net = self._diamond_network()
        with pytest.raises(RoutingError):
            net.shortest_path(0, 999)

    def test_get_edge_missing_raises_routing_error(self) -> None:
        net = self._diamond_network()
        with pytest.raises(RoutingError):
            net.get_edge(0, 3)  # not directly connected
