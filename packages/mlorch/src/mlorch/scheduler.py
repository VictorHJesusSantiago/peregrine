"""The task scheduler: the piece of mlorch that owns *policy*, not mechanism.

`DAG` (see `mlorch.dag`) knows structure: which tasks exist, what depends on what,
topological order, parallel-eligible groupings. `Executor` (see `mlorch.executors`) knows how
to actually invoke a callable. Neither knows anything about *when* to run a task, what to do
when one fails, or how to retry — that is this module's job, and it is hand-written scheduling
logic, not delegated to networkx or any external orchestration framework.

Policy implemented here:

- Tasks run in `DAG.parallel_groups()` order: level by level, so that every task's upstream
  dependencies have already completed (or been definitively skipped) before it is considered.
- A failed task blocks exactly its transitive downstream dependents — computed via
  `DAG.all_downstream_of` — and nothing else; unrelated branches keep running.
- Each task gets up to `max_retries` extra attempts after an initial failure, retried
  individually (a batch's other tasks are not held up waiting on one task's retries, since
  retries happen as a next round of the same batch containing only the tasks that still need
  it).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mlorch.dag import DAG, Task, TaskStatus
from mlorch.errors import TaskExecutionError
from mlorch.executors import ExecutionOutcome, Executor


@dataclass(frozen=True, slots=True)
class TaskResult:
    name: str
    status: TaskStatus
    value: Any = None
    error: BaseException | None = None
    attempts: int = 0


class RunReport:
    """Queryable record of one DAG run's outcome: which tasks succeeded, failed, were skipped
    because an upstream failure blocked them, or never got a chance to run at all."""

    def __init__(self, dag: DAG) -> None:
        self.dag = dag
        self._results: dict[str, TaskResult] = {
            t.name: TaskResult(t.name, TaskStatus.PENDING) for t in dag.tasks
        }

    def status_of(self, name: str) -> TaskStatus:
        return self._results[name].status

    def result_of(self, name: str) -> TaskResult:
        return self._results[name]

    def statuses(self) -> dict[str, TaskStatus]:
        return {name: r.status for name, r in self._results.items()}

    def succeeded(self) -> tuple[str, ...]:
        return self._names_with(TaskStatus.SUCCEEDED)

    def failed(self) -> tuple[str, ...]:
        return self._names_with(TaskStatus.FAILED)

    def skipped(self) -> tuple[str, ...]:
        return self._names_with(TaskStatus.SKIPPED)

    def pending(self) -> tuple[str, ...]:
        return self._names_with(TaskStatus.PENDING)

    def _names_with(self, status: TaskStatus) -> tuple[str, ...]:
        return tuple(n for n, r in self._results.items() if r.status is status)

    @property
    def all_succeeded(self) -> bool:
        return all(r.status is TaskStatus.SUCCEEDED for r in self._results.values())

    def raise_if_failed(self) -> None:
        """Convenience for callers that want run failures to surface as an exception rather
        than being inspected via `.failed()`."""
        failed = self.failed()
        if failed:
            first = self._results[failed[0]]
            raise TaskExecutionError(
                f"task {failed[0]!r} failed: {first.error!r} ({len(failed)} task(s) failed total)"
            ) from first.error

    def _set(self, result: TaskResult) -> None:
        self._results[result.name] = result


class Scheduler:
    """Runs a `DAG` to completion using a given `Executor`, applying this project's own
    scheduling policy on top."""

    def __init__(self, executor: Executor, *, max_retries: int = 0) -> None:
        self.executor = executor
        self.max_retries = max_retries

    def run(self, dag: DAG) -> RunReport:
        report = RunReport(dag)
        blocked: set[str] = set()

        for level in dag.parallel_groups():
            for name in level:
                if name in blocked:
                    report._set(TaskResult(name, TaskStatus.SKIPPED))

            runnable = [name for name in level if name not in blocked]
            if not runnable:
                continue

            batch: list[tuple[Task, dict[str, Any]]] = []
            for name in runnable:
                task = dag.task(name)
                kwargs = {upstream: report.result_of(upstream).value for upstream in task.depends_on}
                batch.append((task, kwargs))
                report._set(TaskResult(name, TaskStatus.RUNNING))

            outcomes = self._run_with_retries(batch)

            for name, (outcome, attempts) in outcomes.items():
                if outcome.ok:
                    report._set(TaskResult(name, TaskStatus.SUCCEEDED, value=outcome.value, attempts=attempts))
                else:
                    report._set(TaskResult(name, TaskStatus.FAILED, error=outcome.error, attempts=attempts))
                    blocked |= dag.all_downstream_of(name)

        return report

    def _run_with_retries(
        self, batch: list[tuple[Task, dict[str, Any]]]
    ) -> dict[str, tuple[ExecutionOutcome, int]]:
        """Run `batch`, retrying only the tasks that fail, up to `max_retries` extra attempts
        each. Tasks that already succeeded are not re-run alongside stragglers still retrying —
        each retry round's batch shrinks to exactly the tasks still failing."""
        remaining = batch
        attempts: dict[str, int] = {task.name: 0 for task, _ in batch}
        final: dict[str, tuple[ExecutionOutcome, int]] = {}

        while remaining:
            outcomes = self.executor.run_batch(remaining)
            next_round: list[tuple[Task, dict[str, Any]]] = []
            for task, kwargs in remaining:
                outcome = outcomes[task.name]
                attempts[task.name] += 1
                if outcome.ok or attempts[task.name] > self.max_retries:
                    final[task.name] = (outcome, attempts[task.name])
                else:
                    next_round.append((task, kwargs))
            remaining = next_round

        return final
