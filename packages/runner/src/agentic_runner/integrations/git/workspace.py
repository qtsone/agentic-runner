from __future__ import annotations

import os
import re
import stat
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit
from uuid import UUID

from agentic_runner.integrations.git.contracts import (
    AdoptWorkspaceRequest,
    CheckoutDefaultBranchRequest,
    CheckoutWorkBranchRequest,
    CheckoutWorkBranchResult,
    CloneWorkspaceRequest,
    CloneWorkspaceResult,
    CommitAllRequest,
    CommitAllResult,
    FetchBranchRequest,
    FetchBranchResult,
    PrepareVerifierWorkspaceRequest,
    PrepareVerifierWorkspaceResult,
    PushBranchRequest,
    PushBranchResult,
)

if TYPE_CHECKING:
    from agentic_runner.integrations.git.evidence import GitEvidence

PROTECTED_BRANCHES = frozenset(("main", "master"))
_GENERATED_WORK_BRANCH_RE = re.compile(
    r"^agent/(?P<work_record_id>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})-"
    r"(?P<slug>[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)$"
)
_UNSAFE_BRANCH_TOKENS = (
    "..",
    " ",
    ":",
    "+",
    "~",
    "^",
    "?",
    "*",
    "[",
    "\\",
    "refs/tags",
)
_BASE_BRANCH_ALLOWED_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
_COMMIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_REPO_PATH_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_WORKSPACE_ID_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_TOKEN_ENV_KEY = "AGENTIC_OS_GIT_TOKEN"
_ASKPASS_SCRIPT = """#!/bin/sh
case "$1" in
  *Username*) printf '%s\\n' 'x-access-token' ;;
  *) printf '%s\\n' "${AGENTIC_OS_GIT_TOKEN}" ;;
esac
"""


class GitWorkspacePolicyError(ValueError):
    """Raised when a workspace or branch violates local git safety policy."""


class GitWorkspaceCommandError(RuntimeError):
    """Raised when a sanitized local git subprocess fails."""


@dataclass(frozen=True)
class GitCommandResult:
    """Sanitized, bounded public record of one git subprocess invocation."""

    argv: tuple[str, ...]
    cwd: Path | None
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


@dataclass(frozen=True)
class _LocalWorkspaceRecord:
    repo_full_name: str
    remote_url: str
    workspace_root: Path
    workspace_path: Path


def validate_work_branch(work_branch: str) -> str:
    """Validate that a branch is a generated WorkRecord branch, not a refspec."""

    if work_branch in PROTECTED_BRANCHES:
        raise GitWorkspacePolicyError(
            f"Protected branch '{work_branch}' cannot be used as work branch"
        )
    if work_branch in {"--all", "--mirror"} or work_branch.startswith("-"):
        raise GitWorkspacePolicyError(f"Refspec-like branch '{work_branch}' is not allowed")
    if work_branch.startswith(("/", ".")) or work_branch.endswith(("/", ".")):
        raise GitWorkspacePolicyError(f"Unsafe branch boundary in '{work_branch}'")
    if any(token in work_branch for token in _UNSAFE_BRANCH_TOKENS):
        raise GitWorkspacePolicyError(f"Unsafe branch content in '{work_branch}'")

    match = _GENERATED_WORK_BRANCH_RE.fullmatch(work_branch)
    if match is None:
        raise GitWorkspacePolicyError(
            "Work branch must match 'agent/<work_record_id>-<slug>' with a UUID work_record_id"
        )

    UUID(match.group("work_record_id"))
    return work_branch


