"""Delivery agents: route planning and live per-agent simulation state.

`RoutePlan` is the immutable *plan* (which nodes to visit, in which order,
and which of them are delivery stops). `DeliveryAgent` is the mutable *live
state* that changes as the simulation clock advances -- current position in
the route, distance covered, timings -- deliberately a plain stateful class
rather than a dataclass, because mutation is the entire point of it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

from digitaltwin.errors import SimulationError
from digitaltwin.network import NodeId, RoadNetwork


@dataclass(frozen=True, slots=True)
class RoutePlan:
    """An ordered sequence of nodes to traverse, with a subset flagged as delivery stops.

    `nodes` includes every intersection passed through, not just the
    delivery stops -- it is the concatenation of shortest paths between
    consecutive stops. `stops` holds the *indices* into `nodes` (not node
    ids) that are actual delivery points, where an agent dwells to make a
    delivery rather than just passing through.
    """

    nodes: tuple[NodeId, ...]
    stops: frozenset[int]


def nearest_neighbor_order(
    network: RoadNetwork, depot: NodeId, demand_points: Sequence[NodeId]
) -> list[NodeId]:
    """Order demand points via a greedy nearest-neighbor heuristic.

    From the current location, always visit the closest unvisited demand
    point next. This is deliberately *not* a vehicle-routing-problem solver
    -- optimal multi-stop routing (TSP/VRP) is out of scope for this
    package. Nearest-neighbor is a simple, deterministic heuristic that
    produces a believable, non-crossing-ish multi-stop route.
    """
    remaining = list(demand_points)
    order: list[NodeId] = []
    current = depot
    while remaining:
        nxt = min(remaining, key=lambda n: network.shortest_path_length(current, n))
        order.append(nxt)
        remaining.remove(nxt)
        current = nxt
    return order


def clarke_wright_routes(
    network: RoadNetwork,
    depot: NodeId,
    demand_points: Sequence[NodeId],
    *,
    vehicle_capacity: int | None = None,
) -> list[list[NodeId]]:
    """Cluster and order demand points into multiple vehicle routes via Clarke-Wright savings.

    Unlike `nearest_neighbor_order` -- which only ever orders stops *within*
    a single, already-assigned route -- this decides both *how many routes
    there are* and *which points belong on each one*. It is a genuine, if
    still heuristic, multi-vehicle VRP solver:

    1. Start from one dedicated round trip per demand point (depot -> point
       -> depot).
    2. Compute the savings of serving two points `i` and `j` back-to-back on
       the same route instead of on two separate round trips:
       `s(i, j) = d(depot, i) + d(depot, j) - d(i, j)`, using
       `RoadNetwork.shortest_path_length` for every distance term.
    3. Walk the pairs in descending order of savings, greedily merging two
       routes whenever `i` and `j` are each still an *endpoint* of their own
       route (directly adjacent to the depot -- a point that has already
       been merged into the interior of a route has two neighbours already
       and can never be reattached), the two points are on different
       routes, the savings is positive, and the merge would not exceed
       `vehicle_capacity` (if given).
    4. Stop once no legal, positive-savings merge remains.

    Capacity is measured in *number of stops per route*, not a weighted
    demand quantity -- this package has no notion of per-point delivery
    weight (every stop costs the same fixed `dwell_time`), so "capacity" here
    means "how many deliveries one vehicle may carry in a single route." This
    is a real, if simplified, version of the capacitated savings algorithm;
    pass `vehicle_capacity=None` (the default) for the uncapacitated variant,
    which merges as aggressively as the savings and endpoint rules allow and
    may return far fewer routes than there are demand points -- even a
    single route, if the geometry favors it.

    This is still a heuristic, not an exact solver: it does not guarantee a
    minimal total distance or a minimal number of routes, and (like
    `nearest_neighbor_order`) it assumes `demand_points` contains no
    duplicate node ids. What it *does* provide, that `nearest_neighbor_order`
    never can, is a genuine multi-vehicle grouping decision.

    Returns one ordered list of demand points per resulting route -- as many
    routes as the merges settled on, which may be fewer than
    `len(demand_points)`.
    """
    points = list(demand_points)
    if not points:
        return []
    if len(points) == 1:
        return [points]

    depot_distance = {p: network.shortest_path_length(depot, p) for p in points}

    savings: list[tuple[float, NodeId, NodeId]] = []
    for idx, i in enumerate(points):
        for j in points[idx + 1 :]:
            s = depot_distance[i] + depot_distance[j] - network.shortest_path_length(i, j)
            savings.append((s, i, j))
    savings.sort(key=lambda t: t[0], reverse=True)

    routes: dict[NodeId, list[NodeId]] = {p: [p] for p in points}

    for s, i, j in savings:
        if s <= 0:
            # Savings are sorted descending: once we hit a non-positive one,
            # every remaining pair is non-positive too -- merging would not
            # shorten anything, so there is nothing left worth doing.
            break

        route_i = routes[i]
        route_j = routes[j]
        if route_i is route_j:
            continue  # already on the same route
        if i not in (route_i[0], route_i[-1]):
            continue  # i is already interior to its route
        if j not in (route_j[0], route_j[-1]):
            continue  # j is already interior to its route
        if vehicle_capacity is not None and len(route_i) + len(route_j) > vehicle_capacity:
            continue

        if route_i[-1] != i:
            route_i = route_i[::-1]
        if route_j[0] != j:
            route_j = route_j[::-1]
        merged = route_i + route_j
        for p in merged:
            routes[p] = merged

    seen: set[int] = set()
    result: list[list[NodeId]] = []
    for p in points:
        route = routes[p]
        if id(route) not in seen:
            seen.add(id(route))
            result.append(list(route))
    return result


def build_route_plan(network: RoadNetwork, depot: NodeId, stop_order: Sequence[NodeId]) -> RoutePlan:
    """Build a `RoutePlan` by concatenating shortest paths depot -> stop_order[0] -> stop_order[1] -> ...

    Every intersection along each leg's shortest path is included in
    `nodes`; only the requested stops themselves are flagged in `stops`.
    """
    nodes: list[NodeId] = [depot]
    stop_indices: set[int] = set()
    current = depot
    for stop in stop_order:
        leg = network.shortest_path(current, stop)
        # leg[0] == current, which is already the last element of `nodes`.
        nodes.extend(leg[1:])
        stop_indices.add(len(nodes) - 1)
        current = stop
    return RoutePlan(nodes=tuple(nodes), stops=frozenset(stop_indices))


class AgentStatus(Enum):
    PENDING = "pending"
    IN_TRANSIT = "in_transit"
    DWELLING = "dwelling"
    COMPLETED = "completed"


class DeliveryAgent:
    """Mutable live state of one delivery agent during a simulation run."""

    def __init__(
        self,
        agent_id: str,
        route: RoutePlan,
        max_speed: float,
        acceleration: float,
        dwell_time: float,
    ) -> None:
        self.agent_id = agent_id
        self.route = route
        self.max_speed = max_speed
        self.acceleration = acceleration
        self.dwell_time = dwell_time

        self.route_index = 0
        self.status = AgentStatus.PENDING
        self.distance_traveled = 0.0
        self.moving_time = 0.0
        self.start_time: float | None = None
        self.completion_time: float | None = None

    @property
    def current_node(self) -> NodeId:
        return self.route.nodes[self.route_index]

    @property
    def is_finished(self) -> bool:
        """Whether the agent has reached the last node of its route."""
        return self.route_index >= len(self.route.nodes) - 1

    @property
    def next_node(self) -> NodeId:
        if self.is_finished:
            raise SimulationError(f"agent {self.agent_id} has no remaining road segments")
        return self.route.nodes[self.route_index + 1]

    def is_stop(self, node_index: int) -> bool:
        """Whether the node at `node_index` in the route is a delivery stop."""
        return node_index in self.route.stops
