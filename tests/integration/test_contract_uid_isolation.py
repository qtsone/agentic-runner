"""Two Contracts on one Runner, really separated (ADR-0015 §1, PRD issue 30 AC1).

This is the only test that actually changes uid, so it is the only one that can prove the
claim: a Directive of Contract A cannot read Contract B's Workspace or its harness config
root. It spawns through the production seam (``workers/_runtime_support.run_subprocess_exec``
with the sandbox the Runner builds), not a hand-rolled ``subprocess.run``.

It needs ``CAP_SETUID``, which a GitHub-hosted runner's unprivileged user does not have,
so it skips without it. A skip that nobody notices is the same as no test, so CI's
``contract-uid-isolation`` job sets ``AGENTIC_OS_REQUIRE_CONTRACT_UID_ISOLATION=1`` inside
a root container and the skip becomes a failure there.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

from agentic_runner.integrations.git.workspace import (
    GitWorkspacePolicyError,
    require_runner_owned_git_dir,
)
from agentic_runner.workers._runtime_support import run_subprocess_exec
from agentic_runner.workers.acp_runtime import AcpRuntime
from agentic_runner.workers.agent_runtime import AuthMode, DirectiveRequest
from agentic_runner.workers.contract_isolation import ContractIsolation
from agentic_runner.workers.settings import WorkerSettings
from agentic_runner.workers.skills import SkillDeliveryError, remove_skills, write_skills
from agentic_runner_contracts.runtime_context import SkillVersionSpec

CONTRACT_A = "11111111-2222-4333-8444-555555555555"
CONTRACT_B = "66666666-7777-4888-8999-aaaaaaaaaaaa"
WORK_RECORD_A = "123e4567-e89b-12d3-a456-426614174000"
WORK_RECORD_B = "223e4567-e89b-12d3-a456-426614174001"

_REQUIRE_ENV = "AGENTIC_OS_REQUIRE_CONTRACT_UID_ISOLATION"


def require_uid_separation() -> None:
    """Skip where the Runner cannot change uid — unless CI says it must be able to."""

    if os.geteuid() == 0:
        return
    if os.environ.get(_REQUIRE_ENV) == "1":
        pytest.fail(
            f"{_REQUIRE_ENV} is set but this process lacks CAP_SETUID: the Contract uid "
            "gate cannot silently stop running"
        )
    pytest.skip("Contract uid separation needs CAP_SETUID (root inside the container)")


def test_the_skip_is_a_failure_on_the_job_that_must_run_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard above, guarded: the skip must not be able to quietly swallow CI."""

    monkeypatch.setattr(os, "geteuid", lambda: 1001)
    monkeypatch.setenv(_REQUIRE_ENV, "1")

    with pytest.raises(pytest.fail.Exception, match="CAP_SETUID"):
        require_uid_separation()

    # ...and stays a plain skip on a developer laptop, which has no capability to give.
    monkeypatch.delenv(_REQUIRE_ENV)
    with pytest.raises(pytest.skip.Exception, match="CAP_SETUID"):
        require_uid_separation()


def _make_traversable(leaf: Path) -> None:
    """pytest's tmp dirs are 0700; a Contract uid must at least be able to walk to them.

    Stops at the first ancestor others can already traverse rather than at the temp root:
    tests/conftest.py points the temp root at tmp_path itself, so a temp-root boundary
    would chmod nothing.
    """

    for parent in (leaf, *leaf.parents):
        if parent.stat().st_mode & 0o001 or parent == Path(parent.root):
            break
        parent.chmod(0o755)


async def _spawn(argv: list[str], *, cwd: Path, sandbox: object) -> tuple[int, str, str]:
    result = await run_subprocess_exec(
        argv=argv,
        cwd=cwd,
        env={"PATH": "/usr/bin:/bin"},
        stdin=None,
        timeout_seconds=30,
        output_limit_bytes=4096,
        sandbox=sandbox,  # type: ignore[arg-type]
    )
    return result.exit_code, result.stdout.strip(), result.stderr.strip()


