import networkx as nx
import pytest

from digitaltwin.agents import AgentStatus, DeliveryAgent, build_route_plan
from digitaltwin.errors import SimulationError
from digitaltwin.kinematics import SegmentProfile
from digitaltwin.network import RoadNetwork
from digitaltwin.simulation import Simulation


def _two_node_network(length: float = 100.0, speed_limit: float = 10.0) -> RoadNetwork:
    g = nx.Graph()
    g.add_node(0, x=0.0, y=0.0)
    g.add_node(1, x=length, y=0.0)
    g.add_edge(0, 1, length=length, speed_limit=speed_limit)
    return RoadNetwork(g)


class TestSingleHopSimulation:
    def test_arrival_time_matches_kinematic_profile(self) -> None:
        net = _two_node_network(length=100.0, speed_limit=10.0)
        plan = build_route_plan(net, depot=0, stop_order=[1])
        agent = DeliveryAgent(agent_id="a", route=plan, max_speed=10.0, acceleration=2.0, dwell_time=30.0)
        sim = Simulation(net, {"a": agent})
        sim.run()

        expected_travel_time = SegmentProfile(length=100.0, max_speed=10.0, acceleration=2.0).duration()
        assert agent.status == AgentStatus.COMPLETED
        assert agent.completion_time == pytest.approx(expected_travel_time + 30.0)  # + dwell at the stop
        assert agent.distance_traveled == pytest.approx(100.0)

    def test_trajectory_log_records_travel_then_dwell(self) -> None:
        net = _two_node_network(length=100.0, speed_limit=10.0)
        plan = build_route_plan(net, depot=0, stop_order=[1])
        agent = DeliveryAgent(agent_id="a", route=plan, max_speed=10.0, acceleration=2.0, dwell_time=30.0)
        sim = Simulation(net, {"a": agent})
        sim.run()

        assert len(sim.trajectory_log) == 2
        travel, dwell = sim.trajectory_log
        assert travel.profile is not None
        assert travel.from_node == 0
        assert travel.to_node == 1
        assert dwell.profile is None
        assert dwell.from_node == dwell.to_node == 1
        assert dwell.arrival_time - dwell.depart_time == pytest.approx(30.0)

    def test_speed_capped_by_edge_speed_limit_not_agent_max_speed(self) -> None:
        # Agent could go 100 m/s, but the road only allows 5 m/s.
        net = _two_node_network(length=100.0, speed_limit=5.0)
        plan = build_route_plan(net, depot=0, stop_order=[1])
        agent = DeliveryAgent(agent_id="a", route=plan, max_speed=100.0, acceleration=2.0, dwell_time=0.0)
        sim = Simulation(net, {"a": agent})
        sim.run()

        expected = SegmentProfile(length=100.0, max_speed=5.0, acceleration=2.0).duration()
        assert agent.completion_time == pytest.approx(expected)

    def test_run_twice_raises(self) -> None:
        net = _two_node_network()
        plan = build_route_plan(net, depot=0, stop_order=[1])
        agent = DeliveryAgent(agent_id="a", route=plan, max_speed=10.0, acceleration=2.0, dwell_time=0.0)
        sim = Simulation(net, {"a": agent})
        sim.run()
        with pytest.raises(SimulationError):
            sim.run()

    def test_zero_stop_agent_completes_instantly(self) -> None:
        net = _two_node_network()
        plan = build_route_plan(net, depot=0, stop_order=[])
        agent = DeliveryAgent(agent_id="a", route=plan, max_speed=10.0, acceleration=2.0, dwell_time=30.0)
        sim = Simulation(net, {"a": agent})
        sim.run()
        assert agent.status == AgentStatus.COMPLETED
        assert agent.completion_time == 0.0
        assert agent.distance_traveled == 0.0
        assert sim.trajectory_log == []


class TestMultiHopSimulation:
    def test_pass_through_node_does_not_dwell(self) -> None:
        g = nx.Graph()
        for n in (0, 1, 2):
            g.add_node(n, x=float(n) * 100.0, y=0.0)
        g.add_edge(0, 1, length=100.0, speed_limit=10.0)
        g.add_edge(1, 2, length=100.0, speed_limit=10.0)
        net = RoadNetwork(g)
        # Only node 2 is a delivery stop; node 1 is a pass-through intersection.
        plan = build_route_plan(net, depot=0, stop_order=[2])
        agent = DeliveryAgent(agent_id="a", route=plan, max_speed=10.0, acceleration=2.0, dwell_time=15.0)
        sim = Simulation(net, {"a": agent})
        sim.run()

        # Two travel segments, one dwell segment (at node 2 only) -> 3 total.
        assert len(sim.trajectory_log) == 3
        kinds = [seg.profile is not None for seg in sim.trajectory_log]
        assert kinds == [True, True, False]
        assert agent.distance_traveled == pytest.approx(200.0)
