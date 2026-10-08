"""The GitHub port: the request/response shapes and the client Protocol (ADR-0013 §4).

A contract rather than the Runner's own, because the GitHub App credential is the
*platform's* (ADR-0013 §11: the control plane mints installation tokens and hands them to
the Runner, never the other way round). Both sides therefore hold a client — the Runner's
verb seams and the platform's Reviewer Gate handoff, protected-path diff and Incident PR
comment — and this is the one shape they agree on. Stdlib dataclasses only, so contracts
keeps its single pydantic dependency.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable


class ApprovalState(StrEnum):
    """Deterministic PR approval states exposed by GitHub adapters.

    ``CHANGES_REQUESTED`` is the reviewer's explicit denial of the current head — the
    only state the Reviewer Gate may treat as a rejection. ``PENDING`` (no decisive
    review yet, or the last one was dismissed) means keep waiting, never deny.
    """

    PENDING = "pending"
    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"


def is_human_reviewer(login: str, *, user_type: str | None = None) -> bool:
    """Whether an approving review counts toward ``required_human_approvals``.

    The Agent holds no GitHub identity of its own: every verb it performs runs through
    the installation token, so a review it submits arrives as the App's bot account
    (``<slug>[bot]``, ``user.type == "Bot"``). Excluding bots is therefore exactly
    ADR-0011 s6's "the Agent's own identity never counts", and it needs no per-Agent
    login to be configured anywhere. The owner's confirmation cannot reach this count at
    all — it is a control-plane record, never a GitHub review.
    """

    return user_type != "Bot" and not login.endswith("[bot]")


@dataclass(frozen=True)
class BranchRequest:
    """Request to create a branch from an existing base ref."""

    repo: str
    base_ref: str
    branch: str


@dataclass(frozen=True)
class BranchResponse:
    """Result of a branch create operation."""

    repo: str
    branch: str
    base_ref: str
    created: bool


@dataclass(frozen=True)
class PullRequestRequest:
    """Request to create or update a pull request for a branch.

    ``draft`` applies only on create: GitHub's REST PATCH ignores the field, so a PR
    leaves draft through ``mark_pr_ready_for_review`` and never through an update.
    """

    repo: str
    branch: str
    base: str
    title: str
    body: str
    draft: bool = False


@dataclass(frozen=True)
class PullRequestResponse:
    """Result of a pull request create or update operation."""

    repo: str
    pr_number: int
    branch: str
    base: str
    title: str
    body: str
    created: bool
    merged: bool = False
    draft: bool = False


@dataclass(frozen=True)
class PullRequestReadyRequest:
    """Request to take a pull request out of draft (the ``pr.review`` seam)."""

    repo: str
    pr_number: int


@dataclass(frozen=True)
class PullRequestCloseRequest:
    """Request to close a pull request without merging it, keeping its branch.

    The ending an owner refusal or timeout at a PR verb produces (ADR-0011 s15): the org
    gets nothing to review, and the work survives on the branch for whoever picks it up.
    """

    repo: str
    pr_number: int


@dataclass(frozen=True)
class PullRequestCloseResponse:
    """Result of closing a pull request. ``closed`` is False when it was already closed
    or merged, so a workflow retry is a no-op rather than an error."""

    repo: str
    pr_number: int
    closed: bool


@dataclass(frozen=True)
class PullRequestRetargetRequest:
    """Point an open pull request at a different base branch (PRD issue 59, 19 A3): the
    base of a stacked PR merged, so its dependents move to the default branch."""

    repo: str
    pr_number: int
    base: str


@dataclass(frozen=True)
class PullRequestRetargetResponse:
    """``retargeted`` is False when the PR is no longer open, so a retry is a no-op."""

    repo: str
    pr_number: int
    base: str
    retargeted: bool


@dataclass(frozen=True)
class PullRequestReadyResponse:
    """Result of taking a pull request out of draft.

    ``marked_ready`` is False when the PR was already out of draft, so an activity retry
    is a no-op rather than an error.
    """

    repo: str
    pr_number: int
    ready: bool
    marked_ready: bool


@dataclass(frozen=True)
class ReviewRequest:
    """Request to ask a reviewer persona or GitHub user to review a PR."""

    repo: str
    pr_number: int
    reviewer: str


@dataclass(frozen=True)
class ReviewResponse:
    """Result of requesting review for a PR."""

    repo: str
    pr_number: int
    reviewer: str
    reviewers: tuple[str, ...]
    requested: bool


@dataclass(frozen=True)
class ApprovalRequest:
    """Request to poll deterministic approval state for a PR."""

    repo: str
    pr_number: int


@dataclass(frozen=True)
class ApprovalResponse:
    """Approval state returned for a PR.

    ``pr_merged``/``pr_closed`` carry the PR's own lifecycle as observed on the same
    poll: merged (by anyone), or closed without merging. At most one is True, and either
    supersedes the review-derived ``state`` — a merged/closed PR reports
    ``state=PENDING`` with the lifecycle flag set, because no review can gate it
    anymore. ``merged_by``/``merge_commit_sha`` attribute an observed merge when GitHub
    provides them.

    ``approving_reviewers`` are the distinct **human** logins whose standing review on
    ``current_head_sha`` is an approval - what the merge seam counts against the
    Product's ``required_human_approvals`` (ADR-0011 s6). It is empty whenever the only
    standing approvals are a bot's, even though ``state`` is then still APPROVED: the
    review decision and the four-eyes count are two different questions.
    """

    repo: str
    pr_number: int
    state: ApprovalState
    current_head_sha: str
    approved_head_sha: str | None = None
    approver: str | None = None
    approving_reviewers: tuple[str, ...] = ()
    pr_merged: bool = False
    pr_closed: bool = False
    merged_by: str | None = None
    merge_commit_sha: str | None = None


@dataclass(frozen=True)
class MergeRequest:
    """Request to merge an approved PR."""

    repo: str
    pr_number: int
    commit_title: str
    expected_head_sha: str


@dataclass(frozen=True)
class MergeResponse:
    """Result of a merge operation."""

    repo: str
    pr_number: int
    commit_title: str
    merged: bool
    merge_commit_sha: str


@dataclass(frozen=True)
class PullRequestFilesRequest:
    """Request to list the changed file paths of a pull request."""

    repo: str
    pr_number: int


@dataclass(frozen=True)
class PullRequestFilesResponse:
    """Changed file paths reported for a pull request."""

    repo: str
    pr_number: int
    changed_paths: tuple[str, ...]


@dataclass(frozen=True)
class CommentRequest:
    """Request to append a comment to a PR."""

    repo: str
    pr_number: int
    body: str


@dataclass(frozen=True)
class CommentResponse:
    """Result of appending a PR comment."""

    repo: str
    pr_number: int
    comment_number: int
    body: str
    comments: tuple[str, ...]


@dataclass(frozen=True)
class RepositoryReadRequest:
    """Request to read a repository's metadata: the readiness probe's one GitHub call."""

    repo: str


@dataclass(frozen=True)
class RepositoryReadResponse:
    """A repository the credential could read. GitHub answers 404, never an empty
    body, for a repository the credential does not cover."""

    repo: str
    default_branch: str


class ReviewEvent(StrEnum):
    """The review states an Agent may submit. There is no ``APPROVE``: the Critic's
    `clear` is a comment, so no Agent review can ever read as an approval (PRD issue 54)."""

    REQUEST_CHANGES = "REQUEST_CHANGES"
    COMMENT = "COMMENT"


@dataclass(frozen=True)
class PullRequestReviewRequest:
    """Request to submit a review on a PR, pinned to the head commit it reviewed."""

    repo: str
    pr_number: int
    commit_id: str
    event: ReviewEvent
    body: str


@dataclass(frozen=True)
class PullRequestReviewResponse:
    """Result of submitting a review: GitHub's id, state and the login it arrived as."""

    repo: str
    pr_number: int
    review_id: int
    state: str
    reviewer_login: str


