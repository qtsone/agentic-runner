"""A retried Directive attempt resumes its harness session (local-agents 16).

ADR-0007, amendment 2026-10-03: only on an exact Contract, Workspace path and head match,
never while the prior attempt's process group lives, with one Evidence event per retry.
"""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest
from temporalio.testing import ActivityEnvironment

from agentic_runner.activities import (
    HARNESS_SESSION_EVIDENCE_SOURCE,
    HarnessSession,
    RunnerRalphActivities,
    _liveness_heartbeats,
)
from agentic_runner.attempts import AttemptRecords, PriorAttemptAliveError, process_start_time
from agentic_runner.workers._runtime_support import SubprocessResult, run_subprocess_exec
from agentic_runner.workers.agent_runtime import (
    AgentRuntime,
    AuthMode,
    DirectiveRequest,
    DirectiveResult,
    RuntimeCapabilities,
)
from agentic_runner.workers.claude_runtime import ClaudeRuntime
from agentic_runner.workers.codex_runtime import CodexRuntime
from agentic_runner.workers.contract_isolation import DirectiveSandbox
from agentic_runner.workers.settings import WorkerSettings

_CONTRACT = "contract-a"
_SESSION = "0199a3f2-6b1e-7c4d-9e8f-0a1b2c3d4e5f"


class _RecordingClient:
    def __init__(self) -> None:
        self.evidence: list[dict[str, Any]] = []

    async def append_evidence(
        self, work_record_id: str, *, source: str, payload: dict[str, Any], **kwargs: Any
    ) -> dict[str, Any]:
        self.evidence.append({"source": source, "payload": dict(payload)})
        return {}


class _Runtime:
    """Stands in for a harness that may or may not still hold the session."""

    auth_modes = frozenset({AuthMode.API_KEY})
    host_api_key = False

    def __init__(self, *, holds: set[str]) -> None:
        self.holds = holds

    def capabilities(self) -> RuntimeCapabilities:
        return RuntimeCapabilities(auth_modes=self.auth_modes, permission_mode="fake")

    async def execute_directive(self, request: DirectiveRequest) -> DirectiveResult:
        raise AssertionError("not run here")

    def has_session(self, session_id: str, sandbox: DirectiveSandbox | None) -> bool:
        return session_id in self.holds


class _NonResumableRuntime:
    auth_modes = frozenset({AuthMode.API_KEY})
    host_api_key = False

    def capabilities(self) -> RuntimeCapabilities:
        return RuntimeCapabilities(auth_modes=self.auth_modes, permission_mode="fake")

    async def execute_directive(self, request: DirectiveRequest) -> DirectiveResult:
        raise AssertionError("not run here")


