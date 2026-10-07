from pathlib import Path

import pytest

from agentic_runner.workers.command_policy import CommandPolicy, evaluate_command_policy

WORKSPACE_ROOT = Path("/var/lib/agentic-os/workspaces")
WORKSPACE = WORKSPACE_ROOT / "task-23" / "repo"


def _evaluate(argv: list[str], cwd: Path = WORKSPACE):
    return evaluate_command_policy(
        argv=argv,
        cwd=cwd,
        workspace_root=WORKSPACE_ROOT,
        base_branch="main",
        work_branch="task-23/codex-runtime",
        policy=CommandPolicy(),
    )


@pytest.mark.parametrize(
    "argv",
    [
        ["git", "status", "--short"],
        ["git", "diff", "--check"],
        ["python", "-m", "pytest", "tests/unit/test_codex_runtime.py"],
        ["uv", "run", "pytest", "tests/unit/test_codex_runtime.py", "-v"],
    ],
)
def test_allows_safe_workspace_commands(argv: list[str]) -> None:
    decision = _evaluate(argv)

    assert decision.allowed is True
    assert decision.reason == "allowed"


@pytest.mark.parametrize(
    ("argv", "expected_reason"),
    [
        (["rm", "-rf", "src"], "command is not allowlisted"),
        (["cat", "~/.codex/auth.json"], "secret read is denied"),
        (
            ["git", "diff", "--no-index", "/home/worker/.codex/auth.json", "README.md"],
            "secret read is denied",
        ),
        (["cat", "/var/run/docker.sock"], "host or container escape is denied"),
        (
            ["git", "diff", "--no-index", "/etc/passwd", "README.md"],
            "path argument must stay under workspace root",
        ),
        (["kubectl", "get", "pods"], "control-plane mutation or cluster command is denied"),
        (["git", "push", "origin", "main"], "protected branch mutation is denied"),
        (["git", "push", "origin", "HEAD:main"], "protected branch mutation is denied"),
        (["git", "push", "origin", "refs/heads/main"], "protected branch mutation is denied"),
        (
            ["git", "push", "origin", "feature:refs/heads/main"],
            "protected branch mutation is denied",
        ),
        (["git", "push", "origin", "+HEAD:main"], "protected branch mutation is denied"),
        (
            ["curl", "http://agentic-os/healthz"],
            "control-plane mutation or cluster command is denied",
        ),
        (
            ["curl", "http://localhost:8000/internal/tasks"],
            "control-plane mutation or cluster command is denied",
        ),
        (
            ["curl", "http://127.0.0.1:8000/internal/tasks"],
            "control-plane mutation or cluster command is denied",
        ),
        (["env", "OPENAI_API_KEY=sk-test", "codex", "exec"], "secret-looking argument is denied"),
    ],
)
def test_denies_disallowed_or_sensitive_commands(
    argv: list[str],
    expected_reason: str,
) -> None:
    decision = _evaluate(argv)

    assert decision.allowed is False
    assert decision.reason == expected_reason


def test_denies_commands_outside_workspace_root() -> None:
    decision = _evaluate(["git", "status"], cwd=Path("/tmp/repo"))

    assert decision.allowed is False
    assert decision.reason == "cwd must be under workspace root"


def _evaluate_runtime(argv: list[str], *, program: str = "codex", cwd: Path = WORKSPACE):
    return evaluate_command_policy(
        argv=argv,
        cwd=cwd,
        workspace_root=WORKSPACE_ROOT,
        base_branch="main",
        work_branch="task-23/codex-runtime",
        policy=CommandPolicy(runtime_program=program),
    )


def test_the_agent_runtime_cli_is_allowed_as_the_profiles_own_program() -> None:
    # ADR-0011 §12: the floor is applied to the Directive's own subprocess. The runtime's
    # argv is built by the Runner from worker settings, so its shape is not second-guessed
    # — but it is not in the workspace-command allow-list either, and would otherwise be
    # denied outright, leaving the floor with no call site at all.
    assert _evaluate_runtime(["codex", "exec", "--sandbox", "workspace-write", "-"]).allowed
    assert not _evaluate_runtime(["codex", "exec", "-"], program="claude").allowed


def test_the_real_codex_argv_with_json_is_allowed() -> None:
    # PRD issue 31 blocker: `_codex_argv` gained `--json` (research/29 §1.6, harness Usage
    # Records) after this floor was written -- drive the actual builder output through it
    # rather than a hand-written argv, so a future flag addition is checked here too.
    from agentic_runner.workers.codex_runtime import _codex_argv
    from agentic_runner.workers.settings import WorkerSettings

    settings = WorkerSettings(
        TEMPORAL_ADDRESS="127.0.0.1:7233",
        INTERNAL_FASTAPI_BASE_URL="http://agentic-api.internal:8000",
        WORKSPACE_ROOT=WORKSPACE_ROOT,
        CODEX_SANDBOX_MODE="workspace-write",
        CODEX_ASK_FOR_APPROVAL="never",
    )
    argv = _codex_argv(settings)

    assert "--json" in argv
    assert _evaluate_runtime(argv).allowed


@pytest.mark.parametrize(
    ("argv", "reason"),
    [
        (
            ["codex", "exec", "--config", "api_key=sk-live-1234"],
            "secret-looking argument is denied",
        ),
        (
            ["codex", "exec", "--config", "/var/run/docker.sock"],
            "host or container escape is denied",
        ),
    ],
)
def test_the_floor_still_bites_on_the_runtime_cli(argv: list[str], reason: str) -> None:
    decision = _evaluate_runtime(argv)

    assert not decision.allowed
    assert decision.reason == reason


def test_a_directive_outside_the_workspace_root_is_denied() -> None:
    # The runtime CLI's own path arguments are not second-guessed (its argv is the
    # Runner's), but where it runs is: a Directive must stay under the worker's
    # PVC-scoped workspace root.
    assert not _evaluate_runtime(["codex", "exec", "-"], cwd=Path("/tmp")).allowed
