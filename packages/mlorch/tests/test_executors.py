import base64
import pickle
import re
import shutil
import subprocess

import pytest

from mlorch.dag import Task
from mlorch.errors import ExecutorError
from mlorch.executors import (
    DockerExecutor,
    KubernetesExecutor,
    LocalExecutor,
    _sanitize_pod_name,
    decode_outcome,
    docker_available,
    encode_call,
    kubectl_available,
)


def _square(x: int) -> int:
    return x * x


def _raises(x: int) -> int:
    raise ValueError(f"bad input {x}")


class TestLocalExecutor:
    def test_runs_a_single_task(self) -> None:
        task = Task("t", lambda x: x + 1)
        outcomes = LocalExecutor().run_batch([(task, {"x": 41})])
        assert outcomes["t"].ok is True
        assert outcomes["t"].value == 42

    def test_empty_batch_returns_empty_results(self) -> None:
        assert LocalExecutor().run_batch([]) == {}

    def test_runs_a_batch_of_independent_tasks(self) -> None:
        batch = [(Task(f"t{i}", lambda x: x * 2), {"x": i}) for i in range(5)]
        outcomes = LocalExecutor().run_batch(batch)
        for i in range(5):
            assert outcomes[f"t{i}"].value == i * 2

    def test_max_workers_is_respected_as_a_constructor_option(self) -> None:
        executor = LocalExecutor(max_workers=1)
        outcomes = executor.run_batch([(Task("t", lambda: "ok"), {})])
        assert outcomes["t"].value == "ok"


class TestDockerWireProtocol:
    """Exercises the exact serialization mlorch ships across the container boundary, without
    needing a Docker daemon: this is what runs on each side of the `docker run` pipe."""

    def test_encode_decode_roundtrip_for_a_successful_call(self) -> None:
        payload = encode_call(_square, {"x": 6})
        fn, kwargs = pickle.loads(base64.b64decode(payload))
        result = fn(**kwargs)
        response = base64.b64encode(pickle.dumps((True, result)))
        ok, value = decode_outcome(response)
        assert ok is True
        assert value == 36

    def test_encode_decode_roundtrip_for_a_failing_call(self) -> None:
        payload = encode_call(_raises, {"x": 6})
        fn, kwargs = pickle.loads(base64.b64decode(payload))
        try:
            fn(**kwargs)
            raised: BaseException | None = None
        except ValueError as exc:
            raised = exc
        assert raised is not None
        response = base64.b64encode(pickle.dumps((False, raised)))
        ok, value = decode_outcome(response)
        assert ok is False
        assert isinstance(value, ValueError)
        assert "bad input 6" in str(value)


class TestDockerExecutorAvailabilityHandling:
    def test_refuses_to_run_when_docker_is_unreachable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("mlorch.executors.docker_available", lambda: False)
        executor = DockerExecutor()
        with pytest.raises(ExecutorError):
            executor.run_batch([(Task("t", _square), {"x": 2})])

    def test_empty_batch_short_circuits_before_checking_docker(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("mlorch.executors.docker_available", lambda: False)
        assert DockerExecutor().run_batch([]) == {}


@pytest.mark.skipif(not docker_available(), reason="no reachable Docker daemon in this environment")
class TestDockerExecutorLive:
    def test_runs_a_real_task_inside_a_container(self) -> None:
        outcomes = DockerExecutor().run_batch([(Task("t", _square), {"x": 7})])
        assert outcomes["t"].ok is True
        assert outcomes["t"].value == 49


class TestPodNameSanitization:
    """`_sanitize_pod_name` is the piece of `KubernetesExecutor` that can be fully exercised
    without a cluster: it just has to obey Kubernetes' own pod-name rules."""

    _DNS_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")

    def test_produces_a_dns_label_safe_name_from_weird_input(self) -> None:
        name = _sanitize_pod_name("My Task/With Weird::Chars!! (v2)")
        assert self._DNS_LABEL.match(name)

    def test_produces_a_dns_label_safe_name_from_an_all_invalid_task_name(self) -> None:
        name = _sanitize_pod_name("!!!___...")
        assert self._DNS_LABEL.match(name)

    def test_produces_a_dns_label_safe_name_from_an_empty_task_name(self) -> None:
        name = _sanitize_pod_name("")
        assert self._DNS_LABEL.match(name)

    def test_stays_within_the_253_character_limit(self) -> None:
        name = _sanitize_pod_name("x" * 1000)
        assert len(name) <= 253
        assert self._DNS_LABEL.match(name)

    def test_two_calls_for_the_same_task_name_produce_different_pod_names(self) -> None:
        assert _sanitize_pod_name("same-task") != _sanitize_pod_name("same-task")

    def test_preserves_a_recognizable_prefix_for_an_already_valid_name(self) -> None:
        assert _sanitize_pod_name("train-model").startswith("mlorch-train-model-")


class TestKubectlAvailable:
    """`kubectl_available()` must fail fast rather than let `KubernetesExecutor` claim
    readiness and then fail on the first real task."""

    def test_returns_false_when_kubectl_is_not_on_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(shutil, "which", lambda name: None)
        assert kubectl_available() is False

    def test_returns_false_when_the_cluster_is_unreachable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/kubectl")

        def _raise(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            raise subprocess.CalledProcessError(1, "kubectl cluster-info")

        monkeypatch.setattr(subprocess, "run", _raise)
        assert kubectl_available() is False

    def test_returns_false_when_the_cluster_info_call_times_out(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/kubectl")

        def _raise(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            raise subprocess.TimeoutExpired("kubectl cluster-info", 5)

        monkeypatch.setattr(subprocess, "run", _raise)
        assert kubectl_available() is False

    def test_returns_true_when_the_cli_is_present_and_the_cluster_is_reachable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/kubectl")
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *args, **kwargs: subprocess.CompletedProcess(args[0] if args else [], 0),
        )
        assert kubectl_available() is True


class TestKubernetesExecutorAvailabilityHandling:
    def test_refuses_to_run_when_kubectl_is_unreachable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("mlorch.executors.kubectl_available", lambda: False)
        executor = KubernetesExecutor()
        with pytest.raises(ExecutorError):
            executor.run_batch([(Task("t", _square), {"x": 2})])

    def test_empty_batch_short_circuits_before_checking_kubectl(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("mlorch.executors.kubectl_available", lambda: False)
        assert KubernetesExecutor().run_batch([]) == {}


@pytest.mark.skipif(
    not kubectl_available(), reason="no reachable Kubernetes cluster in this environment"
)
class TestKubernetesExecutorLive:
    def test_runs_a_real_task_inside_a_pod(self) -> None:
        outcomes = KubernetesExecutor().run_batch([(Task("t", _square), {"x": 7})])
        assert outcomes["t"].ok is True
        assert outcomes["t"].value == 49
