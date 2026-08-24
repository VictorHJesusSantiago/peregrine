import pytest

from digitaltwin.agents import (
    AgentStatus,
    DeliveryAgent,
    build_route_plan,
    clarke_wright_routes,
    nearest_neighbor_order,
)
from digitaltwin.errors import SimulationError
from digitaltwin.network import NodeId, RoadNetwork, generate_grid_network
from digitaltwin.scenario import _assign_demand_points


class TestNearestNeighborOrdering:
    def test_visits_closest_point_first(self) -> None:
        # 4x4 grid, cell_size=100. Depot at node 0 == (0,0).
        net = generate_grid_network(4, 4, cell_size=100.0, seed=0)
        # node 5 == (1,1) -> (x=100,y=100); node 15 == (3,3) -> (x=300,y=300).
        far = 15
        near = 5
        order = nearest_neighbor_order(net, depot=0, demand_points=[far, near])
        assert order[0] == near
        assert order[1] == far

    def test_visits_all_points_exactly_once(self) -> None:
        net = generate_grid_network(4, 4, cell_size=100.0, seed=0)
        points = [3, 7, 12, 5]
        order = nearest_neighbor_order(net, depot=0, demand_points=points)
        assert sorted(order) == sorted(points)

    def test_empty_demand_is_empty_order(self) -> None:
        net = generate_grid_network(3, 3, seed=0)
        assert nearest_neighbor_order(net, depot=0, demand_points=[]) == []


class TestRoutePlan:
    def test_route_includes_intermediate_nodes_and_flags_stops(self) -> None:
        net = generate_grid_network(3, 3, cell_size=100.0, seed=0)
        # node ids: row-major, 3 cols -> node(i,j) = 3*i+j. depot = 0 (0,0). stop = 8 (2,2).
        plan = build_route_plan(net, depot=0, stop_order=[8])
        assert plan.nodes[0] == 0
        assert plan.nodes[-1] == 8
        assert (len(plan.nodes) - 1) in plan.stops
        # Manhattan shortest path over a uniform grid has exactly 4 hops from (0,0) to (2,2).
        assert len(plan.nodes) == 5

    def test_multi_stop_route_flags_every_stop(self) -> None:
        net = generate_grid_network(3, 3, cell_size=100.0, seed=0)
        plan = build_route_plan(net, depot=0, stop_order=[2, 8])
        stop_nodes = {plan.nodes[i] for i in plan.stops}
        assert stop_nodes == {2, 8}

    def test_no_stops_route_is_just_the_depot(self) -> None:
        net = generate_grid_network(3, 3, seed=0)
        plan = build_route_plan(net, depot=0, stop_order=[])
        assert plan.nodes == (0,)
        assert plan.stops == frozenset()


class TestDeliveryAgent:
    def _agent(self) -> DeliveryAgent:
        net = generate_grid_network(3, 3, cell_size=100.0, seed=0)
        plan = build_route_plan(net, depot=0, stop_order=[8])
        return DeliveryAgent(agent_id="a", route=plan, max_speed=10.0, acceleration=2.0, dwell_time=30.0)

    def test_initial_state(self) -> None:
        agent = self._agent()
        assert agent.status == AgentStatus.PENDING
        assert agent.current_node == 0
        assert not agent.is_finished
        assert agent.distance_traveled == 0.0

    def test_next_node_advances_along_route(self) -> None:
        agent = self._agent()
        first_hop = agent.next_node
        assert first_hop in (1, 3)  # either grid neighbor is a valid shortest-path first hop
        agent.route_index += 1
        assert agent.current_node == first_hop

    def test_is_finished_at_last_node(self) -> None:
        agent = self._agent()
        agent.route_index = len(agent.route.nodes) - 1
        assert agent.is_finished

    def test_next_node_raises_when_finished(self) -> None:
        agent = self._agent()
        agent.route_index = len(agent.route.nodes) - 1
        with pytest.raises(SimulationError):
            _ = agent.next_node

    def test_is_stop_only_true_for_flagged_indices(self) -> None:
        agent = self._agent()
        last_index = len(agent.route.nodes) - 1
        assert agent.is_stop(last_index)
        assert not agent.is_stop(0)


def _route_distance(network: RoadNetwork, depot: NodeId, route: list[NodeId]) -> float:
    """Total distance of a depot -> route[0] -> route[1] -> ... -> route[-1] -> depot round trip."""
    if not route:
        return 0.0
    total = network.shortest_path_length(depot, route[0])
    for a, b in zip(route, route[1:], strict=False):
        total += network.shortest_path_length(a, b)
    total += network.shortest_path_length(route[-1], depot)
    return total


