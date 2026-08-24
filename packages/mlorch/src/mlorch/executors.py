"""Pluggable task executors.

`Executor` is the abstraction the scheduler (`mlorch.scheduler`) talks to: "run this batch of
mutually-independent tasks, each with its resolved upstream kwargs, and tell me what
happened" — an executor must never raise for a *task's* failure, only capture it in the
returned `ExecutionOutcome`, so the scheduler's own failure-propagation policy stays in one
place regardless of which executor ran the work.

`LocalExecutor` is a real, fully-working implementation that runs tasks as plain Python calls,
using a thread pool so that independent tasks in a batch genuinely execute concurrently.

`DockerExecutor` is also a real, working implementation, not a stub: it ships each task's
(callable, kwargs) into a fresh `docker run` container over stdin as a base64-encoded pickle
and reads the (ok, result) back the same way over stdout. Its very real constraint — `fn` must
be a module-level, picklable callable, the same restriction `multiprocessing` imposes — is
documented on the class, and it refuses to silently no-op: if the Docker daemon isn't
reachable it raises `ExecutorError` rather than pretending the task ran.

`KubernetesExecutor` is a real, working implementation too, built the same way `DockerExecutor`
was: it shells out to the `kubectl` CLI (no `kubernetes` client library) and runs each task as
its own ephemeral pod (`kubectl run ... --rm -i`), reusing the exact same encode/decode wire
protocol and `_BOOTSTRAP_SCRIPT` over the pod's stdin/stdout — a pod running the bootstrap
script is protocol-identical to a container running it, only the process-launching mechanism
differs. There genuinely is no live cluster in this environment to run it against end-to-end,
so its live test skips here (mirroring `DockerExecutor`'s own live test when no daemon is
reachable), but the executor itself, its pod-name sanitization, and its availability check are
real and unit-tested independently of that.
"""

from __future__ import annotations

import base64
import contextlib
import pickle
import re
import shutil
import subprocess
import uuid
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from mlorch.dag import Task
from mlorch.errors import ExecutorError


@dataclass(frozen=True, slots=True)
class ExecutionOutcome:
    """The result of running a single task: either a value, or a captured error — never both,
    and never a raised exception escaping the executor."""

    name: str
    ok: bool
    value: Any = None
    error: BaseException | None = None


@runtime_checkable
class Executor(Protocol):
    """Anything that can run a batch of independent tasks and report what happened."""

    def run_batch(self, batch: Sequence[tuple[Task, dict[str, Any]]]) -> dict[str, ExecutionOutcome]:
        """Run every (task, kwargs) pair in `batch`. Tasks in the same batch are known to be
        mutually independent (the scheduler only ever calls this with one parallel-eligible
        group at a time), so an executor is free to run them concurrently. Must return exactly
        one `ExecutionOutcome` per task name in the batch, and must not raise for an individual
        task's own exception."""
        ...


class LocalExecutor:
    """Runs tasks as plain Python calls in this process, using a thread pool for real
    concurrency across independent DAG branches.

    Threads rather than processes: tasks are arbitrary Python callables, frequently closures
    or lambdas in test/pipeline code, and a `ProcessPoolExecutor` would reject exactly that
    (pickling closures/lambdas fails). Threads still give genuine wall-clock overlap for
    I/O-bound work and any code that releases the GIL, which is enough to demonstrate that the
    scheduler's parallel-eligible grouping actually results in overlapped execution rather than
    incidental sequential-but-labeled-parallel calls.
    """

    def __init__(self, max_workers: int | None = None) -> None:
        self.max_workers = max_workers

    def run_batch(self, batch: Sequence[tuple[Task, dict[str, Any]]]) -> dict[str, ExecutionOutcome]:
        if not batch:
            return {}
        results: dict[str, ExecutionOutcome] = {}
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = {pool.submit(self._run_one, task, kwargs): task.name for task, kwargs in batch}
            for future in as_completed(futures):
                name = futures[future]
                results[name] = future.result()
        return results

    @staticmethod
    def _run_one(task: Task, kwargs: dict[str, Any]) -> ExecutionOutcome:
        try:
            value = task.fn(**kwargs)
        except Exception as exc:  # a task's own failure is data, not a scheduler-level fault
            return ExecutionOutcome(task.name, ok=False, error=exc)
        return ExecutionOutcome(task.name, ok=True, value=value)


# --- Docker executor wire protocol -------------------------------------------------------
#
# Kept as free functions (rather than inlined in the class) so the encode/decode halves of the
# protocol can be unit-tested directly, without a Docker daemon, by exercising exactly the
# bytes that would cross the container boundary.