def _git(workspace: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    return tmp_path / "workspaces"


@pytest.fixture
def workspace(workspace_root: Path) -> Path:
    path = workspace_root / _CONTRACT / "wr-a"
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    _git(path, "commit", "-q", "--allow-empty", "-m", "base")
    return path


@pytest.fixture
def records(tmp_path: Path, workspace_root: Path) -> AttemptRecords:
    return AttemptRecords(tmp_path / "state", workspace_root=workspace_root)


def _prior(workspace: Path, **changes: Any) -> dict[str, Any]:
    session = HarnessSession(
        session_id=_SESSION,
        contract_id=_CONTRACT,
        workspace_path=str(workspace),
        head=_git(workspace, "rev-parse", "HEAD"),
    )
    return dataclasses.asdict(dataclasses.replace(session, **changes))


async def _decide(
    *,
    attempt: int,
    heartbeat_details: list[Any],
    workspace: Path,
    workspace_root: Path,
    runtime: AgentRuntime,
    records: AttemptRecords | None = None,
    report_session: str | None = None,
) -> tuple[str | None, list[dict[str, Any]], list[tuple[Any, ...]]]:
    client = _RecordingClient()
    activities = RunnerRalphActivities(
        client, workspace_root=workspace_root, attempt_records=records
    )
    beats: list[tuple[Any, ...]] = []
    env = ActivityEnvironment()
    env.info = dataclasses.replace(env.info, attempt=attempt, heartbeat_details=heartbeat_details)
    env.on_heartbeat = lambda *details: beats.append(details)

    async def body() -> str | None:
        async with _liveness_heartbeats() as liveness, activities._fenced("wr-a"):
            resume, on_session_started = await activities._harness_session(
                liveness,
                work_record_id="wr-a",
                directive_number=2,
                agent_runtime=runtime,
                contract_id=_CONTRACT,
                workspace_path=workspace,
                sandbox=None,
            )
            if report_session is not None:
                on_session_started(report_session)
            return resume

    resume = await env.run(body)
    return resume, client.evidence, beats


@pytest.mark.asyncio
async def test_a_first_attempt_writes_no_evidence_and_files_its_session_in_the_heartbeat(
    workspace: Path, workspace_root: Path
) -> None:
    resume, evidence, beats = await _decide(
        attempt=1,
        heartbeat_details=[],
        workspace=workspace,
        workspace_root=workspace_root,
        runtime=_Runtime(holds=set()),
        report_session=_SESSION,
    )

    assert resume is None
    assert evidence == []
    assert beats[-1][1] == _prior(workspace)


@pytest.mark.asyncio
async def test_a_retry_resumes_when_contract_workspace_and_head_all_match(
    workspace: Path, workspace_root: Path
) -> None:
    resume, evidence, beats = await _decide(
        attempt=2,
        heartbeat_details=[1000.0, _prior(workspace)],
        workspace=workspace,
        workspace_root=workspace_root,
        runtime=_Runtime(holds={_SESSION}),
    )

    assert resume == _SESSION
    [event] = evidence
    assert event["source"] == HARNESS_SESSION_EVIDENCE_SOURCE
    assert event["payload"] == {
        "event": "directive.harness_session",
        "work_record_id": "wr-a",
        "directive_number": 2,
        "attempt": 2,
        "decision": "resumed",
        "reason": "contract_workspace_and_head_match",
    }
    # Carried into this attempt's beats at once, so a second loss can resume it again.
    assert beats[-1] == (1000.0, _prior(workspace))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"contract_id": "contract-b"}, "contract_changed"),
        ({"workspace_path": "/elsewhere"}, "workspace_changed"),
        ({"head": "0" * 40}, "head_changed"),
    ],
)
async def test_a_retry_starts_fresh_on_any_mismatch(
    workspace: Path, workspace_root: Path, change: dict[str, str], reason: str
) -> None:
    fresh = str(uuid.uuid4())
    resume, evidence, beats = await _decide(
        attempt=3,
        heartbeat_details=[1000.0, _prior(workspace, **change)],
        workspace=workspace,
        workspace_root=workspace_root,
        runtime=_Runtime(holds={_SESSION}),
        report_session=fresh,
    )

    assert resume is None
    [event] = evidence
    assert event["payload"]["decision"] == "fresh"
    assert event["payload"]["reason"] == reason
    assert beats[-1][1] == _prior(workspace, session_id=fresh)


@pytest.mark.asyncio
async def test_a_retry_starts_fresh_when_the_head_moved_since_the_session_started(
    workspace: Path, workspace_root: Path
) -> None:
    prior = _prior(workspace)
    _git(workspace, "commit", "-q", "--allow-empty", "-m", "pushed by the lost attempt")

    resume, evidence, _ = await _decide(
        attempt=2,
        heartbeat_details=[1000.0, prior],
        workspace=workspace,
        workspace_root=workspace_root,
        runtime=_Runtime(holds={_SESSION}),
    )

    assert resume is None
    assert evidence[0]["payload"]["reason"] == "head_changed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("details", "runtime", "reason"),
    [
        ([1000.0], _Runtime(holds={_SESSION}), "no_prior_session"),
        (None, _NonResumableRuntime(), "runtime_cannot_resume"),
        (None, _Runtime(holds=set()), "session_not_found"),
    ],
)
async def test_a_retry_starts_fresh_when_there_is_nothing_to_resume(
    workspace: Path,
    workspace_root: Path,
    details: list[Any] | None,
    runtime: AgentRuntime,
    reason: str,
) -> None:
    resume, evidence, _ = await _decide(
        attempt=2,
        heartbeat_details=details or [1000.0, _prior(workspace)],
        workspace=workspace,
        workspace_root=workspace_root,
        runtime=runtime,
    )

    assert resume is None
    assert [event["payload"]["reason"] for event in evidence] == [reason]