class TestClarkeWrightRoutes:
    # 5x5 grid, cell_size=100, node(i,j) = 5*i+j -> Manhattan shortest paths.
    # Depot (0,0) = node 0. Two hand-pickable "pairs": A=(1,0)=5, B=(1,1)=6 near the
    # depot; C=(3,3)=18, D=(3,4)=19 far from the depot. Distances (in cell_size units):
    #   d(depot,A)=100  d(depot,B)=200  d(depot,C)=600  d(depot,D)=700
    #   d(A,B)=100      d(C,D)=100      d(A,C)=500  d(A,D)=600  d(B,C)=400  d(B,D)=500
    # Savings: s(C,D)=1200, s(B,C)=s(B,D)=400, s(A,B)=s(A,C)=s(A,D)=200.
    _NET = generate_grid_network(5, 5, cell_size=100.0, seed=0)
    _A, _B, _C, _D = 5, 6, 18, 19

    def test_empty_demand_is_empty_routes(self) -> None:
        assert clarke_wright_routes(self._NET, depot=0, demand_points=[]) == []

    def test_single_point_is_its_own_route(self) -> None:
        assert clarke_wright_routes(self._NET, depot=0, demand_points=[self._A]) == [[self._A]]

    def test_uncapacitated_merges_everything_when_savings_stay_positive(self) -> None:
        # Every pairwise savings computed above is positive, and (by hand-tracing the
        # merge order: C-D first, then B-C, then A-B, then the remaining pairs are
        # already-same-route no-ops) nothing blocks a full merge -- so all four points
        # collapse onto a single route, in points-list order.
        routes = clarke_wright_routes(self._NET, depot=0, demand_points=[self._A, self._B, self._C, self._D])
        assert routes == [[self._A, self._B, self._C, self._D]]

    def test_capacity_forces_the_expected_two_routes(self) -> None:
        # With vehicle_capacity=2: C-D merges first (len 1+1=2, OK). Then B-C and B-D
        # are both blocked (1+2=3 > 2). Then A-B merges (1+1=2, OK). Then A-C and A-D
        # are both blocked (2+2=4 > 2). Final grouping, by hand: [[A, B], [C, D]].
        routes = clarke_wright_routes(
            self._NET, depot=0, demand_points=[self._A, self._B, self._C, self._D], vehicle_capacity=2
        )
        assert routes == [[self._A, self._B], [self._C, self._D]]

    def test_every_demand_point_appears_exactly_once(self) -> None:
        points = [3, 7, 12, 5, 20, 24]
        routes = clarke_wright_routes(self._NET, depot=0, demand_points=points)
        flattened = [p for route in routes for p in route]
        assert sorted(flattened) == sorted(points)

    def test_capacity_caps_every_route_length(self) -> None:
        points = [1, 2, 3, 6, 7, 8, 11, 12, 13]
        routes = clarke_wright_routes(self._NET, depot=0, demand_points=points, vehicle_capacity=3)
        assert all(len(route) <= 3 for route in routes)
        # Demand exceeds a single vehicle's capacity, so more than one route is required.
        assert len(routes) >= 3
        flattened = [p for route in routes for p in route]
        assert sorted(flattened) == sorted(points)

    def test_clustered_demand_beats_naive_and_round_robin(self) -> None:
        # Two tight geographic clusters, far apart from each other and from the depot.
        net = generate_grid_network(10, 10, cell_size=50.0, seed=0)
        cluster_near = [11, 12, 21, 22]  # around (1,1)-(2,2)
        cluster_far = [88, 89, 98, 99]  # around (8,8)-(9,9)
        points = cluster_near + cluster_far
        depot = 0

        naive_total = sum(2 * net.shortest_path_length(depot, p) for p in points)

        cw_routes = clarke_wright_routes(net, depot, points)
        cw_total = sum(_route_distance(net, depot, route) for route in cw_routes)

        rr_buckets = _assign_demand_points(points, fleet_size=2)
        rr_orders = [nearest_neighbor_order(net, depot, bucket) for bucket in rr_buckets]
        rr_total = sum(_route_distance(net, depot, order) for order in rr_orders)

        # Clarke-Wright must do real optimization, not just produce *a* valid grouping:
        # meaningfully shorter than giving every point its own round trip (empirically
        # 1800 vs. 8000 for this layout -- see the printed numbers this test's docstring
        # references)...
        assert cw_total < naive_total * 0.7
        # ...and at least as good as a distance-blind round-robin split, which freely
        # interleaves the two clusters across vehicles (empirically 1800 vs. 3500).
        assert cw_total <= rr_total
