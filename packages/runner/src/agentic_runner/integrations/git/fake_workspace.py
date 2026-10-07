from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agentic_runner.integrations.git.contracts import (
    AdoptWorkspaceRequest,
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
from agentic_runner.integrations.git.evidence import GitEvidence, build_git_evidence
from agentic_runner.integrations.git.workspace import (
    GitWorkspacePolicyError,
    build_push_refspec,
    resolve_workspace_path,
    validate_base_branch,
    validate_commit_sha,
    validate_push_branch,
    validate_work_branch,
)


@dataclass(frozen=True)
class FakeGitWorkspaceCall:
    """Recorded fake git operation for deterministic assertions."""

    operation: str
    repo_full_name: str
    workspace_path: Path


@dataclass
class _WorkspaceRecord:
    repo_full_name: str
    remote_url: str
    workspace_root: Path
    workspace_path: Path
    base_branch: str | None = None
    current_branch: str | None = None
    current_branch_base_commit_id: str | None = None
    latest_commit_id: str | None = None
    verifier_head_sha: str | None = None
    verifier_status_summary: str = ""


class FakeGitWorkspace:
    """Deterministic in-memory git workspace adapter for tests and workflow rehearsal."""

    def __init__(self, *, status_evidence: str = "", diff_evidence: str = "") -> None:
        self._workspaces: dict[Path, _WorkspaceRecord] = {}
        self._remote_branch_heads: dict[tuple[str, str], str] = {}
        self._calls: list[FakeGitWorkspaceCall] = []
        self._next_commit_number = 1
        self._status_evidence = status_evidence
        self._diff_evidence = diff_evidence

    @property
    def calls(self) -> tuple[FakeGitWorkspaceCall, ...]:
        return tuple(self._calls)

    def clone_repository(self, request: CloneWorkspaceRequest) -> CloneWorkspaceResult:
        workspace_path = resolve_workspace_path(request.workspace_root, request.workspace_path)
        workspace_root = request.workspace_root.resolve()
        existing_record = self._workspaces.get(workspace_path)
        if existing_record is not None:
            self._require_same_repo(existing_record, request.repo_full_name)
            if existing_record.remote_url != request.remote_url:
                raise GitWorkspacePolicyError(
                    "Existing fake workspace origin does not match expected origin"
                )
            self._record_call("clone_repository", request.repo_full_name, workspace_path)
            return CloneWorkspaceResult(
                repo_full_name=existing_record.repo_full_name,
                remote_url=existing_record.remote_url,
                workspace_root=existing_record.workspace_root,
                workspace_path=existing_record.workspace_path,
            )

        record = _WorkspaceRecord(
            repo_full_name=request.repo_full_name,
            remote_url=request.remote_url,
            workspace_root=workspace_root,
            workspace_path=workspace_path,
        )
        # The real adapter's clone materializes the directory; consumers (e.g. the
        # fix Directive's workspace-exists check) rely on that part of the contract.
        workspace_path.mkdir(parents=True, exist_ok=True)
        self._workspaces[workspace_path] = record
        self._record_call("clone_repository", request.repo_full_name, workspace_path)
        return CloneWorkspaceResult(
            repo_full_name=request.repo_full_name,
            remote_url=request.remote_url,
            workspace_root=workspace_root,
            workspace_path=workspace_path,
        )

    def adopt_existing_clone(self, request: AdoptWorkspaceRequest) -> CloneWorkspaceResult:
        validate_work_branch(request.work_branch)
        workspace_path = resolve_workspace_path(request.workspace_root, request.workspace_path)
        if not workspace_path.exists():
            raise GitWorkspacePolicyError(f"Adopted workspace '{workspace_path}' does not exist")
        record = self._workspaces.get(workspace_path)
        if record is None:
            record = _WorkspaceRecord(
                repo_full_name=request.repo_full_name,
                remote_url=request.remote_url,
                workspace_root=request.workspace_root.resolve(),
                workspace_path=workspace_path,
            )
            self._workspaces[workspace_path] = record
        self._require_same_repo(record, request.repo_full_name)
        # The real adapter reads these off the adopted checkout; the fake is told them,
        # so commit/push see the same "work branch is checked out" state a clone leaves.
        record.current_branch = request.work_branch
        record.current_branch_base_commit_id = self._remote_branch_heads.get(
            (request.repo_full_name, request.work_branch)
        )
        record.latest_commit_id = record.current_branch_base_commit_id
        self._record_call("adopt_existing_clone", request.repo_full_name, workspace_path)
        return CloneWorkspaceResult(
            repo_full_name=record.repo_full_name,
            remote_url=record.remote_url,
            workspace_root=record.workspace_root,
            workspace_path=record.workspace_path,
        )

    def fetch_base_branch(self, request: FetchBranchRequest) -> FetchBranchResult:
        validate_base_branch(request.base_branch)
        record = self._get_workspace(request.workspace_path)
        self._require_same_repo(record, request.repo_full_name)
        record.base_branch = request.base_branch
        self._record_call("fetch_base_branch", request.repo_full_name, record.workspace_path)
        return FetchBranchResult(
            repo_full_name=request.repo_full_name,
            workspace_path=record.workspace_path,
            base_branch=request.base_branch,
        )

    def checkout_work_branch(
        self,
        request: CheckoutWorkBranchRequest,
    ) -> CheckoutWorkBranchResult:
        validate_base_branch(request.base_branch)
        validate_work_branch(request.work_branch)
        record = self._get_workspace(request.workspace_path)
        self._require_same_repo(record, request.repo_full_name)
        record.base_branch = request.base_branch
        record.current_branch = request.work_branch
        record.current_branch_base_commit_id = self._remote_branch_heads.get(
            (request.repo_full_name, request.work_branch)
        )
        record.latest_commit_id = record.current_branch_base_commit_id
        self._record_call("checkout_work_branch", request.repo_full_name, record.workspace_path)
        return CheckoutWorkBranchResult(
            repo_full_name=request.repo_full_name,
            workspace_path=record.workspace_path,
            base_branch=request.base_branch,
            work_branch=request.work_branch,
        )

    def commit_all(self, request: CommitAllRequest) -> CommitAllResult:
        validate_work_branch(request.work_branch)
        record = self._get_workspace(request.workspace_path)
        self._require_same_repo(record, request.repo_full_name)
        if record.current_branch != request.work_branch:
            raise GitWorkspacePolicyError(
                f"Cannot commit branch '{request.work_branch}' while current branch is "
                f"'{record.current_branch}'"
            )
        if (
            record.current_branch_base_commit_id is not None
            and record.latest_commit_id == record.current_branch_base_commit_id
        ):
            self._record_call("commit_all", request.repo_full_name, record.workspace_path)
            return CommitAllResult(
                repo_full_name=request.repo_full_name,
                workspace_path=record.workspace_path,
                work_branch=request.work_branch,
                commit_message=request.commit_message,
                author_name=request.author_name,
                author_email=request.author_email,
                commit_id=record.current_branch_base_commit_id,
            )

        commit_id = validate_commit_sha(f"{self._next_commit_number:040x}")
        self._next_commit_number += 1
        record.latest_commit_id = commit_id
        self._record_call("commit_all", request.repo_full_name, record.workspace_path)
        return CommitAllResult(
            repo_full_name=request.repo_full_name,
            workspace_path=record.workspace_path,
            work_branch=request.work_branch,
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
        if record.current_branch != request.work_branch:
            raise GitWorkspacePolicyError(
                f"Cannot push branch '{request.work_branch}' while current branch is "
                f"'{record.current_branch}'"
            )
        if record.latest_commit_id is None:
            raise GitWorkspacePolicyError("Cannot push before committing workspace changes")

        refspec = build_push_refspec(request.work_branch)
        self._remote_branch_heads[(request.repo_full_name, request.work_branch)] = (
            record.latest_commit_id
        )
        self._record_call("push_branch", request.repo_full_name, record.workspace_path)
        return PushBranchResult(
            repo_full_name=request.repo_full_name,
            workspace_path=record.workspace_path,
            work_branch=request.work_branch,
            refspec=refspec,
            commit_id=record.latest_commit_id,
        )

    def prepare_verifier_workspace(
        self,
        request: PrepareVerifierWorkspaceRequest,
    ) -> PrepareVerifierWorkspaceResult:
        work_branch = validate_work_branch(request.work_branch)
        approved_head_sha = validate_commit_sha(request.approved_head_sha)
        workspace_path = resolve_workspace_path(request.workspace_root, request.workspace_path)
        record = self._get_workspace(workspace_path)
        self._require_same_repo(record, request.repo_full_name)

        remote_head_sha = self._remote_branch_heads.get((request.repo_full_name, work_branch))
        if remote_head_sha != approved_head_sha:
            fetched_head_sha = remote_head_sha or "<missing>"
            raise GitWorkspacePolicyError(
                f"Fetched work branch head '{fetched_head_sha}' does not match "
                f"approved head '{approved_head_sha}'"
            )

        attested_head_sha = record.verifier_head_sha or approved_head_sha
        if attested_head_sha != approved_head_sha:
            raise GitWorkspacePolicyError(
                f"Verifier workspace HEAD '{attested_head_sha}' does not match "
                f"approved head '{approved_head_sha}'"
            )
        if record.verifier_status_summary:
            raise GitWorkspacePolicyError(
                "Verifier workspace is dirty immediately before verifier execution: "
                f"{record.verifier_status_summary!r}"
            )

        record.current_branch = work_branch
        record.verifier_head_sha = approved_head_sha
        self._record_call(
            "prepare_verifier_workspace",
            request.repo_full_name,
            record.workspace_path,
        )
        return PrepareVerifierWorkspaceResult(
            repo_full_name=request.repo_full_name,
            workspace_root=record.workspace_root,
            workspace_path=record.workspace_path,
            work_branch=work_branch,
            approved_head_sha=approved_head_sha,
            attested_head_sha=attested_head_sha,
            status_summary=record.verifier_status_summary,
        )

    def collect_git_evidence(
        self,
        workspace_path: Path,
        output_limit_bytes: int | None = None,
    ) -> GitEvidence:
        record = self._get_workspace(workspace_path)
        self._record_call("collect_git_evidence", record.repo_full_name, record.workspace_path)
        return build_git_evidence(
            workspace_path=record.workspace_path,
            status=self._status_evidence,
            diff=self._diff_evidence,
            output_limit_bytes=output_limit_bytes or 4096,
        )

    def set_verifier_workspace_head(self, workspace_path: Path, head_sha: str) -> None:
        record = self._get_workspace(workspace_path)
        record.verifier_head_sha = validate_commit_sha(head_sha)

    def set_verifier_workspace_status(self, workspace_path: Path, status_summary: str) -> None:
        record = self._get_workspace(workspace_path)
        record.verifier_status_summary = status_summary

    def set_remote_work_branch_head(
        self,
        *,
        repo_full_name: str,
        work_branch: str,
        head_sha: str,
    ) -> None:
        self._remote_branch_heads[(repo_full_name, validate_work_branch(work_branch))] = (
            validate_commit_sha(head_sha)
        )

    def _record_call(self, operation: str, repo_full_name: str, workspace_path: Path) -> None:
        self._calls.append(
            FakeGitWorkspaceCall(
                operation=operation,
                repo_full_name=repo_full_name,
                workspace_path=workspace_path,
            )
        )

    def _get_workspace(self, workspace_path: Path) -> _WorkspaceRecord:
        resolved_path = workspace_path.resolve()
        try:
            return self._workspaces[resolved_path]
        except KeyError as error:
            raise KeyError(f"Git workspace '{resolved_path}' was not cloned") from error

    def _require_same_repo(self, record: _WorkspaceRecord, repo_full_name: str) -> None:
        if record.repo_full_name != repo_full_name:
            raise GitWorkspacePolicyError(
                f"Workspace repo '{record.repo_full_name}' does not match request repo "
                f"'{repo_full_name}'"
            )
