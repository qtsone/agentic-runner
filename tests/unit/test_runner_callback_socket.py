"""The per-attempt callback socket (PRD issue 45, map ticket 26 §4, 17 A11).

The socket is the one place an Agent may reach back into its own Runner. Three things
have to hold, and each is asserted against the real server over a real unix socket: the
attempt's own bearer works, nobody else's does, and a ``verb`` callback is the *same*
evaluation the activity seam runs rather than a quieter second answer (ADR-0011 §10).
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import shutil
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from pydantic import BaseModel

from agentic_runner.activities import RunnerRalphActivities, _RuntimeContextState
from agentic_runner.callback import (
    ROUTES,
    AttemptCallbackServer,
    CallbackError,
    call,
)
from agentic_runner.heartbeat_link import HeartbeatLink
from agentic_runner_contracts import __version__ as contracts_version
from agentic_runner_contracts.grants import Decision, GrantSnapshot, decide_verb
from agentic_runner_contracts.runner_registration import (
    AppliedSnapshot,
    FloorState,
    GrantPush,
    HeartbeatAck,
)

REPOSITORY = "qts/agentic-os"
WORK_RECORD_ID = "123e4567-e89b-12d3-a456-426614174000"
AGENT_ID = "8f14e45f-ceea-467a-9a37-1a2b3c4d5e6f"
CONTRACT_ID = "1d0f6b8c-2e3f-4a5b-8c7d-9e0f1a2b3c4d"
RUNNER_ID = UUID("7c6d5e4f-3a2b-4c1d-8e9f-0a1b2c3d4e5f")

# A field name matching any of these would hand an Agent a value it is not allowed to
# hold (ADR-0011 §9). The point of the closed route table is that this stays empty.
CREDENTIAL_SHAPED = re.compile(
    r"(?i)(token|secret|password|credential|api[_-]?key|authorization|bearer|private_key)"
)


class _FakeClient:
    def __init__(self, snapshot: Mapping[str, Any] | None = None) -> None:
        self.evidence: list[tuple[str, dict[str, Any]]] = []
        self._snapshot = snapshot

    async def append_evidence(
        self,
        work_record_id: str,
        *,
        source: str,
        payload: Mapping[str, Any],
        actor: str = "temporal-worker",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        self.evidence.append((source, dict(payload)))
        return {"ok": True}

    def evaluations(self) -> list[dict[str, Any]]:
        return [payload for source, payload in self.evidence if source == "ralph.grant_evaluation"]


def _payload(verbs: Mapping[str, str]) -> dict[str, Any]:
    return {
        "agent_id": AGENT_ID,
        "contract_id": CONTRACT_ID,
        "contract_state": "active",
        "dispatchable": True,
        "root_grant": _grant(**verbs),
        "agent_grant": _grant(**verbs),
        "persona_allow_list": _grant(**verbs),
        "resources": [
            {
                "resource_type": "repo",
                "selector": REPOSITORY,
                "in_contract_scope": True,
            }
        ],
    }


def _state(*, push: str = "allow", **verbs: str) -> _RuntimeContextState:
    snapshot = GrantSnapshot.from_payload(_payload({"push": push, **verbs}))
    return _RuntimeContextState(
        reviewer=None,
        repository=REPOSITORY,
        verifier_argv=("true",),
        work_branch="agent/work",
        completion_criteria="ship it",
        agent_id=AGENT_ID,
        grant_snapshot=snapshot,
    )


@pytest.fixture
def socket_dir() -> Iterator[Path]:
    """A *short* directory for the sockets.

    `sun_path` is ~104 bytes and pytest's own `tmp_path` spends most of that on the
    platform temp root and the test name, so a socket under it cannot be bound at all —
    which is exactly why `callback.MAX_SOCKET_PATH_BYTES` exists and why the Runner gives
    its sockets a short root of their own instead of a place beside the Workspace.
    """

    path = Path(tempfile.mkdtemp(dir="/tmp"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _grant(**verbs: str) -> dict[str, Any]:
    return {"entries": [{"resource_type": "repo", "selector": REPOSITORY, "verbs": dict(verbs)}]}


async def _server(
    socket_dir: Path,
    activities: RunnerRalphActivities,
    state: _RuntimeContextState,
    workspace: Path,
) -> AttemptCallbackServer:
    return AttemptCallbackServer(
        socket_path=socket_dir / "s.sock",
        handlers=activities._callback_handlers(WORK_RECORD_ID, state, workspace),
    )


def _activities(client: _FakeClient) -> RunnerRalphActivities:
    return RunnerRalphActivities(client)  # type: ignore[arg-type]


async def _post(
    server: AttemptCallbackServer, path: str, payload: dict[str, Any], token: str
) -> dict[str, Any]:
    return await asyncio.to_thread(
        call,
        socket_path=server.socket_path,
        token=token,
        path=path,
        payload=payload,
    )


def test_no_route_in_the_table_carries_a_credential_shaped_field() -> None:
    """The closed set, and the promise that none of it returns a credential."""

    assert set(ROUTES) == {
        ("POST", "/v0/annotate"),
        ("POST", "/v0/artifact"),
        ("POST", "/v0/verb"),
        # PRD issue 52: the Channel seam, over the same socket and the same bearer.
        ("POST", "/v0/message/send"),
        ("POST", "/v0/message/list"),
        # PRD issue 60: `work.ask`, the Question seam.
        ("POST", "/v0/ask"),
        # ADR-0018 §5, §7: an Organisation-scoped Work Record's reads and its binding.
        ("POST", "/v0/repo/read"),
        ("POST", "/v0/repo/branch"),
    }
    models: list[type[BaseModel]] = []
    for route in ROUTES.values():
        models.extend((route.request, route.response))
    offending = [
        f"{model.__name__}.{field}"
        for model in models
        for field in model.model_fields
        if CREDENTIAL_SHAPED.search(field)
    ]
    assert offending == []


@pytest.mark.asyncio
async def test_annotate_with_the_attempts_bearer_is_accepted(
    tmp_path: Path, socket_dir: Path
) -> None:
    client = _FakeClient()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    server = await _server(socket_dir, _activities(client), _state(), workspace)

    async with server:
        answer = await _post(
            server,
            "/v0/annotate",
            {"context": "verify", "body": "3 tests failed"},
            server.token,
        )

    assert answer == {"accepted": True, "context": "verify"}
    assert (
        "runner.annotation",
        {"context": "verify", "style": "info", "body": "3 tests failed"},
    ) in (client.evidence)


@pytest.mark.asyncio
async def test_a_stale_bearer_is_refused(tmp_path: Path, socket_dir: Path) -> None:
    """The token dies with the attempt: the previous Directive's is simply wrong now."""

    client = _FakeClient()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    server = await _server(socket_dir, _activities(client), _state(), workspace)

    async with server:
        with pytest.raises(CallbackError) as refusal:
            await _post(
                server, "/v0/annotate", {"context": "x", "body": "y"}, "stale-attempt-bearer"
            )

    assert refusal.value.status_code == 401
    assert client.evidence == []


