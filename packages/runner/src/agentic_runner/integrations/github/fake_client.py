from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha1

from agentic_runner_contracts.github_port import (
    ApprovalRequest,
    ApprovalResponse,
    ApprovalState,
    BranchRequest,
    BranchResponse,
    CommentRequest,
    CommentResponse,
    MergeRequest,
    MergeResponse,
    PullRequestCloseRequest,
    PullRequestCloseResponse,
    PullRequestFilesRequest,
    PullRequestFilesResponse,
    PullRequestReadyRequest,
    PullRequestReadyResponse,
    PullRequestRequest,
    PullRequestResponse,
    PullRequestRetargetRequest,
    PullRequestRetargetResponse,
    PullRequestReviewRequest,
    PullRequestReviewResponse,
    RepositoryReadRequest,
    RepositoryReadResponse,
    ReviewEvent,
    ReviewRequest,
    ReviewResponse,
    is_human_reviewer,
)


@dataclass
class _PullRequestRecord:
    repo: str
    pr_number: int
    branch: str
    base: str
    title: str
    body: str
    head_revision: int = 1
    draft: bool = False
    reviewers: list[str] = field(default_factory=list)
    comments: list[str] = field(default_factory=list)
    # Reviews submitted through ``submit_review`` (the `pr.comment` seam), in order. They
    # are an Agent's, so like a bot's review on GitHub they never move the approval state.
    reviews: list[PullRequestReviewRequest] = field(default_factory=list)
    changed_paths: list[str] = field(default_factory=list)
    approver: str | None = None
    approval_head_sha: str | None = None
    # Every standing approval, login -> the head SHA it was submitted against. The merge
    # seam counts the distinct human logins standing on the current head (ADR-0011 s6);
    # ``approver``/``approval_head_sha`` stay the newest one, which is the review
    # decision the Reviewer Gate reports.
    approvals: dict[str, str] = field(default_factory=dict)
    changes_requested_by: str | None = None
    changes_requested_head_sha: str | None = None
    merge_commit_sha: str | None = None
    merged: bool = False
    merged_by: str | None = None
    closed: bool = False