def validate_base_branch(base_branch: str) -> str:
    """Validate that a base branch is a safe short branch name, not a ref/revision."""

    if base_branch == "":
        raise GitWorkspacePolicyError("Base branch cannot be empty")
    if not _BASE_BRANCH_ALLOWED_RE.fullmatch(base_branch):
        raise GitWorkspacePolicyError(f"Unsafe base branch content in '{base_branch}'")
    if any(ord(character) < 32 or ord(character) == 127 for character in base_branch):
        raise GitWorkspacePolicyError(f"Unsafe base branch control character in '{base_branch}'")
    if base_branch in {"--all", "--mirror"} or base_branch.startswith("-"):
        raise GitWorkspacePolicyError(f"Refspec-like base branch '{base_branch}' is not allowed")
    if base_branch == "HEAD" or base_branch.startswith("refs/"):
        raise GitWorkspacePolicyError(f"Ref-like base branch '{base_branch}' is not allowed")
    if base_branch.startswith(("/", ".")) or base_branch.endswith(("/", ".")):
        raise GitWorkspacePolicyError(f"Unsafe base branch boundary in '{base_branch}'")
    if any(token in base_branch for token in ("..", "//")):
        raise GitWorkspacePolicyError(f"Unsafe base branch content in '{base_branch}'")
    if any(component.startswith(".") for component in base_branch.split("/")):
        raise GitWorkspacePolicyError(f"Unsafe base branch path component in '{base_branch}'")
    if any(component.endswith(".lock") for component in base_branch.split("/")):
        raise GitWorkspacePolicyError(f"Unsafe base branch lock suffix in '{base_branch}'")

    return base_branch


def validate_push_branch(
    *,
    work_branch: str,
    base_branch: str,
    protected_branches: tuple[str, ...] = (),
) -> str:
    """Reject pushes to protected or request-base branches before building a refspec."""

    protected = PROTECTED_BRANCHES | frozenset(protected_branches) | {base_branch}
    if work_branch in protected:
        raise GitWorkspacePolicyError(f"Protected branch '{work_branch}' cannot be pushed")
    return validate_work_branch(work_branch)


def validate_commit_sha(commit_sha: str) -> str:
    """Validate a full commit SHA before using it as a verifier reset target."""

    if _COMMIT_SHA_RE.fullmatch(commit_sha) is None:
        raise GitWorkspacePolicyError("Approved head must be a full 40-character commit SHA")
    return commit_sha.lower()


def build_push_refspec(work_branch: str) -> str:
    """Build the only allowed push refspec for generated work branches."""

    safe_branch = validate_work_branch(work_branch)
    return f"HEAD:refs/heads/{safe_branch}"


def contract_workspace_path(
    *,
    workspace_root: Path,
    contract_id: str,
    work_record_id: str,
) -> Path:
    """``WORKSPACE_ROOT/{contract_id}/{work_record_id}`` (ADR-0015 §2, PRD issue 30).

    The Contract owns the Workspace, not the repository: two Contracts' Work Records on
    one repo run concurrently on their own branches, and there is no repository lock —
    conflicts surface at the pull request, as between two human contractors. Replaces the
    ``{owner}/{repo}/{work_record_id}`` layout, which keyed the tree on something that is
    neither a platform id nor an isolation boundary.

    Both segments are platform ids (map ticket 07); anything else is refused here rather
    than allowed to walk out of the workspace root.
    """

    for label, segment in (("contract id", contract_id), ("work record id", work_record_id)):
        if not _WORKSPACE_ID_SEGMENT_RE.fullmatch(segment):
            raise GitWorkspacePolicyError(f"{label} is not a safe path segment: {segment!r}")
    return workspace_root / contract_id / work_record_id


GIT_METADATA_DIR = ".git"


def require_runner_owned_git_dir(workspace_path: Path) -> None:
    """Refuse to run the Runner's git over a ``.git`` the Contract could have replaced.

    ``workers/contract_isolation.hand_workspace_to_contract`` keeps ``.git`` owned by the
    Runner, and that is *not* by itself the boundary: the Contract owns the Workspace
    directory containing it, and on POSIX rename/create/unlink of an entry is governed by
    write+execute on the parent, never by the entry's own ownership. A Directive can
    therefore ``mv .git .git.stash; cp -r .git.stash .git`` and end up owning a ``.git``
    of its own carrying an arbitrary ``hooks/pre-commit``, ``core.fsmonitor`` or
    ``filter.*.clean`` — all of which the Runner's next ``status``/``add``/``commit``
    would execute as the Runner, with CAP_SETUID, CAP_CHOWN and CAP_DAC_OVERRIDE. The
    ownership check that would have caught it is exactly the one ``-c safe.directory``
    suppresses, so it is re-made here, on every Runner git call into a Workspace.

    A ``lstat``, so a ``.git`` symlinked or replaced by a gitfile pointing at a tree the
    Contract owns is refused too. There is no TOCTOU window worth chasing: the Runner's
    git steps run between Directives, with none of that Contract's processes executing.
    """

    git_dir = workspace_path / GIT_METADATA_DIR
    try:
        entry = os.lstat(git_dir)
    except OSError as error:
        raise GitWorkspacePolicyError(
            f"Git metadata '{git_dir}' is missing or unreadable"
        ) from error
    if not stat.S_ISDIR(entry.st_mode) or entry.st_uid != os.geteuid():
        raise GitWorkspacePolicyError(
            f"Git metadata '{git_dir}' is not a directory owned by the Runner "
            f"(uid {entry.st_uid}, mode {entry.st_mode:#o}): the Workspace was tampered with"
        )


