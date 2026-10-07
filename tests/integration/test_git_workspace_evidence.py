from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agentic_runner.integrations.git.contracts import (
    CheckoutWorkBranchRequest,
    CloneWorkspaceRequest,
    CommitAllRequest,
    FetchBranchRequest,
)
from agentic_runner.integrations.git.evidence import (
    build_git_evidence,
    collect_git_evidence,
    redact_git_evidence_text,
)
from agentic_runner.integrations.git.workspace import GitWorkspacePolicyError, LocalGitWorkspace

VALID_WORK_BRANCH = "agent/123e4567-e89b-12d3-a456-426614174000-fix-thing"
EVIDENCE_SOURCE = (
    Path(__file__).resolve().parents[2]
    / "packages/runner/src/agentic_runner/integrations/git/evidence.py"
)


def _run_git(args: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        shell=False,
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )


def _seed_bare_remote(tmp_path: Path, *, base_branch: str = "main") -> Path:
    remote_path = tmp_path / "remote.git"
    seed_path = tmp_path / "seed"
    _run_git(["init", "--bare", str(remote_path)])
    _run_git(["init", "-b", base_branch, str(seed_path)])
    _run_git(["config", "user.name", "Seed User"], cwd=seed_path)
    _run_git(["config", "user.email", "seed@example.test"], cwd=seed_path)
    (seed_path / "README.md").write_text("seed\n", encoding="utf-8")
    _run_git(["add", "README.md"], cwd=seed_path)
    _run_git(["commit", "-m", "seed"], cwd=seed_path)
    _run_git(["remote", "add", "origin", str(remote_path)], cwd=seed_path)
    _run_git(["push", "origin", f"HEAD:{base_branch}"], cwd=seed_path)
    return remote_path


def _prepare_workspace(tmp_path: Path) -> tuple[Path, Path]:
    remote_path = _seed_bare_remote(tmp_path)
    root = tmp_path / "workers"
    workspace_path = root / "record-1"
    root.mkdir()
    workspace = LocalGitWorkspace(allow_local_file_remotes=True)
    workspace.clone_repository(
        CloneWorkspaceRequest(
            repo_full_name="qts/agentic-os",
            remote_url=str(remote_path),
            workspace_root=root,
            workspace_path=workspace_path,
        )
    )
    workspace.fetch_base_branch(
        FetchBranchRequest(
            repo_full_name="qts/agentic-os",
            workspace_path=workspace_path,
            base_branch="main",
        )
    )
    workspace.checkout_work_branch(
        CheckoutWorkBranchRequest(
            repo_full_name="qts/agentic-os",
            workspace_path=workspace_path,
            base_branch="main",
            work_branch=VALID_WORK_BRANCH,
        )
    )
    return root, workspace_path


def _combined_evidence_bytes(*, status: str, diff: str, stderr: str) -> int:
    return len(status.encode()) + len(diff.encode()) + len(stderr.encode())


def _assert_private_key_material_redacted(text: str) -> None:
    assert "BEGIN" not in text
    assert "PRIVATE KEY" not in text
    assert "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC" not in text
    assert "openssh-key-v1" not in text
    assert "Proc-Type: 4,ENCRYPTED" not in text
    assert "[REDACTED_PRIVATE_KEY]" in text


@pytest.mark.parametrize(
    "private_key_material",
    (
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC",
        "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAABG5vbmU=\nopenssh-key-v1",
        "-----BEGIN EC PRIVATE KEY-----\nMHcCAQEEIBodyPrefix",
        "-----BEGIN DSA PRIVATE KEY-----\nMIIBuwIBAAKBgQCBodyPrefix",
        "-----BEGIN ENCRYPTED PRIVATE KEY-----\nProc-Type: 4,ENCRYPTED\nDEK-Info: AES-256-CBC",
        "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n-----END PRIVATE KEY-----",
    ),
)
def test_redact_git_evidence_text_redacts_complete_and_truncated_private_keys(
    private_key_material: str,
) -> None:
    redacted = redact_git_evidence_text(f"before\n{private_key_material}")

    assert "before" in redacted
    _assert_private_key_material_redacted(redacted)


