"""The Runner process against a fake control plane (PRD issue 46).

``agentic-runner run`` is stood up in-process with its seams injected: the HTTP transport
is the conformance kit's fake control plane mounted as an ``httpx.MockTransport``, the
Temporal connection and the Worker are fakes, and the capability check is told what it
would have found. What this pins:

* a ``contract_uid`` Runner that lacks ``CAP_SETUID`` exits non-zero naming the
  capability, before any bootstrap is attempted (17 A2);
* an ``isolation: none`` Runner registers, heartbeats ``none``, connects to the namespace
  and queue it was handed with the Runner Token from the first ack, and turns ready;
* two replicas of one Helm release register as two Runners under one Recipient Key --
  the Secret the first replica creates and the second finds (22 A4);
* the capability check reads the effective set, not the uid.
"""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from agentic_runner import service
from agentic_runner.recipient_key_secret import KubernetesSecrets, ensure_recipient_key
from agentic_runner.registration import can_separate_uids
from agentic_runner.sealed_box import RecipientKeyStore, generate_recipient_key
from agentic_runner.testing import FakeControlPlane

CONTROL_PLANE = "http://control-plane.test"


class FakeWorker:
    """``Worker.run()`` blocks until ``shutdown()``; what it was built with is kept."""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self._stopped = asyncio.Event()

    async def run(self) -> None:
        await self._stopped.wait()

    async def shutdown(self) -> None:
        self._stopped.set()


class Harness:
    def __init__(self, plane: FakeControlPlane) -> None:
        self.plane = plane
        self.connects: list[dict[str, Any]] = []
        self.workers: list[FakeWorker] = []
        self.stop = asyncio.Event()

    async def connect(self, address: str, **kwargs: Any) -> Any:
        self.connects.append({"address": address, **kwargs})
        return SimpleNamespace(api_key=kwargs.get("api_key"))

    def worker(self, client: Any, **kwargs: Any) -> FakeWorker:
        worker = FakeWorker(**kwargs)
        self.workers.append(worker)
        return worker

    async def run(self, *, can_change_uid: bool, readiness: service.Readiness | None = None) -> int:
        async with httpx.AsyncClient(transport=self.plane.transport()) as http:
            return await service.run(
                http_client=http,
                connect=self.connect,
                worker_factory=self.worker,
                can_change_uid=can_change_uid,
                stop=self.stop,
                heartbeat_interval=0.05,
                readiness=readiness,
            )


def _environment(monkeypatch: pytest.MonkeyPatch, state_dir: Path, *, isolation: str) -> None:
    monkeypatch.setenv("AGENTIC_CONTROL_PLANE_URL", CONTROL_PLANE)
    monkeypatch.setenv("AGENTIC_AGENT_TOKEN", "agent-token-of-sixteen-plus-chars")
    monkeypatch.setenv("AGENTIC_RUNNER_STATE_DIR", str(state_dir))
    monkeypatch.setenv("AGENTIC_RUNNER_ISOLATION", isolation)
    monkeypatch.setenv("AGENTIC_RUNNER_TAGS", "region=eu-west-1")
    monkeypatch.setenv("TEMPORAL_ADDRESS", "temporal.test:7233")
    monkeypatch.setenv("AGENTIC_RUNNER_SOCKET_DIR", str(state_dir / "sockets"))
    monkeypatch.delenv("INTERNAL_FASTAPI_BASE_URL", raising=False)


@pytest.mark.asyncio
async def test_a_contract_uid_runner_without_cap_setuid_refuses_to_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _environment(monkeypatch, tmp_path, isolation="contract_uid")
    plane = FakeControlPlane()

    exit_code = await Harness(plane).run(can_change_uid=False)

    assert exit_code == 1
    assert "CAP_SETUID" in capsys.readouterr().out
    # Fail closed *before* anything registers: a refused Runner holds no slot of the cap.
    assert plane.bootstraps == []


@pytest.mark.asyncio
async def test_an_isolation_none_runner_starts_and_heartbeats_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _environment(monkeypatch, tmp_path, isolation="none")
    plane = FakeControlPlane()
    harness = Harness(plane)
    readiness = service.Readiness(0)

    async def stop_once_heard() -> None:
        await plane.wait_for(lambda: bool(plane.heartbeats))
        # Ready means registered, heard and polling -- observed over the probe itself.
        while not readiness.ready:
            await asyncio.sleep(0.01)
        async with httpx.AsyncClient() as probe:
            answer = await probe.get(f"http://127.0.0.1:{readiness.port}/healthz")
        assert answer.status_code == 200
        harness.stop.set()

    stopper = asyncio.create_task(stop_once_heard())
    exit_code = await harness.run(can_change_uid=False, readiness=readiness)
    await stopper

    assert exit_code == 0
    [bootstrap] = plane.bootstraps
    assert bootstrap.isolation_mode == "none"
    assert bootstrap.tags == {"region": "eu-west-1"}
    assert plane.heartbeats[0].isolation_mode == "none"
    [runner_id] = plane.runner_ids
    assert plane.heartbeats[0].hosted_task_queue == f"runner.{runner_id}"
    [connect] = harness.connects
    assert connect["address"] == "temporal.test:7233"
    # The Runner Token from the first ack, not the one bootstrap handed out.
    assert connect["api_key"] == f"runner-token-{runner_id}-1"
    [worker] = harness.workers
    assert worker.kwargs["task_queue"] == f"runner.{runner_id}"
    assert connect["namespace"].startswith("org-")
    # Off again once the drain finished, so a restarting pod is not routed to early.
    assert readiness.ready is False


