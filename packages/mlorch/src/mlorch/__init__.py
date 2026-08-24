"""mlorch: an ML pipeline orchestrator with a from-scratch DAG scheduler, content-addressed
dataset versioning, an online/offline feature store, a model registry, drift detection,
incremental backfill, and pluggable executors.

Submodules:

- `mlorch.dag` -- `Task` / `DAG`: dependency graph structure, cycle detection, topological and
  parallel-eligible ordering.
- `mlorch.executors` -- `Executor` protocol, `LocalExecutor` (thread-pool parallel), and
  `DockerExecutor` (real, container-per-task, requires a reachable Docker daemon).
- `mlorch.scheduler` -- `Scheduler` / `RunReport`: the run policy layered on top of a DAG and
  an executor -- ordering, retries, and downstream failure propagation.
- `mlorch.versioning` -- `ContentStore`: content-addressed, local, deduplicating dataset
  storage.
- `mlorch.features` -- `OfflineFeatureStore` / `OnlineFeatureStore` / `materialize`.
- `mlorch.registry` -- `ModelRegistry`: versioned model storage with lineage and stage
  promotion.
- `mlorch.drift` -- `population_stability_index`, `ks_statistic`, `check_drift`.
- `mlorch.backfill` -- `run_backfill`: incremental, partition-skipping recomputation.
"""

from __future__ import annotations

__version__ = "0.1.0"
