"""Automatic parameter calibration: search for the parameter value matching observed data.

Given a target observed metric (e.g. "average delivery time is 42 minutes")
and an `objective` callable that runs a simulation for a candidate parameter
value and returns the corresponding simulated metric, `calibrate_parameter`
searches for the parameter value that minimizes the discrepancy.

scipy is not installed in this environment, so this is a genuine, hand-rolled
gradient-free search rather than a call-out to `scipy.optimize`. For a single
scalar parameter with a response that is monotonic (as is the case for e.g. a
speed factor -- turning it up can only ever decrease travel times), the
squared error `(objective(x) - target) ** 2` is unimodal in `x`, which is
exactly the condition golden-section search is designed for: it brackets the
minimum and narrows the bracket by a constant ratio each iteration without
ever needing a derivative.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from digitaltwin.errors import CalibrationError

_GOLDEN_RATIO = (5**0.5 - 1) / 2  # ~0.618, the golden-section search shrink factor


@dataclass(frozen=True, slots=True)
class CalibrationResult:
    """Outcome of a parameter-calibration search."""

    parameter_name: str
    calibrated_value: float
    target: float
    achieved: float
    iterations: int
    history: tuple[tuple[float, float], ...]  # (candidate_value, simulated_metric) pairs evaluated


def calibrate_parameter(
    *,
    parameter_name: str,
    objective: Callable[[float], float],
    target: float,
    lower_bound: float,
    upper_bound: float,
    tolerance: float = 1e-3,
    max_iterations: int = 100,
) -> CalibrationResult:
    """Search `[lower_bound, upper_bound]` for the parameter value best matching `target`.

    `objective(value)` should run a simulation with the parameter set to
    `value` and return the resulting metric (e.g. average delivery time in
    seconds). The search minimizes `(objective(value) - target) ** 2` via
    golden-section search, and returns the best value actually evaluated.

    Raises `CalibrationError` if the bounds are invalid.
    """
    if lower_bound >= upper_bound:
        raise CalibrationError(f"lower_bound ({lower_bound}) must be < upper_bound ({upper_bound})")
    if tolerance <= 0:
        raise CalibrationError("tolerance must be > 0")

    history: list[tuple[float, float]] = []

    def squared_error(value: float) -> float:
        metric = objective(value)
        history.append((value, metric))
        return (metric - target) ** 2

    a, b = lower_bound, upper_bound
    c = b - _GOLDEN_RATIO * (b - a)
    d = a + _GOLDEN_RATIO * (b - a)
    fc = squared_error(c)
    fd = squared_error(d)

    iterations = 0
    while abs(b - a) > tolerance and iterations < max_iterations:
        if fc < fd:
            b, d, fd = d, c, fc
            c = b - _GOLDEN_RATIO * (b - a)
            fc = squared_error(c)
        else:
            a, c, fc = c, d, fd
            d = a + _GOLDEN_RATIO * (b - a)
            fd = squared_error(d)
        iterations += 1

    best_value, best_metric = min(history, key=lambda pair: (pair[1] - target) ** 2)
    return CalibrationResult(
        parameter_name=parameter_name,
        calibrated_value=best_value,
        target=target,
        achieved=best_metric,
        iterations=iterations,
        history=tuple(history),
    )
