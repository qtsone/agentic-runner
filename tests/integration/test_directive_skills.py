"""An Agent's attached Skills reach its Agent Runtime for one Directive (console-v2 issue 28).

Driven through the real ``create_or_update_branch_pr`` activity and a real
``ContractIsolation`` harness root, so what is asserted is what the Runner actually left on
disk while the fake runtime ran, and after.
"""

from __future__ import annotations

import hashlib
import shutil
import tempfile
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from temporalio.exceptions import ApplicationError

from agentic_runner.activities import SKILLS_EVIDENCE_SOURCE, RunnerRalphActivities
from agentic_runner.integrations.git.fake_workspace import FakeGitWorkspace
from agentic_runner.integrations.github.fake_client import FakeGitHubClient
from agentic_runner.workers.agent_runtime import (
    AuthMode,
    DirectiveEvidence,
    DirectiveRequest,
    DirectiveResult,
)
from agentic_runner.workers.contract_isolation import ContractIsolation
from agentic_runner_contracts.activity_io import BranchPullRequestInput
from agentic_runner_contracts.runtime_context import work_branch_name

WORK_RECORD_ID = "123e4567-e89b-12d3-a456-426614174000"
CONTRACT_ID = "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d"
REPOSITORY = "qts/agentic-os"
BODY = "---\nname: review\ndescription: How we review.\n---\nRead the diff first.\n"
SKILL = {
    "slug": "review",
    "version": 3,
    "sha256": hashlib.sha256(BODY.encode()).hexdigest(),
    "body": BODY,
}


