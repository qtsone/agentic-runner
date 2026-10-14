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
import os
import signal
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from agentic_runner import __version__ as runner_version
from agentic_runner import service
from agentic_runner.build import build_id
from agentic_runner.lifecycle import LifecycleOutbox
from agentic_runner.recipient_key_secret import KubernetesSecrets, ensure_recipient_key
from agentic_runner.registration import (
    CONTROL_PLANE_OLDER_REASON,
    HEARTBEAT_PATH,
    RunnerRegistrationClient,
    RunnerRegistrationError,
    can_separate_uids,
)
from agentic_runner.sealed_box import RecipientKeyStore, generate_recipient_key
from agentic_runner.testing import FakeControlPlane
from agentic_runner_contracts import __version__ as contracts_version
from agentic_runner_contracts.runner_registration import (
    BootstrapRequest,
    EgressPosture,
    IsolationMode,
    LifecycleKind,
    RecipientKey,
)

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
    monkeypatch.setenv("AGENTIC_RUNNER_REGISTRATION_TOKEN", "registration-token-of-sixteen-plus")
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
    # Sent before an identity exists, so a platform can refuse an unpublished build there.
    assert bootstrap.build_id == build_id()
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


async def _run_until_connected(harness: Harness) -> int:
    async def stop_once_connected() -> None:
        await harness.plane.wait_for(lambda: bool(harness.connects))
        harness.stop.set()

    stopper = asyncio.create_task(stop_once_connected())
    exit_code = await harness.run(can_change_uid=False)
    stopper.cancel()
    return exit_code