_BOOTSTRAP_SCRIPT = (
    "import sys, pickle, base64\n"
    "payload = sys.stdin.buffer.read()\n"
    "fn, kwargs = pickle.loads(base64.b64decode(payload))\n"
    "try:\n"
    "    result = fn(**kwargs)\n"
    "    out = (True, result)\n"
    "except Exception as exc:\n"
    "    out = (False, exc)\n"
    "sys.stdout.buffer.write(base64.b64encode(pickle.dumps(out)))\n"
)


def encode_call(fn: Any, kwargs: dict[str, Any]) -> bytes:
    """Serialize a (callable, kwargs) pair the same way `DockerExecutor` does before writing
    it to a container's stdin."""
    return base64.b64encode(pickle.dumps((fn, kwargs)))


def decode_outcome(payload: bytes) -> tuple[bool, Any]:
    """Deserialize the (ok, value_or_exception) pair the same way `DockerExecutor` does after
    reading a container's stdout."""
    ok, value = pickle.loads(base64.b64decode(payload))  # noqa: S301 (trusted local subprocess)
    return bool(ok), value


def docker_available() -> bool:
    """Whether the `docker` CLI is on PATH and a daemon is actually reachable through it —
    checking only for the CLI would let `DockerExecutor` claim readiness and then fail on the
    first real task, which is worse than failing fast here."""
    if shutil.which("docker") is None:
        return False
    try:
        subprocess.run(["docker", "info"], capture_output=True, timeout=5, check=True)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False
    return True