class FakeGitHubClient:
    """Deterministic in-memory GitHub client for tests and Ralph loop rehearsal."""

    # The login every App-token call arrives as, as on GitHub: an Agent has no identity of
    # its own there (``contracts.is_human_reviewer``).
    app_login = "agentic-os[bot]"

    def __init__(self) -> None:
        self._branches: dict[tuple[str, str], BranchResponse] = {}
        self._pull_requests_by_number: dict[tuple[str, int], _PullRequestRecord] = {}
        self._pull_requests_by_branch: dict[tuple[str, str], _PullRequestRecord] = {}
        self._review_requests: list[tuple[str, int, str]] = []
        self._retargets: list[tuple[str, int, str]] = []
        self._default_branches: dict[str, str] = {}
        self._next_pr_number = 1

    @property
    def pull_requests(self) -> tuple[_PullRequestRecord, ...]:
        """Return current pull requests in deterministic PR-number order for assertions."""

        return tuple(
            sorted(
                self._pull_requests_by_number.values(),
                key=lambda record: (record.repo, record.pr_number),
            )
        )

    @property
    def review_requests(self) -> tuple[tuple[str, int, str], ...]:
        """Return recorded review requests for deterministic workflow assertions."""

        return tuple(self._review_requests)

    def create_branch(self, request: BranchRequest) -> BranchResponse:
        key = (request.repo, request.branch)
        existing = self._branches.get(key)
        if existing is not None:
            return BranchResponse(
                repo=existing.repo,
                branch=existing.branch,
                base_ref=existing.base_ref,
                created=False,
            )

        response = BranchResponse(
            repo=request.repo,
            branch=request.branch,
            base_ref=request.base_ref,
            created=True,
        )
        self._branches[key] = response
        return response

    def create_or_update_pr(self, request: PullRequestRequest) -> PullRequestResponse:
        self._require_branch(repo=request.repo, branch=request.branch)
        key = (request.repo, request.branch)
        existing = self._pull_requests_by_branch.get(key)
        if existing is not None:
            self._ensure_pr_is_open(existing)
            existing.base = request.base
            existing.title = request.title
            existing.body = request.body
            return self._to_pr_response(existing, created=False)

        record = _PullRequestRecord(
            repo=request.repo,
            pr_number=self._next_pr_number,
            branch=request.branch,
            base=request.base,
            title=request.title,
            body=request.body,
            draft=request.draft,
        )
        self._next_pr_number += 1
        self._pull_requests_by_branch[key] = record
        self._pull_requests_by_number[(request.repo, record.pr_number)] = record
        return self._to_pr_response(record, created=True)

    def mark_pr_ready_for_review(
        self,
        request: PullRequestReadyRequest,
    ) -> PullRequestReadyResponse:
        record = self._get_pr(repo=request.repo, pr_number=request.pr_number)
        self._ensure_pr_is_open(record)
        marked_ready = record.draft
        record.draft = False
        return PullRequestReadyResponse(
            repo=request.repo,
            pr_number=request.pr_number,
            ready=True,
            marked_ready=marked_ready,
        )

    def close_pr(self, request: PullRequestCloseRequest) -> PullRequestCloseResponse:
        record = self._get_pr(repo=request.repo, pr_number=request.pr_number)
        already_settled = record.merged or record.closed
        record.closed = True
        return PullRequestCloseResponse(
            repo=request.repo,
            pr_number=request.pr_number,
            closed=not already_settled,
        )

    def retarget_pr(self, request: PullRequestRetargetRequest) -> PullRequestRetargetResponse:
        record = self._get_pr(repo=request.repo, pr_number=request.pr_number)
        if record.merged or record.closed:
            return PullRequestRetargetResponse(
                repo=request.repo, pr_number=request.pr_number, base=record.base, retargeted=False
            )
        record.base = request.base
        self._retargets.append((request.repo, request.pr_number, request.base))
        return PullRequestRetargetResponse(
            repo=request.repo, pr_number=request.pr_number, base=request.base, retargeted=True
        )

    @property
    def retargets(self) -> tuple[tuple[str, int, str], ...]:
        """Every base change recorded through ``retarget_pr``, in order (PRD issue 59)."""

        return tuple(self._retargets)

    def request_review(self, request: ReviewRequest) -> ReviewResponse:
        record = self._get_pr(repo=request.repo, pr_number=request.pr_number)
        self._ensure_pr_is_open(record)
        self._review_requests.append((request.repo, request.pr_number, request.reviewer))
        if request.reviewer not in record.reviewers:
            record.reviewers.append(request.reviewer)

        return ReviewResponse(
            repo=request.repo,
            pr_number=request.pr_number,
            reviewer=request.reviewer,
            reviewers=tuple(record.reviewers),
            requested=True,
        )

    def wait_for_approval(self, request: ApprovalRequest) -> ApprovalResponse:
        record = self._get_pr(repo=request.repo, pr_number=request.pr_number)
        current_head_sha = self._head_sha(record)
        # Mirrors the real adapter: the PR lifecycle supersedes any review verdict — a
        # merged or closed PR reports PENDING plus the lifecycle flags, never a review
        # state the gate could act on.
        if record.merged:
            return ApprovalResponse(
                repo=request.repo,
                pr_number=request.pr_number,
                state=ApprovalState.PENDING,
                current_head_sha=current_head_sha,
                pr_merged=True,
                merged_by=record.merged_by,
                merge_commit_sha=record.merge_commit_sha,
            )
        if record.closed:
            return ApprovalResponse(
                repo=request.repo,
                pr_number=request.pr_number,
                state=ApprovalState.PENDING,
                current_head_sha=current_head_sha,
                pr_closed=True,
            )
        approved = record.approver is not None and record.approval_head_sha == current_head_sha
        if approved:
            return ApprovalResponse(
                repo=request.repo,
                pr_number=request.pr_number,
                state=ApprovalState.APPROVED,
                current_head_sha=current_head_sha,
                approved_head_sha=record.approval_head_sha,
                approver=record.approver,
                approving_reviewers=self._human_approvers(record, current_head_sha),
            )
        # Mirrors the real adapter: a changes-requested review only denies the head it
        # was submitted against; a new push returns the gate to PENDING.
        if (
            record.changes_requested_by is not None
            and record.changes_requested_head_sha == current_head_sha
        ):
            return ApprovalResponse(
                repo=request.repo,
                pr_number=request.pr_number,
                state=ApprovalState.CHANGES_REQUESTED,
                current_head_sha=current_head_sha,
                approver=record.changes_requested_by,
            )
        return ApprovalResponse(
            repo=request.repo,
            pr_number=request.pr_number,
            state=ApprovalState.PENDING,
            current_head_sha=current_head_sha,
        )

    def merge_pr(self, request: MergeRequest) -> MergeResponse:
        record = self._get_pr(repo=request.repo, pr_number=request.pr_number)
        self._ensure_pr_is_open(record)
        if record.approver is None:
            raise PermissionError(
                f"Pull request '{request.repo}#{request.pr_number}' requires approval before merge"
            )
        current_head_sha = self._head_sha(record)
        if record.approval_head_sha != current_head_sha:
            raise PermissionError(
                f"Pull request '{request.repo}#{request.pr_number}' requires current head approval"
            )
        if request.expected_head_sha != current_head_sha:
            raise ValueError(
                f"Pull request '{request.repo}#{request.pr_number}' expected head SHA did not match"
            )

        record.merged = True
        record.merge_commit_sha = self._merge_commit_sha(record)
        return MergeResponse(
            repo=request.repo,
            pr_number=request.pr_number,
            commit_title=request.commit_title,
            merged=True,
            merge_commit_sha=record.merge_commit_sha,
        )

    def post_comment(self, request: CommentRequest) -> CommentResponse:
        record = self._get_pr(repo=request.repo, pr_number=request.pr_number)
        self._ensure_pr_is_open(record)
        record.comments.append(request.body)
        return CommentResponse(
            repo=request.repo,
            pr_number=request.pr_number,
            comment_number=len(record.comments),
            body=request.body,
            comments=tuple(record.comments),
        )

    def submit_review(self, request: PullRequestReviewRequest) -> PullRequestReviewResponse:
        record = self._get_pr(repo=request.repo, pr_number=request.pr_number)
        self._ensure_pr_is_open(record)
        record.reviews.append(request)
        return PullRequestReviewResponse(
            repo=request.repo,
            pr_number=request.pr_number,
            review_id=len(record.reviews),
            state=(
                "CHANGES_REQUESTED" if request.event is ReviewEvent.REQUEST_CHANGES else "COMMENTED"
            ),
            reviewer_login=self.app_login,
        )

    def list_changed_files(self, request: PullRequestFilesRequest) -> PullRequestFilesResponse:
        # No open-PR guard: the real API serves the file listing for merged and closed
        # PRs too, and a review may legitimately read one.
        record = self._get_pr(repo=request.repo, pr_number=request.pr_number)
        return PullRequestFilesResponse(
            repo=request.repo,
            pr_number=request.pr_number,
            changed_paths=tuple(record.changed_paths),
        )

    def read_repository(self, request: RepositoryReadRequest) -> RepositoryReadResponse:
        try:
            default_branch = self._default_branches[request.repo]
        except KeyError as error:
            raise KeyError(f"Repository '{request.repo}' not found") from error
        return RepositoryReadResponse(repo=request.repo, default_branch=default_branch)

    def record_repository(self, *, repo: str, default_branch: str = "main") -> None:
        """Test helper: make ``repo`` readable; any other repository answers not-found."""

        self._default_branches[repo] = default_branch

    def record_changed_paths(self, *, repo: str, pr_number: int, changed_paths: list[str]) -> None:
        """Test helper for deterministically setting a PR's changed file paths."""

        record = self._get_pr(repo=repo, pr_number=pr_number)
        record.changed_paths = list(changed_paths)

    def record_approval(self, *, repo: str, pr_number: int, approver: str) -> ApprovalResponse:
        """Test helper for deterministically recording an external approval."""

        record = self._get_pr(repo=repo, pr_number=pr_number)
        self._ensure_pr_is_open(record)
        record.approver = approver
        record.approval_head_sha = self._head_sha(record)
        record.approvals[approver] = record.approval_head_sha
        record.changes_requested_by = None
        record.changes_requested_head_sha = None
        return ApprovalResponse(
            repo=repo,
            pr_number=pr_number,
            state=ApprovalState.APPROVED,
            current_head_sha=record.approval_head_sha,
            approved_head_sha=record.approval_head_sha,
            approver=approver,
            approving_reviewers=self._human_approvers(record, record.approval_head_sha),
        )

    def record_changes_requested(
        self, *, repo: str, pr_number: int, reviewer: str
    ) -> ApprovalResponse:
        """Test helper for deterministically recording a changes-requested review."""

        record = self._get_pr(repo=repo, pr_number=pr_number)
        self._ensure_pr_is_open(record)
        current_head_sha = self._head_sha(record)
        record.approver = None
        record.approval_head_sha = None
        record.approvals.clear()
        record.changes_requested_by = reviewer
        record.changes_requested_head_sha = current_head_sha
        return ApprovalResponse(
            repo=repo,
            pr_number=pr_number,
            state=ApprovalState.CHANGES_REQUESTED,
            current_head_sha=current_head_sha,
            approver=reviewer,
        )

    def record_external_merge(self, *, repo: str, pr_number: int, actor: str) -> ApprovalResponse:
        """Test helper for deterministically merging the PR outside the Ralph loop."""

        record = self._get_pr(repo=repo, pr_number=pr_number)
        self._ensure_pr_is_open(record)
        record.merged = True
        record.merged_by = actor
        record.merge_commit_sha = self._merge_commit_sha(record)
        return ApprovalResponse(
            repo=repo,
            pr_number=pr_number,
            state=ApprovalState.PENDING,
            current_head_sha=self._head_sha(record),
            pr_merged=True,
            merged_by=actor,
            merge_commit_sha=record.merge_commit_sha,
        )

    def record_external_close(self, *, repo: str, pr_number: int) -> ApprovalResponse:
        """Test helper for deterministically closing the PR without merging."""

        record = self._get_pr(repo=repo, pr_number=pr_number)
        self._ensure_pr_is_open(record)
        record.closed = True
        return ApprovalResponse(
            repo=repo,
            pr_number=pr_number,
            state=ApprovalState.PENDING,
            current_head_sha=self._head_sha(record),
            pr_closed=True,
        )

    def record_dismissal(self, *, repo: str, pr_number: int) -> ApprovalResponse:
        """Test helper for deterministically dismissing the current approval."""

        record = self._get_pr(repo=repo, pr_number=pr_number)
        self._ensure_pr_is_open(record)
        current_head_sha = self._head_sha(record)
        record.approver = None
        record.approval_head_sha = None
        record.approvals.clear()
        record.changes_requested_by = None
        record.changes_requested_head_sha = None
        return ApprovalResponse(
            repo=repo,
            pr_number=pr_number,
            state=ApprovalState.PENDING,
            current_head_sha=current_head_sha,
        )

    def record_branch_head_change(self, *, repo: str, branch: str) -> None:
        """Test helper for deterministically modeling pushed branch content changes."""

        self._require_branch(repo=repo, branch=branch)
        try:
            record = self._pull_requests_by_branch[(repo, branch)]
        except KeyError as error:
            raise KeyError(f"Pull request for branch '{repo}:{branch}' not found") from error
        self._ensure_pr_is_open(record)
        record.head_revision += 1

    def _require_branch(self, *, repo: str, branch: str) -> None:
        if (repo, branch) not in self._branches:
            raise KeyError(f"Branch '{repo}:{branch}' not found")

    def _get_pr(self, *, repo: str, pr_number: int) -> _PullRequestRecord:
        try:
            return self._pull_requests_by_number[(repo, pr_number)]
        except KeyError as error:
            raise KeyError(f"Pull request '{repo}#{pr_number}' not found") from error

    def _ensure_pr_is_open(self, record: _PullRequestRecord) -> None:
        if record.merged:
            raise ValueError(f"Pull request '{record.repo}#{record.pr_number}' is already merged")
        if record.closed:
            raise ValueError(f"Pull request '{record.repo}#{record.pr_number}' is closed")

    def _to_pr_response(
        self,
        record: _PullRequestRecord,
        *,
        created: bool,
    ) -> PullRequestResponse:
        return PullRequestResponse(
            repo=record.repo,
            pr_number=record.pr_number,
            branch=record.branch,
            base=record.base,
            title=record.title,
            body=record.body,
            created=created,
            merged=record.merged,
            draft=record.draft,
        )

    @staticmethod
    def _human_approvers(record: _PullRequestRecord, head_sha: str) -> tuple[str, ...]:
        return tuple(
            sorted(
                login
                for login, approved_head_sha in record.approvals.items()
                if approved_head_sha == head_sha and is_human_reviewer(login)
            )
        )

    @staticmethod
    def _head_sha(record: _PullRequestRecord) -> str:
        seed = f"fake-pr:{record.repo}:{record.pr_number}:{record.branch}:{record.head_revision}"
        return sha1(seed.encode("utf-8"), usedforsecurity=False).hexdigest()

    @classmethod
    def _merge_commit_sha(cls, record: _PullRequestRecord) -> str:
        seed = f"fake-merge:{record.repo}:{record.pr_number}:{cls._head_sha(record)}"
        return sha1(seed.encode("utf-8"), usedforsecurity=False).hexdigest()