@pytest.mark.asyncio
async def test_a_retry_is_refused_not_resumed_while_the_prior_attempt_lives(
    workspace: Path, workspace_root: Path, records: AttemptRecords
) -> None:
    sleeper = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        started = process_start_time(sleeper.pid)
        assert started is not None
        records.path("wr-a").parent.mkdir(parents=True)
        records.path("wr-a").write_text(
            json.dumps({"pgid": sleeper.pid, "start_time": started, "attempt": 1})
        )
        with pytest.raises(PriorAttemptAliveError):
            await _decide(
                attempt=2,
                heartbeat_details=[1000.0, _prior(workspace)],
                workspace=workspace,
                workspace_root=workspace_root,
                runtime=_Runtime(holds={_SESSION}),
                records=records,
            )
    finally:
        sleeper.kill()
        sleeper.wait()


@pytest.mark.asyncio
async def test_the_evidence_never_names_the_session(workspace: Path, workspace_root: Path) -> None:
    _, evidence, _ = await _decide(
        attempt=2,
        heartbeat_details=[1000.0, _prior(workspace)],
        workspace=workspace,
        workspace_root=workspace_root,
        runtime=_Runtime(holds={_SESSION}),
    )

    assert _SESSION not in json.dumps(evidence)


def test_malformed_heartbeat_details_name_no_session() -> None:
    assert HarnessSession.from_heartbeat({"session_id": 7}) is None
    assert HarnessSession.from_heartbeat("not a mapping") is None


class _FakeRunner:
    def __init__(self, stdout: str = "") -> None:
        self.calls: list[dict[str, Any]] = []
        self.stdout = stdout

    async def __call__(self, **kwargs: Any) -> SubprocessResult:
        self.calls.append(kwargs)
        if "on_first_stdout_line" in kwargs:
            kwargs["on_first_stdout_line"](self.stdout.split("\n")[0].encode())
        return SubprocessResult(exit_code=0, stdout=self.stdout, stderr="")


def _settings(tmp_path: Path) -> WorkerSettings:
    return WorkerSettings(
        TEMPORAL_ADDRESS="127.0.0.1:7233",
        INTERNAL_FASTAPI_BASE_URL="http://agentic-api.internal:8000",
        WORKSPACE_ROOT=tmp_path / "workspaces",
        CODEX_HOME=tmp_path / "codex-home",
        CODEX_SANDBOX_MODE="workspace-write",
        CODEX_ASK_FOR_APPROVAL="never",
        CODEX_STRICT_CONFIG=True,
        CODEX_POLICY_HOOK_CONFIGURED=True,
        ANTHROPIC_API_KEY="sk-ant-worker-key",
        CLAUDE_DANGEROUSLY_SKIP_PERMISSIONS=True,
    )


def _request(tmp_path: Path, **fields: Any) -> DirectiveRequest:
    workspace = tmp_path / "workspaces" / "wr-a"
    workspace.mkdir(parents=True, exist_ok=True)
    return DirectiveRequest(
        workspace_path=workspace,
        prompt="Apply the fix",
        base_branch="main",
        work_branch="wr/a",
        **({"auth_mode": AuthMode.SUBSCRIPTION} | fields),
    )


def _sandbox(tmp_path: Path) -> DirectiveSandbox:
    return DirectiveSandbox(
        home_dir=tmp_path / "home",
        harness_config_dir=tmp_path / "harness",
        max_processes=64,
        max_memory_bytes=1 << 30,
    )


@pytest.mark.asyncio
async def test_claude_names_a_fresh_session_before_it_spawns(tmp_path: Path) -> None:
    runner = _FakeRunner()
    reported: list[str] = []

    async def spy(**kwargs: Any) -> SubprocessResult:
        assert reported, "the session must be filed before the turn starts"
        return await runner(**kwargs)

    await ClaudeRuntime(settings=_settings(tmp_path), runner=spy).execute_directive(
        _request(tmp_path, on_session_started=reported.append)
    )

    [session] = reported
    argv = runner.calls[0]["argv"]
    assert argv[argv.index("--session-id") + 1] == session
    assert "--resume" not in argv


