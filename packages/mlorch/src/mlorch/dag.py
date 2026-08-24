"""Task and DAG definitions.

A `Task` is a declarative unit of work: a name, a plain Python callable, and the names of
tasks it depends on. A `DAG` wires a collection of tasks into a dependency graph, validating
at construction time (not at run time) that every dependency actually exists and that the
graph is acyclic.

This module uses `networkx` for the graph *data structure* and its topological sort / cycle
detection — reimplementing those from scratch would just be reinventing well-known graph
algorithms, not adding anything to this project's story. What it deliberately does NOT do is
decide *when* tasks run, how failures propagate, or how retries happen — that scheduling
policy is mlorch's own logic and lives in `mlorch.scheduler`, which only asks this module for
structure (topological order, parallel-eligible groups, downstream closure).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any

import networkx as nx

from mlorch.errors import CycleError, DuplicateTaskError, TaskNotFoundError


@dataclass(frozen=True, slots=True)
class Task:
    """A single unit of work.

    `fn` is called with one keyword argument per upstream dependency, named after that
    upstream task, bound to whatever that upstream task returned. A task with no
    dependencies is called with no arguments at all. This gives downstream tasks a way to
    consume upstream output without a separate, parallel data-passing mechanism.
    """

    name: str
    fn: Callable[..., Any]
    depends_on: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("Task name must be non-empty")


class TaskStatus(Enum):
    PENDING = auto()
    RUNNING = auto()
    SUCCEEDED = auto()
    FAILED = auto()
    SKIPPED = auto()


class DAG:
    """An immutable-once-built collection of tasks wired by dependency edges.

    Validation happens eagerly in `__init__`: unknown dependencies and cycles are both
    construction-time errors, so a malformed pipeline never gets far enough to partially
    execute before failing.
    """

    def __init__(self, name: str, tasks: Iterable[Task]) -> None:
        self.name = name
        self._tasks: dict[str, Task] = {}
        self._graph = nx.DiGraph()

        for task in tasks:
            if task.name in self._tasks:
                raise DuplicateTaskError(f"task {task.name!r} is already defined in DAG {name!r}")
            self._tasks[task.name] = task
            self._graph.add_node(task.name)

        for task in self._tasks.values():
            for upstream in task.depends_on:
                if upstream not in self._tasks:
                    raise TaskNotFoundError(
                        f"task {task.name!r} depends on undefined task {upstream!r}"
                    )
                self._graph.add_edge(upstream, task.name)  # edge points upstream -> downstream

        if not nx.is_directed_acyclic_graph(self._graph):
            cycle = nx.find_cycle(self._graph)
            cycle_desc = " -> ".join(u for u, _ in cycle) + f" -> {cycle[0][0]}"
            raise CycleError(f"DAG {name!r} has a cycle: {cycle_desc}")

    def task(self, name: str) -> Task:
        try:
            return self._tasks[name]
        except KeyError:
            raise TaskNotFoundError(f"no task named {name!r} in DAG {self.name!r}") from None

    @property
    def tasks(self) -> tuple[Task, ...]:
        return tuple(self._tasks.values())

    @property
    def task_names(self) -> tuple[str, ...]:
        return tuple(self._tasks.keys())

    def topological_order(self) -> list[str]:
        result: list[str] = list(nx.topological_sort(self._graph))
        return result

    def upstream_of(self, name: str) -> tuple[str, ...]:
        return tuple(self._graph.predecessors(name))

    def downstream_of(self, name: str) -> tuple[str, ...]:
        return tuple(self._graph.successors(name))

    def all_downstream_of(self, name: str) -> set[str]:
        """Transitive closure of everything reachable downstream of `name`.

        Used by the scheduler to figure out exactly which tasks a failure must block —
        deliberately does not include `name` itself or unrelated branches.
        """
        result: set[str] = nx.descendants(self._graph, name)
        return result

    def parallel_groups(self) -> list[list[str]]:
        """Partition tasks into levels such that:

        - every task in level N depends only on tasks in levels < N, and
        - no two tasks in the same level have any dependency path between them (in either
          direction), so they are safe to execute concurrently.

        The level of a task is its longest-path distance from a root (no predecessors = level
        0, otherwise one more than the maximum level of its predecessors). Longest-path
        layering guarantees the no-same-level-edges property: any edge u -> v forces
        level(v) >= level(u) + 1, so u and v can never land in the same level, and by
        induction neither can any longer path between two same-level nodes.
        """
        levels: dict[str, int] = {}
        for n in nx.topological_sort(self._graph):
            preds = list(self._graph.predecessors(n))
            levels[n] = 0 if not preds else max(levels[p] for p in preds) + 1
        grouped: dict[int, list[str]] = {}
        for n, lvl in levels.items():
            grouped.setdefault(lvl, []).append(n)
        return [grouped[lvl] for lvl in sorted(grouped)]
