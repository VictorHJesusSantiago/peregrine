"""End-to-end test wiring several mlorch subsystems together into one realistic pipeline:

    raw data --> version dataset (content store) --> compute offline features
                                                    --> materialize online features
    (dataset address + online features) --> "train" and register a model, with lineage

run through the DAG scheduler, using the real thread-pool LocalExecutor, so this also
exercises parallel-eligible scheduling (feature computation and dataset versioning both
depend only on the raw-data task, not on each other) and status inspection end to end.
"""

from pathlib import Path
from typing import Any

import pandas as pd

from mlorch.backfill import run_backfill
from mlorch.dag import DAG, Task, TaskStatus
from mlorch.executors import LocalExecutor
from mlorch.features import FeatureDefinition, OfflineFeatureStore, OnlineFeatureStore, materialize
from mlorch.registry import ModelRegistry, Stage
from mlorch.scheduler import Scheduler
from mlorch.versioning import ContentStore


class LinearModel:
    """Stand-in for a "trained" model: a picklable object with a coefficient fit from data."""

    def __init__(self, coefficient: float) -> None:
        self.coefficient = coefficient

    def predict(self, amount: float) -> float:
        return amount * self.coefficient


def build_pipeline(store: ContentStore, offline: OfflineFeatureStore, online: OnlineFeatureStore, registry: ModelRegistry) -> DAG:
    def load_raw() -> pd.DataFrame:
        return pd.DataFrame(
            {
                "user_id": [1, 1, 2, 2, 3],
                "timestamp": [1, 2, 1, 2, 1],
                "amount": [10.0, 20.0, 5.0, 7.0, 100.0],
            }
        )

    def version_dataset(load_raw: pd.DataFrame) -> str:
        version = store.put_dataframe("transactions", load_raw)
        return version.address

    def compute_features(load_raw: pd.DataFrame) -> str:
        view_name = "txn_features"
        offline.write(
            view_name,
            load_raw,
            [
                FeatureDefinition("amount", "user_id", float, lambda df: df["amount"]),
                FeatureDefinition("amount_x2", "user_id", float, lambda df: df["amount"] * 2),
            ],
        )
        return view_name

    def materialize_online(compute_features: str) -> int:
        return materialize(offline, online, compute_features, ["amount", "amount_x2"])

    def train_and_register(version_dataset: str, materialize_online: int) -> Any:
        # A deliberately trivial "training" step: fit a single coefficient as the mean ratio
        # of amount_x2 to amount across every entity now available in the online store, which
        # is exactly what a real training step would do -- read from the online/offline store,
        # not straight from raw data.
        ratios = [
            online.get(entity, "amount_x2") / online.get(entity, "amount")
            for entity in online.entities()
        ]
        coefficient = sum(ratios) / len(ratios)
        model = LinearModel(coefficient)
        return registry.register(
            "txn-multiplier",
            model,
            params={"n_entities": materialize_online},
            metrics={"mean_ratio": coefficient},
            dataset_addresses=(version_dataset,),
        )

    return DAG(
        "txn-pipeline",
        [
            Task("load_raw", load_raw),
            Task("version_dataset", version_dataset, ("load_raw",)),
            Task("compute_features", compute_features, ("load_raw",)),
            Task("materialize_online", materialize_online, ("compute_features",)),
            Task("train_and_register", train_and_register, ("version_dataset", "materialize_online")),
        ],
    )


class TestEndToEndPipeline:
    def test_full_pipeline_runs_and_wires_every_subsystem_together(self, tmp_path: Path) -> None:
        store = ContentStore(tmp_path / "store")
        offline = OfflineFeatureStore()
        online = OnlineFeatureStore()
        registry = ModelRegistry()

        dag = build_pipeline(store, offline, online, registry)
        report = Scheduler(LocalExecutor()).run(dag)

        assert report.all_succeeded, report.statuses()

        # DAG structure: dataset versioning and feature computation are parallel-eligible,
        # both depending only on load_raw -- this is what the scheduler should have grouped
        # together for concurrent execution.
        groups = dag.parallel_groups()
        assert groups[0] == ["load_raw"]
        assert set(groups[1]) == {"version_dataset", "compute_features"}

        # Content store actually has the versioned dataset.
        dataset_version = store.latest("transactions")
        assert dataset_version.row_count == 5

        # Online store reflects materialized offline features.
        assert online.get(1, "amount") == 20.0  # latest (timestamp=2) value for user 1
        assert online.get(3, "amount") == 100.0

        # Model registry has the trained model with dataset lineage recorded.
        meta = registry.get_metadata("txn-multiplier")
        assert meta.dataset_addresses == (dataset_version.address,)
        assert meta.metrics["mean_ratio"] == 2.0  # amount_x2 is always exactly 2x amount
        model = registry.get("txn-multiplier")
        assert model.predict(50.0) == 100.0

        # Promote it and confirm the registry reflects that too.
        registry.promote("txn-multiplier", meta.version, Stage.PRODUCTION)
        assert registry.get_by_stage("txn-multiplier", Stage.PRODUCTION).version == meta.version

    def test_pipeline_report_shows_all_tasks_succeeded_individually(self, tmp_path: Path) -> None:
        store = ContentStore(tmp_path / "store")
        offline = OfflineFeatureStore()
        online = OnlineFeatureStore()
        registry = ModelRegistry()
        dag = build_pipeline(store, offline, online, registry)

        report = Scheduler(LocalExecutor()).run(dag)

        for name in dag.task_names:
            assert report.status_of(name) is TaskStatus.SUCCEEDED

    def test_backfill_integrates_with_the_same_content_store_used_by_the_pipeline(
        self, tmp_path: Path
    ) -> None:
        store = ContentStore(tmp_path / "store")
        computed_partitions: list[str] = []

        def compute_daily_transactions(partition: str) -> pd.DataFrame:
            computed_partitions.append(partition)
            return pd.DataFrame({"day": [partition], "total": [42.0]})

        first = run_backfill(["2024-01-01", "2024-01-02"], compute_daily_transactions, store, "daily_txn")
        assert first.computed == ("2024-01-01", "2024-01-02")

        second = run_backfill(
            ["2024-01-01", "2024-01-02", "2024-01-03"], compute_daily_transactions, store, "daily_txn"
        )
        assert second.computed == ("2024-01-03",)
        assert computed_partitions == ["2024-01-01", "2024-01-02", "2024-01-03"]
