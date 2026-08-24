"""Road network representation and synthetic network generators.

A `RoadNetwork` is a graph of intersections (`Node`) connected by road
segments (`RoadEdge`, carrying length, speed limit, and a congestion
factor). We lean on `networkx` for the graph data structure and its
well-tested Dijkstra shortest-path implementation -- reinventing graph
algorithms would not demonstrate anything; the hand-built parts of this
package are the discrete-event core, the kinematics, and the calibration
search, not graph theory.

Two deterministic, seeded generators produce fully self-contained synthetic
networks (no real-world map data required): a rectangular grid
(`generate_grid_network`) and a randomly-scattered, planar-ish network
(`generate_random_network`).
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from math import dist
from typing import cast

import networkx as nx

from digitaltwin.errors import NetworkGenerationError, RoutingError

type NodeId = int


@dataclass(frozen=True, slots=True)
class Node:
    """An intersection: a point in the road network."""

    id: NodeId
    x: float
    y: float


@dataclass(frozen=True, slots=True)
class RoadEdge:
    """A road segment connecting two intersections."""

    u: NodeId
    v: NodeId
    length: float
    speed_limit: float
    congestion_factor: float = 1.0

    @property
    def effective_speed_limit(self) -> float:
        """Speed limit after applying the segment's congestion factor."""
        return self.speed_limit * self.congestion_factor


class RoadNetwork:
    """A road network: intersections connected by road segments.

    Wraps a `networkx.Graph` so shortest-path queries reuse a well-tested
    graph library, while exposing our own frozen `Node` / `RoadEdge`
    dataclasses as the public vocabulary -- callers never need to reach into
    the underlying networkx graph (though it remains available via
    `.graph` for advanced use, e.g. custom plotting).
    """

    def __init__(self, graph: nx.Graph) -> None:
        self._graph = graph

    @property
    def graph(self) -> nx.Graph:
        return self._graph

    def nodes(self) -> list[Node]:
        return [
            Node(id=cast(NodeId, n), x=float(data["x"]), y=float(data["y"]))
            for n, data in self._graph.nodes(data=True)
        ]

    def node(self, node_id: NodeId) -> Node:
        if not self._graph.has_node(node_id):
            raise RoutingError(f"no such node: {node_id}")
        data = self._graph.nodes[node_id]
        return Node(id=node_id, x=float(data["x"]), y=float(data["y"]))

    def edges(self) -> list[RoadEdge]:
        return [
            RoadEdge(
                u=cast(NodeId, u),
                v=cast(NodeId, v),
                length=float(data["length"]),
                speed_limit=float(data["speed_limit"]),
                congestion_factor=float(data.get("congestion_factor", 1.0)),
            )
            for u, v, data in self._graph.edges(data=True)
        ]

    def get_edge(self, u: NodeId, v: NodeId) -> RoadEdge:
        if not self._graph.has_edge(u, v):
            raise RoutingError(f"no road segment between {u} and {v}")
        data = self._graph[u][v]
        return RoadEdge(
            u=u,
            v=v,
            length=float(data["length"]),
            speed_limit=float(data["speed_limit"]),
            congestion_factor=float(data.get("congestion_factor", 1.0)),
        )

    def shortest_path(self, source: NodeId, target: NodeId) -> list[NodeId]:
        """Shortest path (by cumulative road-segment length) from source to target.

        Delegates to networkx's Dijkstra implementation -- reusing a
        well-tested shortest-path algorithm is squarely appropriate reuse;
        it is not part of the from-scratch scope (the event queue and
        kinematics are).
        """
        try:
            path = nx.shortest_path(self._graph, source=source, target=target, weight="length")
        except nx.NetworkXNoPath as exc:
            raise RoutingError(f"no path from {source} to {target}") from exc
        except nx.NodeNotFound as exc:
            raise RoutingError(str(exc)) from exc
        return cast(list[NodeId], path)

    def shortest_path_length(self, source: NodeId, target: NodeId) -> float:
        try:
            length = nx.shortest_path_length(self._graph, source=source, target=target, weight="length")
        except nx.NetworkXNoPath as exc:
            raise RoutingError(f"no path from {source} to {target}") from exc
        return float(length)

    def __len__(self) -> int:
        return cast(int, self._graph.number_of_nodes())


