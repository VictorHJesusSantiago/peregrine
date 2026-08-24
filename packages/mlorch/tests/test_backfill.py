from pathlib import Path

import pandas as pd

from mlorch.backfill import run_backfill
from mlorch.versioning import ContentStore


class TestIncrementalBackfill:
    def test_first_run_computes_every_partition(self, tmp_path: Path) -> None:
        store = ContentStore(tmp_path / "store")
        calls: list[str] = []

        def compute(partition: str) -> pd.DataFrame:
            calls.append(partition)
            return pd.DataFrame({"day": [partition], "value": [1]})

        result = run_backfill(["2024-01-01", "2024-01-02", "2024-01-03"], compute, store, "sales")

        assert result.computed == ("2024-01-01", "2024-01-02", "2024-01-03")
        assert result.skipped == ()
        assert calls == ["2024-01-01", "2024-01-02", "2024-01-03"]

    def test_second_run_skips_already_computed_partitions(self, tmp_path: Path) -> None:
        store = ContentStore(tmp_path / "store")

        def compute(partition: str) -> pd.DataFrame:
            return pd.DataFrame({"day": [partition], "value": [1]})

        run_backfill(["2024-01-01", "2024-01-02"], compute, store, "sales")

        calls: list[str] = []

        def compute_and_track(partition: str) -> pd.DataFrame:
            calls.append(partition)
            return pd.DataFrame({"day": [partition], "value": [2]})

        result = run_backfill(
            ["2024-01-01", "2024-01-02", "2024-01-03"], compute_and_track, store, "sales"
        )

        assert calls == ["2024-01-03"]  # only the genuinely new partition triggers compute
        assert result.computed == ("2024-01-03",)
        assert set(result.skipped) == {"2024-01-01", "2024-01-02"}

    def test_force_recomputes_every_partition_even_if_already_done(self, tmp_path: Path) -> None:
        store = ContentStore(tmp_path / "store")

        def compute(partition: str) -> pd.DataFrame:
            return pd.DataFrame({"day": [partition], "value": [1]})

        run_backfill(["2024-01-01"], compute, store, "sales")

        calls: list[str] = []

        def compute_and_track(partition: str) -> pd.DataFrame:
            calls.append(partition)
            return pd.DataFrame({"day": [partition], "value": [1]})

        result = run_backfill(["2024-01-01"], compute_and_track, store, "sales", force=True)

        assert calls == ["2024-01-01"]
        assert result.computed == ("2024-01-01",)
        assert result.skipped == ()

    def test_addresses_are_returned_for_both_computed_and_skipped_partitions(
        self, tmp_path: Path
    ) -> None:
        store = ContentStore(tmp_path / "store")

        def compute(partition: str) -> pd.DataFrame:
            return pd.DataFrame({"day": [partition], "value": [1]})

        run_backfill(["2024-01-01"], compute, store, "sales")
        result = run_backfill(["2024-01-01", "2024-01-02"], compute, store, "sales")

        assert "2024-01-01" in result.addresses
        assert "2024-01-02" in result.addresses
        assert result.addresses["2024-01-01"].startswith("sha256:")

    def test_backfilled_partitions_are_independently_retrievable_from_the_store(
        self, tmp_path: Path
    ) -> None:
        store = ContentStore(tmp_path / "store")

        def compute(partition: str) -> pd.DataFrame:
            return pd.DataFrame({"day": [partition], "value": [len(partition)]})

        result = run_backfill(["2024-01-01", "2024-01-02"], compute, store, "sales")
        df = store.get_dataframe(result.addresses["2024-01-02"])
        assert df["day"].iloc[0] == "2024-01-02"