def test_build_git_evidence_redacts_truncated_private_key_diff(tmp_path: Path) -> None:
    evidence = build_git_evidence(
        workspace_path=tmp_path,
        status=" M key.txt\n",
        diff=(
            "diff --git a/key.txt b/key.txt\n"
            "+-----BEGIN RSA PRIVATE KEY-----\n"
            "+MIIEvQIBADANBgkqhkiG9w0BAQEFAASC"
        ),
        output_limit_bytes=1024,
    )

    _assert_private_key_material_redacted(evidence.diff)


def test_redact_git_evidence_text_redacts_truncated_credentialed_url_userinfo() -> None:
    redacted = redact_git_evidence_text(
        "remote=https://x-token-auth:REMOTE_TOKEN_WITHOUT_AT_OR_HOST"
    )
    generic_redacted = redact_git_evidence_text("remote=https://alice:SUPERSECRET")
    dotted_user_redacted = redact_git_evidence_text("remote=https://alice.dev:SUPERSECRET")
    numeric_redacted = redact_git_evidence_text("remote=https://alice:123456")
    zero_redacted = redact_git_evidence_text("remote=https://alice:000000")
    ordinary_url = redact_git_evidence_text("remote=https://example.test:443/repo.git")
    localhost_url = redact_git_evidence_text("remote=https://localhost:8080/repo.git")

    assert "REMOTE_TOKEN_WITHOUT_AT_OR_HOST" not in redacted
    assert "https://[REDACTED]" in redacted
    assert "SUPERSECRET" not in generic_redacted
    assert generic_redacted == "remote=https://[REDACTED]"
    assert dotted_user_redacted == "remote=https://[REDACTED]"
    assert numeric_redacted == "remote=https://[REDACTED]"
    assert zero_redacted == "remote=https://[REDACTED]"
    assert ordinary_url == "remote=https://example.test:443/repo.git"
    assert localhost_url == "remote=https://localhost:8080/repo.git"


def test_redact_git_evidence_text_redacts_sensitive_key_values_with_quoted_spaces() -> None:
    redacted = redact_git_evidence_text(
        "password=plain-secret\npassword=\"secret with spaces\"\napi_key='key with spaces'"
    )

    assert "plain-secret" not in redacted
    assert "secret with spaces" not in redacted
    assert "key with spaces" not in redacted
    assert "password=[REDACTED]" in redacted
    assert 'password="[REDACTED]"' in redacted
    assert "api_key='[REDACTED]'" in redacted


