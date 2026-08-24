import numpy as np
import pytest

from mlorch.drift import check_drift, ks_statistic, population_stability_index


@pytest.fixture
def rng() -> np.random.Generator:
    return np.random.default_rng(seed=1234)


class TestPopulationStabilityIndex:
    def test_identical_distributions_have_near_zero_psi(self, rng: np.random.Generator) -> None:
        reference = rng.normal(loc=0, scale=1, size=5000)
        current = rng.normal(loc=0, scale=1, size=5000)
        psi = population_stability_index(reference, current)
        assert psi < 0.1

    def test_a_large_mean_shift_produces_a_large_psi(self, rng: np.random.Generator) -> None:
        reference = rng.normal(loc=0, scale=1, size=5000)
        current = rng.normal(loc=5, scale=1, size=5000)
        psi = population_stability_index(reference, current)
        assert psi > 0.25

    def test_psi_is_zero_for_a_perfectly_constant_reference(self) -> None:
        reference = np.full(100, 7.0)
        current = np.full(100, 7.0)
        assert population_stability_index(reference, current) == 0.0

    def test_psi_requires_non_empty_samples(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            population_stability_index(np.array([]), np.array([1.0]))


class TestKSStatistic:
    def test_identical_distributions_have_a_large_p_value_most_of_the_time(
        self, rng: np.random.Generator
    ) -> None:
        reference = rng.normal(loc=0, scale=1, size=2000)
        current = rng.normal(loc=0, scale=1, size=2000)
        d, p_value = ks_statistic(reference, current)
        assert 0 <= d <= 1
        assert p_value > 0.05

    def test_clearly_different_distributions_have_a_small_p_value(self, rng: np.random.Generator) -> None:
        reference = rng.normal(loc=0, scale=1, size=2000)
        current = rng.normal(loc=4, scale=1, size=2000)
        d, p_value = ks_statistic(reference, current)
        assert d > 0.5
        assert p_value < 0.01

    def test_identical_samples_have_zero_statistic_and_p_value_one(self) -> None:
        sample = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
        d, p_value = ks_statistic(sample, sample.copy())
        assert d == 0.0
        assert p_value == 1.0

    def test_ks_requires_non_empty_samples(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            ks_statistic(np.array([]), np.array([1.0]))


class TestCheckDrift:
    def test_psi_method_flags_drift_on_a_shifted_distribution(self, rng: np.random.Generator) -> None:
        reference = rng.normal(loc=0, scale=1, size=5000)
        current = rng.normal(loc=6, scale=1, size=5000)
        report = check_drift(reference, current, method="psi")
        assert report.drifted is True
        assert report.statistic_name == "psi"

    def test_psi_method_does_not_flag_drift_on_matching_distributions(
        self, rng: np.random.Generator
    ) -> None:
        reference = rng.normal(loc=0, scale=1, size=5000)
        current = rng.normal(loc=0, scale=1, size=5000)
        report = check_drift(reference, current, method="psi")
        assert report.drifted is False

    def test_ks_method_flags_drift_on_a_shifted_distribution(self, rng: np.random.Generator) -> None:
        reference = rng.normal(loc=0, scale=1, size=2000)
        current = rng.uniform(low=10, high=20, size=2000)
        report = check_drift(reference, current, method="ks")
        assert report.drifted is True
        assert report.statistic_name == "ks_pvalue"

    def test_ks_method_does_not_flag_drift_on_matching_distributions(
        self, rng: np.random.Generator
    ) -> None:
        reference = rng.normal(loc=0, scale=1, size=2000)
        current = rng.normal(loc=0, scale=1, size=2000)
        report = check_drift(reference, current, method="ks")
        assert report.drifted is False

    def test_custom_threshold_is_respected(self, rng: np.random.Generator) -> None:
        reference = rng.normal(loc=0, scale=1, size=5000)
        current = rng.normal(loc=0.5, scale=1, size=5000)
        lenient = check_drift(reference, current, method="psi", threshold=100.0)
        assert lenient.drifted is False
        assert lenient.threshold == 100.0
