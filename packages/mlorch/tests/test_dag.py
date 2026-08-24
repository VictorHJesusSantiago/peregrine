import pytest

from mlorch.dag import DAG, Task
from mlorch.errors import CycleError, DuplicateTaskError, TaskNotFoundError


def make_dag(**overrides: tuple[str, ...]) -> DAG:
    """Diamond DAG: a -> b, a -> c, b -> d, c -> d."""
    tasks = [
        Task("a", lambda: 1, overrides.get("a", ())),
        Task("b", lambda a: a + 1, overrides.get("b", ("a",))),
        Task("c", lambda a: a + 2, overrides.get("c", ("a",))),
        Task("d", lambda b, c: b + c, overrides.get("d", ("b", "c"))),
    ]
    return DAG("diamond", tasks)


class TestDAGConstruction:
    def test_builds_a_valid_dag(self) -> None:
        dag = make_dag()
        assert set(dag.task_names) == {"a", "b", "c", "d"}

    def test_duplicate_task_name_is_rejected(self) -> None:
        with pytest.raises(DuplicateTaskError):
            DAG("dup", [Task("x", lambda: 1), Task("x", lambda: 2)])

    def test_unknown_dependency_is_rejected(self) -> None:
        with pytest.raises(TaskNotFoundError):
            DAG("bad", [Task("x", lambda: 1, ("ghost",))])

    def test_task_lookup_of_unknown_name_raises(self) -> None:
        dag = make_dag()
        with pytest.raises(TaskNotFoundError):
            dag.task("nope")


class TestCycleDetection:
    def test_direct_two_cycle_is_rejected(self) -> None:
        with pytest.raises(CycleError):
            DAG(
                "cycle",
                [
                    Task("x", lambda y: y, ("y",)),
                    Task("y", lambda x: x, ("x",)),
                ],
            )

    def test_longer_cycle_is_rejected(self) -> None:
        with pytest.raises(CycleError):
            DAG(
                "cycle3",
                [
                    Task("x", lambda z: z, ("z",)),
                    Task("y", lambda x: x, ("x",)),
                    Task("z", lambda y: y, ("y",)),
                ],
            )

    def test_self_loop_is_rejected(self) -> None:
        with pytest.raises(CycleError):
            DAG("selfloop", [Task("x", lambda x: x, ("x",))])


class TestTopologicalOrder:
    def test_upstream_tasks_precede_downstream_tasks(self) -> None:
        dag = make_dag()
        order = dag.topological_order()
        assert order.index("a") < order.index("b")
        assert order.index("a") < order.index("c")
        assert order.index("b") < order.index("d")
        assert order.index("c") < order.index("d")

    def test_independent_dag_has_a_valid_order_too(self) -> None:
        dag = DAG("indep", [Task("p", lambda: 1), Task("q", lambda: 2)])
        assert set(dag.topological_order()) == {"p", "q"}


class TestParallelGroups:
    def test_diamond_groups_b_and_c_together(self) -> None:
        dag = make_dag()
        groups = dag.parallel_groups()
        assert groups[0] == ["a"]
        assert set(groups[1]) == {"b", "c"}
        assert groups[2] == ["d"]

    def test_fully_independent_tasks_are_all_one_group(self) -> None:
        dag = DAG("indep", [Task("p", lambda: 1), Task("q", lambda: 2), Task("r", lambda: 3)])
        groups = dag.parallel_groups()
        assert len(groups) == 1
        assert set(groups[0]) == {"p", "q", "r"}

    def test_fully_linear_chain_is_all_singleton_groups(self) -> None:
        dag = DAG(
            "chain",
            [
                Task("a", lambda: 1),
                Task("b", lambda a: a, ("a",)),
                Task("c", lambda b: b, ("b",)),
            ],
        )
        assert dag.parallel_groups() == [["a"], ["b"], ["c"]]


class TestGraphNavigation:
    def test_downstream_of_root_includes_full_diamond(self) -> None:
        dag = make_dag()
        assert dag.all_downstream_of("a") == {"b", "c", "d"}

    def test_downstream_of_leaf_is_empty(self) -> None:
        dag = make_dag()
        assert dag.all_downstream_of("d") == set()

    def test_upstream_and_downstream_direct_neighbors(self) -> None:
        dag = make_dag()
        assert dag.upstream_of("d") == ("b", "c")
        assert dag.downstream_of("a") == ("b", "c")
