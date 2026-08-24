"""Constant-acceleration kinematic model for an agent traversing one road segment.

Genuine physics, not teleportation: an agent starts a segment at rest,
accelerates at a constant rate up to a capped cruise speed, cruises, then
decelerates back to rest by the far end -- modelling every intersection as a
stop-and-go point. If the segment is too short for the agent to ever reach
cruise speed, the profile degrades gracefully to a triangular (accelerate,
immediately decelerate) shape instead of a trapezoidal one.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import sqrt


@dataclass(frozen=True, slots=True)
class SegmentProfile:
    """The kinematic profile of one agent traversing one road segment.

    Parameters
    ----------
    length:
        Segment length in meters.
    max_speed:
        The cruise speed cap in m/s (already the minimum of the road's
        speed limit and the agent's own top speed -- this class doesn't
        know about roads or agents, only numbers).
    acceleration:
        Constant magnitude of acceleration and deceleration, in m/s^2.
    """

    length: float
    max_speed: float
    acceleration: float

    def __post_init__(self) -> None:
        if self.length < 0:
            raise ValueError(f"segment length must be >= 0, got {self.length}")
        if self.max_speed <= 0:
            raise ValueError(f"max_speed must be > 0, got {self.max_speed}")
        if self.acceleration <= 0:
            raise ValueError(f"acceleration must be > 0, got {self.acceleration}")

    @property
    def _accel_distance_to_max_speed(self) -> float:
        """Distance covered while accelerating from rest to `max_speed` (= decelerating back)."""
        return self.max_speed**2 / (2 * self.acceleration)

    @property
    def is_trapezoidal(self) -> bool:
        """Whether the segment is long enough to actually reach `max_speed` before decelerating."""
        return 2 * self._accel_distance_to_max_speed <= self.length

    @property
    def peak_speed(self) -> float:
        """The highest speed actually reached (== max_speed unless the segment is too short)."""
        if self.length == 0:
            return 0.0
        if self.is_trapezoidal:
            return self.max_speed
        return sqrt(self.acceleration * self.length)

    def duration(self) -> float:
        """Total time to traverse the segment, start (at rest) to end (back at rest)."""
        if self.length == 0:
            return 0.0
        if self.is_trapezoidal:
            accel_time = self.max_speed / self.acceleration
            cruise_distance = self.length - 2 * self._accel_distance_to_max_speed
            cruise_time = cruise_distance / self.max_speed
            return 2 * accel_time + cruise_time
        accel_time = self.peak_speed / self.acceleration
        return 2 * accel_time

    def distance_at(self, t: float) -> float:
        """Distance traveled along the segment at elapsed time `t` since departure.

        Clamped to `[0, length]` so callers may safely query `t` outside
        `[0, duration()]`.
        """
        if self.length == 0 or t <= 0:
            return 0.0
        total = self.duration()
        if t >= total:
            return self.length

        a = self.acceleration
        if self.is_trapezoidal:
            accel_time = self.max_speed / a
            cruise_distance = self.length - 2 * self._accel_distance_to_max_speed
            cruise_time = cruise_distance / self.max_speed
            if t <= accel_time:
                return 0.5 * a * t**2
            if t <= accel_time + cruise_time:
                return self._accel_distance_to_max_speed + self.max_speed * (t - accel_time)
            t_decel = t - accel_time - cruise_time
            return (
                self._accel_distance_to_max_speed
                + cruise_distance
                + self.max_speed * t_decel
                - 0.5 * a * t_decel**2
            )

        peak = self.peak_speed
        accel_time = peak / a
        if t <= accel_time:
            return 0.5 * a * t**2
        t_decel = t - accel_time
        return 0.5 * a * accel_time**2 + peak * t_decel - 0.5 * a * t_decel**2

    def speed_at(self, t: float) -> float:
        """Instantaneous speed at elapsed time `t` since departure (clamped to the segment span)."""
        if self.length == 0:
            return 0.0
        total = self.duration()
        t = min(max(t, 0.0), total)
        a = self.acceleration
        if self.is_trapezoidal:
            accel_time = self.max_speed / a
            cruise_distance = self.length - 2 * self._accel_distance_to_max_speed
            cruise_time = cruise_distance / self.max_speed
            if t <= accel_time:
                return a * t
            if t <= accel_time + cruise_time:
                return self.max_speed
            return max(0.0, self.max_speed - a * (t - accel_time - cruise_time))

        peak = self.peak_speed
        accel_time = peak / a
        if t <= accel_time:
            return a * t
        return max(0.0, peak - a * (t - accel_time))