@pytest.mark.asyncio
async def test_claude_resumes_the_session_it_is_given(tmp_path: Path) -> None:
    runner = _FakeRunner()
    reported: list[str] = []

    await ClaudeRuntime(settings=_settings(tmp_path), runner=runner).execute_directive(
        _request(tmp_path, resume_session_id=_SESSION, on_session_started=reported.append)
    )

    argv = runner.calls[0]["argv"]
    assert argv[argv.index("--resume") + 1] == _SESSION
    assert "--session-id" not in argv
    assert reported == []


@pytest.mark.asyncio
async def test_claude_without_a_session_hook_runs_as_before(tmp_path: Path) -> None:
    runner = _FakeRunner()

    await ClaudeRuntime(settings=_settings(tmp_path), runner=runner).execute_directive(
        _request(tmp_path)
    )

    assert not {"--session-id", "--resume"} & set(runner.calls[0]["argv"])


def test_claude_holds_a_session_only_in_the_contracts_harness_root(tmp_path: Path) -> None:
    runtime = ClaudeRuntime(settings=_settings(tmp_path))
    sandbox = _sandbox(tmp_path)
    project = sandbox.harness_config_dir / "projects" / "-workspaces-wr-a"
    project.mkdir(parents=True)
    (project / f"{_SESSION}.jsonl").write_text("{}\n")

    assert runtime.has_session(_SESSION, sandbox)
    assert not runtime.has_session(str(uuid.uuid4()), sandbox)
    assert not runtime.has_session(_SESSION, None)
    assert not runtime.has_session("*", sandbox)


@pytest.mark.asyncio
async def test_codex_names_a_fresh_thread_from_its_opening_event(tmp_path: Path) -> None:
    runner = _FakeRunner(
        stdout=json.dumps({"type": "thread.started", "thread_id": _SESSION})
        + '\n{"type":"turn.started"}\n'
    )
    reported: list[str] = []

    await CodexRuntime(settings=_settings(tmp_path), runner=runner).execute_directive(
        _request(tmp_path, on_session_started=reported.append)
    )

    assert reported == [_SESSION]
    assert "resume" not in runner.calls[0]["argv"]


@pytest.mark.asyncio
async def test_codex_resumes_with_the_exec_subcommand_before_the_stdin_prompt(
    tmp_path: Path,
) -> None:
    runner = _FakeRunner()

    await CodexRuntime(settings=_settings(tmp_path), runner=runner).execute_directive(
        _request(tmp_path, resume_session_id=_SESSION, on_session_started=lambda _: None)
    )

    call = runner.calls[0]
    assert call["argv"][-3:] == ["resume", _SESSION, "-"]
    assert "on_first_stdout_line" not in call


def test_codex_holds_a_thread_while_its_rollout_exists(tmp_path: Path) -> None:
    runtime = CodexRuntime(settings=_settings(tmp_path))
    sandbox = _sandbox(tmp_path)
    day = sandbox.harness_config_dir / "sessions" / "2026" / "10" / "08"
    day.mkdir(parents=True)
    (day / f"rollout-2026-10-08T14-29-04-{_SESSION}.jsonl").write_text("{}\n")

    assert runtime.has_session(_SESSION, sandbox)
    assert not runtime.has_session(str(uuid.uuid4()), sandbox)
    assert not runtime.has_session(_SESSION, None)


@pytest.mark.asyncio
async def test_the_subprocess_runner_hands_over_the_first_stdout_line_mid_run(
    tmp_path: Path,
) -> None:
    seen: list[tuple[bytes, bool]] = []
    marker = tmp_path / "second-line-written"

    def on_first_line(line: bytes) -> None:
        seen.append((line, marker.exists()))

    await run_subprocess_exec(
        argv=[
            sys.executable,
            "-c",
            "import sys, time; print('first', flush=True); time.sleep(0.5); "
            f"open({str(marker)!r}, 'w'); print('second')",
        ],
        cwd=tmp_path,
        env=dict(os.environ),
        stdin=None,
        timeout_seconds=30,
        output_limit_bytes=1024,
        on_first_stdout_line=on_first_line,
    )

    assert seen == [(b"first", False)]
