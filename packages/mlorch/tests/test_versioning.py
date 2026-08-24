from pathlib import Path

import pandas as pd
import pytest

from mlorch.errors import ContentStoreError
from mlorch.versioning import ContentStore, hash_dataframe


def make_store(tmp_path: Path) -> ContentStore:
    return ContentStore(tmp_path / "store")


class TestContentAddressing:
    def test_identical_content_hashes_to_the_same_address(self) -> None:
        df1 = pd.DataFrame({"a": [1, 2, 3], "b": ["x", "y", "z"]})
        df2 = pd.DataFrame({"a": [1, 2, 3], "b": ["x", "y", "z"]})
        assert hash_dataframe(df1) == hash_dataframe(df2)

    def test_different_content_hashes_differently(self) -> None:
        df1 = pd.DataFrame({"a": [1, 2, 3]})
        df2 = pd.DataFrame({"a": [1, 2, 4]})
        assert hash_dataframe(df1) != hash_dataframe(df2)

    def test_column_order_does_not_affect_the_address(self) -> None:
        df1 = pd.DataFrame({"a": [1, 2], "b": [3, 4]})
        df2 = pd.DataFrame({"b": [3, 4], "a": [1, 2]})
        assert hash_dataframe(df1) == hash_dataframe(df2)

    def test_row_order_does_affect_the_address(self) -> None:
        df1 = pd.DataFrame({"a": [1, 2]})
        df2 = pd.DataFrame({"a": [2, 1]})
        assert hash_dataframe(df1) != hash_dataframe(df2)


class TestContentStoreWriteAndRetrieve:
    def test_write_then_retrieve_round_trips_the_dataframe(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        df = pd.DataFrame({"a": [1, 2, 3], "b": [4.0, 5.0, 6.0]})
        version = store.put_dataframe("ds", df)
        fetched = store.get_dataframe(version.address)
        pd.testing.assert_frame_equal(fetched, df)

    def test_retrieving_an_unknown_address_raises(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        with pytest.raises(ContentStoreError):
            store.get_dataframe("sha256:doesnotexist")

    def test_history_of_an_unknown_name_raises(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        with pytest.raises(ContentStoreError):
            store.history("ghost")


class TestDeduplication:
    def test_writing_identical_content_twice_reuses_the_same_object_on_disk(
        self, tmp_path: Path
    ) -> None:
        store = make_store(tmp_path)
        df = pd.DataFrame({"a": [1, 2, 3]})
        v1 = store.put_dataframe("ds", df)
        v2 = store.put_dataframe("ds", df.copy())
        assert v1.address == v2.address
        objects = list((tmp_path / "store" / "objects").iterdir())
        assert len(objects) == 1

    def test_writing_identical_content_twice_does_not_duplicate_history(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        df = pd.DataFrame({"a": [1, 2, 3]})
        store.put_dataframe("ds", df)
        store.put_dataframe("ds", df.copy())
        assert len(store.history("ds")) == 1

    def test_identical_content_under_different_names_shares_one_object(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        df = pd.DataFrame({"a": [1, 2, 3]})
        v1 = store.put_dataframe("ds-one", df)
        v2 = store.put_dataframe("ds-two", df.copy())
        assert v1.address == v2.address
        objects = list((tmp_path / "store" / "objects").iterdir())
        assert len(objects) == 1


class TestVersionHistory:
    def test_history_grows_with_each_distinct_write(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        store.put_dataframe("ds", pd.DataFrame({"a": [1]}))
        store.put_dataframe("ds", pd.DataFrame({"a": [1, 2]}))
        store.put_dataframe("ds", pd.DataFrame({"a": [1, 2, 3]}))
        history = store.history("ds")
        assert len(history) == 3
        assert [v.row_count for v in history] == [1, 2, 3]

    def test_latest_returns_the_most_recent_version(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        store.put_dataframe("ds", pd.DataFrame({"a": [1]}))
        v2 = store.put_dataframe("ds", pd.DataFrame({"a": [1, 2]}))
        assert store.latest("ds").address == v2.address

    def test_names_lists_every_logical_dataset_stored(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        store.put_dataframe("ds-a", pd.DataFrame({"a": [1]}))
        store.put_dataframe("ds-b", pd.DataFrame({"a": [1]}))
        assert set(store.names()) == {"ds-a", "ds-b"}

    def test_store_state_persists_across_new_instances_pointed_at_the_same_root(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "store"
        store1 = ContentStore(root)
        version = store1.put_dataframe("ds", pd.DataFrame({"a": [1, 2]}))

        store2 = ContentStore(root)
        assert store2.latest("ds").address == version.address
        pd.testing.assert_frame_equal(store2.get_dataframe(version.address), pd.DataFrame({"a": [1, 2]}))