@pytest.mark.asyncio
async def test_a_directive_of_one_contract_cannot_read_another_contracts_tree(
    tmp_path: Path,
) -> None:
    require_uid_separation()

    isolation = ContractIsolation(
        workspace_root=tmp_path / "workspaces",
        state_dir=tmp_path / "state",
        uid_min=60_000,
        uid_max=60_009,
        max_processes=512,
        memory_limit_bytes=4 * 1024**3,
    )
    _make_traversable(tmp_path)

    workspace_a = isolation.prepare_workspace(CONTRACT_A, WORK_RECORD_A)
    workspace_b = isolation.prepare_workspace(CONTRACT_B, WORK_RECORD_B)
    sandbox_a = isolation.sandbox(CONTRACT_A, runtime_kind="codex_cli")
    sandbox_b = isolation.sandbox(CONTRACT_B, runtime_kind="codex_cli")
    secret_b = sandbox_b.harness_config_dir / "auth.json"
    secret_b.write_text('{"refresh_token": "contract-b"}')
    os.chown(secret_b, sandbox_b.uid or 0, sandbox_b.gid or 0)
    (workspace_b / "NOTES.md").write_text("contract b's checkout")
    os.chown(workspace_b / "NOTES.md", sandbox_b.uid or 0, sandbox_b.gid or 0)

    # The Directive really runs as the Contract's own uid, and two Contracts are two uids.
    exit_code, stdout, _ = await _spawn(["id", "-u"], cwd=workspace_a, sandbox=sandbox_a)
    assert (exit_code, stdout) == (0, str(sandbox_a.uid))
    assert sandbox_a.uid != sandbox_b.uid

    # It can read its own harness config root — otherwise the refusals below prove nothing.
    own_root = sandbox_a.harness_config_dir / "auth.json"
    own_root.write_text('{"refresh_token": "contract-a"}')
    os.chown(own_root, sandbox_a.uid or 0, sandbox_a.gid or 0)
    exit_code, stdout, _ = await _spawn(["cat", str(own_root)], cwd=workspace_a, sandbox=sandbox_a)
    assert exit_code == 0
    assert "contract-a" in stdout

    # ...and it cannot read the other Contract's harness config root (ADR-0015 §4)...
    exit_code, stdout, stderr = await _spawn(
        ["cat", str(secret_b)], cwd=workspace_a, sandbox=sandbox_a
    )
    assert exit_code != 0
    assert "contract-b" not in stdout
    assert "Permission denied" in stderr

    # ...nor its Workspace (ADR-0015 §2).
    exit_code, stdout, stderr = await _spawn(
        ["cat", str(workspace_b / "NOTES.md")], cwd=workspace_a, sandbox=sandbox_a
    )
    assert exit_code != 0
    assert "Permission denied" in stderr

    exit_code, stdout, stderr = await _spawn(
        ["ls", str(workspace_b)], cwd=workspace_a, sandbox=sandbox_a
    )
    assert exit_code != 0
    assert "Permission denied" in stderr


@pytest.mark.asyncio
async def test_the_acp_bridge_and_its_harness_run_as_the_contracts_uid(tmp_path: Path) -> None:
    """Local-agents 12: the bridge is spawned like a per-CLI harness, so the same line holds.

    A fake ACP agent stands in for the bridge and records its own uid, the uid of a child
    it spawns (the harness, in production) and what it can read of both Contracts' roots.
    """

    require_uid_separation()

    isolation = ContractIsolation(
        workspace_root=tmp_path / "workspaces",
        state_dir=tmp_path / "state",
        uid_min=60_000,
        uid_max=60_009,
        max_processes=512,
        memory_limit_bytes=4 * 1024**3,
    )
    _make_traversable(tmp_path)
    workspace_a = isolation.prepare_workspace(CONTRACT_A, WORK_RECORD_A)
    isolation.prepare_workspace(CONTRACT_B, WORK_RECORD_B)
    sandbox_a = isolation.sandbox(CONTRACT_A, runtime_kind="codex_cli")
    sandbox_b = isolation.sandbox(CONTRACT_B, runtime_kind="codex_cli")
    secret_b = sandbox_b.harness_config_dir / "auth.json"
    secret_b.write_text('{"refresh_token": "contract-b"}')
    os.chown(secret_b, sandbox_b.uid or 0, sandbox_b.gid or 0)
    record = tmp_path / "record.jsonl"
    record.touch(mode=0o666)
    record.chmod(0o666)
    scenario = tmp_path / "scenario.json"
    scenario.write_text(json.dumps({"record": str(record), "probe_reads": [str(secret_b)]}))
    scenario.chmod(0o644)
    fake_agent = Path(__file__).parents[1] / "fixtures" / "fake_acp_agent.py"
    runtime = AcpRuntime(
        cli_kind="codex_cli",
        settings=WorkerSettings(
            TEMPORAL_ADDRESS="127.0.0.1:7233",
            INTERNAL_FASTAPI_BASE_URL="http://agentic-api.internal:8000",
            WORKSPACE_ROOT=tmp_path / "workspaces",
            CODEX_HOME=tmp_path / "codex-home",
        ),
        bridge_argv=[sys.executable, str(fake_agent)],
    )

    result = await runtime.execute_directive(
        DirectiveRequest(
            workspace_path=workspace_a,
            prompt="Implement the change.",
            base_branch="main",
            work_branch="agent/wr-a",
            sandbox=sandbox_a,
            extra_env=(
                ("FAKE_ACP_SCENARIO", str(scenario)),
                ("OPENAI_BASE_URL", "http://127.0.0.1:4000/attempt/a/v1"),
                ("OPENAI_API_KEY", "attempt-bearer-not-a-secret"),
            ),
            auth_mode=AuthMode.API_KEY,
        )
    )

    assert result.exit_code == 0, result.error
    entries = [json.loads(line) for line in record.read_text().splitlines()]
    seen = next(entry for entry in entries if "uid" in entry)
    assert seen["uid"] == sandbox_a.uid
    assert seen["child_uid"] == str(sandbox_a.uid)
    assert seen["reads"][str(secret_b)] == "PermissionError"
    # The per-attempt provider root is the Contract's own, readable by the bridge it serves.
    started_on = next(entry for entry in entries if "codex_config" in entry)
    assert 'model_provider = "agentic_runner"' in started_on["codex_config"]


