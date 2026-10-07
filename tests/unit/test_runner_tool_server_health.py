"""The heartbeat says whether each Tool Server started (console-v2 issue 29).

What this pins, per acceptance criterion:

* a Runner-hosted server that exits at start is on the next beat as ``started: false``,
  a healthy one as ``started: true``, and the bridge's first answered call counts too;
* an entry carries the slug, the boolean and the time, and nothing a credential could
  ride in;
* a heartbeat without ``tool_servers`` still parses, and an empty one is not sent at
  all, so a control plane on the previous contracts minor parses it unchanged.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from agentic_runner import service
from agentic_runner.credentials import CredentialResolver, EmptyCredentialStore
from agentic_runner.llm_proxy import CeilingStore, LlmProxy, SlotStore, UsageOutbox
from agentic_runner.mcp import RunnerHostedServer, ToolServerHealthLog
from agentic_runner.registration import RunnerState
from agentic_runner.sealed_box import RecipientKeyStore, SealedCredentialStream
from agentic_runner_contracts import __version__ as contracts_version
from agentic_runner_contracts.runner_registration import (
    FloorState,
    HeartbeatAck,
    HeartbeatEnvelope,
    ToolServerHealth,
)
from agentic_runner_contracts.runtime_context import McpServerSpec

STARTED_AT = datetime(2026, 10, 8, 9, 0, tzinfo=UTC)
TOKEN = "attempt-bearer"

# Answers one JSON-RPC request per line with an empty result, until stdin closes.
_ECHO = (
    "import json, sys\n"
    "for line in sys.stdin:\n"
    "    message = json.loads(line)\n"
    "    print(json.dumps({'jsonrpc': '2.0', 'id': message['id'], 'result': {}}), flush=True)\n"
)


class _Registration:
    server_date = None

    def __init__(self) -> None:
        self.sent: list[HeartbeatEnvelope] = []

    async def heartbeat(self, state: Any, envelope: HeartbeatEnvelope) -> HeartbeatAck:
        self.sent.append(envelope)
        return HeartbeatAck(
            runner_id=uuid4(),
            floor_state=FloorState.OK,
            contracts_floor=contracts_version,
            tag_set_version=1,
            accepts_new_directives=True,
        )


def _stream(tmp_path: Path, health: ToolServerHealthLog) -> tuple[Any, _Registration]:
    registration = _Registration()
    runner_id = uuid4()
    stream = service.ControlPlaneStream(
        client=registration,  # type: ignore[arg-type]
        state=RunnerState(
            runner_id=runner_id,
            identity_id="id",
            private_key_pem="unused",
            temporal_namespace="org-x",
            task_queue=f"runner.{runner_id}",
        ),
        isolation=service.IsolationMode.NONE,
        proxy=LlmProxy(slots=SlotStore(), ceilings=CeilingStore(), outbox=UsageOutbox()),
        sealed=SealedCredentialStream(RecipientKeyStore(tmp_path)),
        credentials=CredentialResolver(store=EmptyCredentialStore()),
        load=service._LoadInterceptor(),
        tool_servers=health,
    )
    return stream, registration


def _spec(slug: str, program: str) -> McpServerSpec:
    return McpServerSpec(
        slug=slug,
        transport="stdio",
        config={"command": sys.executable, "args": ["-c", program]},
        credential_reference="acme_token",
    )


class _Spawned:
    """The real spawner, keeping the process so a test can wait for it to exit."""

    def __init__(self) -> None:
        self.process: asyncio.subprocess.Process | None = None

    async def __call__(self, *args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        self.process = await asyncio.create_subprocess_exec(*args, **kwargs)
        return self.process


def _hosted(spec: McpServerSpec, health: ToolServerHealthLog, spawner: Any) -> RunnerHostedServer:
    return RunnerHostedServer(
        spec, credential_env={"ACME_TOKEN": "secret"}, token=TOKEN, spawner=spawner, health=health
    )


@pytest.mark.asyncio
async def test_a_server_that_exits_at_start_is_reported_down_and_a_healthy_one_up(
    tmp_path: Path,
) -> None:
    health = ToolServerHealthLog(clock=lambda: STARTED_AT)
    stream, registration = _stream(tmp_path, health)

    crashing = _Spawned()
    async with _hosted(_spec("crashing", "raise SystemExit(1)"), health, crashing):
        assert crashing.process is not None
        await crashing.process.wait()
    async with _hosted(_spec("healthy", _ECHO), health, _Spawned()):
        pass
    await stream.exchange([])

    [beat] = registration.sent
    assert {entry.slug: entry.started for entry in beat.tool_servers} == {
        "crashing": False,
        "healthy": True,
    }
    assert {entry.last_started_at for entry in beat.tool_servers} == {STARTED_AT}


@pytest.mark.asyncio
async def test_the_first_call_through_the_bridge_decides_the_report(tmp_path: Path) -> None:
    health = ToolServerHealthLog(clock=lambda: STARTED_AT)
    request = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    headers = {"Authorization": f"Bearer {TOKEN}"}

    async with (
        _hosted(_spec("answers", _ECHO), health, _Spawned()) as answers,
        httpx.AsyncClient() as client,
    ):
        response = await client.post(answers.url, json=request, headers=headers)
        assert response.status_code == 200

    gone = _Spawned()
    async with (
        _hosted(_spec("gone", "raise SystemExit(0)"), health, gone) as server,
        httpx.AsyncClient() as client,
    ):
        assert gone.process is not None
        await gone.process.wait()
        response = await client.post(server.url, json=request, headers=headers)
        assert response.status_code == 502

    assert {entry.slug: entry.started for entry in health.latest()} == {
        "answers": True,
        "gone": False,
    }


@pytest.mark.asyncio
async def test_a_server_that_cannot_be_spawned_is_reported_down() -> None:
    health = ToolServerHealthLog(clock=lambda: STARTED_AT)

    async def missing(*_: Any, **__: Any) -> asyncio.subprocess.Process:
        raise FileNotFoundError("no such MCP server binary")

    with pytest.raises(FileNotFoundError):
        async with _hosted(_spec("missing", "pass"), health, missing):
            pass

    assert [(entry.slug, entry.started) for entry in health.latest()] == [("missing", False)]


@pytest.mark.asyncio
async def test_a_server_not_started_since_the_runner_began_is_absent(tmp_path: Path) -> None:
    stream, registration = _stream(tmp_path, ToolServerHealthLog())

    await stream.exchange([])

    assert registration.sent[0].tool_servers == []


def test_the_latest_start_per_slug_is_reported_and_the_list_is_bounded() -> None:
    health = ToolServerHealthLog()
    health.record("flaky", started=False, at=STARTED_AT)
    health.record("flaky", started=True, at=STARTED_AT + timedelta(minutes=1))
    for index in range(70):
        health.record(f"server-{index}", started=True, at=STARTED_AT + timedelta(hours=index))

    latest = health.latest()

    assert len(latest) == 64
    # The oldest fall off first, so the server started most recently is never dropped.
    assert latest[-1].slug == "server-69"
    assert "flaky" not in {entry.slug for entry in latest}
    health.record("flaky", started=True, at=STARTED_AT + timedelta(days=30))
    assert health.latest()[-1] == ToolServerHealth(
        slug="flaky", started=True, last_started_at=STARTED_AT + timedelta(days=30)
    )


def test_an_entry_carries_the_slug_the_boolean_and_the_time_and_nothing_else() -> None:
    entry = ToolServerHealth(slug="github", started=False, last_started_at=STARTED_AT)

    assert set(json.loads(entry.model_dump_json())) == {"slug", "started", "last_started_at"}
    for smuggled in (
        {"command": "npx server --token=ghp_x"},
        {"url": "https://user:pass@mcp.example"},
        {"env": {"ACME_TOKEN": "secret"}},
        {"error": "Traceback ..."},
    ):
        with pytest.raises(ValidationError):
            ToolServerHealth.model_validate({**json.loads(entry.model_dump_json()), **smuggled})


_ENVELOPE: dict[str, Any] = {
    "runner_version": "0.1.0",
    "contracts_version": contracts_version,
    "resource_pressure": {"cpu_percent": 1.0, "memory_percent": 2.0, "disk_percent": 3.0},
    "max_concurrent_directives": 1,
    "current_load": 0,
    "egress_posture": "allowlisted",
    "isolation_mode": "none",
    "tag_set_version": 1,
    "hosted_task_queue": "runner.test",
}


def test_a_heartbeat_without_tool_servers_still_parses_and_round_trips() -> None:
    envelope = HeartbeatEnvelope.model_validate(_ENVELOPE)

    assert envelope.tool_servers == []
    # Not sent while empty: a control plane on the previous minor forbids unknown keys.
    assert "tool_servers" not in json.loads(envelope.model_dump_json())
    assert HeartbeatEnvelope.model_validate_json(envelope.model_dump_json()) == envelope

    reporting = HeartbeatEnvelope.model_validate(
        {
            **_ENVELOPE,
            "tool_servers": [
                {"slug": "github", "started": True, "last_started_at": STARTED_AT.isoformat()}
            ],
        }
    )
    assert HeartbeatEnvelope.model_validate_json(reporting.model_dump_json()) == reporting
    with pytest.raises(ValidationError):
        HeartbeatEnvelope.model_validate(
            {**_ENVELOPE, "tool_servers": [reporting.tool_servers[0].model_dump()] * 65}
        )
