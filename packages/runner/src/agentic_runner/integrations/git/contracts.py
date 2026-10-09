from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from agentic_runner.integrations.git.evidence import GitEvidence

DEFAULT_COMMIT_AUTHOR_NAME = "QTS Agentic OS"
DEFAULT_COMMIT_AUTHOR_EMAIL = "agentic-os[bot]@qts.one"


@dataclass(frozen=True)
class CloneWorkspaceRequest:
    """Request to clone a tokenless remote into a worker-local workspace path."""

    repo_full_name: str
    remote_url: str
    workspace_root: Path
    workspace_path: Path


@dataclass(frozen=True)
class CloneWorkspaceResult:
    """Result of preparing a local repository workspace."""

    repo_full_name: str
    remote_url: str
    workspace_root: Path
    workspace_path: Path


@dataclass(frozen=True)
class AdoptWorkspaceRequest:
    """A checkout produced outside this adapter, offered for adoption."""

    repo_full_name: str
    remote_url: str
    workspace_root: Path
    workspace_path: Path
    work_branch: str


@dataclass(frozen=True)
class FetchBranchRequest:
    """Request to fetch a base branch into an existing workspace."""

    repo_full_name: str
    workspace_path: Path
    base_branch: str


@dataclass(frozen=True)
class FetchBranchResult:
    """Result of fetching a base branch."""

    repo_full_name: str
    workspace_path: Path
    base_branch: str


@dataclass(frozen=True)
class CheckoutDefaultBranchRequest:
    """Request to check out ``origin``'s default branch, detached, in a fresh clone."""

    repo_full_name: str
    workspace_path: Path


@dataclass(frozen=True)
class CheckoutWorkBranchRequest:
    """Request to create or reset a generated work branch from the base branch."""

    repo_full_name: str
    workspace_path: Path
    base_branch: str
    work_branch: str


@dataclass(frozen=True)
class CheckoutWorkBranchResult:
    """Result of checking out a generated work branch."""

    repo_full_name: str
    workspace_path: Path
    base_branch: str
    work_branch: str


@dataclass(frozen=True)
class CommitAllRequest:
    """Request to commit all workspace changes with a deterministic author."""

    repo_full_name: str
    workspace_path: Path
    work_branch: str
    commit_message: str
    author_name: str = DEFAULT_COMMIT_AUTHOR_NAME
    author_email: str = DEFAULT_COMMIT_AUTHOR_EMAIL


@dataclass(frozen=True)
class CommitAllResult:
    """Result of committing all workspace changes."""

    repo_full_name: str
    workspace_path: Path
    work_branch: str
    commit_message: str
    author_name: str
    author_email: str
    commit_id: str


@dataclass(frozen=True)
class PushBranchRequest:
    """Request to push the current HEAD to a generated work branch."""

    repo_full_name: str
    workspace_path: Path
    base_branch: str
    work_branch: str
    protected_branches: tuple[str, ...] = ()


@dataclass(frozen=True)
class PushBranchResult:
    """Result of pushing a generated work branch."""

    repo_full_name: str
    workspace_path: Path
    work_branch: str
    refspec: str
    commit_id: str


@dataclass(frozen=True)
class PrepareVerifierWorkspaceRequest:
    """Request to attest a verifier workspace at the approved generated branch head."""

    repo_full_name: str
    workspace_root: Path
    workspace_path: Path
    work_branch: str
    approved_head_sha: str


@dataclass(frozen=True)
class PrepareVerifierWorkspaceResult:
    """Attestation proving the verifier workspace path, HEAD, and status."""

    repo_full_name: str
    workspace_root: Path
    workspace_path: Path
    work_branch: str
    approved_head_sha: str
    attested_head_sha: str
    status_summary: str


@runtime_checkable
class GitWorkspace(Protocol):
    """Deep seam for local git workspace lifecycle operations."""

    def clone_repository(self, request: CloneWorkspaceRequest) -> CloneWorkspaceResult:
        """Clone a tokenless remote into a worker-local workspace."""

    def adopt_existing_clone(self, request: AdoptWorkspaceRequest) -> CloneWorkspaceResult:
        """Adopt a checkout this adapter did not clone (the ``checkout`` hook, issue 45).

        Every other method here is keyed on a workspace this adapter cloned itself, so a
        ``checkout`` Runner Hook -- a separate process, which cannot reach the adapter --
        has to hand its result back across the port or the Directive dies on the first
        git call after it. Validated rather than trusted: a work tree root, ``origin`` at
        the repository's own remote (the push refspec goes there), and the work branch
        already checked out.
        """

    def fetch_base_branch(self, request: FetchBranchRequest) -> FetchBranchResult:
        """Fetch the requested base branch into a workspace."""

    def checkout_default_branch(self, request: CheckoutDefaultBranchRequest) -> FetchBranchResult:
        """Check out the remote's default branch detached; its name is the result's base.

        A repository an Organisation-scoped Work Record reads has no base branch of its
        own yet (ADR-0018 §7): the clone's ``origin/HEAD`` is the one it reads.
        """

    def checkout_work_branch(
        self,
        request: CheckoutWorkBranchRequest,
    ) -> CheckoutWorkBranchResult:
        """Checkout a generated work branch from the base branch."""

    def commit_all(self, request: CommitAllRequest) -> CommitAllResult:
        """Commit all workspace changes."""

    def push_branch(self, request: PushBranchRequest) -> PushBranchResult:
        """Push the work branch using a safe HEAD-to-branch refspec."""

    def prepare_verifier_workspace(
        self,
        request: PrepareVerifierWorkspaceRequest,
    ) -> PrepareVerifierWorkspaceResult:
        """Prepare and attest a clean verifier workspace at the approved head."""

    def collect_git_evidence(
        self,
        workspace_path: Path,
        output_limit_bytes: int | None = None,
    ) -> GitEvidence:
        """Collect bounded, redacted status and diff evidence for a workspace."""