def _add_road_edge(
    graph: nx.Graph,
    u: NodeId,
    v: NodeId,
    length: float,
    speed_limit: float,
    congestion_jitter: float,
    rng: random.Random,
) -> None:
    """Add an edge with a congestion factor jittered down from 1.0 by up to `congestion_jitter`."""
    congestion_factor = 1.0 - rng.uniform(0.0, congestion_jitter)
    graph.add_edge(u, v, length=length, speed_limit=speed_limit, congestion_factor=congestion_factor)


def generate_grid_network(
    rows: int,
    cols: int,
    *,
    cell_size: float = 100.0,
    speed_limit: float = 13.4,
    congestion_jitter: float = 0.15,
    seed: int = 0,
) -> RoadNetwork:
    """Generate a deterministic `rows` x `cols` rectangular grid road network.

    Nodes sit on a regular grid spaced `cell_size` meters apart; each node
    connects to its immediate horizontal/vertical neighbors. `speed_limit` is
    the base limit (m/s) for every edge, before a per-edge congestion factor
    -- drawn from `random.Random(seed)` in `[1 - congestion_jitter, 1]` -- is
    applied. Same `seed` always yields the same network.
    """
    if rows < 2 or cols < 2:
        raise NetworkGenerationError("grid network needs at least 2 rows and 2 cols")
    if cell_size <= 0:
        raise NetworkGenerationError("cell_size must be > 0")

    rng = random.Random(seed)
    graph: nx.Graph = nx.Graph()

    def node_id(i: int, j: int) -> NodeId:
        return i * cols + j

    for i in range(rows):
        for j in range(cols):
            graph.add_node(node_id(i, j), x=float(j * cell_size), y=float(i * cell_size))

    for i in range(rows):
        for j in range(cols):
            u = node_id(i, j)
            if j + 1 < cols:
                _add_road_edge(graph, u, node_id(i, j + 1), cell_size, speed_limit, congestion_jitter, rng)
            if i + 1 < rows:
                _add_road_edge(graph, u, node_id(i + 1, j), cell_size, speed_limit, congestion_jitter, rng)

    return RoadNetwork(graph)


def generate_random_network(
    num_nodes: int,
    *,
    area: float = 1000.0,
    connect_radius: float = 260.0,
    speed_limit: float = 13.4,
    congestion_jitter: float = 0.15,
    seed: int = 0,
) -> RoadNetwork:
    """Generate a deterministic, randomly-scattered, planar-ish road network.

    `num_nodes` points are scattered uniformly at random in an
    `area` x `area` square (seeded by `seed`); any two nodes within
    `connect_radius` of each other get a road segment (a random geometric
    graph). Any components left disconnected by that radius cutoff are
    stitched together by connecting the closest node pair between them, so
    `shortest_path` is always defined between any two nodes -- important for
    self-contained, deterministic tests that don't depend on real map data.
    """
    if num_nodes < 2:
        raise NetworkGenerationError("random network needs at least 2 nodes")

    rng = random.Random(seed)
    positions: dict[NodeId, tuple[float, float]] = {
        i: (rng.uniform(0, area), rng.uniform(0, area)) for i in range(num_nodes)
    }
    graph: nx.Graph = nx.Graph()
    for node_id, (x, y) in positions.items():
        graph.add_node(node_id, x=x, y=y)

    for i in range(num_nodes):
        for j in range(i + 1, num_nodes):
            d = dist(positions[i], positions[j])
            if d <= connect_radius:
                _add_road_edge(graph, i, j, d, speed_limit, congestion_jitter, rng)

    _connect_components(graph, positions, speed_limit, congestion_jitter, rng)
    return RoadNetwork(graph)


def _connect_components(
    graph: nx.Graph,
    positions: dict[NodeId, tuple[float, float]],
    speed_limit: float,
    congestion_jitter: float,
    rng: random.Random,
) -> None:
    """Stitch disconnected components together via each pair's closest nodes.

    Guarantees the final graph is connected (a real road network wouldn't
    have unreachable islands), while still leaving the bulk of the topology
    to the random geometric process above.
    """
    components: list[set[NodeId]] = [cast(set[NodeId], c) for c in nx.connected_components(graph)]
    if len(components) <= 1:
        return
    anchor = components[0]
    for other in components[1:]:
        best: tuple[NodeId, NodeId, float] | None = None
        for u in anchor:
            for v in other:
                d = dist(positions[u], positions[v])
                if best is None or d < best[2]:
                    best = (u, v, d)
        assert best is not None
        u, v, d = best
        _add_road_edge(graph, u, v, d, speed_limit, congestion_jitter, rng)
        anchor = anchor | other
