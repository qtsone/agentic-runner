"""Runner Hooks around a real Directive (PRD issue 45, ADR-0013 §10, map ticket 26 §1).

The catalogue and the hook environment are unit-level (``tests/unit/test_runner_hooks.py``);
what this file holds is the part only the activity can show: the fixed execution order,
the ``pre_directive`` reject gate, the deferred ``pre_exit`` teardown, the one override
(``checkout``), a ``post_verify`` that cannot touch the verdict, and the two — and only
two — environment names the Agent Runtime subprocess gains from its callback socket.

Every hook here is a real executable appending its own name to a log, so the order under
test is the order the Runner actually ran, not a list this file restates.
"""

from __future__ import annotations

import shutil
import stat
import subprocess
import tempfile
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from agentic_runner.activities import RunnerRalphActivities
from agentic_runner.callback import CALLBACK_SOCKET_ENV, CALLBACK_TOKEN_ENV
from agentic_runner.hooks import HookRunner
from agentic_runner.integrations.git.fake_workspace import FakeGitWorkspace
from agentic_runner.integrations.github.fake_client import FakeGitHubClient
from agentic_runner.workers.agent_runtime import (
    AuthMode,
    DirectiveEvidence,
    DirectiveRequest,
    DirectiveResult,
)
from agentic_runner_contracts.activity_io import BranchPullRequestInput, VerifierRunInput
from agentic_runner_contracts.runtime_context import work_branch_name

WORK_RECORD_ID = "123e4567-e89b-12d3-a456-426614174000"
REPOSITORY = "qts/agentic-os"
APPROVED_HEAD_SHA = "a" * 40
WORK_BRANCH = work_branch_name(work_record_id=WORK_RECORD_ID, profile_slug="engineer")

# Every Directive slot except `checkout`, which is an override and is installed only by
# the test that means to override (map ticket 26 §1).
DIRECTIVE_SLOTS = (
    "pre_directive",
    "environment",
    "pre_checkout",
    "post_checkout",
    "pre_runtime",
    "post_runtime",
    "pre_artifact",
    "post_artifact",
    "pre_exit",
)