@pytest.mark.asyncio
async def test_the_spawn_floor_binds_the_contracts_processes(tmp_path: Path) -> None:
    """``RLIMIT_NPROC`` and the memory ceiling are on the process, not just in a config."""

    require_uid_separation()

    isolation = ContractIsolation(
        workspace_root=tmp_path / "workspaces",
        state_dir=tmp_path / "state",
        uid_min=60_010,
        uid_max=60_019,
        max_processes=64,
        memory_limit_bytes=2 * 1024**3,
    )
    _make_traversable(tmp_path)
    workspace = isolation.prepare_workspace(CONTRACT_A, WORK_RECORD_A)
    sandbox = isolation.sandbox(CONTRACT_A, runtime_kind="codex_cli")

    # Read off the kernel rather than a shell builtin: `ulimit` is inconsistent between
    # shells about which resources it can report, and this must not pass vacuously.
    exit_code, stdout, _ = await _spawn(
        ["cat", "/proc/self/limits"], cwd=workspace, sandbox=sandbox
    )

    assert exit_code == 0
    limits = {
        line.split("  ")[0].strip(): line.split()
        for line in stdout.splitlines()
        if line.startswith("Max ")
    }
    assert limits["Max processes"][2:4] == ["64", "64"]
    assert limits["Max data size"][3:5] == [str(2 * 1024**3), str(2 * 1024**3)]


@pytest.mark.asyncio
async def test_a_directive_cannot_write_the_git_directory_the_runner_runs_git_in(
    tmp_path: Path,
) -> None:
    """The boundary in the loop the Ralph Directive actually runs (ADR-0015 §1).

    One ``create_or_update_branch_pr`` hands the checkout to the Contract and then runs
    the Runner's own git over it again — ``collect_git_evidence``, ``commit_all``,
    ``push_branch`` — as the Runner. ``.git`` is the part of that checkout git reads as
    commands (hooks, ``core.fsmonitor``, ``core.hooksPath``, ``filter.*.clean``), so a
    Directive that could write it would be executing as the Runner, with CAP_SETUID and
    CAP_DAC_OVERRIDE, on the very next git call.
    """

    require_uid_separation()

    isolation = ContractIsolation(
        workspace_root=tmp_path / "workspaces",
        state_dir=tmp_path / "state",
        uid_min=60_020,
        uid_max=60_029,
        max_processes=512,
        memory_limit_bytes=4 * 1024**3,
    )
    _make_traversable(tmp_path)
    workspace = isolation.prepare_workspace(CONTRACT_A, WORK_RECORD_A)
    # What the Runner's own clone/checkout leaves behind, as the Runner.
    (workspace / ".git" / "hooks").mkdir(parents=True)
    (workspace / ".git" / "config").write_text("[core]\n")
    (workspace / "src").mkdir()
    (workspace / "src" / "app.py").write_text("x = 1\n")

    isolation.hand_workspace_to_contract(CONTRACT_A, workspace)
    sandbox = isolation.sandbox(CONTRACT_A, runtime_kind="codex_cli")

    # The Directive owns the worktree — it is there to edit it...
    exit_code, _stdout, stderr = await _spawn(
        ["touch", str(workspace / "src" / "app.py")], cwd=workspace, sandbox=sandbox
    )
    assert exit_code == 0, stderr

    # ...and owns none of the git metadata.
    for target in (".git/hooks/pre-commit", ".git/config"):
        exit_code, _stdout, stderr = await _spawn(
            ["touch", str(workspace / target)], cwd=workspace, sandbox=sandbox
        )
        assert exit_code != 0, f"the Contract could write {target}"
        assert "Permission denied" in stderr