@runtime_checkable
class GitHubClient(Protocol):
    """Boundary contract for GitHub App branch and PR lifecycle operations."""

    def create_branch(self, request: BranchRequest) -> BranchResponse:
        """Create a branch from a base ref."""

    def create_or_update_pr(self, request: PullRequestRequest) -> PullRequestResponse:
        """Create or update a pull request for a branch."""

    def mark_pr_ready_for_review(
        self,
        request: PullRequestReadyRequest,
    ) -> PullRequestReadyResponse:
        """Take a draft pull request out of draft so reviewers can act on it."""

    def close_pr(self, request: PullRequestCloseRequest) -> PullRequestCloseResponse:
        """Close a pull request without merging it; the branch is left in place."""

    def retarget_pr(self, request: PullRequestRetargetRequest) -> PullRequestRetargetResponse:
        """Move an open pull request onto another base branch (a stack's base merged)."""

    def request_review(self, request: ReviewRequest) -> ReviewResponse:
        """Request review from a reviewer persona or user."""

    def wait_for_approval(self, request: ApprovalRequest) -> ApprovalResponse:
        """Return current approval state for a pull request."""

    def merge_pr(self, request: MergeRequest) -> MergeResponse:
        """Merge an approved pull request."""

    def post_comment(self, request: CommentRequest) -> CommentResponse:
        """Append a comment to a pull request."""

    def submit_review(self, request: PullRequestReviewRequest) -> PullRequestReviewResponse:
        """Submit a review on a pull request (the `pr.comment` seam's one GitHub call)."""

    def list_changed_files(self, request: PullRequestFilesRequest) -> PullRequestFilesResponse:
        """List the changed file paths of a pull request."""

    def read_repository(self, request: RepositoryReadRequest) -> RepositoryReadResponse:
        """Read a repository's metadata, proving the credential covers it."""