class FakeSecretsApi:
    """The Kubernetes Secrets endpoint for one name: 404 until created, 409 after."""

    def __init__(self) -> None:
        self.stored: dict[str, dict[str, str]] = {}
        self.creates = 0

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        name = request.url.path.rsplit("/", 1)[-1]
        if request.method == "GET":
            if name not in self.stored:
                return httpx.Response(404, json={"reason": "NotFound"})
            return httpx.Response(200, json={"data": self.stored[name]})
        body = json.loads(request.content)
        self.creates += 1
        if body["metadata"]["name"] in self.stored:
            return httpx.Response(409, json={"reason": "AlreadyExists"})
        self.stored[body["metadata"]["name"]] = body["data"]
        return httpx.Response(201, json=body)


def _secrets(api: FakeSecretsApi) -> KubernetesSecrets:
    return KubernetesSecrets(
        base_url="https://kubernetes.default.svc",
        kube_ns="agentic-os",
        token="projected-token",
        client=httpx.Client(transport=api.transport()),
    )


@pytest.mark.asyncio
async def test_two_replicas_register_as_distinct_runners_under_one_recipient_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeSecretsApi()
    first_dir, second_dir = tmp_path / "runner-0", tmp_path / "runner-1"
    # Each pod's init container: the first creates the release's Secret, the second reads
    # what the first wrote -- one key per installation, two state directories.
    first_key = ensure_recipient_key(
        _secrets(api), secret_name="rel-recipient-key", kube_ns="agentic-os", state_dir=first_dir
    )
    second_key = ensure_recipient_key(
        _secrets(api), secret_name="rel-recipient-key", kube_ns="agentic-os", state_dir=second_dir
    )
    assert first_key == second_key
    assert api.creates == 1
    assert (first_dir / "recipient-key.json").stat().st_mode & 0o077 == 0

    plane = FakeControlPlane()
    for state_dir in (first_dir, second_dir):
        _environment(monkeypatch, state_dir, isolation="none")
        harness = Harness(plane)
        heard = len(plane.heartbeats)

        async def stop_once_heard(harness: Harness = harness, heard: int = heard) -> None:
            await plane.wait_for(lambda: len(plane.heartbeats) > heard)
            harness.stop.set()

        stopper = asyncio.create_task(stop_once_heard())
        assert await harness.run(can_change_uid=False) == 0
        await stopper

    first, second = plane.bootstraps
    assert first.recipient_key == second.recipient_key
    assert first.recipient_key.key_id == first_key.key_id
    assert len(set(plane.heartbeat_runner_ids)) == 2

    # A restart re-reads the identity rather than registering a third Runner.
    _environment(monkeypatch, second_dir, isolation="none")
    harness = Harness(plane)
    heard = len(plane.heartbeats)

    async def stop_again() -> None:
        await plane.wait_for(lambda: len(plane.heartbeats) > heard)
        harness.stop.set()

    stopper = asyncio.create_task(stop_again())
    assert await harness.run(can_change_uid=False) == 0
    await stopper
    assert len(plane.bootstraps) == 2


def test_an_installer_managed_key_is_never_rotated_by_the_process(tmp_path: Path) -> None:
    store = RecipientKeyStore(tmp_path)
    installed = generate_recipient_key()
    now = datetime.now(UTC)

    assert store.install(installed, managed_by="secret:agentic-os/rel", now=now) is True
    assert store.current() == installed
    assert store.previous() is None
    # Same key again (a pod restart): nothing to do.
    assert store.install(installed, managed_by="secret:agentic-os/rel", now=now) is False
    # Past the process-driven renewal window, still not due: the installer rotates.
    assert store.due(now + timedelta(days=45)) is False

    # The installer rotated the Secret: the old key stays openable until the re-seal
    # confirms (22 A7), exactly as a process-driven renewal would keep it.
    rotated = generate_recipient_key()
    assert store.install(rotated, managed_by="secret:agentic-os/rel", now=now) is True
    assert store.current() == rotated
    assert store.previous() == installed


def test_a_secret_that_is_not_a_recipient_key_is_refused() -> None:
    api = FakeSecretsApi()
    api.stored["rel-recipient-key"] = {"key_id": base64.b64encode(b"only-an-id").decode()}

    with pytest.raises(RuntimeError, match="does not hold a Recipient Key"):
        _secrets(api).get("rel-recipient-key")


def test_can_separate_uids_reads_the_effective_capability_set(tmp_path: Path) -> None:
    """Root with ALL dropped is root and still cannot setuid: the bit decides, not the uid."""

    status = tmp_path / "status"
    status.write_text("Name:\tagentic-runner\nCapEff:\t00000000000000c1\n")  # SETUID|SETGID|CHOWN
    assert can_separate_uids(proc_status=status) is True

    status.write_text("Name:\tagentic-runner\nCapEff:\t0000000000000000\n")
    assert can_separate_uids(proc_status=status) is False