@pytest.mark.asyncio
async def test_a_directive_that_swaps_the_git_directory_stops_the_runners_next_git(
    tmp_path: Path,
) -> None:
    """Not writing ``.git`` is not the same as not replacing it (ADR-0015 §1).

    The Contract owns the Workspace directory that *contains* ``.git``, and on POSIX
    rename/create/unlink of an entry is governed by write+execute on the parent, never by
    the entry's own ownership. So the ``mv`` below really does succeed — the boundary
    cannot be ownership of ``.git`` alone. What closes it is the Runner re-checking that
    ``.git`` is still its own directory before it runs git there, which is exactly the
    check its ``-c safe.directory`` flag suppresses.
    """

    require_uid_separation()

    isolation = ContractIsolation(
        workspace_root=tmp_path / "workspaces",
        state_dir=tmp_path / "state",
        uid_min=60_030,
        uid_max=60_039,
        max_processes=512,
        memory_limit_bytes=4 * 1024**3,
    )
    _make_traversable(tmp_path)
    workspace = isolation.prepare_workspace(CONTRACT_A, WORK_RECORD_A)
    (workspace / ".git" / "hooks").mkdir(parents=True)
    (workspace / ".git" / "config").write_text("[core]\n")

    isolation.hand_workspace_to_contract(CONTRACT_A, workspace)
    sandbox = isolation.sandbox(CONTRACT_A, runtime_kind="codex_cli")
    assert require_runner_owned_git_dir(workspace) is None

    exit_code, _stdout, stderr = await _spawn(
        [
            "sh",
            "-c",
            f"mv {workspace}/.git {workspace}/.git.stash "
            f"&& cp -r {workspace}/.git.stash {workspace}/.git "
            f"&& echo evil > {workspace}/.git/hooks/pre-commit",
        ],
        cwd=workspace,
        sandbox=sandbox,
    )

    assert exit_code == 0, f"the escape is real and this test must exercise it: {stderr}"
    assert (workspace / ".git").stat().st_uid == sandbox.uid
    with pytest.raises(GitWorkspacePolicyError, match="tampered"):
        require_runner_owned_git_dir(workspace)


@pytest.mark.asyncio
async def test_skills_are_written_as_the_contracts_uid_and_never_through_its_symlink(
    tmp_path: Path,
) -> None:
    """Console-v2 issue 28: the harness root is the Contract's, so it may hold a symlink."""

    require_uid_separation()
    isolation = ContractIsolation(
        workspace_root=tmp_path / "workspaces",
        state_dir=tmp_path / "state",
        uid_min=60_030,
        uid_max=60_039,
        max_processes=512,
        memory_limit_bytes=4 * 1024**3,
    )
    _make_traversable(tmp_path)
    sandbox = isolation.sandbox(CONTRACT_A, runtime_kind="codex_cli")
    body = "Read the diff first.\n"
    skill = SkillVersionSpec(
        slug="review", version=1, sha256=hashlib.sha256(body.encode()).hexdigest(), body=body
    )
    skills_dir = sandbox.harness_config_dir / "skills"

    await write_skills(sandbox.harness_config_dir, [skill], uid=sandbox.uid)
    written = skills_dir / "review" / "SKILL.md"
    assert written.stat().st_uid == sandbox.uid
    assert written.read_text() == body
    await remove_skills(sandbox.harness_config_dir, [skill], uid=sandbox.uid)
    assert not written.parent.exists()

    runner_only = tmp_path / "runner-only"
    runner_only.mkdir()
    runner_only.chmod(0o755)
    skills_dir.rmdir()
    skills_dir.symlink_to(runner_only)
    with pytest.raises(SkillDeliveryError):
        await write_skills(sandbox.harness_config_dir, [skill], uid=sandbox.uid)
    assert list(runner_only.iterdir()) == []
