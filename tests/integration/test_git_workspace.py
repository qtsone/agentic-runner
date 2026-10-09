from __future__ import annotations

import subprocess
from inspect import signature
from pathlib import Path

import pytest

from agentic_runner.integrations.git.contracts import (
    DEFAULT_COMMIT_AUTHOR_EMAIL,
    DEFAULT_COMMIT_AUTHOR_NAME,
    AdoptWorkspaceRequest,
    CheckoutDefaultBranchRequest,
    CheckoutWorkBranchRequest,
    CloneWorkspaceRequest,
    CommitAllRequest,
    FetchBranchRequest,
    GitWorkspace,
    PrepareVerifierWorkspaceRequest,
    PushBranchRequest,
)
from agentic_runner.integrations.git.fake_workspace import FakeGitWorkspace
from agentic_runner.integrations.git.workspace import (
    GitCommandResult,
    GitWorkspacePolicyError,
    LocalGitWorkspace,
    build_push_refspec,
    resolve_workspace_path,
    validate_commit_sha,
    validate_work_branch,
)

VALID_WORK_BRANCH = "agent/123e4567-e89b-12d3-a456-426614174000-fix-thing"
UNSAFE_BASE_BRANCHES = (
    "",
    "-main",
    "--all",
    "--mirror",
    "refs/heads/main",
    "HEAD",
    "main@{1}",
    "@{-1}",
    "feature@main",
    "feature{main",
    "feature}main",
    "feature\nmain",
    "feature\tmain",
    "feature\x7fmain",
    "feature//main",
    "feature.lock",
    "release/.hidden",
    "feature:main",
    "+main",
    "release/../main",
    "feature~main",
    "feature^main",
    "feature?main",
    "feature*main",
    "feature[main",
    r"feature\main",
    "/main",
    "main/",
    ".main",
    "main.",
    "refs/tags/v1.0.0",
    ":refs/heads/main",
    "+refs/heads/main:refs/heads/main",
)


