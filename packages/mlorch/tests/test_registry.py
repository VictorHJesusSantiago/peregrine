import pytest

from mlorch.errors import ModelRegistryError
from mlorch.registry import ModelRegistry, Stage


class DummyModel:
    def __init__(self, coef: float) -> None:
        self.coef = coef

    def predict(self, x: float) -> float:
        return x * self.coef


class TestRegisterAndRetrieve:
    def test_first_registration_is_version_one(self) -> None:
        reg = ModelRegistry()
        meta = reg.register("m", DummyModel(2.0))
        assert meta.version == 1
        assert meta.name == "m"

    def test_successive_registrations_auto_increment_version(self) -> None:
        reg = ModelRegistry()
        reg.register("m", DummyModel(1.0))
        v2 = reg.register("m", DummyModel(2.0))
        v3 = reg.register("m", DummyModel(3.0))
        assert v2.version == 2
        assert v3.version == 3

    def test_different_model_names_version_independently(self) -> None:
        reg = ModelRegistry()
        reg.register("a", DummyModel(1.0))
        first_b = reg.register("b", DummyModel(2.0))
        assert first_b.version == 1

    def test_get_retrieves_the_exact_object_registered(self) -> None:
        reg = ModelRegistry()
        model = DummyModel(9.0)
        reg.register("m", model)
        assert reg.get("m", 1) is model

    def test_get_with_no_version_returns_latest(self) -> None:
        reg = ModelRegistry()
        reg.register("m", DummyModel(1.0))
        latest = DummyModel(2.0)
        reg.register("m", latest)
        assert reg.get("m") is latest

    def test_get_unknown_name_raises(self) -> None:
        reg = ModelRegistry()
        with pytest.raises(ModelRegistryError):
            reg.get("ghost")

    def test_get_unknown_version_raises(self) -> None:
        reg = ModelRegistry()
        reg.register("m", DummyModel(1.0))
        with pytest.raises(ModelRegistryError):
            reg.get("m", 99)


class TestMetadataTracking:
    def test_params_metrics_and_lineage_are_recorded(self) -> None:
        reg = ModelRegistry()
        meta = reg.register(
            "m",
            DummyModel(1.0),
            params={"lr": 0.01, "epochs": 10},
            metrics={"rmse": 0.5},
            dataset_addresses=("sha256:abc", "sha256:def"),
        )
        assert meta.params == {"lr": 0.01, "epochs": 10}
        assert meta.metrics == {"rmse": 0.5}
        assert meta.dataset_addresses == ("sha256:abc", "sha256:def")
        assert meta.created_at > 0

    def test_history_lists_every_version_in_order(self) -> None:
        reg = ModelRegistry()
        reg.register("m", DummyModel(1.0))
        reg.register("m", DummyModel(2.0))
        history = reg.history("m")
        assert [m.version for m in history] == [1, 2]

    def test_history_of_unknown_name_is_empty_not_an_error(self) -> None:
        reg = ModelRegistry()
        assert reg.history("ghost") == ()

    def test_newly_registered_version_starts_at_stage_none(self) -> None:
        reg = ModelRegistry()
        meta = reg.register("m", DummyModel(1.0))
        assert meta.stage is Stage.NONE


class TestPromotion:
    def test_promote_to_staging(self) -> None:
        reg = ModelRegistry()
        reg.register("m", DummyModel(1.0))
        promoted = reg.promote("m", 1, Stage.STAGING)
        assert promoted.stage is Stage.STAGING
        assert reg.get_metadata("m", 1).stage is Stage.STAGING

    def test_promoting_to_production_demotes_the_previous_production_version(self) -> None:
        reg = ModelRegistry()
        reg.register("m", DummyModel(1.0))
        reg.register("m", DummyModel(2.0))
        reg.promote("m", 1, Stage.PRODUCTION)
        assert reg.get_metadata("m", 1).stage is Stage.PRODUCTION

        reg.promote("m", 2, Stage.PRODUCTION)
        assert reg.get_metadata("m", 2).stage is Stage.PRODUCTION
        assert reg.get_metadata("m", 1).stage is Stage.STAGING  # demoted, not left dangling

    def test_get_by_stage_finds_the_production_version(self) -> None:
        reg = ModelRegistry()
        reg.register("m", DummyModel(1.0))
        reg.register("m", DummyModel(2.0))
        reg.promote("m", 2, Stage.PRODUCTION)
        prod = reg.get_by_stage("m", Stage.PRODUCTION)
        assert prod.version == 2

    def test_get_by_stage_with_no_match_raises(self) -> None:
        reg = ModelRegistry()
        reg.register("m", DummyModel(1.0))
        with pytest.raises(ModelRegistryError):
            reg.get_by_stage("m", Stage.PRODUCTION)

    def test_promoting_an_unknown_version_raises(self) -> None:
        reg = ModelRegistry()
        reg.register("m", DummyModel(1.0))
        with pytest.raises(ModelRegistryError):
            reg.promote("m", 99, Stage.STAGING)