@pytest.mark.asyncio
async def test_another_attempts_bearer_does_not_open_this_attempts_socket(
    tmp_path: Path, socket_dir: Path
) -> None:
    """Two concurrent Directives on one Runner: neither may drive the other's socket."""

    client = _FakeClient()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    first = AttemptCallbackServer(
        socket_path=socket_dir / "first.sock",
        handlers=_activities(client)._callback_handlers(WORK_RECORD_ID, _state(), workspace),
    )
    second = AttemptCallbackServer(
        socket_path=socket_dir / "second.sock",
        handlers=_activities(client)._callback_handlers(WORK_RECORD_ID, _state(), workspace),
    )
    assert first.token != second.token

    async with first, second:
        with pytest.raises(CallbackError) as refusal:
            await _post(second, "/v0/annotate", {"context": "x", "body": "y"}, first.token)
        accepted = await _post(second, "/v0/annotate", {"context": "x", "body": "y"}, second.token)

    assert refusal.value.status_code == 401
    assert accepted["accepted"] is True


@pytest.mark.asyncio
async def test_a_verb_callback_is_the_same_evaluation_and_evidence_as_the_seam(
    tmp_path: Path, socket_dir: Path
) -> None:
    """A callback is one more entry point into the seam, never a bypass (ADR-0011 §10)."""

    client = _FakeClient()
    activities = _activities(client)
    state = _state(push="deny")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    server = await _server(socket_dir, activities, state, workspace)

    async with server:
        over_the_socket = await _post(
            server, "/v0/verb", {"verb": "push", "resource": REPOSITORY}, server.token
        )
    through_the_seam = await activities._authorize(
        WORK_RECORD_ID, state, verb="push", repository=REPOSITORY
    )

    assert over_the_socket["decision"] == "deny"
    assert over_the_socket["allowed"] is False
    assert over_the_socket["reason"] == through_the_seam.decision.reason
    callback_evidence, seam_evidence = client.evaluations()
    assert callback_evidence == seam_evidence