def test_redact_git_evidence_text_redacts_common_secret_like_material() -> None:
    openai_key = "sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890"
    github_token = "ghp_abcdefghijklmnopqrstuvwxyzABCDEF123456"
    # Assembled so the public repository's secret scanner does not read the fixture as a token.
    slack_token = "xox" + "b-123456789012-123456789012-abcdefghijklmnopqrstuvwx"
    bearer_jwt = (
        "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
        "eyJzdWIiOiIxMjM0NTY3ODkwIn0."
        "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    )
    bare_jwt = (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
        "eyJzdWIiOiIxMjM0NTY3ODkwIn0."
        "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    )
    private_key = (
        "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n-----END PRIVATE KEY-----"
    )

    redacted = redact_git_evidence_text(
        "before context\n"
        f"openai={openai_key}\n"
        f"github {github_token}\n"
        f"slack {slack_token}\n"
        f"auth {bearer_jwt}\n"
        f"bare {bare_jwt}\n"
        f"key\n{private_key}\n"
        "url https://alice:correct-horse-battery-staple@example.test/repo.git\n"
        "token=literal-token\n"
        "api_key: literal-api-key\n"
        "password=literal-password\n"
        "secret: literal-secret\n"
        "after context"
    )

    assert "before context" in redacted
    assert "after context" in redacted
    for secret in (
        openai_key,
        github_token,
        slack_token,
        bearer_jwt,
        bare_jwt,
        "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC",
        "correct-horse-battery-staple",
        "literal-token",
        "literal-api-key",
        "literal-password",
        "literal-secret",
    ):
        assert secret not in redacted
    assert "https://[REDACTED]@example.test/repo.git" in redacted
    assert "token=[REDACTED]" in redacted
    assert "api_key: [REDACTED]" in redacted
    assert "password=[REDACTED]" in redacted
    assert "secret: [REDACTED]" in redacted


def test_collect_git_evidence_returns_bounded_redacted_status_and_diff(
    tmp_path: Path,
) -> None:
    root, workspace_path = _prepare_workspace(tmp_path)
    bare_jwt = (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
        "eyJzdWIiOiIxMjM0NTY3ODkwIn0."
        "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    )

    (workspace_path / "README.md").write_text(
        "generated change\n"
        "api_key = 'sk_live_DO_NOT_LEAK'\n"
        "password=plain-secret\n"
        f"raw_jwt={bare_jwt}\n"
        "remote=https://x-token-auth:REMOTE-TOKEN@example.test/repo.git\n"
        "-----BEGIN PRIVATE KEY-----\nprivate material\n-----END PRIVATE KEY-----\n",
        encoding="utf-8",
    )

    evidence = collect_git_evidence(
        workspace_path=workspace_path,
        workspace_root=root,
        output_limit_bytes=2048,
    )
    bounded_evidence = collect_git_evidence(
        workspace_path=workspace_path,
        workspace_root=root,
        output_limit_bytes=64,
    )

    combined = f"{evidence.status}\n{evidence.diff}\n{evidence.stderr}"
    assert "README.md" in evidence.status
    assert "README.md" in evidence.diff
    assert len(bounded_evidence.status.encode()) <= 64
    assert len(bounded_evidence.diff.encode()) <= 64
    assert "sk_live_DO_NOT_LEAK" not in combined
    assert "plain-secret" not in combined
    assert bare_jwt not in combined
    assert "REMOTE-TOKEN" not in combined
    assert "BEGIN PRIVATE KEY" not in combined
    assert "[REDACTED]" in combined
    assert (
        _combined_evidence_bytes(
            status=bounded_evidence.status,
            diff=bounded_evidence.diff,
            stderr=bounded_evidence.stderr,
        )
        <= 64
    )


def test_collect_git_evidence_does_not_execute_repo_local_fsmonitor(
    tmp_path: Path,
) -> None:
    root, workspace_path = _prepare_workspace(tmp_path)
    sentinel_path = tmp_path / "fsmonitor-executed"
    fsmonitor_path = tmp_path / "fsmonitor-hook.sh"
    fsmonitor_path.write_text(
        f"#!/bin/sh\ntouch {sentinel_path}\nexit 0\n",
        encoding="utf-8",
    )
    fsmonitor_path.chmod(0o755)
    _run_git(["config", "core.fsmonitor", str(fsmonitor_path)], cwd=workspace_path)

    evidence = collect_git_evidence(
        workspace_path=workspace_path,
        workspace_root=root,
        output_limit_bytes=2048,
    )

    assert evidence.status_returncode == 0
    assert not sentinel_path.exists()


def _embed_hostile_repo(workspace_path: Path, sentinel_path: Path) -> None:
    """What a Directive can do to a checkout it owns but does not own the ``.git`` of.

    ``git init`` inside the Workspace makes a second repository whose config the Contract
    writes. The Runner's own ``git add --all`` records it as a gitlink, and from then on
    every ``status``/``diff`` the Runner runs would descend into it — executing that
    config as the Runner (ADR-0015 §1). git's dubious-ownership check does not fire there:
    a submodule is opened from the superproject, not discovered from a directory.
    """

    hook_path = workspace_path.parent / "fsmonitor-hook.sh"
    hook_path.write_text(f"#!/bin/sh\ntouch {sentinel_path}\nexit 0\n", encoding="utf-8")
    hook_path.chmod(0o755)
    embedded = workspace_path / "vendored"
    embedded.mkdir()
    _run_git(["init", "-b", "main", "."], cwd=embedded)
    (embedded / "f.txt").write_text("x\n", encoding="utf-8")
    _run_git(["add", "f.txt"], cwd=embedded)
    _run_git(["-c", "user.name=E", "-c", "user.email=e@e.test", "commit", "-m", "e"], cwd=embedded)
    _run_git(["config", "core.fsmonitor", str(hook_path)], cwd=embedded)


def test_an_embedded_repository_never_runs_its_own_config_as_the_runner(
    tmp_path: Path,
) -> None:
    root, workspace_path = _prepare_workspace(tmp_path)
    sentinel_path = tmp_path / "embedded-fsmonitor-executed"
    _embed_hostile_repo(workspace_path, sentinel_path)
    workspace = LocalGitWorkspace(allow_local_file_remotes=True)
    workspace.clone_repository(
        CloneWorkspaceRequest(
            repo_full_name="qts/agentic-os",
            remote_url=str(tmp_path / "remote.git"),
            workspace_root=root,
            workspace_path=workspace_path,
        )
    )

    # One Directive's commit puts the embedded repository in the index as a gitlink...
    workspace.commit_all(
        CommitAllRequest(
            repo_full_name="qts/agentic-os",
            workspace_path=workspace_path,
            work_branch=VALID_WORK_BRANCH,
            commit_message="embed",
        )
    )

    assert not sentinel_path.exists()

    # ...and the next Directive's evidence read is the one that would descend into it.
    evidence = collect_git_evidence(
        workspace_path=workspace_path,
        workspace_root=root,
        output_limit_bytes=4096,
    )

    assert evidence.status_returncode == 0
    assert not sentinel_path.exists()


def test_collect_git_evidence_enforces_one_total_output_budget(tmp_path: Path) -> None:
    root, workspace_path = _prepare_workspace(tmp_path)
    (workspace_path / "README.md").write_text("tracked change\n" * 200, encoding="utf-8")
    (workspace_path / "new-file.txt").write_text("untracked change\n" * 200, encoding="utf-8")

    evidence = collect_git_evidence(
        workspace_path=workspace_path,
        workspace_root=root,
        output_limit_bytes=256,
    )

    assert (
        _combined_evidence_bytes(
            status=evidence.status,
            diff=evidence.diff,
            stderr=evidence.stderr,
        )
        <= 256
    )
    assert "README.md" in evidence.status
    assert "README.md" in evidence.diff


def test_collect_git_evidence_includes_staged_tracked_changes(tmp_path: Path) -> None:
    root, workspace_path = _prepare_workspace(tmp_path)
    (workspace_path / "README.md").write_text("staged generated change\n", encoding="utf-8")
    _run_git(["add", "README.md"], cwd=workspace_path)

    evidence = collect_git_evidence(
        workspace_path=workspace_path,
        workspace_root=root,
        output_limit_bytes=2048,
    )

    assert "README.md" in evidence.status
    assert "README.md" in evidence.diff
    assert "staged generated change" in evidence.diff


def test_collect_git_evidence_includes_untracked_text_without_following_escape_symlink(
    tmp_path: Path,
) -> None:
    root, workspace_path = _prepare_workspace(tmp_path)
    (workspace_path / "new-file.txt").write_text(
        "untracked generated change\napi_key=sk_live_UNTRACKED_SECRET\n",
        encoding="utf-8",
    )
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("OUTSIDE_SECRET_MUST_NOT_APPEAR\n", encoding="utf-8")
    (workspace_path / "outside-link.txt").symlink_to(outside)

    evidence = collect_git_evidence(
        workspace_path=workspace_path,
        workspace_root=root,
        output_limit_bytes=2048,
    )

    assert "new-file.txt" in evidence.status
    assert "new-file.txt" in evidence.diff
    assert "untracked generated change" in evidence.diff
    assert "sk_live_UNTRACKED_SECRET" not in evidence.diff
    assert "api_key=[REDACTED]" in evidence.diff
    assert "outside-link.txt" in evidence.status
    assert "OUTSIDE_SECRET_MUST_NOT_APPEAR" not in evidence.diff


def test_collect_git_evidence_escapes_control_characters_in_untracked_paths(
    tmp_path: Path,
) -> None:
    root, workspace_path = _prepare_workspace(tmp_path)
    injected_name = "safe\nFORGED-DIFF-LINE.txt"
    (workspace_path / injected_name).write_text("generated change\n", encoding="utf-8")

    evidence = collect_git_evidence(
        workspace_path=workspace_path,
        workspace_root=root,
        output_limit_bytes=2048,
    )

    assert "safe\\nFORGED-DIFF-LINE.txt" in evidence.diff
    assert "\nFORGED-DIFF-LINE.txt" not in evidence.diff
    assert "diff --git a/safe\n" not in evidence.diff


def test_collect_git_evidence_does_not_execute_textconv_for_diffs(tmp_path: Path) -> None:
    root, workspace_path = _prepare_workspace(tmp_path)
    sentinel = workspace_path / "textconv-executed"
    script = workspace_path / "textconv.sh"
    script.write_text(f'#!/bin/sh\ntouch {sentinel}\ncat "$1"\n', encoding="utf-8")
    script.chmod(0o700)
    (workspace_path / ".gitattributes").write_text("README.md diff=evil\n", encoding="utf-8")
    _run_git(["config", "diff.evil.textconv", str(script)], cwd=workspace_path)
    _run_git(["add", ".gitattributes"], cwd=workspace_path)
    (workspace_path / "README.md").write_text("unstaged textconv probe\n", encoding="utf-8")

    unstaged = collect_git_evidence(
        workspace_path=workspace_path,
        workspace_root=root,
        output_limit_bytes=2048,
    )
    assert "unstaged textconv probe" in unstaged.diff
    assert not sentinel.exists()

    _run_git(["add", "README.md"], cwd=workspace_path)
    staged = collect_git_evidence(
        workspace_path=workspace_path,
        workspace_root=root,
        output_limit_bytes=2048,
    )

    assert "unstaged textconv probe" in staged.diff
    assert not sentinel.exists()


def test_collect_git_evidence_redacts_truncated_private_key_from_untracked_file(
    tmp_path: Path,
) -> None:
    root, workspace_path = _prepare_workspace(tmp_path)
    (workspace_path / "key.txt").write_text(
        "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAABG5vbmU=\nopenssh-key-v1",
        encoding="utf-8",
    )

    evidence = collect_git_evidence(
        workspace_path=workspace_path,
        workspace_root=root,
        output_limit_bytes=2048,
    )

    assert "key.txt" in evidence.status
    assert "key.txt" in evidence.diff
    _assert_private_key_material_redacted(evidence.diff)


def test_collect_git_evidence_bounds_large_untracked_file_and_avoids_capture_output(
    tmp_path: Path,
) -> None:
    root, workspace_path = _prepare_workspace(tmp_path)
    (workspace_path / "large-untracked.txt").write_text(
        "large generated change\n" * 10_000,
        encoding="utf-8",
    )

    evidence = collect_git_evidence(
        workspace_path=workspace_path,
        workspace_root=root,
        output_limit_bytes=1024,
    )
    source_text = EVIDENCE_SOURCE.read_text(encoding="utf-8")

    assert "capture_output=True" not in source_text
    assert (
        _combined_evidence_bytes(
            status=evidence.status,
            diff=evidence.diff,
            stderr=evidence.stderr,
        )
        <= 1024
    )
    assert "large-untracked.txt" in evidence.status
    assert "large-untracked.txt" in evidence.diff


def test_collect_git_evidence_rejects_workspace_path_escape(tmp_path: Path) -> None:
    root = tmp_path / "workers"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    with pytest.raises(GitWorkspacePolicyError):
        collect_git_evidence(
            workspace_path=outside,
            workspace_root=root,
            output_limit_bytes=1024,
        )
