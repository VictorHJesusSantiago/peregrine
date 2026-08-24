"""Exception hierarchy for mlorch.

One narrow class per real failure category, all deriving from `MlorchError` so callers can
catch broadly when they don't care which subsystem failed, or narrowly when they do.
"""

from __future__ import annotations


class MlorchError(Exception):
    """Base class for every error raised by mlorch."""


class CycleError(MlorchError):
    """Raised when a DAG's declared dependencies form a cycle."""


class DuplicateTaskError(MlorchError):
    """Raised when two tasks in the same DAG declare the same name."""


class TaskNotFoundError(MlorchError):
    """Raised when a task name is referenced (as a dependency or a lookup) but not defined."""


class TaskExecutionError(MlorchError):
    """Raised when a scheduler run is asked to surface a task failure as an exception."""


class ExecutorError(MlorchError):
    """Raised for executor-level failures: an executor unable to run what it was asked to run.

    Never raised for an individual *task's* own exception (that is captured and reported via
    `ExecutionOutcome`) — this is reserved for the executor itself being unable to function,
    e.g. no reachable Docker daemon.
    """


class ContentStoreError(MlorchError):
    """Raised for content-addressed store failures: unknown address, unknown dataset name."""


class FeatureStoreError(MlorchError):
    """Raised for feature store failures: unknown feature view, missing entity/timestamp column."""


class ModelRegistryError(MlorchError):
    """Raised for model registry failures: unknown model name, unknown version, unknown stage."""


class BackfillError(MlorchError):
    """Raised for incremental backfill failures: invalid partition specification."""