@pytest.mark.asyncio
async def test_a_verb_callback_on_a_stale_link_is_refused_not_answered_by_a_fault(
    tmp_path: Path, socket_dir: Path
) -> None:
    """The liveness gate reaches the socket too (PRD issue 44).

    A Directive asking over its own socket is *answered* -- refused, with the reason --
    rather than faulted: the subprocess keeps running, and it is the activity seam that
    fails the attempt for Temporal to retry.
    """

    client = _FakeClient()
    # A link that has never had an acknowledged exchange is stale by construction.
    activities = RunnerRalphActivities(
        client,  # type: ignore[arg-type]
        heartbeat_link=HeartbeatLink(_SilentStream()),
    )
    state = _state(push="allow")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    server = await _server(socket_dir, activities, state, workspace)

    async with server:
        answer = await _post(
            server, "/v0/verb", {"verb": "push", "resource": REPOSITORY}, server.token
        )

    assert answer == {
        **answer,
        "decision": "deny",
        "allowed": False,
        "reason": "heartbeat_stale",
    }
    assert client.evaluations()[-1]["reason"] == "heartbeat_stale"


class _FakeStream:
    """A control plane whose next beat carries whatever the test queued (PRD issue 44)."""

    def __init__(self) -> None:
        self.queued: list[GrantPush] = []
        self.acknowledged: list[tuple[AppliedSnapshot, ...]] = []

    def queue(self, version: str, verbs: Mapping[str, str]) -> None:
        self.queued = [
            GrantPush(agent_id=UUID(AGENT_ID), version=version, snapshot=_payload(verbs))
        ]

    async def exchange(self, applied: Sequence[AppliedSnapshot]) -> HeartbeatAck:
        self.acknowledged.append(tuple(applied))
        pushes, self.queued = self.queued, []
        return HeartbeatAck(
            runner_id=RUNNER_ID,
            floor_state=FloorState.OK,
            contracts_floor=contracts_version,
            tag_set_version=1,
            accepts_new_directives=True,
            grant_pushes=pushes,
        )


