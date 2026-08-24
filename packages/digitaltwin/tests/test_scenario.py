import dataclasses
from typing import Any

import pytest

from digitaltwin.errors import SimulationError
from digitaltwin.network import generate_grid_network
from digitaltwin.scenario import Scenario, run_scenario

_NETWORK = generate_grid_network(5, 5, cell_size=120.0, seed=5)

_BASE_SCENARIO = Scenario(
    network=_NETWORK,
    depot=0,
    demand_points=(4, 8, 12, 16, 20, 24),
    fleet_size=3,
    base_speed=13.4,
    speed_factor=1.0,
    acceleration=2.5,
    dwell_time=45.0,
    seed=1,
)


def _scenario(**overrides: Any) -> Scenario:
    return dataclasses.replace(_BASE_SCENARIO, **overrides)


class TestScenarioValidation:
    def test_rejects_zero_fleet(self) -> None:
        with pytest.raises(SimulationError):
            _scenario(fleet_size=0)

    def test_rejects_empty_demand(self) -> None:
        with pytest.raises(SimulationError):
            _scenario(demand_points=())

    def test_rejects_nonpositive_speed(self) -> None:
        with pytest.raises(SimulationError):
            _scenario(base_speed=0.0)


class TestEndToEndScenarioRun:
    def test_produces_sane_aggregate_metrics(self) -> None:
        result = run_scenario(_scenario())

        assert result.total_distance > 0.0
        assert result.average_delivery_time > 0.0
        assert result.makespan > 0.0
        assert 0.0 <= result.utilization <= 1.0

        # All 3 agents must be present and every one of them completed.
        assert len(result.agent_results) == 3
        for agent_result in result.agent_results:
            assert agent_result.delivery_time > 0.0
            assert agent_result.distance_traveled > 0.0

        # Sane upper bound: a 5x5 grid with 120m cells has a network diagonal of
        # (4+4)*120=960m. Even at a crawl (1 m/s) plus dwelling at every one of
        # the 6 stops for 45s each, no agent should plausibly take over an hour.
        for agent_result in result.agent_results:
            assert agent_result.delivery_time < 3600.0

    def test_total_distance_is_sum_of_agent_distances(self) -> None:
        result = run_scenario(_scenario())
        assert result.total_distance == pytest.approx(
            sum(r.distance_traveled for r in result.agent_results)
        )

    def test_every_demand_point_is_assigned_to_some_agent(self) -> None:
        scenario = _scenario()
        result = run_scenario(scenario)
        assigned = {stop for r in result.agent_results for stop in r.stops}
        assert assigned == set(scenario.demand_points)

    def test_more_agents_than_demand_points_still_completes(self) -> None:
        result = run_scenario(_scenario(fleet_size=10, demand_points=(4, 8)))
        assert len(result.agent_results) == 10
        # Agents with no assigned stops complete instantly with zero distance.
        idle = [r for r in result.agent_results if r.distance_traveled == 0.0]
        busy = [r for r in result.agent_results if r.distance_traveled > 0.0]
        assert len(idle) == 8
        assert len(busy) == 2

    def test_higher_speed_factor_reduces_average_delivery_time(self) -> None:
        slow = run_scenario(_scenario(speed_factor=0.5))
        fast = run_scenario(_scenario(speed_factor=2.0))
        assert fast.average_delivery_time < slow.average_delivery_time

    def test_deterministic_given_same_scenario(self) -> None:
        a = run_scenario(_scenario())
        b = run_scenario(_scenario())
        assert a.total_distance == pytest.approx(b.total_distance)
        assert a.average_delivery_time == pytest.approx(b.average_delivery_time)


class TestClarkeWrightScenarioRun:
    def test_produces_sane_aggregate_metrics(self) -> None:
        result = run_scenario(_scenario(routing_strategy="clarke_wright"))

        assert result.total_distance > 0.0
        assert result.average_delivery_time > 0.0
        assert result.makespan > 0.0
        assert 0.0 <= result.utilization <= 1.0

        # Same fleet size as the nearest-neighbor scenario: 3 agents must be present
        # and every one of them completed. Uncapacitated Clarke-Wright merges this
        # scenario's 6 demand points onto a single route (see
        # test_unused_vehicles_are_idle_when_merges_need_fewer_routes below), so only
        # the busy agent has a nonzero delivery time -- idle agents complete instantly.
        assert len(result.agent_results) == 3
        busy = [r for r in result.agent_results if r.distance_traveled > 0.0]
        assert busy
        for agent_result in busy:
            assert agent_result.delivery_time > 0.0
            # Same sane upper bound as the nearest-neighbor end-to-end test.
            assert agent_result.delivery_time < 3600.0

    def test_every_demand_point_is_assigned_to_some_agent(self) -> None:
        scenario = _scenario(routing_strategy="clarke_wright")
        result = run_scenario(scenario)
        assigned = {stop for r in result.agent_results for stop in r.stops}
        assert assigned == set(scenario.demand_points)

    def test_total_distance_is_sum_of_agent_distances(self) -> None:
        result = run_scenario(_scenario(routing_strategy="clarke_wright"))
        assert result.total_distance == pytest.approx(
            sum(r.distance_traveled for r in result.agent_results)
        )

    def test_unused_vehicles_are_idle_when_merges_need_fewer_routes(self) -> None:
        # Uncapacitated Clarke-Wright on this base scenario's 6 demand points settles
        # on a single merged route, so 2 of the 3 fleet vehicles should sit idle --
        # same "extra agents complete instantly with zero distance" behavior as the
        # nearest-neighbor strategy gets when the fleet outnumbers the routes needed.
        result = run_scenario(_scenario(routing_strategy="clarke_wright"))
        idle = [r for r in result.agent_results if r.distance_traveled == 0.0]
        busy = [r for r in result.agent_results if r.distance_traveled > 0.0]
        assert len(idle) == 2
        assert len(busy) == 1

    def test_raises_when_fleet_too_small_for_capacity_constrained_routes(self) -> None:
        # vehicle_capacity=1 forces every demand point onto its own route (no two
        # points can ever be merged), i.e. 6 routes -- but fleet_size is only 3.
        with pytest.raises(SimulationError):
            run_scenario(_scenario(routing_strategy="clarke_wright", vehicle_capacity=1))

    def test_capacity_constrained_routing_completes_with_enough_vehicles(self) -> None:
        result = run_scenario(
            _scenario(routing_strategy="clarke_wright", vehicle_capacity=1, fleet_size=6)
        )
        assert len(result.agent_results) == 6
        assert all(r.distance_traveled > 0.0 for r in result.agent_results)

    def test_rejects_unknown_routing_strategy(self) -> None:
        with pytest.raises(SimulationError):
            _scenario(routing_strategy="orienteering")

    def test_rejects_nonpositive_vehicle_capacity(self) -> None:
        with pytest.raises(SimulationError):
            _scenario(vehicle_capacity=0)
