from math import sqrt

import pytest

from digitaltwin.kinematics import SegmentProfile


class TestTrapezoidalProfile:
    """length=100m, max_speed=10 m/s, acceleration=2 m/s^2.

    Hand-computed: accel distance to reach 10 m/s at 2 m/s^2 is v^2/2a = 25m,
    so 2*25=50m <= 100m -> trapezoidal. accel_time = v/a = 5s (same for decel).
    cruise_distance = 100 - 50 = 50m, cruise_time = 50/10 = 5s.
    total duration = 5 + 5 + 5 = 15s.
    """

    def setup_method(self) -> None:
        self.profile = SegmentProfile(length=100.0, max_speed=10.0, acceleration=2.0)

    def test_is_trapezoidal(self) -> None:
        assert self.profile.is_trapezoidal
        assert self.profile.peak_speed == pytest.approx(10.0)

    def test_duration(self) -> None:
        assert self.profile.duration() == pytest.approx(15.0)

    def test_distance_at_end_of_acceleration_phase(self) -> None:
        # x(5) = 0.5 * 2 * 5^2 = 25
        assert self.profile.distance_at(5.0) == pytest.approx(25.0)

    def test_distance_at_end_of_cruise_phase(self) -> None:
        # x(10) = 25 + 10 * (10 - 5) = 75
        assert self.profile.distance_at(10.0) == pytest.approx(75.0)

    def test_distance_at_full_duration(self) -> None:
        assert self.profile.distance_at(15.0) == pytest.approx(100.0)

    def test_distance_clamped_before_start_and_after_end(self) -> None:
        assert self.profile.distance_at(-5.0) == 0.0
        assert self.profile.distance_at(1000.0) == pytest.approx(100.0)

    def test_speed_profile(self) -> None:
        assert self.profile.speed_at(0.0) == pytest.approx(0.0)
        assert self.profile.speed_at(5.0) == pytest.approx(10.0)  # just reached cruise speed
        assert self.profile.speed_at(7.5) == pytest.approx(10.0)  # mid-cruise
        assert self.profile.speed_at(15.0) == pytest.approx(0.0)  # fully stopped


class TestTriangularProfile:
    """length=9m, max_speed=10 m/s, acceleration=2 m/s^2.

    Hand-computed: 2*25=50m > 9m, so the segment is too short to reach
    max_speed -- triangular profile. peak_speed = sqrt(a*L) = sqrt(18).
    accel_time = peak/a = sqrt(18)/2. duration = 2*accel_time = sqrt(18).
    At the midpoint in time, distance should be exactly length/2 = 4.5m
    (symmetric accelerate-then-decelerate).
    """

    def setup_method(self) -> None:
        self.profile = SegmentProfile(length=9.0, max_speed=10.0, acceleration=2.0)

    def test_is_triangular(self) -> None:
        assert not self.profile.is_trapezoidal
        assert self.profile.peak_speed == pytest.approx(sqrt(18.0))

    def test_duration(self) -> None:
        assert self.profile.duration() == pytest.approx(sqrt(18.0))

    def test_distance_at_peak_is_half_length(self) -> None:
        peak_time = self.profile.peak_speed / self.profile.acceleration
        assert self.profile.distance_at(peak_time) == pytest.approx(4.5)

    def test_distance_at_full_duration(self) -> None:
        assert self.profile.distance_at(self.profile.duration()) == pytest.approx(9.0)


class TestValidation:
    def test_negative_length_rejected(self) -> None:
        with pytest.raises(ValueError, match="length"):
            SegmentProfile(length=-1.0, max_speed=1.0, acceleration=1.0)

    def test_nonpositive_max_speed_rejected(self) -> None:
        with pytest.raises(ValueError, match="max_speed"):
            SegmentProfile(length=1.0, max_speed=0.0, acceleration=1.0)

    def test_nonpositive_acceleration_rejected(self) -> None:
        with pytest.raises(ValueError, match="acceleration"):
            SegmentProfile(length=1.0, max_speed=1.0, acceleration=0.0)


class TestZeroLengthSegment:
    def test_zero_length_is_instantaneous(self) -> None:
        profile = SegmentProfile(length=0.0, max_speed=5.0, acceleration=1.0)
        assert profile.duration() == 0.0
        assert profile.distance_at(0.0) == 0.0
        assert profile.distance_at(10.0) == 0.0