class _FakeClient:
    def __init__(self) -> None:
        self.evidence: list[tuple[str, dict[str, Any]]] = []

    async def get_runtime_context(self, work_record_id: str) -> dict[str, Any]:
        return {
            "work_record_id": WORK_RECORD_ID,
            "profile_slug": "engineer",
            "cli_kind": "codex_cli",
            "repo": REPOSITORY,
            "base_branch": "main",
            "reviewer": "ralph-reviewer",
            "completion_criteria": "Add a Production section to the README",
            "persona_slug": None,
            "persona_instructions": "",
            "task_queue": "ralph-pr-loop",
            "worker_secret_refs": [],
            "command_policy": {},
            "network_policy": {},
            "product_verifier_command_source": {
                "source": "runtime-profile",
                "available": True,
                "metadata": {"command": "python -m pytest -q"},
            },
        }

    async def get_directive_usage(self, work_record_id: str, directive_id: str) -> dict[str, Any]:
        return {}

    async def report_harness_usage(self, payload: Mapping[str, Any]) -> list[dict[str, Any]]:
        return []

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

    async def record_verifier_result(
        self, work_record_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return {"ok": True}

    async def transition_work_record(
        self, work_record_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return {"ok": True}

    def sources(self) -> list[str]:
        return [source for source, _ in self.evidence]

    def hook_runs(self) -> list[dict[str, Any]]:
        return [payload for source, payload in self.evidence if source == "runner.hook"]


@dataclass
class _FakeRuntime:
    auth_modes: frozenset[AuthMode] = frozenset({AuthMode.API_KEY, AuthMode.SUBSCRIPTION})
    requests: list[DirectiveRequest] = field(default_factory=list)

    async def execute_directive(self, request: DirectiveRequest) -> DirectiveResult:
        self.requests.append(request)
        return DirectiveResult(
            exit_code=0,
            stdout="edited files",
            stderr="",
            error="",
            command_hash="hash",
            evidence=DirectiveEvidence(
                workspace_id="qts-agentic-os",
                base_branch=request.base_branch,
                work_branch=request.work_branch,
                command_hash="hash",
                guard_mode="workspace-write/no-approval",
                notes=[],
            ),
        )


@pytest.fixture
def socket_dir() -> Iterator[Path]:
    """A short root for the attempt sockets — `sun_path` is ~104 bytes (callback.py)."""

    path = Path(tempfile.mkdtemp(dir="/tmp"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _install(hooks_path: Path, name: str, body: str) -> None:
    hooks_path.mkdir(parents=True, exist_ok=True)
    hook = hooks_path / name
    hook.write_text(body)
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR)


def _logging_hooks(hooks_path: Path, log: Path, *, names: tuple[str, ...]) -> None:
    for name in names:
        _install(hooks_path, name, f"#!/bin/sh\necho {name} >> {log}\nexit 0\n")


def _log_lines(log: Path) -> list[str]:
    return log.read_text().split() if log.exists() else []


def _activities(
    client: _FakeClient,
    *,
    tmp_path: Path,
    socket_dir: Path,
    hooks_path: Path | None = None,
    git_workspace: FakeGitWorkspace | None = None,
    runtime: _FakeRuntime | None = None,
    verifier_runner: Any = None,
) -> RunnerRalphActivities:
    return RunnerRalphActivities(
        client,  # type: ignore[arg-type]
        agent_runtimes={"codex_cli": runtime or _FakeRuntime()},
        git_workspace=git_workspace
        or FakeGitWorkspace(status_evidence=" M README.md", diff_evidence="+change"),
        github_client=FakeGitHubClient(),
        workspace_root=tmp_path,
        hooks=HookRunner(hooks_path=hooks_path),
        socket_dir=socket_dir,
        **({} if verifier_runner is None else {"verifier_runner": verifier_runner}),
    )


def _branch_pr_input() -> BranchPullRequestInput:
    return BranchPullRequestInput(
        work_record_id=WORK_RECORD_ID,
        repository=REPOSITORY,
        base_ref="main",
        branch_name=WORK_BRANCH,
        pr_title="Add a Production section",
        pr_body="body",
    )


@pytest.mark.asyncio
async def test_the_directive_runs_its_hooks_in_the_fixed_catalogue_order(
    tmp_path: Path, socket_dir: Path
) -> None:
    log = tmp_path / "hooks.log"
    _logging_hooks(tmp_path / "hooks", log, names=DIRECTIVE_SLOTS)
    client = _FakeClient()

    result = await _activities(
        client, tmp_path=tmp_path, socket_dir=socket_dir, hooks_path=tmp_path / "hooks"
    ).create_or_update_branch_pr(_branch_pr_input())

    assert result.pr_number > 0
    assert _log_lines(log) == [
        "pre_directive",
        "environment",
        "pre_checkout",
        "post_checkout",
        "pre_runtime",
        "post_runtime",
        "pre_artifact",
        "post_artifact",
        "pre_exit",
    ]
    # ...and every one of them is an Evidence Event of its own (map ticket 26 §1).
    assert [run["hook"] for run in client.hook_runs()] == _log_lines(log)
    assert all(run["exit_code"] == 0 and "duration_ms" in run for run in client.hook_runs())


@pytest.mark.asyncio
async def test_a_pre_directive_refusal_stops_the_directive_and_still_runs_pre_exit(
    tmp_path: Path, socket_dir: Path
) -> None:
    """The reject gate: exit non-zero and no Directive runs, with Evidence saying so."""

    log = tmp_path / "hooks.log"
    _logging_hooks(tmp_path / "hooks", log, names=("pre_runtime", "pre_exit"))
    _install(
        tmp_path / "hooks", "pre_directive", f"#!/bin/sh\necho pre_directive >> {log}\nexit 1\n"
    )
    client = _FakeClient()
    runtime = _FakeRuntime()

    result = await _activities(
        client,
        tmp_path=tmp_path,
        socket_dir=socket_dir,
        hooks_path=tmp_path / "hooks",
        runtime=runtime,
    ).create_or_update_branch_pr(_branch_pr_input())

    assert result.guard_mode_refused is True
    assert result.pr_number == 0
    assert runtime.requests == [], "the Agent Runtime must not run past a refused gate"
    # `pre_exit` is a deferred teardown, so it runs even though the attempt was refused;
    # `pre_runtime` never does, because the Directive never got that far.
    assert _log_lines(log) == ["pre_directive", "pre_exit"]
    rejections = [
        payload for source, payload in client.evidence if source == "runner.directive_rejected"
    ]
    assert len(rejections) == 1
    assert rejections[0]["hook"] == "pre_directive"
    assert rejections[0]["exit_code"] == 1


@pytest.mark.asyncio
async def test_a_checkout_hook_replaces_the_runners_own_clone(
    tmp_path: Path, socket_dir: Path
) -> None:
    """The one override worth keeping: a mirror or warm cache (map ticket 26 §1)."""

    log = tmp_path / "hooks.log"
    _logging_hooks(tmp_path / "hooks", log, names=("pre_checkout", "checkout", "post_checkout"))
    git_workspace = FakeGitWorkspace(status_evidence=" M README.md", diff_evidence="+change")
    # Nothing is seeded on the adapter: a real `checkout` hook is a separate process and
    # can only hand its checkout back through `adopt_existing_clone`. Seeding here would
    # manufacture registry state production cannot produce.
    result = await _activities(
        _FakeClient(),
        tmp_path=tmp_path,
        socket_dir=socket_dir,
        hooks_path=tmp_path / "hooks",
        git_workspace=git_workspace,
    ).create_or_update_branch_pr(_branch_pr_input())

    assert result.pr_number > 0
    assert _log_lines(log) == ["pre_checkout", "checkout", "post_checkout"]
    operations = [call.operation for call in git_workspace.calls]
    replaced = {"clone_repository", "fetch_base_branch", "checkout_work_branch"}
    assert replaced.isdisjoint(operations)
    # ...and the rest of the Directive still ran its git over that workspace.
    assert operations[0] == "adopt_existing_clone"
    assert {"collect_git_evidence", "commit_all", "push_branch"} <= set(operations)


@pytest.mark.asyncio
async def test_a_failing_post_verify_hook_does_not_change_the_verifier_verdict(
    tmp_path: Path, socket_dir: Path
) -> None:
    """The Verifier is a Gate input: a hook may wrap it, never overturn it."""

    log = tmp_path / "hooks.log"
    _logging_hooks(tmp_path / "hooks", log, names=("pre_verify",))
    _install(tmp_path / "hooks", "post_verify", f"#!/bin/sh\necho post_verify >> {log}\nexit 1\n")
    client = _FakeClient()
    git_workspace = FakeGitWorkspace(status_evidence=" M README.md", diff_evidence="+change")
    activities = _activities(
        client,
        tmp_path=tmp_path,
        socket_dir=socket_dir,
        hooks_path=tmp_path / "hooks",
        git_workspace=git_workspace,
        verifier_runner=_passing_verifier_runner,
    )
    await activities.create_or_update_branch_pr(_branch_pr_input())
    git_workspace.set_remote_work_branch_head(
        repo_full_name=REPOSITORY,
        work_branch=WORK_BRANCH,
        head_sha=APPROVED_HEAD_SHA,
    )

    result = await activities.run_verifier(
        VerifierRunInput(
            work_record_id=WORK_RECORD_ID,
            repository=REPOSITORY,
            pr_number=1,
            merge_commit_sha="e" * 40,
            command="runtime-profile:sha256:label",
            approved_head_sha=APPROVED_HEAD_SHA,
        )
    )

    assert result.passed is True
    assert _log_lines(log)[-2:] == ["pre_verify", "post_verify"]
    failed = [run for run in client.hook_runs() if run["hook"] == "post_verify"]
    assert failed and failed[0]["exit_code"] == 1


@pytest.mark.asyncio
async def test_the_agent_runtime_env_gains_the_callback_pair_and_nothing_else(
    tmp_path: Path, socket_dir: Path
) -> None:
    """Issue 09's env allow-list, amended by issue 45: these names and no others.

    The `environment` hook's exports land beside them — that is the slot's purpose — but
    an export of a name the Runner reserves is dropped, because that is how a hook would
    otherwise move the Contract's harness root or replace the attempt's own bearer.
    """

    hooks_path = tmp_path / "hooks"
    _install(
        hooks_path,
        "environment",
        "#!/bin/sh\n"
        'printf "BUILD_FLAVOUR=fast\\n" >> "$AGENTIC_RUNNER_ENV_FILE"\n'
        'printf "HOME=/tmp/hijacked\\n" >> "$AGENTIC_RUNNER_ENV_FILE"\n'
        f'printf "{CALLBACK_TOKEN_ENV}=hijacked\\n" >> "$AGENTIC_RUNNER_ENV_FILE"\n'
        "exit 0\n",
    )
    runtime = _FakeRuntime()

    await _activities(
        _FakeClient(),
        tmp_path=tmp_path,
        socket_dir=socket_dir,
        hooks_path=hooks_path,
        runtime=runtime,
    ).create_or_update_branch_pr(_branch_pr_input())

    (request,) = runtime.requests
    extra = dict(request.extra_env)
    assert set(extra) == {CALLBACK_SOCKET_ENV, CALLBACK_TOKEN_ENV, "BUILD_FLAVOUR"}
    assert extra["BUILD_FLAVOUR"] == "fast"
    assert extra[CALLBACK_TOKEN_ENV] != "hijacked"
    assert extra[CALLBACK_SOCKET_ENV].endswith("s.sock")


@pytest.mark.asyncio
async def test_the_attempt_socket_is_gone_once_the_directive_ends(
    tmp_path: Path, socket_dir: Path
) -> None:
    """The bearer's whole defence under ADR-0011 §9: it dies with the attempt."""

    runtime = _FakeRuntime()

    await _activities(
        _FakeClient(), tmp_path=tmp_path, socket_dir=socket_dir, runtime=runtime
    ).create_or_update_branch_pr(_branch_pr_input())

    (request,) = runtime.requests
    socket_path = Path(dict(request.extra_env)[CALLBACK_SOCKET_ENV])
    assert not socket_path.exists()
    assert not socket_path.parent.exists()


def _passing_verifier_runner(
    argv: tuple[str, ...],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout_seconds: float,
    **spawn_kwargs: Any,
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=list(argv), returncode=0, stdout="ok", stderr="")
