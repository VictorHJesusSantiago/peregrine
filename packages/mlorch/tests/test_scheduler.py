import threading
import time

import pytest

from mlorch.dag import DAG, Task, TaskStatus
from mlorch.errors import TaskExecutionError
from mlorch.executors import ExecutionOutcome, LocalExecutor
from mlorch.scheduler import Scheduler


class TestBasicExecution:
    def test_all_tasks_succeed_in_a_simple_chain(self) -> None:
        dag = DAG(
            "chain",
            [
                Task("a", lambda: 1),
                Task("b", lambda a: a + 1, ("a",)),
                Task("c", lambda b: b * 10, ("b",)),
            ],
        )
        report = Scheduler(LocalExecutor()).run(dag)
        assert report.all_succeeded
        assert report.result_of("a").value == 1
        assert report.result_of("b").value == 2
        assert report.result_of("c").value == 20

    def test_upstream_output_is_passed_by_task_name_kwarg(self) -> None:
        dag = DAG(
            "merge",
            [
                Task("left", lambda: 3),
                Task("right", lambda: 4),
                Task("sum", lambda left, right: left + right, ("left", "right")),
            ],
        )
        report = Scheduler(LocalExecutor()).run(dag)
        assert report.result_of("sum").value == 7


class TestParallelExecution:
    def test_independent_branches_actually_overlap_in_wall_clock_time(self) -> None:
        # Two independent, sleeping tasks: if they ran sequentially this would take >= 0.4s;
        # if genuinely parallel (thread pool), it should take much closer to 0.2s.
        def slow(tag: str) -> str:
            time.sleep(0.2)
            return tag

        dag = DAG(
            "parallel",
            [
                Task("x", lambda: slow("x")),
                Task("y", lambda: slow("y")),
            ],
        )
        start = time.monotonic()
        report = Scheduler(LocalExecutor()).run(dag)
        elapsed = time.monotonic() - start
        assert report.all_succeeded
        assert elapsed < 0.35

    def test_independent_tasks_run_on_different_threads_concurrently(self) -> None:
        barrier = threading.Barrier(2, timeout=2)

        def touch_barrier() -> int:
            barrier.wait()
            return 1

        dag = DAG("barrier", [Task("p", touch_barrier), Task("q", touch_barrier)])
        report = Scheduler(LocalExecutor()).run(dag)
        assert report.all_succeeded


class TestFailurePropagation:
    def test_failure_blocks_only_its_own_downstream(self) -> None:
        # a -> b (fails) -> d ;  a -> c -> d as well ;  and independent e with no relation.
        # b's failure must block d (it depends on b), but not c on its own, and not e at all.
        def boom() -> int:
            raise ValueError("kaboom")

        dag = DAG(
            "partial-failure",
            [
                Task("a", lambda: 1),
                Task("b", boom, ("a",)),
                Task("c", lambda a: a + 1, ("a",)),
                Task("d", lambda b, c: b + c, ("b", "c")),
                Task("e", lambda: "independent"),
            ],
        )
        report = Scheduler(LocalExecutor()).run(dag)

        assert report.status_of("a") is TaskStatus.SUCCEEDED
        assert report.status_of("b") is TaskStatus.FAILED
        assert report.status_of("c") is TaskStatus.SUCCEEDED
        assert report.status_of("d") is TaskStatus.SKIPPED
        assert report.status_of("e") is TaskStatus.SUCCEEDED
        assert not report.all_succeeded
        assert report.failed() == ("b",)
        assert report.skipped() == ("d",)

    def test_failure_error_is_captured_on_the_task_result(self) -> None:
        def boom() -> int:
            raise ValueError("kaboom")

        dag = DAG("fail", [Task("a", boom)])
        report = Scheduler(LocalExecutor()).run(dag)
        error = report.result_of("a").error
        assert isinstance(error, ValueError)
        assert "kaboom" in str(error)

    def test_raise_if_failed_wraps_the_first_failure(self) -> None:
        def boom() -> int:
            raise ValueError("kaboom")

        dag = DAG("fail", [Task("a", boom)])
        report = Scheduler(LocalExecutor()).run(dag)
        with pytest.raises(TaskExecutionError):
            report.raise_if_failed()

    def test_deep_transitive_failure_blocks_the_entire_downstream_chain(self) -> None:
        def boom() -> int:
            raise ValueError("kaboom")

        dag = DAG(
            "deep",
            [
                Task("a", boom),
                Task("b", lambda a: a, ("a",)),
                Task("c", lambda b: b, ("b",)),
                Task("d", lambda c: c, ("c",)),
            ],
        )
        report = Scheduler(LocalExecutor()).run(dag)
        assert report.status_of("a") is TaskStatus.FAILED
        assert report.skipped() == ("b", "c", "d") or set(report.skipped()) == {"b", "c", "d"}


class TestRetries:
    def test_a_task_that_succeeds_on_retry_is_reported_as_succeeded(self) -> None:
        attempts = {"count": 0}

        def flaky() -> str:
            attempts["count"] += 1
            if attempts["count"] < 2:
                raise RuntimeError("not yet")
            return "ok"

        dag = DAG("flaky", [Task("f", flaky)])
        report = Scheduler(LocalExecutor(), max_retries=2).run(dag)
        assert report.status_of("f") is TaskStatus.SUCCEEDED
        assert report.result_of("f").value == "ok"
        assert report.result_of("f").attempts == 2

    def test_a_task_that_never_succeeds_is_failed_after_exhausting_retries(self) -> None:
        attempts = {"count": 0}

        def always_fails() -> None:
            attempts["count"] += 1
            raise RuntimeError("nope")

        dag = DAG("always-fails", [Task("f", always_fails)])
        report = Scheduler(LocalExecutor(), max_retries=2).run(dag)
        assert report.status_of("f") is TaskStatus.FAILED
        assert attempts["count"] == 3  # 1 initial attempt + 2 retries


class TestRunReportInspection:
    def test_pending_tasks_are_reported_before_a_run(self) -> None:
        dag = DAG("chain", [Task("a", lambda: 1), Task("b", lambda a: a, ("a",))])
        from mlorch.scheduler import RunReport

        report = RunReport(dag)
        assert set(report.pending()) == {"a", "b"}
        assert report.succeeded() == ()


class TestExecutorOutcomeShape:
    def test_local_executor_returns_one_outcome_per_task_and_never_raises(self) -> None:
        def boom() -> None:
            raise KeyError("x")

        dag = DAG("mixed", [Task("ok", lambda: 1), Task("bad", boom)])
        outcomes = LocalExecutor().run_batch([(dag.task("ok"), {}), (dag.task("bad"), {})])
        assert set(outcomes) == {"ok", "bad"}
        assert isinstance(outcomes["ok"], ExecutionOutcome)
        assert outcomes["ok"].ok is True
        assert outcomes["bad"].ok is False
        assert isinstance(outcomes["bad"].error, KeyError)
