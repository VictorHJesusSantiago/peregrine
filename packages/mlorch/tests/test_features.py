import pandas as pd
import pytest

from mlorch.errors import FeatureStoreError
from mlorch.features import FeatureDefinition, OfflineFeatureStore, OnlineFeatureStore, materialize


def raw_data() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "user_id": [1, 1, 2, 2, 3],
            "timestamp": [1, 2, 1, 2, 1],
            "amount": [10.0, 20.0, 5.0, 7.0, 100.0],
        }
    )


def feature_defs() -> list[FeatureDefinition]:
    return [
        FeatureDefinition("amount", "user_id", float, lambda df: df["amount"]),
        FeatureDefinition("amount_x2", "user_id", float, lambda df: df["amount"] * 2),
    ]


class TestOfflineFeatureStore:
    def test_write_produces_one_row_per_input_row(self) -> None:
        store = OfflineFeatureStore()
        out = store.write("txn_features", raw_data(), feature_defs())
        assert len(out) == 5
        assert list(out.columns) == ["entity", "timestamp", "amount", "amount_x2"]

    def test_write_computes_features_correctly(self) -> None:
        store = OfflineFeatureStore()
        out = store.write("txn_features", raw_data(), feature_defs())
        assert out["amount_x2"].tolist() == [20.0, 40.0, 10.0, 14.0, 200.0]

    def test_write_requires_at_least_one_feature(self) -> None:
        store = OfflineFeatureStore()
        with pytest.raises(FeatureStoreError):
            store.write("v", raw_data(), [])

    def test_write_requires_the_timestamp_column(self) -> None:
        store = OfflineFeatureStore()
        bad = raw_data().drop(columns=["timestamp"])
        with pytest.raises(FeatureStoreError):
            store.write("v", bad, feature_defs())

    def test_reading_an_unwritten_view_raises(self) -> None:
        store = OfflineFeatureStore()
        with pytest.raises(FeatureStoreError):
            store.read("ghost")

    def test_successive_writes_append_to_history(self) -> None:
        store = OfflineFeatureStore()
        store.write("v", raw_data(), feature_defs())
        more = pd.DataFrame({"user_id": [1], "timestamp": [3], "amount": [999.0]})
        store.write("v", more, feature_defs())
        assert len(store.read("v")) == 6

    def test_latest_per_entity_picks_the_max_timestamp_row(self) -> None:
        store = OfflineFeatureStore()
        store.write("v", raw_data(), feature_defs())
        latest = store.latest_per_entity("v")
        by_entity = latest.set_index("entity")
        assert by_entity.loc[1, "amount"] == 20.0  # user 1's timestamp=2 row
        assert by_entity.loc[2, "amount"] == 7.0  # user 2's timestamp=2 row
        assert by_entity.loc[3, "amount"] == 100.0  # user 3's only row


class TestOnlineFeatureStore:
    def test_get_after_set_many_returns_the_value(self) -> None:
        store = OnlineFeatureStore()
        store.set_many(42, {"amount": 1.0, "amount_x2": 2.0})
        assert store.get(42, "amount") == 1.0
        assert store.get(42, "amount_x2") == 2.0

    def test_get_all_returns_every_feature_for_an_entity(self) -> None:
        store = OnlineFeatureStore()
        store.set_many(1, {"a": 1, "b": 2})
        assert store.get_all(1) == {"a": 1, "b": 2}

    def test_set_many_merges_rather_than_replaces(self) -> None:
        store = OnlineFeatureStore()
        store.set_many(1, {"a": 1})
        store.set_many(1, {"b": 2})
        assert store.get_all(1) == {"a": 1, "b": 2}

    def test_get_on_missing_entity_raises(self) -> None:
        store = OnlineFeatureStore()
        with pytest.raises(FeatureStoreError):
            store.get("ghost", "a")

    def test_get_all_on_missing_entity_returns_empty_dict(self) -> None:
        store = OnlineFeatureStore()
        assert store.get_all("ghost") == {}


class TestMaterialization:
    def test_materialize_pushes_latest_offline_values_into_online_store(self) -> None:
        offline = OfflineFeatureStore()
        online = OnlineFeatureStore()
        offline.write("v", raw_data(), feature_defs())

        count = materialize(offline, online, "v", ["amount", "amount_x2"])

        assert count == 3  # three distinct entities: users 1, 2, 3
        assert online.get(1, "amount") == 20.0  # latest (timestamp=2) value for user 1
        assert online.get(2, "amount") == 7.0
        assert online.get(3, "amount") == 100.0

    def test_materialize_overwrites_stale_online_values_with_newer_offline_batch(self) -> None:
        offline = OfflineFeatureStore()
        online = OnlineFeatureStore()
        offline.write("v", raw_data(), feature_defs())
        materialize(offline, online, "v", ["amount"])
        assert online.get(1, "amount") == 20.0

        newer = pd.DataFrame({"user_id": [1], "timestamp": [3], "amount": [555.0]})
        offline.write("v", newer, feature_defs())
        materialize(offline, online, "v", ["amount"])
        assert online.get(1, "amount") == 555.0