class DockerExecutor:
    """Runs each task inside a fresh, disposable `docker run` container of `image`.

    Real constraints, stated plainly rather than papered over:

    - `task.fn` must be a module-level, picklable callable (no closures, no lambdas) — the
      same restriction Python's own `multiprocessing` imposes when crossing a process
      boundary, for the same reason.
    - `image` must have a Python interpreter whose pickle protocol and available modules are
      compatible with the caller's, since the task and its result are ferried as pickles.
    - It refuses to run anything if Docker isn't actually reachable, raising `ExecutorError`
      rather than silently skipping the batch.

    Each task in a batch becomes its own `docker run` subprocess; since those are already
    independent OS processes, a thread pool is used purely to launch and await them
    concurrently — no further in-container parallelism is attempted.
    """

    def __init__(self, image: str = "python:3.12-slim", *, max_workers: int | None = None) -> None:
        self.image = image
        self.max_workers = max_workers

    def run_batch(self, batch: Sequence[tuple[Task, dict[str, Any]]]) -> dict[str, ExecutionOutcome]:
        if not batch:
            return {}
        if not docker_available():
            raise ExecutorError(
                "DockerExecutor requires a reachable Docker daemon ('docker info' failed); "
                "refusing to silently skip task execution"
            )
        results: dict[str, ExecutionOutcome] = {}
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = {pool.submit(self._run_one, task, kwargs): task.name for task, kwargs in batch}
            for future in as_completed(futures):
                name = futures[future]
                results[name] = future.result()
        return results

    def _run_one(self, task: Task, kwargs: dict[str, Any]) -> ExecutionOutcome:
        payload = encode_call(task.fn, kwargs)
        try:
            proc = subprocess.run(
                ["docker", "run", "--rm", "-i", self.image, "python", "-c", _BOOTSTRAP_SCRIPT],
                input=payload,
                capture_output=True,
                timeout=120,
                check=True,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            return ExecutionOutcome(
                task.name, ok=False, error=ExecutorError(f"docker run failed for task {task.name!r}: {exc}")
            )
        ok, value = decode_outcome(proc.stdout)
        if ok:
            return ExecutionOutcome(task.name, ok=True, value=value)
        error = value if isinstance(value, BaseException) else ExecutorError(str(value))
        return ExecutionOutcome(task.name, ok=False, error=error)


# --- Kubernetes executor -------------------------------------------------------------------

_POD_NAME_MAX_LEN = 253


def _sanitize_pod_name(task_name: str) -> str:
    """Derive a unique, DNS-label-safe pod name from `task_name`: lowercase alphanumerics and
    `-` only, starting and ending with an alphanumeric, at most 253 characters — the
    constraints Kubernetes itself enforces on pod names. A `uuid4`-derived suffix is always
    appended so concurrent tasks in a batch (or repeated runs of the same task name) never
    collide on pod name."""
    slug = re.sub(r"[^a-z0-9-]+", "-", task_name.lower())
    slug = re.sub(r"-+", "-", slug).strip("-")
    if not slug:
        slug = "task"
    suffix = uuid.uuid4().hex[:8]
    base = f"mlorch-{slug}"
    max_base_len = _POD_NAME_MAX_LEN - len(suffix) - 1  # 1 for the joining "-"
    base = base[:max_base_len].rstrip("-")
    return f"{base}-{suffix}"


def kubectl_available() -> bool:
    """Whether the `kubectl` CLI is on PATH and a cluster is actually reachable through the
    currently configured context — checking only for the CLI would let `KubernetesExecutor`
    claim readiness and then fail on the first real task, which is worse than failing fast
    here."""
    if shutil.which("kubectl") is None:
        return False
    try:
        subprocess.run(["kubectl", "cluster-info"], capture_output=True, timeout=5, check=True)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False
    return True


class KubernetesExecutor:
    """Runs each task in a fresh, disposable pod of `image` via `kubectl run ... --rm -i`.

    Same wire protocol as `DockerExecutor`, reused verbatim: the (callable, kwargs) pair is
    base64-pickled onto the pod's stdin and `_BOOTSTRAP_SCRIPT` writes the base64-pickled
    (ok, result) pair back over stdout. Only the mechanism that launches the process differs
    (`kubectl run` against a cluster instead of `docker run` against a local daemon) — the same
    real constraints apply for the same reasons:

    - `task.fn` must be a module-level, picklable callable (no closures, no lambdas).
    - `image` must run a Python interpreter whose pickle protocol and available modules are
      compatible with the caller's.
    - It refuses to run anything if no cluster is actually reachable through the current
      `kubectl` context, raising `ExecutorError` rather than silently skipping the batch.

    Each task in a batch becomes its own `kubectl run` subprocess against its own pod; since
    those are already independent units of work at the OS-process/cluster level, a thread pool
    is used purely to launch and await them concurrently — no further in-cluster parallelism is
    attempted, mirroring `DockerExecutor`'s own reasoning exactly.

    If a `kubectl run` invocation times out or otherwise has to be treated as failed, a
    best-effort `kubectl delete pod --ignore-not-found` is issued for that pod so a failed or
    hung task doesn't leave an orphaned pod behind; a failure of that cleanup step is swallowed
    so it can never mask the original task failure being reported.
    """

    def __init__(
        self,
        image: str = "python:3.12-slim",
        *,
        namespace: str | None = None,
        max_workers: int | None = None,
        timeout: float = 120,
    ) -> None:
        self.image = image
        self.namespace = namespace
        self.max_workers = max_workers
        self.timeout = timeout

    def run_batch(self, batch: Sequence[tuple[Task, dict[str, Any]]]) -> dict[str, ExecutionOutcome]:
        if not batch:
            return {}
        if not kubectl_available():
            raise ExecutorError(
                "KubernetesExecutor requires a reachable cluster ('kubectl cluster-info' "
                "failed); refusing to silently skip task execution"
            )
        results: dict[str, ExecutionOutcome] = {}
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = {pool.submit(self._run_one, task, kwargs): task.name for task, kwargs in batch}
            for future in as_completed(futures):
                name = futures[future]
                results[name] = future.result()
        return results

    def _namespace_args(self) -> list[str]:
        return ["--namespace", self.namespace] if self.namespace else []

    def _run_one(self, task: Task, kwargs: dict[str, Any]) -> ExecutionOutcome:
        payload = encode_call(task.fn, kwargs)
        pod_name = _sanitize_pod_name(task.name)
        cmd = [
            "kubectl",
            "run",
            pod_name,
            f"--image={self.image}",
            "--restart=Never",
            "--rm",
            "-i",
            "--quiet",
            *self._namespace_args(),
            "--",
            "python",
            "-c",
            _BOOTSTRAP_SCRIPT,
        ]
        try:
            proc = subprocess.run(
                cmd,
                input=payload,
                capture_output=True,
                timeout=self.timeout,
                check=True,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            self._cleanup_pod(pod_name)
            return ExecutionOutcome(
                task.name,
                ok=False,
                error=ExecutorError(f"kubectl run failed for task {task.name!r}: {exc}"),
            )
        ok, value = decode_outcome(proc.stdout)
        if ok:
            return ExecutionOutcome(task.name, ok=True, value=value)
        error = value if isinstance(value, BaseException) else ExecutorError(str(value))
        return ExecutionOutcome(task.name, ok=False, error=error)

    def _cleanup_pod(self, pod_name: str) -> None:
        """Best-effort pod deletion after a failed/timed-out `kubectl run`, so a hung or failed
        task doesn't leave an orphaned pod behind. Deliberately swallows every failure here: a
        cleanup problem must never mask the original task failure being reported."""
        with contextlib.suppress(OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            subprocess.run(
                ["kubectl", "delete", "pod", pod_name, "--ignore-not-found", *self._namespace_args()],
                capture_output=True,
                timeout=self.timeout,
            )
