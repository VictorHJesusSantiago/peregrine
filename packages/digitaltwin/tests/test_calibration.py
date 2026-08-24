import pytest

from digitaltwin.calibration import calibrate_parameter
from digitaltwin.errors import CalibrationError
from digitaltwin.network import generate_grid_network
from digitaltwin.scenario import Scenario, run_scenario

_NETWORK = generate_grid_network(5, 5, cell_size=150.0, seed=11)
_DEMAND_POINTS = (6, 12, 18, 24)


def _average_delivery_time_for(speed_factor: float) -> float:
    """Run a small, fully deterministic scenario and return its average delivery time."""
    scenario = Scenario(
        network=_NETWORK,
        depot=0,
        demand_points=_DEMAND_POINTS,
        fleet_size=2,
        speed_factor=speed_factor,
        dwell_time=20.0,
    )
    return run_scenario(scenario).average_delivery_time


class TestCalibrationConvergence:
    def test_recovers_known_speed_factor_from_synthetic_observations(self) -> None:
        # Generate "observed" data from a known-correct parameter value...
        true_speed_factor = 0.8
        observed_average_delivery_time = _average_delivery_time_for(true_speed_factor)

        # ...then calibrate starting the search from bounds that do *not* have
        # the true value anywhere near their center, and check it converges back.
        result = calibrate_parameter(
            parameter_name="speed_factor",
            objective=_average_delivery_time_for,
            target=observed_average_delivery_time,
            lower_bound=0.3,
            upper_bound=1.8,
            tolerance=1e-3,
            max_iterations=100,
        )

        assert result.calibrated_value == pytest.approx(true_speed_factor, abs=0.02)
        assert result.achieved == pytest.approx(observed_average_delivery_time, rel=0.02)
        assert result.iterations > 0

    def test_higher_speed_factor_never_increases_delivery_time(self) -> None:
        # Sanity check on the monotonicity assumption golden-section search relies on.
        slow = _average_delivery_time_for(0.5)
        fast = _average_delivery_time_for(1.5)
        assert fast <= slow

    def test_rejects_invalid_bounds(self) -> None:
        with pytest.raises(CalibrationError):
            calibrate_parameter(
                parameter_name="x",
                objective=lambda x: x,
                target=1.0,
                lower_bound=2.0,
                upper_bound=1.0,
            )

    def test_history_records_every_evaluation(self) -> None:
        result = calibrate_parameter(
            parameter_name="x",
            objective=lambda x: x,
            target=5.0,
            lower_bound=0.0,
            upper_bound=10.0,
            tolerance=1e-2,
        )
        # 2 initial bracket evaluations, then exactly one new evaluation per iteration.
        assert len(result.history) == result.iterations + 2
        assert all(0.0 <= v <= 10.0 for v, _ in result.history)
