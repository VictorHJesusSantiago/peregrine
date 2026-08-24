import json

import pytest

from digitaltwin.network import generate_grid_network
from digitaltwin.scenario import Scenario, SimulationResult, run_scenario
from digitaltwin.viz import (
    export_network,
    export_trajectories,
    plot_network_and_trajectories,
    sample_trajectories,
    trajectories_to_dataframe,
)


def _result() -> SimulationResult:
    net = generate_grid_network(4, 4, cell_size=100.0, seed=2)
    scenario = Scenario(
        network=net,
        depot=0,
        demand_points=(3, 12, 15),
        fleet_size=3,
        acceleration=2.0,
        dwell_time=20.0,
    )
    return run_scenario(scenario)


class TestTrajectorySampling:
    def test_every_agent_has_a_complete_trajectory(self) -> None:
        result = _result()
        points = sample_trajectories(result, dt=1.0)
        agent_ids = {r.agent_id for r in result.agent_results if r.distance_traveled > 0}

        seen_agents = {p.agent_id for p in points}
        assert seen_agents == agent_ids

        for agent_result in result.agent_results:
            if agent_result.distance_traveled == 0.0:
                continue
            agent_points = sorted((p for p in points if p.agent_id == agent_result.agent_id), key=lambda p: p.t)
            assert agent_points[0].t == pytest.approx(0.0)
            assert agent_points[-1].t == pytest.approx(agent_result.delivery_time)

    def test_rejects_nonpositive_dt(self) -> None:
        with pytest.raises(ValueError, match="dt"):
            sample_trajectories(_result(), dt=0.0)

    def test_positions_stay_within_network_bounding_box(self) -> None:
        result = _result()
        points = sample_trajectories(result, dt=2.0)
        xs = [n.x for n in result.network.nodes()]
        ys = [n.y for n in result.network.nodes()]
        for p in points:
            assert min(xs) - 1e-6 <= p.x <= max(xs) + 1e-6
            assert min(ys) - 1e-6 <= p.y <= max(ys) + 1e-6


class TestDataFrameExport:
    def test_dataframe_has_expected_columns_and_row_count(self) -> None:
        result = _result()
        points = sample_trajectories(result, dt=5.0)
        df = trajectories_to_dataframe(points)
        assert list(df.columns) == ["agent_id", "t", "x", "y"]
        assert len(df) == len(points)


class TestJsonExport:
    def test_network_export_is_json_serializable_and_complete(self) -> None:
        result = _result()
        data = export_network(result.network)
        serialized = json.dumps(data)
        assert serialized  # round-trips without raising
        assert len(data["nodes"]) == len(result.network.nodes())
        assert len(data["edges"]) == len(result.network.edges())
        assert set(data["nodes"][0].keys()) == {"id", "x", "y"}
        assert set(data["edges"][0].keys()) == {"u", "v", "length", "speed_limit", "congestion_factor"}

    def test_trajectory_export_is_json_serializable_and_complete(self) -> None:
        result = _result()
        points = sample_trajectories(result, dt=3.0)
        data = export_trajectories(points)
        json.dumps(data)  # round-trips without raising
        assert len(data) == len(points)
        assert set(data[0].keys()) == {"agent_id", "t", "x", "y"}


class TestOptionalPlot:
    def test_plot_network_and_trajectories_returns_axes(self) -> None:
        result = _result()
        points = sample_trajectories(result, dt=5.0)
        ax = plot_network_and_trajectories(result.network, points)
        assert ax is not None
        import matplotlib.pyplot as plt
        from matplotlib.figure import Figure

        fig = ax.get_figure()
        if isinstance(fig, Figure):
            plt.close(fig)