@pytest.mark.asyncio
async def test_a_runner_connects_to_the_temporal_address_its_registration_gave(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ADR-0022 §4: the install carries only the platform URL and the token.
    _environment(monkeypatch, tmp_path, isolation="none")
    monkeypatch.delenv("TEMPORAL_ADDRESS")
    harness = Harness(FakeControlPlane(temporal_address="temporal.plane.test:443"))

    assert await _run_until_connected(harness) == 0

    [connect] = harness.connects
    assert connect["address"] == "temporal.plane.test:443"


@pytest.mark.asyncio
async def test_the_registration_address_wins_over_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _environment(monkeypatch, tmp_path, isolation="none")
    harness = Harness(FakeControlPlane(temporal_address="temporal.plane.test:443"))

    assert await _run_until_connected(harness) == 0

    [connect] = harness.connects
    assert connect["address"] == "temporal.plane.test:443"


@pytest.mark.asyncio
async def test_a_registration_without_an_address_falls_back_to_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A control plane that predates §4 sends no address at all.
    _environment(monkeypatch, tmp_path, isolation="none")
    harness = Harness(FakeControlPlane())

    assert await _run_until_connected(harness) == 0

    [connect] = harness.connects
    assert connect["address"] == "temporal.test:7233"


@pytest.mark.asyncio
async def test_a_runner_with_no_temporal_address_anywhere_refuses_to_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _environment(monkeypatch, tmp_path, isolation="none")
    monkeypatch.delenv("TEMPORAL_ADDRESS")
    harness = Harness(FakeControlPlane())

    assert await harness.run(can_change_uid=False) == 1

    assert harness.connects == []
    assert "TEMPORAL_ADDRESS" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_a_sigterm_before_the_worker_starts_exits_zero_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # QTS-1536: the first heartbeat never answers, so start-up is still waiting on it
    # when SIGTERM lands. Before, nothing handled the signal until the Worker polled.
    _environment(monkeypatch, tmp_path, isolation="none")
    plane = FakeControlPlane()
    harness = Harness(plane)
    heartbeat_sent = asyncio.Event()

    async def first_heartbeat_hangs(request: httpx.Request) -> httpx.Response:
        if request.url.path == HEARTBEAT_PATH:
            heartbeat_sent.set()
            await asyncio.Event().wait()
        answer = plane.handle(
            request.method, request.url.raw_path.decode("ascii"), request.headers, request.content
        )
        return httpx.Response(answer.status, json=answer.body)

    async def terminate_once_heartbeating() -> None:
        await heartbeat_sent.wait()
        os.kill(os.getpid(), signal.SIGTERM)

    terminator = asyncio.create_task(terminate_once_heartbeating())
    async with httpx.AsyncClient(transport=httpx.MockTransport(first_heartbeat_hangs)) as http:
        exit_code = await asyncio.wait_for(
            service.run(
                http_client=http,
                connect=harness.connect,
                worker_factory=harness.worker,
                can_change_uid=False,
            ),
            timeout=5,
        )
    await terminator

    assert exit_code == 0
    assert harness.connects == []
    assert harness.workers == []
    # Queued for the next start, which carries it on its first beat.
    pending = LifecycleOutbox(tmp_path).pending()
    assert [event.kind for event in pending] == [LifecycleKind.START, LifecycleKind.STOP]


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


@pytest.mark.asyncio
async def test_a_control_plane_older_than_the_runner_is_named_not_dumped_as_a_422() -> None:

    def older_control_plane(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={
                "detail": [
                    {
                        "type": "extra_forbidden",
                        "loc": ["body", "build_id"],
                        "msg": "Extra inputs are not permitted",
                        "input": build_id(),
                    }
                ]
            },
        )

    key = generate_recipient_key()
    async with httpx.AsyncClient(transport=httpx.MockTransport(older_control_plane)) as client:
        registration = RunnerRegistrationClient(base_url="https://cp.test", client=client)
        with pytest.raises(RunnerRegistrationError) as refused:
            await registration.bootstrap(
                registration_token="registration-token-from-the-admin",
                tags={},
                isolation_mode=IsolationMode.NONE,
                recipient_key=RecipientKey(key_id=key.key_id, public_key=key.public_key),
                can_separate_uids=False,
            )

    message = str(refused.value)
    assert refused.value.reason == CONTROL_PLANE_OLDER_REASON
    assert "control plane is older than this Runner" in message
    assert f"agentic-runner {runner_version}" in message
    assert f"agentic-runner-contracts {contracts_version}" in message
    assert "build_id" in message
    assert "extra_forbidden" not in message


def _bootstrap_request(**token: str) -> BootstrapRequest:
    key = generate_recipient_key()
    return BootstrapRequest(
        **token,
        isolation_mode=IsolationMode.NONE,
        contracts_version=contracts_version,
        runner_version=runner_version,
        recipient_key=RecipientKey(key_id=key.key_id, public_key=key.public_key),
    )


def test_bootstrap_takes_the_registration_token_under_either_name_but_never_both() -> None:
    new = _bootstrap_request(registration_token="registration-token-of-sixteen-plus")
    old = _bootstrap_request(agent_token="agent-token-of-sixteen-plus-chars")

    assert new.token == "registration-token-of-sixteen-plus"
    assert old.token == "agent-token-of-sixteen-plus-chars"
    # A control plane below 3.16 forbids a key it does not know, so the absent one is
    # never sent, not even as null.
    assert "agent_token" not in json.loads(new.model_dump_json())
    assert "registration_token" not in json.loads(old.model_dump_json())
    with pytest.raises(ValueError, match="exactly one"):
        _bootstrap_request(
            registration_token="registration-token-of-sixteen-plus",
            agent_token="agent-token-of-sixteen-plus-chars",
        )
    with pytest.raises(ValueError, match="exactly one"):
        _bootstrap_request()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("environment", "presented", "warned"),
    [
        ({"AGENTIC_RUNNER_REGISTRATION_TOKEN": "new-name-of-sixteen-plus"}, "new-name", False),
        ({"AGENTIC_AGENT_TOKEN": "old-name-of-sixteen-plus-chars"}, "old-name", True),
        (
            {
                "AGENTIC_RUNNER_REGISTRATION_TOKEN": "new-name-of-sixteen-plus",
                "AGENTIC_AGENT_TOKEN": "old-name-of-sixteen-plus-chars",
            },
            "new-name",
            False,
        ),
    ],
)
async def test_the_runner_registers_with_the_new_variable_and_still_the_deprecated_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    environment: dict[str, str],
    presented: str,
    warned: bool,
) -> None:
    _environment(monkeypatch, tmp_path, isolation="none")
    monkeypatch.delenv("AGENTIC_RUNNER_REGISTRATION_TOKEN")
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    plane = FakeControlPlane()

    async with httpx.AsyncClient(transport=plane.transport()) as http:
        await service.register(state_dir=tmp_path, can_separate_uids=False, client=http)

    (bootstrap,) = plane.bootstraps
    # Sent under the new name whichever variable held it.
    assert bootstrap.registration_token is not None
    assert bootstrap.registration_token.startswith(presented)
    assert bootstrap.agent_token is None
    assert ("AGENTIC_AGENT_TOKEN is deprecated" in capsys.readouterr().err) is warned


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason", ["registration_token_unknown", "agent_token_unknown", "agent_token_revoked"]
)
async def test_a_refused_registration_token_is_named_under_its_old_or_new_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    reason: str,
) -> None:
    _environment(monkeypatch, tmp_path, isolation="none")

    def refusing_control_plane(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": {"reason": reason, "detail": "refused"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(refusing_control_plane)) as http:
        exit_code = await service.run(
            http_client=http,
            connect=Harness(FakeControlPlane()).connect,
            can_change_uid=False,
            stop=asyncio.Event(),
        )

    assert exit_code == 1
    assert "Runner Registration Token is unknown, revoked or expired" in capsys.readouterr().out


# ADR-0022 §57: the Runner Limit is read and checked before anything registers, and
# rides registration and every heartbeat (Runner Limits ticket 02).


@pytest.mark.asyncio
async def test_the_runner_refuses_to_start_on_a_limit_it_cannot_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _environment(monkeypatch, tmp_path, isolation="none")
    monkeypatch.setenv("AGENTIC_RUNNER_LIMITS", json.dumps({"folders": ["/"]}))
    plane = FakeControlPlane()

    exit_code = await Harness(plane).run(can_change_uid=False)

    assert exit_code == 1
    assert "refusing to start (runner_limit): Runner Limit folders[0]" in capsys.readouterr().out
    assert plane.bootstraps == []


@pytest.mark.asyncio
async def test_the_runner_refuses_to_start_on_a_limit_that_does_not_parse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _environment(monkeypatch, tmp_path, isolation="none")
    monkeypatch.setenv("AGENTIC_RUNNER_LIMITS", "{not json")
    plane = FakeControlPlane()

    exit_code = await Harness(plane).run(can_change_uid=False)

    assert exit_code == 1
    assert "refusing to start (configuration_invalid)" in capsys.readouterr().out
    assert plane.bootstraps == []


@pytest.mark.asyncio
async def test_registration_and_every_heartbeat_carry_the_limit_and_its_posture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _environment(monkeypatch, tmp_path, isolation="none")
    monkeypatch.setenv("AGENTIC_RUNNER_EGRESS_POSTURE", "unenforced")
    monkeypatch.setenv(
        "AGENTIC_RUNNER_LIMITS",
        json.dumps({"networks": ["evil.example"], "folders": [str(tmp_path / "clients")]}),
    )
    plane = FakeControlPlane()
    harness = Harness(plane)

    async def stop_once_heard() -> None:
        await plane.wait_for(lambda: len(plane.heartbeats) >= 2)
        harness.stop.set()

    stopper = asyncio.create_task(stop_once_heard())
    assert await harness.run(can_change_uid=False) == 0
    await stopper

    [bootstrap] = plane.bootstraps
    assert bootstrap.runner_limit is not None
    assert bootstrap.runner_limit.limit.networks == ("evil.example",)
    assert bootstrap.runner_limit.posture.networks is EgressPosture.UNENFORCED
    for beat in plane.heartbeats[:2]:
        assert beat.runner_limit == bootstrap.runner_limit