def _run_git(args: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    # Identity via -c keeps these tests hermetic: the production worker pod has no
    # global/system gitconfig and git cannot auto-detect an email there ("(none)"
    # hostname domain), so any bare `git commit` in a fixture repo fails with
    # "Author identity unknown". Dev machines mask this via ~/.gitconfig.
    return subprocess.run(
        ["git", "-c", "user.name=Test User", "-c", "user.email=test@example.test", *args],
        cwd=cwd,
        shell=False,
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )


def _seed_bare_remote(tmp_path: Path, *, base_branch: str = "main") -> tuple[Path, str]:
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
    base_commit = _run_git(["rev-parse", "HEAD"], cwd=seed_path).stdout.strip()
    return remote_path, base_commit


def _prepare_local_workspace(tmp_path: Path) -> tuple[LocalGitWorkspace, Path, Path]:
    remote_path, _base_commit = _seed_bare_remote(tmp_path)
    root = tmp_path / "workers"
    root.mkdir()
    workspace_path = root / "record-1"
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
    return workspace, remote_path, workspace_path


def _push_generated_work_branch(workspace_path: Path, remote_path: Path) -> str:
    (workspace_path / "change.txt").write_text("approved verifier change\n", encoding="utf-8")
    _run_git(["add", "change.txt"], cwd=workspace_path)
    _run_git(["commit", "-m", "feat: approved change"], cwd=workspace_path)
    approved_head = _run_git(["rev-parse", "HEAD"], cwd=workspace_path).stdout.strip()
    _run_git(["push", str(remote_path), f"HEAD:refs/heads/{VALID_WORK_BRANCH}"], cwd=workspace_path)
    return approved_head


def _set_origin_to_github_with_local_rewrite(workspace_path: Path, remote_path: Path) -> None:
    github_remote = "https://github.com/qts/agentic-os.git"
    _run_git(["remote", "set-url", "origin", github_remote], cwd=workspace_path)
    _run_git(["config", f"url.{remote_path}.insteadOf", github_remote], cwd=workspace_path)


def _clone_workspace(
    tmp_path: Path,
    remote_path: Path,
    *,
    workspace_name: str = "record-1",
) -> tuple[LocalGitWorkspace, Path]:
    root = tmp_path / "workers"
    root.mkdir(exist_ok=True)
    workspace_path = root / workspace_name
    workspace = LocalGitWorkspace(allow_local_file_remotes=True)
    workspace.clone_repository(
        CloneWorkspaceRequest(
            repo_full_name="qts/agentic-os",
            remote_url=str(remote_path),
            workspace_root=root,
            workspace_path=workspace_path,
        )
    )
    return workspace, workspace_path


def test_valid_generated_work_branch_is_accepted_and_safe_refspec_is_produced() -> None:
    branch = validate_work_branch(VALID_WORK_BRANCH)

    assert branch == VALID_WORK_BRANCH
    assert build_push_refspec(VALID_WORK_BRANCH) == f"HEAD:refs/heads/{VALID_WORK_BRANCH}"


@pytest.mark.parametrize(
    "branch",
    (
        "main",
        "master",
        "feature/fix-thing",
        "agent/not-a-uuid-fix-thing",
        "agent/123e4567-e89b-12d3-a456-426614174000-Fix-Thing",
        "agent/123e4567-e89b-12d3-a456-426614174000-fix thing",
        "agent/123e4567-e89b-12d3-a456-426614174000-fix..thing",
        "agent/123e4567-e89b-12d3-a456-426614174000-fix:thing",
        "agent/123e4567-e89b-12d3-a456-426614174000-fix+thing",
        "agent/123e4567-e89b-12d3-a456-426614174000-fix~thing",
        "agent/123e4567-e89b-12d3-a456-426614174000-fix^thing",
        "agent/123e4567-e89b-12d3-a456-426614174000-fix?thing",
        "agent/123e4567-e89b-12d3-a456-426614174000-fix*thing",
        "agent/123e4567-e89b-12d3-a456-426614174000-fix[thing",
        r"agent/123e4567-e89b-12d3-a456-426614174000-fix\thing",
        "refs/tags/v1.0.0",
        "/agent/123e4567-e89b-12d3-a456-426614174000-fix-thing",
        "agent/123e4567-e89b-12d3-a456-426614174000-fix-thing/",
        ".agent/123e4567-e89b-12d3-a456-426614174000-fix-thing",
        "agent/123e4567-e89b-12d3-a456-426614174000-fix-thing.",
        ":refs/heads/main",
        "+HEAD:refs/heads/main",
        "--all",
        "--mirror",
        "agent/123e4567-e89b-12d3-a456-426614174000-fix-thing:refs/heads/main",
    ),
)
def test_invalid_branch_names_and_refspec_like_strings_are_rejected(branch: str) -> None:
    with pytest.raises(GitWorkspacePolicyError):
        validate_work_branch(branch)

    with pytest.raises(GitWorkspacePolicyError):
        build_push_refspec(branch)


@pytest.mark.parametrize("protected_branch", ("main", "master", "release/2026-06"))
def test_push_to_protected_branch_is_rejected_before_fake_records_push_call(
    tmp_path: Path,
    protected_branch: str,
) -> None:
    workspace = FakeGitWorkspace()
    root = tmp_path / "workers"
    workspace_path = root / "record-1"
    root.mkdir()
    workspace_path.mkdir()

    workspace.clone_repository(
        CloneWorkspaceRequest(
            repo_full_name="qts/agentic-os",
            remote_url="https://github.com/qts/agentic-os.git",
            workspace_root=root,
            workspace_path=workspace_path,
        )
    )

    with pytest.raises(GitWorkspacePolicyError):
        workspace.push_branch(
            PushBranchRequest(
                repo_full_name="qts/agentic-os",
                workspace_path=workspace_path,
                base_branch="release/2026-06",
                work_branch=protected_branch,
            )
        )

    assert tuple(call.operation for call in workspace.calls) == ("clone_repository",)


@pytest.mark.parametrize("base_branch", UNSAFE_BASE_BRANCHES)
def test_fake_workspace_rejects_unsafe_fetch_base_branch_before_recording_or_state_mutation(
    tmp_path: Path,
    base_branch: str,
) -> None:
    workspace = FakeGitWorkspace()
    root = tmp_path / "workers"
    workspace_path = root / "record-1"
    root.mkdir()
    workspace_path.mkdir()

    workspace.clone_repository(
        CloneWorkspaceRequest(
            repo_full_name="qts/agentic-os",
            remote_url="https://github.com/qts/agentic-os.git",
            workspace_root=root,
            workspace_path=workspace_path,
        )
    )

    with pytest.raises(GitWorkspacePolicyError):
        workspace.fetch_base_branch(
            FetchBranchRequest(
                repo_full_name="qts/agentic-os",
                workspace_path=workspace_path,
                base_branch=base_branch,
            )
        )

    record = workspace._workspaces[workspace_path.resolve()]
    assert record.base_branch is None
    assert tuple(call.operation for call in workspace.calls) == ("clone_repository",)


@pytest.mark.parametrize("base_branch", UNSAFE_BASE_BRANCHES)
def test_fake_workspace_rejects_unsafe_checkout_base_branch_before_recording_or_state_mutation(
    tmp_path: Path,
    base_branch: str,
) -> None:
    workspace = FakeGitWorkspace()
    root = tmp_path / "workers"
    workspace_path = root / "record-1"
    root.mkdir()
    workspace_path.mkdir()

    workspace.clone_repository(
        CloneWorkspaceRequest(
            repo_full_name="qts/agentic-os",
            remote_url="https://github.com/qts/agentic-os.git",
            workspace_root=root,
            workspace_path=workspace_path,
        )
    )

    with pytest.raises(GitWorkspacePolicyError):
        workspace.checkout_work_branch(
            CheckoutWorkBranchRequest(
                repo_full_name="qts/agentic-os",
                workspace_path=workspace_path,
                base_branch=base_branch,
                work_branch=VALID_WORK_BRANCH,
            )
        )

    record = workspace._workspaces[workspace_path.resolve()]
    assert record.base_branch is None
    assert record.current_branch is None
    assert tuple(call.operation for call in workspace.calls) == ("clone_repository",)


@pytest.mark.parametrize(
    "base_branch",
    ("main", "master", "develop", "release/2026-06", "feature/my-branch"),
)
def test_fake_workspace_accepts_normal_base_branches_for_fetch_and_checkout(
    tmp_path: Path,
    base_branch: str,
) -> None:
    workspace = FakeGitWorkspace()
    root = tmp_path / "workers"
    workspace_path = root / "record-1"
    root.mkdir()
    workspace_path.mkdir()

    workspace.clone_repository(
        CloneWorkspaceRequest(
            repo_full_name="qts/agentic-os",
            remote_url="https://github.com/qts/agentic-os.git",
            workspace_root=root,
            workspace_path=workspace_path,
        )
    )

    fetch = workspace.fetch_base_branch(
        FetchBranchRequest(
            repo_full_name="qts/agentic-os",
            workspace_path=workspace_path,
            base_branch=base_branch,
        )
    )
    checkout = workspace.checkout_work_branch(
        CheckoutWorkBranchRequest(
            repo_full_name="qts/agentic-os",
            workspace_path=workspace_path,
            base_branch=base_branch,
            work_branch=VALID_WORK_BRANCH,
        )
    )

    assert fetch.base_branch == base_branch
    assert checkout.base_branch == base_branch
    assert tuple(call.operation for call in workspace.calls) == (
        "clone_repository",
        "fetch_base_branch",
        "checkout_work_branch",
    )


def test_workspace_path_under_root_is_accepted_and_parent_escape_is_rejected(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workers"
    accepted = root / "record-1"
    escape = root / ".." / "outside"
    root.mkdir()
    accepted.mkdir()

    assert resolve_workspace_path(root, accepted) == accepted.resolve()
    with pytest.raises(GitWorkspacePolicyError):
        resolve_workspace_path(root, escape)


def test_local_git_workspace_prepares_verifier_workspace_at_approved_head(
    tmp_path: Path,
) -> None:
    workspace, remote_path, workspace_path = _prepare_local_workspace(tmp_path)
    approved_head = _push_generated_work_branch(workspace_path, remote_path)

    result = workspace.prepare_verifier_workspace(
        PrepareVerifierWorkspaceRequest(
            repo_full_name="qts/agentic-os",
            workspace_root=tmp_path / "workers",
            workspace_path=workspace_path,
            work_branch=VALID_WORK_BRANCH,
            approved_head_sha=approved_head,
        )
    )

    current_head = _run_git(["rev-parse", "HEAD"], cwd=workspace_path).stdout.strip()
    assert result.repo_full_name == "qts/agentic-os"
    assert result.workspace_path == workspace_path.resolve()
    assert result.work_branch == VALID_WORK_BRANCH
    assert result.approved_head_sha == approved_head
    assert result.attested_head_sha == approved_head
    assert result.status_summary == ""
    assert current_head == approved_head


def test_local_git_workspace_rehydrates_valid_existing_verifier_clone(
    tmp_path: Path,
) -> None:
    prior_workspace, remote_path, workspace_path = _prepare_local_workspace(tmp_path)
    approved_head = _push_generated_work_branch(workspace_path, remote_path)
    _set_origin_to_github_with_local_rewrite(workspace_path, remote_path)
    fresh_workspace = LocalGitWorkspace()

    result = fresh_workspace.prepare_verifier_workspace(
        PrepareVerifierWorkspaceRequest(
            repo_full_name="qts/agentic-os",
            workspace_root=tmp_path / "workers",
            workspace_path=workspace_path,
            work_branch=VALID_WORK_BRANCH,
            approved_head_sha=approved_head,
        )
    )

    assert result.workspace_path == workspace_path.resolve()
    assert result.attested_head_sha == approved_head
    assert result.status_summary == ""
    assert prior_workspace.command_results


def test_local_git_workspace_rejects_rehydrated_clone_with_wrong_origin_remote(
    tmp_path: Path,
) -> None:
    _workspace, remote_path, workspace_path = _prepare_local_workspace(tmp_path)
    approved_head = _push_generated_work_branch(workspace_path, remote_path)
    _run_git(
        ["remote", "set-url", "origin", "https://github.com/qts/wrong-repo.git"],
        cwd=workspace_path,
    )
    fresh_workspace = LocalGitWorkspace()

    with pytest.raises(GitWorkspacePolicyError, match="origin"):
        fresh_workspace.prepare_verifier_workspace(
            PrepareVerifierWorkspaceRequest(
                repo_full_name="qts/agentic-os",
                workspace_root=tmp_path / "workers",
                workspace_path=workspace_path,
                work_branch=VALID_WORK_BRANCH,
                approved_head_sha=approved_head,
            )
        )


def test_local_git_workspace_rejects_rehydration_when_git_metadata_is_missing(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workers"
    workspace_path = root / "record-1"
    workspace_path.mkdir(parents=True)
    fresh_workspace = LocalGitWorkspace()

    with pytest.raises(GitWorkspacePolicyError, match="git"):
        fresh_workspace.prepare_verifier_workspace(
            PrepareVerifierWorkspaceRequest(
                repo_full_name="qts/agentic-os",
                workspace_root=root,
                workspace_path=workspace_path,
                work_branch=VALID_WORK_BRANCH,
                approved_head_sha="0" * 40,
            )
        )


def test_local_git_workspace_rejects_missing_verifier_workspace_with_policy_error(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workers"
    root.mkdir()
    workspace_path = root / "missing-record"
    fresh_workspace = LocalGitWorkspace()

    with pytest.raises(GitWorkspacePolicyError, match="does not exist"):
        fresh_workspace.prepare_verifier_workspace(
            PrepareVerifierWorkspaceRequest(
                repo_full_name="qts/agentic-os",
                workspace_root=root,
                workspace_path=workspace_path,
                work_branch=VALID_WORK_BRANCH,
                approved_head_sha="0" * 40,
            )
        )

    assert fresh_workspace.command_results == ()


def test_local_git_workspace_rejects_verifier_workspace_symlink_escape_before_subprocess(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workers"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    workspace_path = root / "record-1"
    workspace_path.symlink_to(outside, target_is_directory=True)
    fresh_workspace = LocalGitWorkspace()

    with pytest.raises(GitWorkspacePolicyError, match="escapes"):
        fresh_workspace.prepare_verifier_workspace(
            PrepareVerifierWorkspaceRequest(
                repo_full_name="qts/agentic-os",
                workspace_root=root,
                workspace_path=workspace_path,
                work_branch=VALID_WORK_BRANCH,
                approved_head_sha="0" * 40,
            )
        )

    assert fresh_workspace.command_results == ()


def test_local_git_workspace_rejects_clean_wrong_verifier_head_before_reset(
    tmp_path: Path,
) -> None:
    workspace, remote_path, workspace_path = _prepare_local_workspace(tmp_path)
    approved_head = _push_generated_work_branch(workspace_path, remote_path)
    _run_git(["checkout", "main"], cwd=workspace_path)
    wrong_local_head = _run_git(["rev-parse", "HEAD"], cwd=workspace_path).stdout.strip()
    command_count = len(workspace.command_results)

    with pytest.raises(GitWorkspacePolicyError, match="local HEAD"):
        workspace.prepare_verifier_workspace(
            PrepareVerifierWorkspaceRequest(
                repo_full_name="qts/agentic-os",
                workspace_root=tmp_path / "workers",
                workspace_path=workspace_path,
                work_branch=VALID_WORK_BRANCH,
                approved_head_sha=approved_head,
            )
        )

    current_head = _run_git(["rev-parse", "HEAD"], cwd=workspace_path).stdout.strip()
    commands_after_rejection = workspace.command_results[command_count:]
    ran_checkout = any(
        result.argv[3:6] == ("checkout", "-B", VALID_WORK_BRANCH)
        for result in commands_after_rejection
    )
    ran_reset = any(
        result.argv[3:6] == ("reset", "--hard", approved_head)
        for result in commands_after_rejection
    )
    assert wrong_local_head != approved_head
    assert current_head == wrong_local_head
    assert not ran_checkout
    assert not ran_reset


def test_local_git_workspace_prepares_new_clone_without_local_head(
    tmp_path: Path,
) -> None:
    remote_path, approved_head = _seed_bare_remote(tmp_path)
    _run_git(
        [
            "--git-dir",
            str(remote_path),
            "update-ref",
            f"refs/heads/{VALID_WORK_BRANCH}",
            approved_head,
        ]
    )
    _run_git(["--git-dir", str(remote_path), "symbolic-ref", "HEAD", "refs/heads/missing-main"])
    workspace, workspace_path = _clone_workspace(tmp_path, remote_path)

    with pytest.raises(subprocess.CalledProcessError):
        _run_git(["rev-parse", "--verify", "HEAD"], cwd=workspace_path)

    result = workspace.prepare_verifier_workspace(
        PrepareVerifierWorkspaceRequest(
            repo_full_name="qts/agentic-os",
            workspace_root=tmp_path / "workers",
            workspace_path=workspace_path,
            work_branch=VALID_WORK_BRANCH,
            approved_head_sha=approved_head,
        )
    )

    current_head = _run_git(["rev-parse", "HEAD"], cwd=workspace_path).stdout.strip()
    assert result.attested_head_sha == approved_head
    assert result.status_summary == ""
    assert current_head == approved_head


def test_local_git_workspace_checks_out_the_remotes_default_branch_detached(
    tmp_path: Path,
) -> None:
    """An Organisation-scoped Work Record's read has no base of its own (ADR-0018 §7)."""

    remote_path, base_commit = _seed_bare_remote(tmp_path, base_branch="trunk")
    _run_git(["--git-dir", str(remote_path), "symbolic-ref", "HEAD", "refs/heads/trunk"])
    workspace, workspace_path = _clone_workspace(tmp_path, remote_path)

    result = workspace.checkout_default_branch(
        CheckoutDefaultBranchRequest(repo_full_name="qts/agentic-os", workspace_path=workspace_path)
    )

    assert result.base_branch == "trunk"
    assert (workspace_path / "README.md").read_text(encoding="utf-8") == "seed\n"
    assert _run_git(["rev-parse", "HEAD"], cwd=workspace_path).stdout.strip() == base_commit
    with pytest.raises(subprocess.CalledProcessError):
        _run_git(["symbolic-ref", "-q", "HEAD"], cwd=workspace_path)


def test_local_git_workspace_rejects_verifier_workspace_when_approved_sha_is_not_work_branch_tip(
    tmp_path: Path,
) -> None:
    workspace, remote_path, workspace_path = _prepare_local_workspace(tmp_path)
    _push_generated_work_branch(workspace_path, remote_path)
    wrong_approved_head = _run_git(["rev-parse", "HEAD~1"], cwd=workspace_path).stdout.strip()

    with pytest.raises(GitWorkspacePolicyError, match="approved head"):
        workspace.prepare_verifier_workspace(
            PrepareVerifierWorkspaceRequest(
                repo_full_name="qts/agentic-os",
                workspace_root=tmp_path / "workers",
                workspace_path=workspace_path,
                work_branch=VALID_WORK_BRANCH,
                approved_head_sha=wrong_approved_head,
            )
        )


def test_local_git_workspace_rejects_dirty_verifier_workspace_immediately_before_verifier(
    tmp_path: Path,
) -> None:
    workspace, remote_path, workspace_path = _prepare_local_workspace(tmp_path)
    approved_head = _push_generated_work_branch(workspace_path, remote_path)
    (workspace_path / "untracked-verifier-state.txt").write_text("dirty\n", encoding="utf-8")

    with pytest.raises(GitWorkspacePolicyError, match="dirty"):
        workspace.prepare_verifier_workspace(
            PrepareVerifierWorkspaceRequest(
                repo_full_name="qts/agentic-os",
                workspace_root=tmp_path / "workers",
                workspace_path=workspace_path,
                work_branch=VALID_WORK_BRANCH,
                approved_head_sha=approved_head,
            )
        )


def test_local_git_workspace_rejects_tracked_dirty_verifier_workspace_before_reset(
    tmp_path: Path,
) -> None:
    workspace, remote_path, workspace_path = _prepare_local_workspace(tmp_path)
    approved_head = _push_generated_work_branch(workspace_path, remote_path)
    dirty_content = "dirty tracked verifier change\n"
    (workspace_path / "change.txt").write_text(dirty_content, encoding="utf-8")

    with pytest.raises(GitWorkspacePolicyError, match="dirty"):
        workspace.prepare_verifier_workspace(
            PrepareVerifierWorkspaceRequest(
                repo_full_name="qts/agentic-os",
                workspace_root=tmp_path / "workers",
                workspace_path=workspace_path,
                work_branch=VALID_WORK_BRANCH,
                approved_head_sha=approved_head,
            )
        )

    assert (workspace_path / "change.txt").read_text(encoding="utf-8") == dirty_content


def test_local_git_workspace_rejects_verifier_workspace_path_escape_before_subprocess(
    tmp_path: Path,
) -> None:
    workspace = LocalGitWorkspace(allow_local_file_remotes=True)
    root = tmp_path / "workers"
    root.mkdir()

    with pytest.raises(GitWorkspacePolicyError, match="escapes"):
        workspace.prepare_verifier_workspace(
            PrepareVerifierWorkspaceRequest(
                repo_full_name="qts/agentic-os",
                workspace_root=root,
                workspace_path=root / ".." / "outside",
                work_branch=VALID_WORK_BRANCH,
                approved_head_sha="0" * 40,
            )
        )

    assert workspace.command_results == ()


def test_fake_workspace_models_verifier_workspace_wrong_head_and_dirty_failures(
    tmp_path: Path,
) -> None:
    workspace = FakeGitWorkspace()
    root = tmp_path / "workers"
    workspace_path = root / "record-1"
    root.mkdir()
    workspace_path.mkdir()
    wrong_head = "b" * 40
    workspace.clone_repository(
        CloneWorkspaceRequest(
            repo_full_name="qts/agentic-os",
            remote_url="https://github.com/qts/agentic-os.git",
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
    commit = workspace.commit_all(
        CommitAllRequest(
            repo_full_name="qts/agentic-os",
            workspace_path=workspace_path,
            work_branch=VALID_WORK_BRANCH,
            commit_message="feat: implement verifier change",
        )
    )
    workspace.push_branch(
        PushBranchRequest(
            repo_full_name="qts/agentic-os",
            workspace_path=workspace_path,
            base_branch="main",
            work_branch=VALID_WORK_BRANCH,
        )
    )
    approved_head = commit.commit_id

    clean = workspace.prepare_verifier_workspace(
        PrepareVerifierWorkspaceRequest(
            repo_full_name="qts/agentic-os",
            workspace_root=root,
            workspace_path=workspace_path,
            work_branch=VALID_WORK_BRANCH,
            approved_head_sha=approved_head,
        )
    )
    workspace.set_verifier_workspace_head(workspace_path, wrong_head)
    with pytest.raises(GitWorkspacePolicyError, match="approved head"):
        workspace.prepare_verifier_workspace(
            PrepareVerifierWorkspaceRequest(
                repo_full_name="qts/agentic-os",
                workspace_root=root,
                workspace_path=workspace_path,
                work_branch=VALID_WORK_BRANCH,
                approved_head_sha=approved_head,
            )
        )
    workspace.set_verifier_workspace_head(workspace_path, approved_head)
    workspace.set_verifier_workspace_status(workspace_path, "?? dirty.txt\n")
    with pytest.raises(GitWorkspacePolicyError, match="dirty"):
        workspace.prepare_verifier_workspace(
            PrepareVerifierWorkspaceRequest(
                repo_full_name="qts/agentic-os",
                workspace_root=root,
                workspace_path=workspace_path,
                work_branch=VALID_WORK_BRANCH,
                approved_head_sha=approved_head,
            )
        )

    assert clean.attested_head_sha == approved_head
    assert clean.status_summary == ""


def test_fake_workspace_rejects_unrelated_valid_approved_head_sha(
    tmp_path: Path,
) -> None:
    workspace = FakeGitWorkspace()
    root = tmp_path / "workers"
    workspace_path = root / "record-1"
    root.mkdir()
    workspace_path.mkdir()
    unrelated_head = "f" * 40
    workspace.clone_repository(
        CloneWorkspaceRequest(
            repo_full_name="qts/agentic-os",
            remote_url="https://github.com/qts/agentic-os.git",
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
    workspace.commit_all(
        CommitAllRequest(
            repo_full_name="qts/agentic-os",
            workspace_path=workspace_path,
            work_branch=VALID_WORK_BRANCH,
            commit_message="feat: implement verifier change",
        )
    )
    workspace.push_branch(
        PushBranchRequest(
            repo_full_name="qts/agentic-os",
            workspace_path=workspace_path,
            base_branch="main",
            work_branch=VALID_WORK_BRANCH,
        )
    )

    with pytest.raises(GitWorkspacePolicyError, match="approved head"):
        workspace.prepare_verifier_workspace(
            PrepareVerifierWorkspaceRequest(
                repo_full_name="qts/agentic-os",
                workspace_root=root,
                workspace_path=workspace_path,
                work_branch=VALID_WORK_BRANCH,
                approved_head_sha=unrelated_head,
            )
        )


def test_fake_workspace_happy_path_records_state_and_default_commit_author(tmp_path: Path) -> None:
    workspace = FakeGitWorkspace()
    root = tmp_path / "workers"
    workspace_path = root / "record-1"
    root.mkdir()
    workspace_path.mkdir()

    clone = workspace.clone_repository(
        CloneWorkspaceRequest(
            repo_full_name="qts/agentic-os",
            remote_url="https://github.com/qts/agentic-os.git",
            workspace_root=root,
            workspace_path=workspace_path,
        )
    )
    fetch = workspace.fetch_base_branch(
        FetchBranchRequest(
            repo_full_name="qts/agentic-os",
            workspace_path=workspace_path,
            base_branch="main",
        )
    )
    checkout = workspace.checkout_work_branch(
        CheckoutWorkBranchRequest(
            repo_full_name="qts/agentic-os",
            workspace_path=workspace_path,
            base_branch="main",
            work_branch=VALID_WORK_BRANCH,
        )
    )
    commit = workspace.commit_all(
        CommitAllRequest(
            repo_full_name="qts/agentic-os",
            workspace_path=workspace_path,
            work_branch=VALID_WORK_BRANCH,
            commit_message="feat: implement change",
        )
    )
    push = workspace.push_branch(
        PushBranchRequest(
            repo_full_name="qts/agentic-os",
            workspace_path=workspace_path,
            base_branch="main",
            work_branch=VALID_WORK_BRANCH,
        )
    )

    assert clone.workspace_path == workspace_path.resolve()
    assert fetch.base_branch == "main"
    assert checkout.work_branch == VALID_WORK_BRANCH
    assert commit.author_name == DEFAULT_COMMIT_AUTHOR_NAME
    assert commit.author_email == DEFAULT_COMMIT_AUTHOR_EMAIL
    assert validate_commit_sha(commit.commit_id) == commit.commit_id
    assert commit.commit_id == "0000000000000000000000000000000000000001"
    assert push.refspec == f"HEAD:refs/heads/{VALID_WORK_BRANCH}"
    assert push.commit_id == commit.commit_id
    assert tuple(call.operation for call in workspace.calls) == (
        "clone_repository",
        "fetch_base_branch",
        "checkout_work_branch",
        "commit_all",
        "push_branch",
    )


def test_fake_workspace_duplicate_clone_returns_existing_registration_without_resetting_state(
    tmp_path: Path,
) -> None:
    workspace = FakeGitWorkspace()
    root = tmp_path / "workers"
    workspace_path = root / "record-1"
    root.mkdir()
    workspace_path.mkdir()
    request = CloneWorkspaceRequest(
        repo_full_name="qts/agentic-os",
        remote_url="https://github.com/qts/agentic-os.git",
        workspace_root=root,
        workspace_path=workspace_path,
    )

    first_clone = workspace.clone_repository(request)
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

    second_clone = workspace.clone_repository(request)
    commit = workspace.commit_all(
        CommitAllRequest(
            repo_full_name="qts/agentic-os",
            workspace_path=workspace_path,
            work_branch=VALID_WORK_BRANCH,
            commit_message="feat: retry-safe clone",
        )
    )

    assert second_clone == first_clone
    assert validate_commit_sha(commit.commit_id) == commit.commit_id
    assert commit.commit_id == "0000000000000000000000000000000000000001"
    assert tuple(call.operation for call in workspace.calls) == (
        "clone_repository",
        "fetch_base_branch",
        "checkout_work_branch",
        "clone_repository",
        "commit_all",
    )


def test_fake_workspace_reuses_existing_remote_work_branch_on_retry_checkout(
    tmp_path: Path,
) -> None:
    workspace = FakeGitWorkspace()
    root = tmp_path / "workers"
    workspace_path = root / "record-1"
    root.mkdir()
    workspace_path.mkdir()
    clone_request = CloneWorkspaceRequest(
        repo_full_name="qts/agentic-os",
        remote_url="https://github.com/qts/agentic-os.git",
        workspace_root=root,
        workspace_path=workspace_path,
    )
    checkout_request = CheckoutWorkBranchRequest(
        repo_full_name="qts/agentic-os",
        workspace_path=workspace_path,
        base_branch="main",
        work_branch=VALID_WORK_BRANCH,
    )
    push_request = PushBranchRequest(
        repo_full_name="qts/agentic-os",
        workspace_path=workspace_path,
        base_branch="main",
        work_branch=VALID_WORK_BRANCH,
    )

    workspace.clone_repository(clone_request)
    workspace.fetch_base_branch(
        FetchBranchRequest(
            repo_full_name="qts/agentic-os",
            workspace_path=workspace_path,
            base_branch="main",
        )
    )
    workspace.checkout_work_branch(checkout_request)
    first_commit = workspace.commit_all(
        CommitAllRequest(
            repo_full_name="qts/agentic-os",
            workspace_path=workspace_path,
            work_branch=VALID_WORK_BRANCH,
            commit_message="feat: first fake change",
        )
    )
    workspace.push_branch(push_request)

    workspace.clone_repository(clone_request)
    workspace.checkout_work_branch(checkout_request)

    record = workspace._workspaces[workspace_path.resolve()]
    assert record.current_branch_base_commit_id == first_commit.commit_id


def test_fake_workspace_commit_all_returns_existing_head_for_clean_retry(
    tmp_path: Path,
) -> None:
    workspace = FakeGitWorkspace()
    root = tmp_path / "workers"
    workspace_path = root / "record-1"
    root.mkdir()
    workspace_path.mkdir()
    clone_request = CloneWorkspaceRequest(
        repo_full_name="qts/agentic-os",
        remote_url="https://github.com/qts/agentic-os.git",
        workspace_root=root,
        workspace_path=workspace_path,
    )
    checkout_request = CheckoutWorkBranchRequest(
        repo_full_name="qts/agentic-os",
        workspace_path=workspace_path,
        base_branch="main",
        work_branch=VALID_WORK_BRANCH,
    )
    commit_request = CommitAllRequest(
        repo_full_name="qts/agentic-os",
        workspace_path=workspace_path,
        work_branch=VALID_WORK_BRANCH,
        commit_message="feat: retry generated change",
    )
    push_request = PushBranchRequest(
        repo_full_name="qts/agentic-os",
        workspace_path=workspace_path,
        base_branch="main",
        work_branch=VALID_WORK_BRANCH,
    )

    workspace.clone_repository(clone_request)
    workspace.fetch_base_branch(
        FetchBranchRequest(
            repo_full_name="qts/agentic-os",
            workspace_path=workspace_path,
            base_branch="main",
        )
    )
    workspace.checkout_work_branch(checkout_request)
    first_commit = workspace.commit_all(commit_request)
    workspace.push_branch(push_request)

    workspace.clone_repository(clone_request)
    workspace.checkout_work_branch(checkout_request)
    retry_commit = workspace.commit_all(commit_request)
    retry_push = workspace.push_branch(push_request)

    assert retry_commit.commit_id == first_commit.commit_id
    assert retry_push.commit_id == first_commit.commit_id


def test_fake_workspace_rejects_duplicate_clone_when_existing_origin_is_wrong(
    tmp_path: Path,
) -> None:
    workspace = FakeGitWorkspace()
    root = tmp_path / "workers"
    workspace_path = root / "record-1"
    root.mkdir()
    workspace_path.mkdir()
    workspace.clone_repository(
        CloneWorkspaceRequest(
            repo_full_name="qts/agentic-os",
            remote_url="https://github.com/qts/agentic-os.git",
            workspace_root=root,
            workspace_path=workspace_path,
        )
    )

    with pytest.raises(GitWorkspacePolicyError, match="origin"):
        workspace.clone_repository(
            CloneWorkspaceRequest(
                repo_full_name="qts/agentic-os",
                remote_url="https://github.com/qts/wrong-repo.git",
                workspace_root=root,
                workspace_path=workspace_path,
            )
        )


def test_fake_workspace_returns_deterministic_git_evidence_without_subprocess(
    tmp_path: Path,
) -> None:
    workspace = FakeGitWorkspace(
        status_evidence=" M changed.txt\n",
        diff_evidence="diff --git a/changed.txt b/changed.txt\n+generated change\n",
    )
    root = tmp_path / "workers"
    workspace_path = root / "record-1"
    root.mkdir()
    workspace_path.mkdir()
    workspace.clone_repository(
        CloneWorkspaceRequest(
            repo_full_name="qts/agentic-os",
            remote_url="https://github.com/qts/agentic-os.git",
            workspace_root=root,
            workspace_path=workspace_path,
        )
    )

    evidence = workspace.collect_git_evidence(workspace_path, output_limit_bytes=4096)

    assert evidence.status == " M changed.txt\n"
    assert evidence.diff == "diff --git a/changed.txt b/changed.txt\n+generated change\n"
    assert tuple(call.operation for call in workspace.calls) == (
        "clone_repository",
        "collect_git_evidence",
    )


def test_fake_workspace_git_evidence_obeys_total_output_budget(tmp_path: Path) -> None:
    workspace = FakeGitWorkspace(
        status_evidence=" M changed.txt\n" * 10,
        diff_evidence="diff --git a/changed.txt b/changed.txt\n+generated change\n" * 10,
    )
    root = tmp_path / "workers"
    workspace_path = root / "record-1"
    root.mkdir()
    workspace_path.mkdir()
    workspace.clone_repository(
        CloneWorkspaceRequest(
            repo_full_name="qts/agentic-os",
            remote_url="https://github.com/qts/agentic-os.git",
            workspace_root=root,
            workspace_path=workspace_path,
        )
    )

    evidence = workspace.collect_git_evidence(workspace_path, output_limit_bytes=96)

    assert (
        len(evidence.status.encode()) + len(evidence.diff.encode()) + len(evidence.stderr.encode())
        <= 96
    )


def test_fake_workspace_rejects_push_when_current_branch_is_not_work_branch(tmp_path: Path) -> None:
    workspace = FakeGitWorkspace()
    root = tmp_path / "workers"
    workspace_path = root / "record-1"
    root.mkdir()
    workspace_path.mkdir()

    workspace.clone_repository(
        CloneWorkspaceRequest(
            repo_full_name="qts/agentic-os",
            remote_url="https://github.com/qts/agentic-os.git",
            workspace_root=root,
            workspace_path=workspace_path,
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
    workspace.commit_all(
        CommitAllRequest(
            repo_full_name="qts/agentic-os",
            workspace_path=workspace_path,
            work_branch=VALID_WORK_BRANCH,
            commit_message="feat: implement change",
        )
    )

    with pytest.raises(GitWorkspacePolicyError, match="current branch"):
        workspace.push_branch(
            PushBranchRequest(
                repo_full_name="qts/agentic-os",
                workspace_path=workspace_path,
                base_branch="main",
                work_branch="agent/123e4567-e89b-12d3-a456-426614174001-fix-thing",
            )
        )

    assert tuple(call.operation for call in workspace.calls) == (
        "clone_repository",
        "checkout_work_branch",
        "commit_all",
    )


def test_local_git_workspace_refuses_a_workspace_whose_git_directory_was_swapped(
    tmp_path: Path,
) -> None:
    """The Contract owns the directory that *contains* ``.git`` (ADR-0015 §1).

    Keeping ``.git`` out of ``hand_workspace_to_contract``'s chown stops the Contract
    *writing* it, but on POSIX renaming an entry is governed by write+execute on the
    parent — so a Directive can move the Runner's ``.git`` aside and point the name at a
    tree of its own, carrying any ``hooks/pre-commit``, ``core.fsmonitor`` or
    ``filter.*.clean`` it likes. Every Runner git seam over a Workspace has to refuse
    that before it runs, and ``-c safe.directory`` is precisely the flag that would
    otherwise let it through.
    """

    workspace, _remote_path, workspace_path = _prepare_local_workspace(tmp_path)
    (workspace_path / "change.txt").write_text("generated change\n", encoding="utf-8")
    hostile_git = tmp_path / "hostile-git"
    (workspace_path / ".git").rename(hostile_git)
    (workspace_path / ".git").symlink_to(hostile_git, target_is_directory=True)

    with pytest.raises(GitWorkspacePolicyError, match="tampered"):
        workspace.commit_all(
            CommitAllRequest(
                repo_full_name="qts/agentic-os",
                workspace_path=workspace_path,
                work_branch=VALID_WORK_BRANCH,
                commit_message="feat: generated change",
            )
        )
    with pytest.raises(GitWorkspacePolicyError, match="tampered"):
        workspace.collect_git_evidence(workspace_path)

    # ...and a `.git` simply carried off is refused too, rather than letting git rediscover
    # a repository somewhere above the Workspace.
    (workspace_path / ".git").unlink()
    with pytest.raises(GitWorkspacePolicyError, match="missing or unreadable"):
        workspace.collect_git_evidence(workspace_path)


def test_local_git_workspace_pushes_committed_work_branch_to_local_bare_remote(
    tmp_path: Path,
) -> None:
    remote_path, base_commit = _seed_bare_remote(tmp_path)
    root = tmp_path / "workers"
    workspace_path = root / "record-1"
    root.mkdir()
    workspace = LocalGitWorkspace(
        command_timeout_seconds=10,
        allow_local_file_remotes=True,
    )

    clone = workspace.clone_repository(
        CloneWorkspaceRequest(
            repo_full_name="qts/agentic-os",
            remote_url=str(remote_path),
            workspace_root=root,
            workspace_path=workspace_path,
        )
    )
    fetch = workspace.fetch_base_branch(
        FetchBranchRequest(
            repo_full_name="qts/agentic-os",
            workspace_path=workspace_path,
            base_branch="main",
        )
    )
    checkout = workspace.checkout_work_branch(
        CheckoutWorkBranchRequest(
            repo_full_name="qts/agentic-os",
            workspace_path=workspace_path,
            base_branch="main",
            work_branch=VALID_WORK_BRANCH,
        )
    )
    (workspace_path / "change.txt").write_text("generated change\n", encoding="utf-8")
    commit = workspace.commit_all(
        CommitAllRequest(
            repo_full_name="qts/agentic-os",
            workspace_path=workspace_path,
            work_branch=VALID_WORK_BRANCH,
            commit_message="feat: generated change",
        )
    )
    push = workspace.push_branch(
        PushBranchRequest(
            repo_full_name="qts/agentic-os",
            workspace_path=workspace_path,
            base_branch="main",
            work_branch=VALID_WORK_BRANCH,
        )
    )

    pushed_commit = _run_git(
        ["--git-dir", str(remote_path), "rev-parse", f"refs/heads/{VALID_WORK_BRANCH}"]
    ).stdout.strip()
    pushed_file = _run_git(
        [
            "--git-dir",
            str(remote_path),
            "show",
            f"refs/heads/{VALID_WORK_BRANCH}:change.txt",
        ]
    ).stdout
    config_text = (workspace_path / ".git" / "config").read_text(encoding="utf-8")

    assert clone.workspace_path == workspace_path.resolve()
    assert fetch.base_branch == "main"
    assert checkout.work_branch == VALID_WORK_BRANCH
    assert commit.commit_id == pushed_commit
    assert commit.commit_id != base_commit
    assert commit.author_name == DEFAULT_COMMIT_AUTHOR_NAME
    assert commit.author_email == DEFAULT_COMMIT_AUTHOR_EMAIL
    assert push.refspec == f"HEAD:refs/heads/{VALID_WORK_BRANCH}"
    assert push.commit_id == commit.commit_id
    assert pushed_file == "generated change\n"
    assert "credential" not in config_text.lower()


def test_local_git_workspace_retries_existing_work_branch_without_non_fast_forward(
    tmp_path: Path,
) -> None:
    remote_path, _base_commit = _seed_bare_remote(tmp_path)
    root = tmp_path / "workers"
    root.mkdir()
    workspace_path = root / "record-1"
    workspace = LocalGitWorkspace(
        command_timeout_seconds=10,
        allow_local_file_remotes=True,
    )
    clone_request = CloneWorkspaceRequest(
        repo_full_name="qts/agentic-os",
        remote_url=str(remote_path),
        workspace_root=root,
        workspace_path=workspace_path,
    )
    fetch_request = FetchBranchRequest(
        repo_full_name="qts/agentic-os",
        workspace_path=workspace_path,
        base_branch="main",
    )
    checkout_request = CheckoutWorkBranchRequest(
        repo_full_name="qts/agentic-os",
        workspace_path=workspace_path,
        base_branch="main",
        work_branch=VALID_WORK_BRANCH,
    )
    push_request = PushBranchRequest(
        repo_full_name="qts/agentic-os",
        workspace_path=workspace_path,
        base_branch="main",
        work_branch=VALID_WORK_BRANCH,
    )

    workspace.clone_repository(clone_request)
    workspace.fetch_base_branch(fetch_request)
    workspace.checkout_work_branch(checkout_request)
    (workspace_path / "retry.txt").write_text("first attempt\n", encoding="utf-8")
    first_commit = workspace.commit_all(
        CommitAllRequest(
            repo_full_name="qts/agentic-os",
            workspace_path=workspace_path,
            work_branch=VALID_WORK_BRANCH,
            commit_message="feat: first generated change",
        )
    )
    workspace.push_branch(push_request)

    workspace.clone_repository(clone_request)
    workspace.fetch_base_branch(fetch_request)
    workspace.checkout_work_branch(checkout_request)
    (workspace_path / "retry.txt").write_text("second attempt\n", encoding="utf-8")
    second_commit = workspace.commit_all(
        CommitAllRequest(
            repo_full_name="qts/agentic-os",
            workspace_path=workspace_path,
            work_branch=VALID_WORK_BRANCH,
            commit_message="feat: second generated change",
        )
    )
    second_push = workspace.push_branch(push_request)

    remote_head = _run_git(
        ["--git-dir", str(remote_path), "rev-parse", f"refs/heads/{VALID_WORK_BRANCH}"]
    ).stdout.strip()
    second_parent = _run_git(["rev-parse", "HEAD~1"], cwd=workspace_path).stdout.strip()
    remote_file = _run_git(
        [
            "--git-dir",
            str(remote_path),
            "show",
            f"refs/heads/{VALID_WORK_BRANCH}:retry.txt",
        ]
    ).stdout

    assert second_push.commit_id == second_commit.commit_id
    assert remote_head == second_commit.commit_id
    assert second_parent == first_commit.commit_id
    assert remote_file == "second attempt\n"


def test_local_git_workspace_clean_retry_commit_all_and_push_are_idempotent(
    tmp_path: Path,
) -> None:
    remote_path, _base_commit = _seed_bare_remote(tmp_path)
    root = tmp_path / "workers"
    root.mkdir()
    workspace_path = root / "record-1"
    workspace = LocalGitWorkspace(
        command_timeout_seconds=10,
        allow_local_file_remotes=True,
    )
    clone_request = CloneWorkspaceRequest(
        repo_full_name="qts/agentic-os",
        remote_url=str(remote_path),
        workspace_root=root,
        workspace_path=workspace_path,
    )
    fetch_request = FetchBranchRequest(
        repo_full_name="qts/agentic-os",
        workspace_path=workspace_path,
        base_branch="main",
    )
    checkout_request = CheckoutWorkBranchRequest(
        repo_full_name="qts/agentic-os",
        workspace_path=workspace_path,
        base_branch="main",
        work_branch=VALID_WORK_BRANCH,
    )
    commit_request = CommitAllRequest(
        repo_full_name="qts/agentic-os",
        workspace_path=workspace_path,
        work_branch=VALID_WORK_BRANCH,
        commit_message="feat: retry generated change",
    )
    push_request = PushBranchRequest(
        repo_full_name="qts/agentic-os",
        workspace_path=workspace_path,
        base_branch="main",
        work_branch=VALID_WORK_BRANCH,
    )

    workspace.clone_repository(clone_request)
    workspace.fetch_base_branch(fetch_request)
    workspace.checkout_work_branch(checkout_request)
    (workspace_path / "retry.txt").write_text("first attempt\n", encoding="utf-8")
    first_commit = workspace.commit_all(commit_request)
    workspace.push_branch(push_request)

    workspace.clone_repository(clone_request)
    workspace.fetch_base_branch(fetch_request)
    workspace.checkout_work_branch(checkout_request)
    retry_commit = workspace.commit_all(commit_request)
    retry_push = workspace.push_branch(push_request)

    remote_head = _run_git(
        ["--git-dir", str(remote_path), "rev-parse", f"refs/heads/{VALID_WORK_BRANCH}"]
    ).stdout.strip()
    local_status = _run_git(["status", "--short"], cwd=workspace_path).stdout

    assert retry_commit.commit_id == first_commit.commit_id
    assert retry_push.commit_id == first_commit.commit_id
    assert remote_head == first_commit.commit_id
    assert local_status == ""


def test_local_git_workspace_rejects_protected_branch_push_before_subprocess(
    tmp_path: Path,
) -> None:
    workspace, _remote_path, workspace_path = _prepare_local_workspace(tmp_path)
    command_count = len(workspace.command_results)

    with pytest.raises(GitWorkspacePolicyError):
        workspace.push_branch(
            PushBranchRequest(
                repo_full_name="qts/agentic-os",
                workspace_path=workspace_path,
                base_branch="main",
                work_branch="main",
            )
        )

    assert len(workspace.command_results) == command_count


def test_local_git_workspace_rejects_commit_and_push_from_wrong_current_branch(
    tmp_path: Path,
) -> None:
    workspace, _remote_path, workspace_path = _prepare_local_workspace(tmp_path)
    _run_git(["checkout", "main"], cwd=workspace_path)

    with pytest.raises(GitWorkspacePolicyError, match="current branch"):
        workspace.commit_all(
            CommitAllRequest(
                repo_full_name="qts/agentic-os",
                workspace_path=workspace_path,
                work_branch=VALID_WORK_BRANCH,
                commit_message="feat: generated change",
            )
        )

    _run_git(["checkout", VALID_WORK_BRANCH], cwd=workspace_path)
    (workspace_path / "change.txt").write_text("generated change\n", encoding="utf-8")
    workspace.commit_all(
        CommitAllRequest(
            repo_full_name="qts/agentic-os",
            workspace_path=workspace_path,
            work_branch=VALID_WORK_BRANCH,
            commit_message="feat: generated change",
        )
    )
    _run_git(["checkout", "main"], cwd=workspace_path)

    with pytest.raises(GitWorkspacePolicyError, match="current branch"):
        workspace.push_branch(
            PushBranchRequest(
                repo_full_name="qts/agentic-os",
                workspace_path=workspace_path,
                base_branch="main",
                work_branch=VALID_WORK_BRANCH,
            )
        )


def test_local_git_workspace_keeps_token_out_of_argv_config_results_and_errors(
    tmp_path: Path,
) -> None:
    token = "TOKEN-SENTINEL-DO-NOT-LEAK"
    remote_path, _base_commit = _seed_bare_remote(tmp_path)
    root = tmp_path / "workers"
    root.mkdir()
    workspace_path = root / "record-1"
    workspace = LocalGitWorkspace(
        git_token=token,
        output_limit_bytes=128,
        allow_local_file_remotes=True,
    )
    workspace.clone_repository(
        CloneWorkspaceRequest(
            repo_full_name="qts/agentic-os",
            remote_url=str(remote_path),
            workspace_root=root,
            workspace_path=workspace_path,
        )
    )

    with pytest.raises(Exception) as error_info:
        workspace.fetch_base_branch(
            FetchBranchRequest(
                repo_full_name="qts/agentic-os",
                workspace_path=workspace_path,
                base_branch="missing-branch",
            )
        )

    config_text = (workspace_path / ".git" / "config").read_text(encoding="utf-8")
    public_results = "\n".join(
        " ".join(result.argv) + result.stdout + result.stderr
        for result in workspace.command_results
    )
    chain_messages: list[str] = []
    error: BaseException | None = error_info.value
    while error is not None:
        chain_messages.append(str(error))
        error = error.__cause__

    assert token not in config_text
    assert token not in public_results
    assert all(token not in message for message in chain_messages)


def test_local_git_workspace_uses_allowlisted_git_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dangerous_env = {
        "AGENTIC_OS_SENTINEL": "must-not-leak",
        "GIT_CONFIG_GLOBAL": "/tmp/host-gitconfig",
        "GIT_CONFIG_COUNT": "1",
        "GIT_SSH": "/tmp/host-ssh",
        "GIT_SSH_COMMAND": "ssh -i /tmp/key",
        "SSH_AUTH_SOCK": "/tmp/agent.sock",
        "GIT_TRACE": "1",
        "GIT_TRACE_PACKET": "1",
        "GIT_DIR": "/tmp/host-git-dir",
        "GIT_WORK_TREE": "/tmp/host-work-tree",
        "GIT_INDEX_FILE": "/tmp/host-index",
        "HTTP_PROXY": "http://proxy.example.test:8080",
        "HTTPS_PROXY": "http://proxy.example.test:8080",
        "NO_PROXY": "example.test",
    }
    for key, value in dangerous_env.items():
        monkeypatch.setenv(key, value)

    env = LocalGitWorkspace()._git_env(tmp_path / "workers")

    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["HOME"] == str(tmp_path / "workers" / ".agentic-os-git-home")
    assert Path(env["GIT_ASKPASS"]).name == "askpass.sh"
    assert "PATH" in env
    assert "AGENTIC_OS_GIT_TOKEN" not in env
    assert not set(dangerous_env).intersection(env)


def test_local_git_workspace_sets_private_token_only_when_present(tmp_path: Path) -> None:
    env = LocalGitWorkspace(git_token="token-123")._git_env(tmp_path / "workers")

    assert env["AGENTIC_OS_GIT_TOKEN"] == "token-123"


def test_local_git_workspace_resolves_fresh_token_from_provider_each_command(
    tmp_path: Path,
) -> None:
    tokens = iter(["inst-token-1", "inst-token-2"])
    workspace = LocalGitWorkspace(git_token_provider=lambda: next(tokens))

    first = workspace._git_env(tmp_path / "workers")
    second = workspace._git_env(tmp_path / "workers")

    assert first["AGENTIC_OS_GIT_TOKEN"] == "inst-token-1"
    assert second["AGENTIC_OS_GIT_TOKEN"] == "inst-token-2"


def test_local_git_workspace_provider_returning_none_sets_no_token(tmp_path: Path) -> None:
    env = LocalGitWorkspace(git_token_provider=lambda: None)._git_env(tmp_path / "workers")

    assert "AGENTIC_OS_GIT_TOKEN" not in env


def test_local_git_workspace_rejects_static_token_and_provider_together() -> None:
    with pytest.raises(ValueError, match="not both"):
        LocalGitWorkspace(git_token="static", git_token_provider=lambda: "dynamic")


def test_local_git_workspace_redacts_provider_resolved_token(tmp_path: Path) -> None:
    workspace = LocalGitWorkspace(git_token_provider=lambda: "PROVIDER-TOKEN-LEAK")
    workspace._git_env(tmp_path / "workers")

    redacted = workspace._sanitize_output("fatal: credential PROVIDER-TOKEN-LEAK rejected")

    assert "PROVIDER-TOKEN-LEAK" not in redacted
    assert "[REDACTED]" in redacted


def test_local_git_workspace_with_provider_enforces_github_match_before_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "workers"
    root.mkdir()
    workspace = LocalGitWorkspace(git_token_provider=lambda: "inst-token")

    def fail_if_run_git(
        self: LocalGitWorkspace,
        args: list[str],
        *,
        cwd: Path | None,
        record: object,
    ) -> GitCommandResult:
        raise AssertionError(f"git subprocess must not run for {args} in {cwd}")

    monkeypatch.setattr(LocalGitWorkspace, "_run_git", fail_if_run_git)

    with pytest.raises(GitWorkspacePolicyError):
        workspace.clone_repository(
            CloneWorkspaceRequest(
                repo_full_name="qts/agentic-os",
                remote_url="https://attacker.example/qts/agentic-os.git",
                workspace_root=root,
                workspace_path=root / "record-1",
            )
        )

    assert workspace.command_results == ()


@pytest.mark.parametrize(
    "remote_url",
    (
        "git@github.com:qts/agentic-os.git",
        "github.com:qts/agentic-os.git",
        "ssh://github.com/qts/agentic-os.git",
        "git://github.com/qts/agentic-os.git",
        "file:///tmp/agentic-os.git",
        "../agentic-os.git",
        "agentic-os.git",
        "https://github.com/qts/agentic-os.git?token=secret",
        "https://github.com/qts/agentic-os.git#main",
        "https://x-token-auth:secret@github.com/qts/agentic-os.git",
    ),
)
def test_local_git_workspace_rejects_non_https_tokenless_remotes_before_subprocess(
    tmp_path: Path,
    remote_url: str,
) -> None:
    root = tmp_path / "workers"
    root.mkdir()
    workspace = LocalGitWorkspace()

    with pytest.raises(GitWorkspacePolicyError):
        workspace.clone_repository(
            CloneWorkspaceRequest(
                repo_full_name="qts/agentic-os",
                remote_url=remote_url,
                workspace_root=root,
                workspace_path=root / "record-1",
            )
        )

    assert workspace.command_results == ()


def test_local_git_workspace_rejects_local_absolute_remote_by_default(tmp_path: Path) -> None:
    remote_path, _base_commit = _seed_bare_remote(tmp_path)
    root = tmp_path / "workers"
    root.mkdir()
    workspace = LocalGitWorkspace()

    with pytest.raises(GitWorkspacePolicyError, match="local"):
        workspace.clone_repository(
            CloneWorkspaceRequest(
                repo_full_name="qts/agentic-os",
                remote_url=str(remote_path),
                workspace_root=root,
                workspace_path=root / "record-1",
            )
        )

    assert workspace.command_results == ()


def test_local_git_workspace_accepts_local_absolute_remote_only_with_explicit_option(
    tmp_path: Path,
) -> None:
    remote_path, _base_commit = _seed_bare_remote(tmp_path)
    root = tmp_path / "workers"
    root.mkdir()
    workspace = LocalGitWorkspace(allow_local_file_remotes=True)

    clone = workspace.clone_repository(
        CloneWorkspaceRequest(
            repo_full_name="qts/agentic-os",
            remote_url=str(remote_path),
            workspace_root=root,
            workspace_path=root / "record-1",
        )
    )

    assert clone.remote_url == str(remote_path)
    assert len(workspace.command_results) == 1


def test_local_git_workspace_duplicate_clone_returns_existing_workspace_without_discarding_state(
    tmp_path: Path,
) -> None:
    remote_path, _base_commit = _seed_bare_remote(tmp_path)
    root = tmp_path / "workers"
    root.mkdir()
    workspace_path = root / "record-1"
    workspace = LocalGitWorkspace(allow_local_file_remotes=True)
    request = CloneWorkspaceRequest(
        repo_full_name="qts/agentic-os",
        remote_url=str(remote_path),
        workspace_root=root,
        workspace_path=workspace_path,
    )

    first_clone = workspace.clone_repository(request)
    local_marker = workspace_path / "retry-local-state.txt"
    local_marker.write_text("preserve local retry state\n", encoding="utf-8")
    second_clone = workspace.clone_repository(request)

    assert second_clone == first_clone
    assert second_clone.workspace_path == workspace_path.resolve()
    assert local_marker.read_text(encoding="utf-8") == "preserve local retry state\n"


def test_local_git_workspace_rejects_duplicate_clone_when_existing_origin_is_wrong(
    tmp_path: Path,
) -> None:
    remote_path, _base_commit = _seed_bare_remote(tmp_path)
    wrong_root = tmp_path / "wrong"
    wrong_root.mkdir()
    wrong_remote_path, _wrong_base_commit = _seed_bare_remote(wrong_root)
    root = tmp_path / "workers"
    root.mkdir()
    workspace_path = root / "record-1"
    workspace = LocalGitWorkspace(allow_local_file_remotes=True)
    request = CloneWorkspaceRequest(
        repo_full_name="qts/agentic-os",
        remote_url=str(remote_path),
        workspace_root=root,
        workspace_path=workspace_path,
    )
    workspace.clone_repository(request)
    _run_git(["remote", "set-url", "origin", str(wrong_remote_path)], cwd=workspace_path)

    with pytest.raises(GitWorkspacePolicyError, match="origin"):
        workspace.clone_repository(request)


def test_local_git_workspace_rejects_duplicate_clone_when_existing_path_is_not_git(
    tmp_path: Path,
) -> None:
    remote_path, _base_commit = _seed_bare_remote(tmp_path)
    root = tmp_path / "workers"
    workspace_path = root / "record-1"
    workspace_path.mkdir(parents=True)
    (workspace_path / "not-git.txt").write_text("unsafe existing state\n", encoding="utf-8")
    workspace = LocalGitWorkspace(allow_local_file_remotes=True)

    with pytest.raises(GitWorkspacePolicyError, match="git"):
        workspace.clone_repository(
            CloneWorkspaceRequest(
                repo_full_name="qts/agentic-os",
                remote_url=str(remote_path),
                workspace_root=root,
                workspace_path=workspace_path,
            )
        )


def test_local_git_workspace_rejects_remote_url_with_embedded_userinfo(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workers"
    root.mkdir()
    workspace = LocalGitWorkspace()

    with pytest.raises(GitWorkspacePolicyError, match="userinfo"):
        workspace.clone_repository(
            CloneWorkspaceRequest(
                repo_full_name="qts/agentic-os",
                remote_url="https://x-token-auth:secret@example.test/qts/agentic-os.git",
                workspace_root=root,
                workspace_path=root / "record-1",
            )
        )

    assert workspace.command_results == ()


@pytest.mark.parametrize(
    "remote_url",
    (
        "https://attacker.example/qts/agentic-os.git",
        "https://github.com/other/repo.git",
        "https://github.com/qts/agentic-os.git?token=secret",
        "https://github.com/qts/agentic-os.git#main",
        "https://x-token-auth:secret@github.com/qts/agentic-os.git",
    ),
)
def test_local_git_workspace_rejects_untrusted_tokenized_https_remotes_before_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    remote_url: str,
) -> None:
    root = tmp_path / "workers"
    root.mkdir()
    workspace = LocalGitWorkspace(git_token="TOKEN-SENTINEL-DO-NOT-LEAK")

    def fail_if_run_git(
        self: LocalGitWorkspace,
        args: list[str],
        *,
        cwd: Path | None,
        record: object,
    ) -> GitCommandResult:
        raise AssertionError(f"git subprocess must not run for {args} in {cwd}")

    monkeypatch.setattr(LocalGitWorkspace, "_run_git", fail_if_run_git)

    with pytest.raises(GitWorkspacePolicyError):
        workspace.clone_repository(
            CloneWorkspaceRequest(
                repo_full_name="qts/agentic-os",
                remote_url=remote_url,
                workspace_root=root,
                workspace_path=root / "record-1",
            )
        )

    assert workspace.command_results == ()


def test_local_git_workspace_accepts_matching_github_https_remote_with_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "workers"
    root.mkdir()
    workspace = LocalGitWorkspace(git_token="TOKEN-SENTINEL-DO-NOT-LEAK")
    calls: list[list[str]] = []

    def record_run_git(
        self: LocalGitWorkspace,
        args: list[str],
        *,
        cwd: Path | None,
        record: object,
    ) -> GitCommandResult:
        calls.append(args)
        return GitCommandResult(
            argv=("git", *args),
            cwd=cwd,
            returncode=0,
            stdout="",
            stderr="",
        )

    monkeypatch.setattr(LocalGitWorkspace, "_run_git", record_run_git)

    clone = workspace.clone_repository(
        CloneWorkspaceRequest(
            repo_full_name="qts/agentic-os",
            remote_url="https://github.com/qts/agentic-os.git",
            workspace_root=root,
            workspace_path=root / "record-1",
        )
    )

    assert clone.remote_url == "https://github.com/qts/agentic-os.git"
    assert calls == [
        [
            "clone",
            "--no-checkout",
            "https://github.com/qts/agentic-os.git",
            str(root / "record-1"),
        ]
    ]


@pytest.mark.parametrize(
    "work_branch",
    (
        "main",
        "agent/123e4567-e89b-12d3-a456-426614174000-fix-thing:refs/heads/main",
        "+HEAD:refs/heads/main",
    ),
)
def test_local_git_workspace_enforces_branch_and_refspec_policy_before_subprocess(
    tmp_path: Path,
    work_branch: str,
) -> None:
    workspace, _remote_path, workspace_path = _prepare_local_workspace(tmp_path)
    command_count = len(workspace.command_results)

    with pytest.raises(GitWorkspacePolicyError):
        workspace.checkout_work_branch(
            CheckoutWorkBranchRequest(
                repo_full_name="qts/agentic-os",
                workspace_path=workspace_path,
                base_branch="main",
                work_branch=work_branch,
            )
        )

    with pytest.raises(GitWorkspacePolicyError):
        workspace.push_branch(
            PushBranchRequest(
                repo_full_name="qts/agentic-os",
                workspace_path=workspace_path,
                base_branch="main",
                work_branch=work_branch,
            )
        )

    assert len(workspace.command_results) == command_count


def test_local_git_workspace_command_failure_is_sanitized_and_bounded(tmp_path: Path) -> None:
    remote_path, _base_commit = _seed_bare_remote(tmp_path)
    root = tmp_path / "workers"
    root.mkdir()
    workspace_path = root / "record-1"
    workspace = LocalGitWorkspace(
        output_limit_bytes=80,
        allow_local_file_remotes=True,
    )
    workspace.clone_repository(
        CloneWorkspaceRequest(
            repo_full_name="qts/agentic-os",
            remote_url=str(remote_path),
            workspace_root=root,
            workspace_path=workspace_path,
        )
    )

    with pytest.raises(RuntimeError) as error_info:
        workspace.fetch_base_branch(
            FetchBranchRequest(
                repo_full_name="qts/agentic-os",
                workspace_path=workspace_path,
                base_branch="missing-branch",
            )
        )

    latest_result = workspace.command_results[-1]
    assert latest_result.returncode != 0
    assert len(latest_result.stdout) <= 80
    assert len(latest_result.stderr) <= 80
    assert len(str(error_info.value)) < 500


def _hook_style_checkout(tmp_path: Path, remote_path: Path) -> tuple[Path, Path]:
    """What a `checkout` Runner Hook leaves: a clone this adapter never made."""

    root = tmp_path / "workers"
    root.mkdir(exist_ok=True)
    workspace_path = root / "record-1"
    _run_git(["clone", str(remote_path), str(workspace_path)])
    _run_git(["checkout", "-B", VALID_WORK_BRANCH], cwd=workspace_path)
    return root, workspace_path


def _adopt_request(root: Path, workspace_path: Path, remote_path: Path) -> AdoptWorkspaceRequest:
    return AdoptWorkspaceRequest(
        repo_full_name="qts/agentic-os",
        remote_url=str(remote_path),
        workspace_root=root,
        workspace_path=workspace_path,
        work_branch=VALID_WORK_BRANCH,
    )


def test_adopting_a_hook_checkout_registers_it_for_every_later_git_call(tmp_path: Path) -> None:
    remote_path, _base_commit = _seed_bare_remote(tmp_path)
    root, workspace_path = _hook_style_checkout(tmp_path, remote_path)
    workspace = LocalGitWorkspace(allow_local_file_remotes=True)

    result = workspace.adopt_existing_clone(_adopt_request(root, workspace_path, remote_path))

    assert result.workspace_path == workspace_path.resolve()
    # The point of the adoption: the calls the Directive makes next stop raising
    # "was not cloned" on a workspace this adapter never cloned (PRD issue 45).
    (workspace_path / "change.txt").write_text("hook checkout\n", encoding="utf-8")
    assert "change.txt" in workspace.collect_git_evidence(workspace_path).status
    commit = workspace.commit_all(
        CommitAllRequest(
            repo_full_name="qts/agentic-os",
            workspace_path=workspace_path,
            work_branch=VALID_WORK_BRANCH,
            commit_message="feat: hook checkout",
        )
    )
    assert commit.commit_id


def test_adopting_a_hook_checkout_refuses_a_foreign_origin(tmp_path: Path) -> None:
    remote_path, _base_commit = _seed_bare_remote(tmp_path)
    root, workspace_path = _hook_style_checkout(tmp_path, remote_path)
    _run_git(["remote", "set-url", "origin", str(tmp_path / "mirror.git")], cwd=workspace_path)
    workspace = LocalGitWorkspace(allow_local_file_remotes=True)

    with pytest.raises(GitWorkspacePolicyError, match="origin"):
        workspace.adopt_existing_clone(_adopt_request(root, workspace_path, remote_path))


def test_adopting_a_hook_checkout_refuses_a_workspace_on_the_wrong_branch(tmp_path: Path) -> None:
    remote_path, _base_commit = _seed_bare_remote(tmp_path)
    root, workspace_path = _hook_style_checkout(tmp_path, remote_path)
    _run_git(["checkout", "-B", "main"], cwd=workspace_path)
    workspace = LocalGitWorkspace(allow_local_file_remotes=True)

    with pytest.raises(GitWorkspacePolicyError, match="Cannot adopt"):
        workspace.adopt_existing_clone(_adopt_request(root, workspace_path, remote_path))


def test_local_git_workspace_satisfies_workspace_interface_structurally() -> None:
    local = LocalGitWorkspace()

    assert isinstance(local, GitWorkspace)
    for method_name in (
        "adopt_existing_clone",
        "clone_repository",
        "fetch_base_branch",
        "checkout_work_branch",
        "commit_all",
        "push_branch",
        "prepare_verifier_workspace",
    ):
        contract_method = getattr(GitWorkspace, method_name)
        assert signature(getattr(type(local), method_name)) == signature(contract_method)


def test_fake_workspace_satisfies_workspace_interface_structurally() -> None:
    fake = FakeGitWorkspace()

    assert isinstance(fake, GitWorkspace)
    for method_name in (
        "adopt_existing_clone",
        "clone_repository",
        "fetch_base_branch",
        "checkout_work_branch",
        "commit_all",
        "push_branch",
        "prepare_verifier_workspace",
    ):
        contract_method = getattr(GitWorkspace, method_name)
        assert signature(getattr(type(fake), method_name)) == signature(contract_method)