@pytest.mark.asyncio
async def test_a_snapshot_pushed_mid_directive_is_read_by_the_next_verb_callback(
    tmp_path: Path, socket_dir: Path
) -> None:
    """The slice's acceptance criterion, at the seam that runs while a Directive does.

    The state a callback handler closes over is materialised once, at the start of the
    activity; the heartbeat that narrows `pr.merge` lands while the subprocess is still
    running. The verb has to be decided on what the link holds *now*, the Runner has to
    acknowledge the version it applied, and what it held before has to be gone -- a push
    replaces an Agent's snapshot, it never merges into it.
    """

    stream = _FakeStream()
    stream.queue("snapshot-0001", {"push": "allow", "pr.merge": "allow"})
    link = HeartbeatLink(stream)
    await link.exchange()
    client = _FakeClient()
    activities = RunnerRalphActivities(client, heartbeat_link=link)  # type: ignore[arg-type]
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    # The state the Directive started under still says allow, and never changes.
    state = _state(**{"push": "allow", "pr.merge": "allow"})
    server = await _server(socket_dir, activities, state, workspace)

    async with server:
        allowed = await _post(
            server, "/v0/verb", {"verb": "pr.merge", "resource": REPOSITORY}, server.token
        )

        # The owner narrows the Agent; the next beat carries it, mid-Directive.
        stream.queue("snapshot-0002", {"push": "allow", "pr.merge": "deny"})
        await link.exchange()

        refused = await _post(
            server, "/v0/verb", {"verb": "pr.merge", "resource": REPOSITORY}, server.token
        )

    assert allowed["allowed"] is True
    assert refused["allowed"] is False
    assert refused["decision"] == "deny"
    # Acknowledged: the beat that carried 0002 said it held 0001, and the one after it
    # would say 0002 -- which is how the control plane tells "applied" from "sent".
    assert [entry.version for entry in stream.acknowledged[-1]] == ["snapshot-0001"]
    assert [entry.version for entry in link.applied()] == ["snapshot-0002"]
    # ...and the previous snapshot is gone rather than standing beside the narrowed one.
    held = link.snapshot_for(AGENT_ID)
    assert held is not None
    assert decide_verb(held, verb="pr.merge", identifier=REPOSITORY).decision is Decision.DENY


class _SilentStream:
    """A control plane that is never reached; the link stays stale."""

    async def exchange(self, applied: object) -> HeartbeatAck:
        raise ConnectionError("the control plane is unreachable")


@pytest.mark.asyncio
async def test_an_artifact_outside_the_workspace_is_refused(
    tmp_path: Path, socket_dir: Path
) -> None:
    client = _FakeClient()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "report.txt").write_text("coverage 91%")
    (tmp_path / "outside.txt").write_text("the Runner's own files")
    server = await _server(socket_dir, _activities(client), _state(), workspace)

    async with server:
        accepted = await _post(server, "/v0/artifact", {"path": "report.txt"}, server.token)
        escaped = await _post(server, "/v0/artifact", {"path": "../outside.txt"}, server.token)

    assert accepted["accepted"] is True
    assert accepted["size_bytes"] == len("coverage 91%")
    assert escaped["accepted"] is False
    assert "inside this Workspace" in escaped["reason"]


@pytest.mark.asyncio
async def test_an_oversized_request_head_is_answered_431(tmp_path: Path, socket_dir: Path) -> None:
    """Past the StreamReader's own limit, which used to answer 500 through the catch-all."""

    client = _FakeClient()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    server = await _server(socket_dir, _activities(client), _state(), workspace)

    async with server:
        reader, writer = await asyncio.open_unix_connection(str(server.socket_path))
        # Past 64 KiB: the default reader limit, where `readuntil` gives up before any
        # length of ours is ever compared.
        writer.write(b"POST /v0/annotate HTTP/1.1\r\nX-Pad: " + b"p" * 200_000 + b"\r\n\r\n")
        status_line = await asyncio.wait_for(reader.readline(), timeout=5)
        writer.close()
        with contextlib.suppress(ConnectionResetError, BrokenPipeError):
            await writer.wait_closed()

    assert status_line.startswith(b"HTTP/1.1 431 ")


@pytest.mark.asyncio
async def test_an_unknown_route_is_not_served(tmp_path: Path, socket_dir: Path) -> None:
    """`pipeline upload` and `meta-data` have no analogue and never grow one by accident."""

    client = _FakeClient()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    server = await _server(socket_dir, _activities(client), _state(), workspace)

    async with server:
        with pytest.raises(CallbackError) as refusal:
            await _post(server, "/v0/pipeline", {"steps": []}, server.token)

    assert refusal.value.status_code == 404