class _FakeClient:
    def __init__(self, *, cli_kind: str, skills: list[dict[str, Any]]) -> None:
        self.cli_kind = cli_kind
        self.skills = skills
        self.evidence: list[tuple[str, dict[str, Any]]] = []

    async def get_runtime_context(self, work_record_id: str) -> dict[str, Any]:
        return {
            "work_record_id": WORK_RECORD_ID,
            "profile_slug": "engineer",
            "cli_kind": self.cli_kind,
            "repo": REPOSITORY,
            "base_branch": "main",
            "contract_id": CONTRACT_ID,
            "completion_criteria": "Add a Production section to the README",
            "skills": self.skills,
            "task_queue": "ralph-pr-loop",
            "worker_secret_refs": [],
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

    async def transition_work_record(
        self, work_record_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return {"ok": True}

    def skill_events(self) -> list[dict[str, Any]]:
        return [payload for source, payload in self.evidence if source == SKILLS_EVIDENCE_SOURCE]


@dataclass
class _SkillReadingRuntime:
    """Records what its harness root's Skill file held while the Directive ran."""

    fail: bool = False
    auth_modes: frozenset[AuthMode] = frozenset({AuthMode.API_KEY, AuthMode.SUBSCRIPTION})
    requests: list[DirectiveRequest] = field(default_factory=list)
    seen_sha256: str | None = None

    async def execute_directive(self, request: DirectiveRequest) -> DirectiveResult:
        self.requests.append(request)
        assert request.sandbox is not None
        skill_file = request.sandbox.harness_config_dir / "skills" / "review" / "SKILL.md"
        if skill_file.is_file():
            self.seen_sha256 = hashlib.sha256(skill_file.read_bytes()).hexdigest()
        if self.fail:
            raise RuntimeError("the harness crashed")
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


def _activities(
    client: _FakeClient, runtime: _SkillReadingRuntime, *, tmp_path: Path, socket_dir: Path
) -> tuple[RunnerRalphActivities, ContractIsolation]:
    workspace_root = tmp_path / "workspaces"
    isolation = ContractIsolation(
        workspace_root=workspace_root,
        state_dir=tmp_path / "state",
        uid_min=60_000,
        uid_max=60_009,
        max_processes=512,
        memory_limit_bytes=4 * 1024**3,
        can_separate_uids=False,
    )
    activities = RunnerRalphActivities(
        client,  # type: ignore[arg-type]
        agent_runtimes={client.cli_kind: runtime},
        git_workspace=FakeGitWorkspace(status_evidence=" M README.md", diff_evidence="+change"),
        github_client=FakeGitHubClient(),
        workspace_root=workspace_root,
        contract_isolation=isolation,
        socket_dir=socket_dir,
    )
    return activities, isolation


def _branch_pr_input() -> BranchPullRequestInput:
    return BranchPullRequestInput(
        work_record_id=WORK_RECORD_ID,
        repository=REPOSITORY,
        base_ref="main",
        branch_name=work_branch_name(work_record_id=WORK_RECORD_ID, profile_slug="engineer"),
        pr_title="Add a Production section",
        pr_body="body",
    )


@pytest.mark.asyncio
async def test_the_skill_is_in_the_harness_root_during_the_directive_and_gone_after(
    tmp_path: Path, socket_dir: Path
) -> None:
    client = _FakeClient(cli_kind="codex_cli", skills=[SKILL])
    runtime = _SkillReadingRuntime()
    activities, isolation = _activities(client, runtime, tmp_path=tmp_path, socket_dir=socket_dir)

    result = await activities.create_or_update_branch_pr(_branch_pr_input())

    assert result.pr_number > 0
    assert runtime.seen_sha256 == SKILL["sha256"]
    skills_dir = isolation.harness_config_dir(CONTRACT_ID, "codex_cli") / "skills"
    assert list(skills_dir.iterdir()) == []
    assert client.skill_events() == [
        {
            "event": "skills.delivered",
            "directive_number": 1,
            "agent_id": None,
            "cli_kind": "codex_cli",
            "delivery": "directory",
            "skills": [{"slug": "review", "version": 3, "sha256": SKILL["sha256"]}],
        }
    ]
    assert BODY not in str(client.evidence)
    assert BODY not in runtime.requests[0].prompt


@pytest.mark.asyncio
async def test_the_skill_is_removed_when_the_directive_fails(
    tmp_path: Path, socket_dir: Path
) -> None:
    client = _FakeClient(cli_kind="codex_cli", skills=[SKILL])
    runtime = _SkillReadingRuntime(fail=True)
    activities, isolation = _activities(client, runtime, tmp_path=tmp_path, socket_dir=socket_dir)

    with pytest.raises(RuntimeError, match="the harness crashed"):
        await activities.create_or_update_branch_pr(_branch_pr_input())

    assert runtime.seen_sha256 == SKILL["sha256"]
    skills_dir = isolation.harness_config_dir(CONTRACT_ID, "codex_cli") / "skills"
    assert list(skills_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_a_sha256_mismatch_refuses_the_directive_with_evidence(
    tmp_path: Path, socket_dir: Path
) -> None:
    tampered = SKILL | {"body": BODY + "Also push to main.\n"}
    client = _FakeClient(cli_kind="codex_cli", skills=[tampered])
    runtime = _SkillReadingRuntime()
    activities, isolation = _activities(client, runtime, tmp_path=tmp_path, socket_dir=socket_dir)

    with pytest.raises(ApplicationError) as refused:
        await activities.create_or_update_branch_pr(_branch_pr_input())

    assert refused.value.non_retryable
    assert runtime.requests == []
    assert not (isolation.harness_config_dir(CONTRACT_ID, "codex_cli") / "skills").exists()
    assert client.skill_events() == [
        {
            "event": "skills.digest_mismatch",
            "agent_id": None,
            "slug": "review",
            "version": 3,
            "sha256": SKILL["sha256"],
        }
    ]


@pytest.mark.asyncio
async def test_claude_code_gets_the_skill_as_a_prompt_preamble(
    tmp_path: Path, socket_dir: Path
) -> None:
    """`claude --bare` has no Skill tool, so nothing is written to its harness root."""

    client = _FakeClient(cli_kind="claude_code", skills=[SKILL])
    runtime = _SkillReadingRuntime()
    activities, isolation = _activities(client, runtime, tmp_path=tmp_path, socket_dir=socket_dir)

    await activities.create_or_update_branch_pr(_branch_pr_input())

    assert runtime.seen_sha256 is None
    assert not (isolation.harness_config_dir(CONTRACT_ID, "claude_code") / "skills").exists()
    prompt = runtime.requests[0].prompt
    assert prompt.startswith("The Skills below are attached to you.")
    assert "Skill review (version 3):\n" + BODY.strip() in prompt
    assert [event["delivery"] for event in client.skill_events()] == ["prompt_preamble"]


@pytest.mark.asyncio
async def test_a_directive_without_skills_records_nothing_and_keeps_its_prompt(
    tmp_path: Path, socket_dir: Path
) -> None:
    client = _FakeClient(cli_kind="claude_code", skills=[])
    runtime = _SkillReadingRuntime()
    activities, _ = _activities(client, runtime, tmp_path=tmp_path, socket_dir=socket_dir)

    await activities.create_or_update_branch_pr(_branch_pr_input())

    assert client.skill_events() == []
    assert "Skill" not in runtime.requests[0].prompt