def resolve_workspace_path(workspace_root: Path, workspace_path: Path) -> Path:
    """Resolve a workspace path and ensure it stays inside the worker workspace root."""

    resolved_root = workspace_root.resolve()
    resolved_path = workspace_path.resolve()
    if resolved_path != resolved_root and resolved_root not in resolved_path.parents:
        raise GitWorkspacePolicyError(
            f"Workspace path '{resolved_path}' escapes worker workspace root '{resolved_root}'"
        )
    return resolved_path


class LocalGitWorkspace:
    """Subprocess-backed git workspace using tokenless remotes and isolated auth state."""

    def __init__(
        self,
        *,
        git_token: str | None = None,
        git_token_provider: Callable[[], str | None] | None = None,
        command_timeout_seconds: int = 30,
        output_limit_bytes: int = 4096,
        allow_local_file_remotes: bool = False,
    ) -> None:
        if command_timeout_seconds <= 0:
            raise ValueError("command_timeout_seconds must be positive")
        if output_limit_bytes <= 0:
            raise ValueError("output_limit_bytes must be positive")
        if git_token is not None and git_token_provider is not None:
            raise ValueError("Provide either git_token or git_token_provider, not both")
        self._static_git_token = git_token
        self._git_token_provider = git_token_provider
        self._has_token_auth = git_token is not None or git_token_provider is not None
        self._last_resolved_token: str | None = None
        self._command_timeout_seconds = command_timeout_seconds
        self._output_limit_bytes = output_limit_bytes
        self._allow_local_file_remotes = allow_local_file_remotes
        self._command_results: list[GitCommandResult] = []
        self._workspaces: dict[Path, _LocalWorkspaceRecord] = {}

    def _resolve_git_token(self) -> str | None:
        """Resolve the current git token, minting a fresh one per call when a provider is set."""

        if self._git_token_provider is not None:
            token = self._git_token_provider()
        else:
            token = self._static_git_token
        self._last_resolved_token = token or None
        return self._last_resolved_token

    @property
    def command_results(self) -> tuple[GitCommandResult, ...]:
        return tuple(self._command_results)

    def clone_repository(self, request: CloneWorkspaceRequest) -> CloneWorkspaceResult:
        _validate_tokenless_remote_url(
            request.remote_url,
            repo_full_name=request.repo_full_name,
            require_github_match=self._has_token_auth,
            allow_local_file_remotes=self._allow_local_file_remotes,
        )
        workspace_path = resolve_workspace_path(request.workspace_root, request.workspace_path)
        workspace_root = request.workspace_root.resolve()
        workspace_root.mkdir(parents=True, exist_ok=True)
        workspace_path.parent.mkdir(parents=True, exist_ok=True)
        record = _LocalWorkspaceRecord(
            repo_full_name=request.repo_full_name,
            remote_url=request.remote_url,
            workspace_root=workspace_root,
            workspace_path=workspace_path,
        )
        if workspace_path.exists():
            record = self._rehydrate_existing_clone(record)
            return CloneWorkspaceResult(
                repo_full_name=record.repo_full_name,
                remote_url=record.remote_url,
                workspace_root=record.workspace_root,
                workspace_path=record.workspace_path,
            )

        self._run_git(
            ["clone", "--no-checkout", request.remote_url, str(workspace_path)],
            cwd=None,
            record=record,
        )
        self._workspaces[workspace_path] = record
        return CloneWorkspaceResult(
            repo_full_name=request.repo_full_name,
            remote_url=request.remote_url,
            workspace_root=workspace_root,
            workspace_path=workspace_path,
        )

    def _rehydrate_existing_clone(self, record: _LocalWorkspaceRecord) -> _LocalWorkspaceRecord:
        try:
            is_work_tree = self._run_git(
                ["rev-parse", "--is-inside-work-tree"],
                cwd=record.workspace_path,
                record=record,
            ).stdout.strip()
            top_level = self._run_git(
                ["rev-parse", "--show-toplevel"],
                cwd=record.workspace_path,
                record=record,
            ).stdout.strip()
            origin_url = self._run_git(
                ["config", "--get", "remote.origin.url"],
                cwd=record.workspace_path,
                record=record,
            ).stdout.strip()
        except GitWorkspaceCommandError as error:
            raise GitWorkspacePolicyError(
                f"Existing clone workspace '{record.workspace_path}' is not a git work tree"
            ) from error

        if is_work_tree != "true" or Path(top_level).resolve() != record.workspace_path:
            raise GitWorkspacePolicyError(
                f"Existing clone workspace '{record.workspace_path}' is not a git work tree root"
            )
        if origin_url != record.remote_url:
            raise GitWorkspacePolicyError(
                "Existing clone workspace origin does not match expected origin"
            )

        self._workspaces[record.workspace_path] = record
        return record

    def adopt_existing_clone(self, request: AdoptWorkspaceRequest) -> CloneWorkspaceResult:
        work_branch = validate_work_branch(request.work_branch)
        record = self._rehydrate_existing_clone(
            _LocalWorkspaceRecord(
                repo_full_name=request.repo_full_name,
                remote_url=request.remote_url,
                workspace_root=request.workspace_root.resolve(),
                workspace_path=resolve_workspace_path(
                    request.workspace_root, request.workspace_path
                ),
            )
        )
        self._require_current_branch(record, work_branch, operation="adopt")
        return CloneWorkspaceResult(
            repo_full_name=record.repo_full_name,
            remote_url=record.remote_url,
            workspace_root=record.workspace_root,
            workspace_path=record.workspace_path,
        )

    def fetch_base_branch(self, request: FetchBranchRequest) -> FetchBranchResult:
        base_branch = validate_base_branch(request.base_branch)
        record = self._get_workspace(request.workspace_path)
        self._require_same_repo(record, request.repo_full_name)
        self._run_git(
            ["fetch", "origin", f"{base_branch}:refs/remotes/origin/{base_branch}"],
            cwd=record.workspace_path,
            record=record,
        )
        return FetchBranchResult(
            repo_full_name=request.repo_full_name,
            workspace_path=record.workspace_path,
            base_branch=base_branch,
        )

    def checkout_default_branch(self, request: CheckoutDefaultBranchRequest) -> FetchBranchResult:
        record = self._get_workspace(request.workspace_path)
        self._require_same_repo(record, request.repo_full_name)
        # Asked of the remote, not read off ``refs/remotes/origin/HEAD``: a clone sets that
        # ref only when the remote's HEAD resolved at clone time.
        advertised = self._run_git(
            ["ls-remote", "--symref", "origin", "HEAD"],
            cwd=record.workspace_path,
            record=record,
        ).stdout
        remote_head = next(
            (
                line.split()[1]
                for line in advertised.splitlines()
                if line.startswith("ref: ") and line.endswith("\tHEAD")
            ),
            "",
        )
        base_branch = validate_base_branch(remote_head.removeprefix("refs/heads/"))
        self._run_git(
            ["checkout", "--detach", f"refs/remotes/origin/{base_branch}"],
            cwd=record.workspace_path,
            record=record,
        )
        return FetchBranchResult(
            repo_full_name=request.repo_full_name,
            workspace_path=record.workspace_path,
            base_branch=base_branch,
        )

    def checkout_work_branch(
        self,
        request: CheckoutWorkBranchRequest,
    ) -> CheckoutWorkBranchResult:
        base_branch = validate_base_branch(request.base_branch)
        work_branch = validate_work_branch(request.work_branch)
        record = self._get_workspace(request.workspace_path)
        self._require_same_repo(record, request.repo_full_name)
        checkout_ref = f"refs/remotes/origin/{base_branch}"
        if self._remote_work_branch_exists(record, work_branch):
            checkout_ref = f"refs/remotes/origin/{work_branch}"
            self._run_git(
                ["fetch", "origin", f"{work_branch}:{checkout_ref}"],
                cwd=record.workspace_path,
                record=record,
            )
        self._run_git(
            ["checkout", "-B", work_branch, checkout_ref],
            cwd=record.workspace_path,
            record=record,
        )
        return CheckoutWorkBranchResult(
            repo_full_name=request.repo_full_name,
            workspace_path=record.workspace_path,
            base_branch=base_branch,
            work_branch=work_branch,
        )

    def _remote_work_branch_exists(
        self,
        record: _LocalWorkspaceRecord,
        work_branch: str,
    ) -> bool:
        try:
            self._run_git(
                ["ls-remote", "--exit-code", "origin", f"refs/heads/{work_branch}"],
                cwd=record.workspace_path,
                record=record,
            )
        except GitWorkspaceCommandError:
            latest_result = self._command_results[-1]
            if latest_result.returncode == 2:
                return False
            raise
        return True

    def commit_all(self, request: CommitAllRequest) -> CommitAllResult:
        work_branch = validate_work_branch(request.work_branch)
        record = self._get_workspace(request.workspace_path)
        self._require_same_repo(record, request.repo_full_name)
        self._require_current_branch(record, work_branch, operation="commit")

        self._run_git(["add", "--all"], cwd=record.workspace_path, record=record)
        status = self._run_git(["status", "--porcelain"], cwd=record.workspace_path, record=record)
        if status.stdout == "":
            commit_id = self._run_git(
                ["rev-parse", "HEAD"],
                cwd=record.workspace_path,
                record=record,
            ).stdout.strip()
            return CommitAllResult(
                repo_full_name=request.repo_full_name,
                workspace_path=record.workspace_path,
                work_branch=work_branch,
                commit_message=request.commit_message,
                author_name=request.author_name,
                author_email=request.author_email,
                commit_id=commit_id,
            )
        self._run_git(
            [
                "-c",
                f"user.name={request.author_name}",
                "-c",
                f"user.email={request.author_email}",
                "commit",
                "-m",
                request.commit_message,
                "--author",
                f"{request.author_name} <{request.author_email}>",
            ],
            cwd=record.workspace_path,
            record=record,
        )
        commit_id = self._run_git(
            ["rev-parse", "HEAD"],
            cwd=record.workspace_path,
            record=record,
        ).stdout.strip()
        return CommitAllResult(
            repo_full_name=request.repo_full_name,
            workspace_path=record.workspace_path,
            work_branch=work_branch,
            commit_message=request.commit_message,
            author_name=request.author_name,
            author_email=request.author_email,
            commit_id=commit_id,
        )

    def push_branch(self, request: PushBranchRequest) -> PushBranchResult:
        validate_push_branch(
            work_branch=request.work_branch,
            base_branch=request.base_branch,
            protected_branches=request.protected_branches,
        )
        record = self._get_workspace(request.workspace_path)
        self._require_same_repo(record, request.repo_full_name)
        self._require_current_branch(record, request.work_branch, operation="push")
        commit_id = self._run_git(
            ["rev-parse", "HEAD"],
            cwd=record.workspace_path,
            record=record,
        ).stdout.strip()
        refspec = build_push_refspec(request.work_branch)
        self._run_git(["push", "origin", refspec], cwd=record.workspace_path, record=record)
        return PushBranchResult(
            repo_full_name=request.repo_full_name,
            workspace_path=record.workspace_path,
            work_branch=request.work_branch,
            refspec=refspec,
            commit_id=commit_id,
        )

    def prepare_verifier_workspace(
        self,
        request: PrepareVerifierWorkspaceRequest,
    ) -> PrepareVerifierWorkspaceResult:
        work_branch = validate_work_branch(request.work_branch)
        approved_head_sha = validate_commit_sha(request.approved_head_sha)
        record = self._get_or_rehydrate_verifier_workspace(
            workspace_root=request.workspace_root,
            workspace_path=request.workspace_path,
            repo_full_name=request.repo_full_name,
        )
        self._require_same_repo(record, request.repo_full_name)
        self._require_clean_verifier_workspace(record)

        remote_ref = f"refs/remotes/origin/{work_branch}"
        self._run_git(
            ["fetch", "origin", f"{work_branch}:{remote_ref}"],
            cwd=record.workspace_path,
            record=record,
        )
        fetched_head_sha = (
            self._run_git(
                ["rev-parse", remote_ref],
                cwd=record.workspace_path,
                record=record,
            )
            .stdout.strip()
            .lower()
        )
        if fetched_head_sha != approved_head_sha:
            raise GitWorkspacePolicyError(
                f"Fetched work branch head '{fetched_head_sha}' does not match "
                f"approved head '{approved_head_sha}'"
            )

        local_head_sha = self._local_head_sha_or_none(record)
        if local_head_sha is not None and local_head_sha != approved_head_sha:
            raise GitWorkspacePolicyError(
                f"Verifier workspace local HEAD '{local_head_sha}' does not match "
                f"approved head '{approved_head_sha}' before verifier reset"
            )

        self._run_git(
            ["checkout", "-B", work_branch, approved_head_sha],
            cwd=record.workspace_path,
            record=record,
        )
        self._run_git(
            ["reset", "--hard", approved_head_sha],
            cwd=record.workspace_path,
            record=record,
        )
        attested_head_sha = (
            self._run_git(
                ["rev-parse", "HEAD"],
                cwd=record.workspace_path,
                record=record,
            )
            .stdout.strip()
            .lower()
        )
        if attested_head_sha != approved_head_sha:
            raise GitWorkspacePolicyError(
                f"Verifier workspace HEAD '{attested_head_sha}' does not match "
                f"approved head '{approved_head_sha}'"
            )

        status_summary = self._require_clean_verifier_workspace(record)

        return PrepareVerifierWorkspaceResult(
            repo_full_name=request.repo_full_name,
            workspace_root=record.workspace_root,
            workspace_path=record.workspace_path,
            work_branch=work_branch,
            approved_head_sha=approved_head_sha,
            attested_head_sha=attested_head_sha,
            status_summary=status_summary,
        )

    def collect_git_evidence(
        self,
        workspace_path: Path,
        output_limit_bytes: int | None = None,
    ) -> GitEvidence:
        from agentic_runner.integrations.git.evidence import collect_git_evidence

        record = self._get_workspace(workspace_path)
        return collect_git_evidence(
            workspace_path=record.workspace_path,
            workspace_root=record.workspace_root,
            output_limit_bytes=output_limit_bytes or self._output_limit_bytes,
            command_timeout_seconds=self._command_timeout_seconds,
        )

    def _get_workspace(self, workspace_path: Path) -> _LocalWorkspaceRecord:
        resolved_path = workspace_path.resolve()
        try:
            record = self._workspaces[resolved_path]
        except KeyError as error:
            raise GitWorkspacePolicyError(
                f"Git workspace '{resolved_path}' was not cloned"
            ) from error
        resolve_workspace_path(record.workspace_root, workspace_path)
        return record

    def _get_or_rehydrate_verifier_workspace(
        self,
        *,
        workspace_root: Path,
        workspace_path: Path,
        repo_full_name: str,
    ) -> _LocalWorkspaceRecord:
        resolved_path = resolve_workspace_path(workspace_root, workspace_path)
        try:
            record = self._workspaces[resolved_path]
        except KeyError:
            record = self._rehydrate_verifier_workspace(
                repo_full_name=repo_full_name,
                workspace_root=workspace_root,
                workspace_path=resolved_path,
            )
        else:
            resolve_workspace_path(record.workspace_root, workspace_path)
        return record

    def _rehydrate_verifier_workspace(
        self,
        *,
        repo_full_name: str,
        workspace_root: Path,
        workspace_path: Path,
    ) -> _LocalWorkspaceRecord:
        expected_remote_url = _expected_github_remote_url(repo_full_name)
        record = _LocalWorkspaceRecord(
            repo_full_name=repo_full_name,
            remote_url=expected_remote_url,
            workspace_root=workspace_root.resolve(),
            workspace_path=workspace_path,
        )
        if not record.workspace_path.exists():
            raise GitWorkspacePolicyError(
                f"Existing verifier workspace '{record.workspace_path}' does not exist"
            )
        try:
            is_work_tree = self._run_git(
                ["rev-parse", "--is-inside-work-tree"],
                cwd=record.workspace_path,
                record=record,
            ).stdout.strip()
            top_level = self._run_git(
                ["rev-parse", "--show-toplevel"],
                cwd=record.workspace_path,
                record=record,
            ).stdout.strip()
            origin_url = self._run_git(
                ["config", "--get", "remote.origin.url"],
                cwd=record.workspace_path,
                record=record,
            ).stdout.strip()
        except GitWorkspaceCommandError as error:
            raise GitWorkspacePolicyError(
                f"Existing verifier workspace '{record.workspace_path}' is not a git work tree"
            ) from error

        if is_work_tree != "true" or Path(top_level).resolve() != record.workspace_path:
            raise GitWorkspacePolicyError(
                f"Existing verifier workspace '{record.workspace_path}' is not a git work tree root"
            )
        if origin_url != expected_remote_url:
            raise GitWorkspacePolicyError(
                f"Existing verifier workspace origin '{origin_url}' does not match expected "
                f"origin '{expected_remote_url}'"
            )
        _validate_tokenless_remote_url(
            origin_url,
            repo_full_name=repo_full_name,
            require_github_match=True,
            allow_local_file_remotes=False,
        )

        self._workspaces[record.workspace_path] = record
        return record

    def _require_same_repo(self, record: _LocalWorkspaceRecord, repo_full_name: str) -> None:
        if record.repo_full_name != repo_full_name:
            raise GitWorkspacePolicyError(
                f"Workspace repo '{record.repo_full_name}' does not match request repo "
                f"'{repo_full_name}'"
            )

    def _require_current_branch(
        self,
        record: _LocalWorkspaceRecord,
        work_branch: str,
        *,
        operation: str,
    ) -> None:
        current_branch = self._run_git(
            ["branch", "--show-current"],
            cwd=record.workspace_path,
            record=record,
        ).stdout.strip()
        if current_branch != work_branch:
            raise GitWorkspacePolicyError(
                f"Cannot {operation} branch '{work_branch}' while current branch is "
                f"'{current_branch}'"
            )

    def _require_clean_verifier_workspace(self, record: _LocalWorkspaceRecord) -> str:
        status_summary = self._run_git(
            ["status", "--short"],
            cwd=record.workspace_path,
            record=record,
        ).stdout
        if status_summary:
            raise GitWorkspacePolicyError(
                "Verifier workspace is dirty immediately before verifier execution: "
                f"{status_summary!r}"
            )
        return status_summary

    def _local_head_sha_or_none(self, record: _LocalWorkspaceRecord) -> str | None:
        try:
            return (
                self._run_git(
                    ["rev-parse", "--verify", "HEAD"],
                    cwd=record.workspace_path,
                    record=record,
                )
                .stdout.strip()
                .lower()
            )
        except GitWorkspaceCommandError:
            return None

    def _run_git(
        self,
        args: list[str],
        *,
        cwd: Path | None,
        record: _LocalWorkspaceRecord,
    ) -> GitCommandResult:
        # Two settings guard the same seam: the Runner runs git as its own uid over a
        # worktree owned by a Contract's uid (ADR-0015 §1 — the Runner is the only thing
        # that may touch the git credential, so the clone/fetch/commit/push half stays
        # here while the Directive runs as the Contract).
        #
        # `safe.directory` names this one workspace and never `*`. Without it git refuses
        # every command in that tree with "detected dubious ownership"; with `*` it would
        # also trust every *other* repository it meets, including one the Contract made.
        # It also suppresses the one check that would notice the Contract swapping the
        # whole `.git` out from under the Runner (it owns the directory that contains
        # it), so `require_runner_owned_git_dir` re-makes that check below before every
        # call into a Workspace: what git reads as commands there (hooks,
        # `core.fsmonitor`, `filter.*.clean`) has to be the Runner's own.
        #
        # `diff.ignoreSubmodules=dirty` covers the way back in. A Directive can `git init`
        # a directory inside its checkout, and once `git add --all` records that as a
        # gitlink the Runner's next `status`/`diff` descends into it to ask whether it is
        # dirty — which runs a git *inside* it, reading the config the Contract owns:
        # `core.fsmonitor` and `filter.*.clean` there both execute as the Runner. The
        # ownership check does not fire on that path (a submodule is opened from the
        # superproject, not discovered from a directory), so the descent itself is what
        # has to stop; `git status` honours this through `git_diff_ui_config`. `dirty`
        # rather than `all`: it drops exactly the worktree question that needs the
        # descent, while a gitlink still shows up and still commits — with `all`, `git
        # commit` reports "nothing to commit" for a gitlink-only change and exits 1.
        # Nothing real is lost: the Runner clones `--no-checkout` and never initialises a
        # submodule, so it has no submodule worktree to report on.
        argv = [
            "git",
            "-c",
            "credential.helper=",
            "-c",
            f"safe.directory={record.workspace_path}",
            "-c",
            "diff.ignoreSubmodules=dirty",
            *args,
        ]
        if cwd is not None:
            require_runner_owned_git_dir(record.workspace_path)
        env = self._git_env(record.workspace_root)
        try:
            completed = subprocess.run(
                argv,
                cwd=cwd,
                env=env,
                shell=False,
                capture_output=True,
                text=True,
                timeout=self._command_timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            result = GitCommandResult(
                argv=tuple(argv),
                cwd=cwd,
                returncode=-1,
                stdout=self._sanitize_output(error.stdout),
                stderr=self._sanitize_output(error.stderr),
                timed_out=True,
            )
            self._command_results.append(result)
            raise GitWorkspaceCommandError(
                f"Git command timed out after {self._command_timeout_seconds}s: "
                f"{_format_safe_argv(argv)}"
            ) from None

        result = GitCommandResult(
            argv=tuple(argv),
            cwd=cwd,
            returncode=completed.returncode,
            stdout=self._sanitize_output(completed.stdout),
            stderr=self._sanitize_output(completed.stderr),
        )
        self._command_results.append(result)
        if completed.returncode != 0:
            raise GitWorkspaceCommandError(
                f"Git command failed with exit code {completed.returncode}: "
                f"{_format_safe_argv(argv)}; stderr={result.stderr!r}"
            ) from None
        return result

    def _git_env(self, workspace_root: Path) -> dict[str, str]:
        home = workspace_root / ".agentic-os-git-home"
        home.mkdir(mode=0o700, parents=True, exist_ok=True)
        askpass_path = home / "askpass.sh"
        if not askpass_path.exists() or askpass_path.read_text(encoding="utf-8") != _ASKPASS_SCRIPT:
            askpass_path.write_text(_ASKPASS_SCRIPT, encoding="utf-8")
            askpass_path.chmod(0o700)

        env = _base_git_subprocess_env()
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        env["HOME"] = str(home)
        env["GIT_ASKPASS"] = str(askpass_path)
        token = self._resolve_git_token()
        if token:
            env[_TOKEN_ENV_KEY] = token
        return env

    def _sanitize_output(self, output: str | bytes | None) -> str:
        if output is None:
            return ""
        text = output.decode(errors="replace") if isinstance(output, bytes) else output
        token = self._last_resolved_token
        if token:
            text = text.replace(token, "[REDACTED]")
        return text[: self._output_limit_bytes]


def _base_git_subprocess_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for key in ("PATH", "LANG", "LC_ALL", "LC_CTYPE"):
        value = os.environ.get(key)
        if value is not None:
            env[key] = value
    return env


def _validate_tokenless_remote_url(
    remote_url: str,
    *,
    repo_full_name: str,
    require_github_match: bool,
    allow_local_file_remotes: bool,
) -> None:
    if Path(remote_url).is_absolute():
        if allow_local_file_remotes:
            return
        raise GitWorkspacePolicyError(
            "Local absolute remotes are only allowed when local file remotes are enabled"
        )

    parsed = urlsplit(remote_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise GitWorkspacePolicyError("Remote URL must be a tokenless HTTPS URL")
    if parsed.username is not None or parsed.password is not None:
        raise GitWorkspacePolicyError("Remote URL must be tokenless and must not contain userinfo")
    if parsed.query or parsed.fragment:
        raise GitWorkspacePolicyError("Remote URL must not contain query or fragment components")
    if require_github_match:
        expected_path = _validated_github_repo_path(repo_full_name)
        if parsed.hostname != "github.com" or parsed.path != expected_path:
            raise GitWorkspacePolicyError(
                "Tokenized git operations require a github.com remote matching repo_full_name"
            )


def _validated_github_repo_path(repo_full_name: str) -> str:
    parts = repo_full_name.split("/")
    if len(parts) != 2:
        raise GitWorkspacePolicyError("repo_full_name must be an owner/repo name")
    if any(
        part in {"", ".", ".."} or _REPO_PATH_SEGMENT_RE.fullmatch(part) is None for part in parts
    ):
        raise GitWorkspacePolicyError("repo_full_name must contain safe owner/repo segments")
    return f"/{parts[0]}/{parts[1]}.git"


def _expected_github_remote_url(repo_full_name: str) -> str:
    return f"https://github.com{_validated_github_repo_path(repo_full_name)}"


def _format_safe_argv(argv: list[str]) -> str:
    return " ".join(argv)