class _AskingClient(_FakeClient):
    """The control plane's `work.ask` seam, answering from a script (PRD issue 60)."""

    def __init__(self, answer: dict[str, Any]) -> None:
        super().__init__()
        self.answer = answer
        self.raised: list[tuple[str, dict[str, Any]]] = []

    async def raise_question(
        self, work_record_id: str, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        self.raised.append((work_record_id, dict(payload)))
        return self.answer


@pytest.mark.asyncio
async def test_ask_allowed_records_one_question_and_refuses_a_second(
    tmp_path: Path, socket_dir: Path
) -> None:
    """`agentic-runner ask` reaches the `work.ask` seam as the attempt's own Agent, and
    the Question rides out on the Directive's output -- one per Directive."""

    client = _AskingClient(
        {"decision": "allow", "reason": "default", "question_id": "q-1", "addressed_to": "owner"}
    )
    asked: list[Any] = []
    server = AttemptCallbackServer(
        socket_path=socket_dir / "s.sock",
        handlers=_activities(client)._callback_handlers(
            WORK_RECORD_ID, _state(), tmp_path, directive_number=3, asked=asked
        ),
    )

    async with server:
        first = await _post(server, "/v0/ask", {"text": "Which key?"}, server.token)
        second = await _post(server, "/v0/ask", {"text": "And now?"}, server.token)

    assert first == {
        "accepted": True,
        "decision": "allow",
        "reason": "default",
        "question_id": "q-1",
        "addressed_to": "owner",
    }
    assert second["accepted"] is False
    assert client.raised == [
        (WORK_RECORD_ID, {"agent_id": AGENT_ID, "text": "Which key?", "directive_number": 3})
    ]
    [question] = asked
    assert (question.question_id, question.owner_confirmation) == ("q-1", None)


@pytest.mark.asyncio
async def test_ask_denied_is_answered_and_the_directive_carries_on(
    tmp_path: Path, socket_dir: Path
) -> None:
    client = _AskingClient({"decision": "deny", "reason": "the Agent's Grant names `work.ask`"})
    asked: list[Any] = []
    server = AttemptCallbackServer(
        socket_path=socket_dir / "s.sock",
        handlers=_activities(client)._callback_handlers(
            WORK_RECORD_ID, _state(), tmp_path, asked=asked
        ),
    )

    async with server:
        answer = await _post(server, "/v0/ask", {"text": "Which key?"}, server.token)

    assert (answer["accepted"], answer["decision"]) == (False, "deny")
    assert asked == []


@pytest.mark.asyncio
async def test_ask_confirm_carries_the_owner_confirmation_out(
    tmp_path: Path, socket_dir: Path
) -> None:
    client = _AskingClient(
        {
            "decision": "confirm",
            "reason": "the Agent's Grant names `work.ask` confirm",
            "question_id": "q-2",
            "addressed_to": "requester",
            "owner_confirmation": {
                "owner_confirmation_id": "oc-1",
                "window_seconds": 86400.0,
                "reminder_after_seconds": 72000.0,
            },
        }
    )
    asked: list[Any] = []
    server = AttemptCallbackServer(
        socket_path=socket_dir / "s.sock",
        handlers=_activities(client)._callback_handlers(
            WORK_RECORD_ID, _state(), tmp_path, asked=asked
        ),
    )

    async with server:
        answer = await _post(server, "/v0/ask", {"text": "May I?"}, server.token)

    assert answer["accepted"] is True and answer["decision"] == "confirm"
    [question] = asked
    assert question.owner_confirmation.owner_confirmation_id == "oc-1"
    assert question.owner_confirmation.verb == "work.ask"


@pytest.mark.asyncio
async def test_a_directive_with_no_owner_step_cannot_ask(tmp_path: Path, socket_dir: Path) -> None:
    client = _AskingClient({"decision": "allow", "question_id": "q-3"})
    server = await _server(socket_dir, _activities(client), _state(), tmp_path)

    async with server:
        answer = await _post(server, "/v0/ask", {"text": "Hm?"}, server.token)

    assert answer["accepted"] is False
    assert client.raised == []
