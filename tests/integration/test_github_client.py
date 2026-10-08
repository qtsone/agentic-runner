from __future__ import annotations

from inspect import signature

import pytest

from agentic_runner.integrations.git.workspace import validate_commit_sha
from agentic_runner.integrations.github.fake_client import FakeGitHubClient
from agentic_runner.integrations.github.gh_client import GitHubAppClient
from agentic_runner_contracts.github_port import (
    ApprovalRequest,
    ApprovalState,
    BranchRequest,
    CommentRequest,
    GitHubClient,
    MergeRequest,
    PullRequestRequest,
    ReviewRequest,
)


def test_fake_and_real_clients_satisfy_github_client_interface_structurally() -> None:
    fake = FakeGitHubClient()
    real = GitHubAppClient()

    assert isinstance(fake, GitHubClient)
    assert isinstance(real, GitHubClient)

    for method_name in (
        "create_branch",
        "create_or_update_pr",
        "request_review",
        "wait_for_approval",
        "merge_pr",
        "post_comment",
    ):
        contract_method = getattr(GitHubClient, method_name)
        assert signature(getattr(type(fake), method_name)) == signature(contract_method)
        assert signature(getattr(type(real), method_name)) == signature(contract_method)


def test_real_client_approval_and_merge_methods_require_configured_credentials() -> None:
    client = GitHubAppClient()

    requests = (
        (client.wait_for_approval, ApprovalRequest(repo="qts/agentic-os", pr_number=1)),
        (
            client.merge_pr,
            MergeRequest(
                repo="qts/agentic-os",
                pr_number=1,
                commit_title="Merge task 8",
                expected_head_sha="fake-pr-1-head-1",
            ),
        ),
    )

    for method, request in requests:
        with pytest.raises(
            RuntimeError,
            match="GitHub App credentials are not configured",
        ):
            method(request)


def test_fake_creates_branch_and_creates_or_updates_pr_deterministically() -> None:
    client = FakeGitHubClient()

    branch = client.create_branch(
        BranchRequest(repo="qts/agentic-os", base_ref="main", branch="ralph/task-8")
    )
    created_pr = client.create_or_update_pr(
        PullRequestRequest(
            repo="qts/agentic-os",
            branch="ralph/task-8",
            base="main",
            title="Task 8",
            body="Initial body.",
        )
    )
    updated_pr = client.create_or_update_pr(
        PullRequestRequest(
            repo="qts/agentic-os",
            branch="ralph/task-8",
            base="main",
            title="Task 8 updated",
            body="Updated body.",
        )
    )

    assert branch.created is True
    assert branch.branch == "ralph/task-8"
    assert created_pr.created is True
    assert created_pr.pr_number == 1
    assert created_pr.title == "Task 8"
    assert updated_pr.created is False
    assert updated_pr.pr_number == created_pr.pr_number
    assert updated_pr.title == "Task 8 updated"
    assert updated_pr.body == "Updated body."


def test_fake_review_request_records_reviewer_persona_or_user() -> None:
    client = FakeGitHubClient()
    client.create_branch(
        BranchRequest(repo="qts/agentic-os", base_ref="main", branch="ralph/task-8")
    )
    pr = client.create_or_update_pr(
        PullRequestRequest(
            repo="qts/agentic-os",
            branch="ralph/task-8",
            base="main",
            title="Task 8",
            body="Initial body.",
        )
    )

    review = client.request_review(
        ReviewRequest(repo="qts/agentic-os", pr_number=pr.pr_number, reviewer="@technical-lead")
    )

    assert review.requested is True
    assert review.reviewers == ("@technical-lead",)


def test_fake_wait_for_approval_returns_pending_then_approved_after_recorded() -> None:
    client = FakeGitHubClient()
    client.create_branch(
        BranchRequest(repo="qts/agentic-os", base_ref="main", branch="ralph/task-8")
    )
    pr = client.create_or_update_pr(
        PullRequestRequest(
            repo="qts/agentic-os",
            branch="ralph/task-8",
            base="main",
            title="Task 8",
            body="Initial body.",
        )
    )

    pending = client.wait_for_approval(
        ApprovalRequest(repo="qts/agentic-os", pr_number=pr.pr_number)
    )
    client.record_approval(
        repo="qts/agentic-os",
        pr_number=pr.pr_number,
        approver="@technical-lead",
    )
    approved = client.wait_for_approval(
        ApprovalRequest(repo="qts/agentic-os", pr_number=pr.pr_number)
    )

    assert pending.state == ApprovalState.PENDING
    assert pending.approver is None
    assert validate_commit_sha(pending.current_head_sha) == pending.current_head_sha
    assert pending.approved_head_sha is None
    assert approved.state == ApprovalState.APPROVED
    assert approved.approver == "@technical-lead"
    assert approved.current_head_sha == pending.current_head_sha
    assert approved.approved_head_sha == pending.current_head_sha


def test_fake_current_head_sha_validates_as_full_commit_sha() -> None:
    client = FakeGitHubClient()
    client.create_branch(
        BranchRequest(repo="qts/agentic-os", base_ref="main", branch="ralph/task-8")
    )
    pr = client.create_or_update_pr(
        PullRequestRequest(
            repo="qts/agentic-os",
            branch="ralph/task-8",
            base="main",
            title="Task 8",
            body="Initial body.",
        )
    )

    approval = client.wait_for_approval(
        ApprovalRequest(repo="qts/agentic-os", pr_number=pr.pr_number)
    )

    assert validate_commit_sha(approval.current_head_sha) == approval.current_head_sha


def test_fake_approval_is_pending_after_pr_head_changes() -> None:
    client = FakeGitHubClient()
    client.create_branch(
        BranchRequest(repo="qts/agentic-os", base_ref="main", branch="ralph/task-8")
    )
    pr = client.create_or_update_pr(
        PullRequestRequest(
            repo="qts/agentic-os",
            branch="ralph/task-8",
            base="main",
            title="Task 8",
            body="Initial body.",
        )
    )
    recorded = client.record_approval(
        repo="qts/agentic-os",
        pr_number=pr.pr_number,
        approver="@technical-lead",
    )

    client.record_branch_head_change(repo="qts/agentic-os", branch="ralph/task-8")
    approval = client.wait_for_approval(
        ApprovalRequest(repo="qts/agentic-os", pr_number=pr.pr_number)
    )

    assert approval.state == ApprovalState.PENDING
    assert approval.approver is None
    assert validate_commit_sha(approval.current_head_sha) == approval.current_head_sha
    assert approval.current_head_sha != recorded.approved_head_sha
    assert approval.approved_head_sha is None


def test_fake_metadata_only_pr_update_preserves_current_approval() -> None:
    client = FakeGitHubClient()
    client.create_branch(
        BranchRequest(repo="qts/agentic-os", base_ref="main", branch="ralph/task-8")
    )
    pr = client.create_or_update_pr(
        PullRequestRequest(
            repo="qts/agentic-os",
            branch="ralph/task-8",
            base="main",
            title="Task 8",
            body="Initial body.",
        )
    )
    recorded = client.record_approval(
        repo="qts/agentic-os",
        pr_number=pr.pr_number,
        approver="@technical-lead",
    )

    client.create_or_update_pr(
        PullRequestRequest(
            repo="qts/agentic-os",
            branch="ralph/task-8",
            base="main",
            title="Task 8 updated",
            body="Updated body.",
        )
    )
    approval = client.wait_for_approval(
        ApprovalRequest(repo="qts/agentic-os", pr_number=pr.pr_number)
    )

    assert approval.state == ApprovalState.APPROVED
    assert approval.approver == "@technical-lead"
    assert approval.current_head_sha == recorded.approved_head_sha
    assert approval.approved_head_sha == recorded.approved_head_sha


def test_fake_dismissed_approval_is_pending_and_merge_fails_closed() -> None:
    client = FakeGitHubClient()
    client.create_branch(
        BranchRequest(repo="qts/agentic-os", base_ref="main", branch="ralph/task-8")
    )
    pr = client.create_or_update_pr(
        PullRequestRequest(
            repo="qts/agentic-os",
            branch="ralph/task-8",
            base="main",
            title="Task 8",
            body="Initial body.",
        )
    )
    client.record_approval(
        repo="qts/agentic-os",
        pr_number=pr.pr_number,
        approver="@technical-lead",
    )

    client.record_dismissal(repo="qts/agentic-os", pr_number=pr.pr_number)
    approval = client.wait_for_approval(
        ApprovalRequest(repo="qts/agentic-os", pr_number=pr.pr_number)
    )

    assert approval.state == ApprovalState.PENDING
    assert approval.approver is None
    assert validate_commit_sha(approval.current_head_sha) == approval.current_head_sha
    assert approval.approved_head_sha is None
    with pytest.raises(PermissionError, match="requires approval"):
        client.merge_pr(
            MergeRequest(
                repo="qts/agentic-os",
                pr_number=pr.pr_number,
                commit_title="Merge task 8",
                expected_head_sha=approval.current_head_sha,
            )
        )
    assert client.pull_requests[0].merged is False


def test_fake_merge_only_succeeds_after_approval_and_marks_pr_merged() -> None:
    client = FakeGitHubClient()
    client.create_branch(
        BranchRequest(repo="qts/agentic-os", base_ref="main", branch="ralph/task-8")
    )
    pr = client.create_or_update_pr(
        PullRequestRequest(
            repo="qts/agentic-os",
            branch="ralph/task-8",
            base="main",
            title="Task 8",
            body="Initial body.",
        )
    )

    pending = client.wait_for_approval(
        ApprovalRequest(repo="qts/agentic-os", pr_number=pr.pr_number)
    )
    with pytest.raises(PermissionError, match="requires approval"):
        client.merge_pr(
            MergeRequest(
                repo="qts/agentic-os",
                pr_number=pr.pr_number,
                commit_title="Merge task 8",
                expected_head_sha=pending.current_head_sha,
            )
        )

    approval = client.record_approval(
        repo="qts/agentic-os",
        pr_number=pr.pr_number,
        approver="@technical-lead",
    )
    merged = client.merge_pr(
        MergeRequest(
            repo="qts/agentic-os",
            pr_number=pr.pr_number,
            commit_title="Merge task 8",
            expected_head_sha=approval.current_head_sha,
        )
    )

    assert merged.merged is True
    assert merged.pr_number == pr.pr_number
    assert merged.merge_commit_sha
    assert validate_commit_sha(merged.merge_commit_sha) == merged.merge_commit_sha


def test_fake_merge_rejects_stale_expected_head_sha() -> None:
    client = FakeGitHubClient()
    client.create_branch(
        BranchRequest(repo="qts/agentic-os", base_ref="main", branch="ralph/task-8")
    )
    pr = client.create_or_update_pr(
        PullRequestRequest(
            repo="qts/agentic-os",
            branch="ralph/task-8",
            base="main",
            title="Task 8",
            body="Initial body.",
        )
    )
    client.record_approval(
        repo="qts/agentic-os",
        pr_number=pr.pr_number,
        approver="@technical-lead",
    )

    with pytest.raises(ValueError, match="expected head SHA"):
        client.merge_pr(
            MergeRequest(
                repo="qts/agentic-os",
                pr_number=pr.pr_number,
                commit_title="Merge task 8",
                expected_head_sha="0000000000000000000000000000000000000001",
            )
        )


def test_fake_duplicate_merge_is_rejected_after_pr_is_merged() -> None:
    client = FakeGitHubClient()
    client.create_branch(
        BranchRequest(repo="qts/agentic-os", base_ref="main", branch="ralph/task-8")
    )
    pr = client.create_or_update_pr(
        PullRequestRequest(
            repo="qts/agentic-os",
            branch="ralph/task-8",
            base="main",
            title="Task 8",
            body="Initial body.",
        )
    )
    approval = client.record_approval(
        repo="qts/agentic-os",
        pr_number=pr.pr_number,
        approver="@technical-lead",
    )
    client.merge_pr(
        MergeRequest(
            repo="qts/agentic-os",
            pr_number=pr.pr_number,
            commit_title="Merge task 8",
            expected_head_sha=approval.current_head_sha,
        )
    )

    with pytest.raises(ValueError, match="already merged"):
        client.merge_pr(
            MergeRequest(
                repo="qts/agentic-os",
                pr_number=pr.pr_number,
                commit_title="Merge task 8 again",
                expected_head_sha=approval.current_head_sha,
            )
        )


def test_fake_update_to_merged_pr_is_rejected() -> None:
    client = FakeGitHubClient()
    client.create_branch(
        BranchRequest(repo="qts/agentic-os", base_ref="main", branch="ralph/task-8")
    )
    pr = client.create_or_update_pr(
        PullRequestRequest(
            repo="qts/agentic-os",
            branch="ralph/task-8",
            base="main",
            title="Task 8",
            body="Initial body.",
        )
    )
    approval = client.record_approval(
        repo="qts/agentic-os",
        pr_number=pr.pr_number,
        approver="@technical-lead",
    )
    client.merge_pr(
        MergeRequest(
            repo="qts/agentic-os",
            pr_number=pr.pr_number,
            commit_title="Merge task 8",
            expected_head_sha=approval.current_head_sha,
        )
    )

    with pytest.raises(ValueError, match="already merged"):
        client.create_or_update_pr(
            PullRequestRequest(
                repo="qts/agentic-os",
                branch="ralph/task-8",
                base="main",
                title="Task 8 after merge",
                body="Updated after merge.",
            )
        )


def test_fake_request_review_to_merged_pr_is_rejected() -> None:
    client = FakeGitHubClient()
    client.create_branch(
        BranchRequest(repo="qts/agentic-os", base_ref="main", branch="ralph/task-8")
    )
    pr = client.create_or_update_pr(
        PullRequestRequest(
            repo="qts/agentic-os",
            branch="ralph/task-8",
            base="main",
            title="Task 8",
            body="Initial body.",
        )
    )
    approval = client.record_approval(
        repo="qts/agentic-os",
        pr_number=pr.pr_number,
        approver="@technical-lead",
    )
    client.merge_pr(
        MergeRequest(
            repo="qts/agentic-os",
            pr_number=pr.pr_number,
            commit_title="Merge task 8",
            expected_head_sha=approval.current_head_sha,
        )
    )

    with pytest.raises(ValueError, match="already merged"):
        client.request_review(
            ReviewRequest(repo="qts/agentic-os", pr_number=pr.pr_number, reviewer="@qa")
        )


def test_fake_post_comment_to_merged_pr_is_rejected() -> None:
    client = FakeGitHubClient()
    client.create_branch(
        BranchRequest(repo="qts/agentic-os", base_ref="main", branch="ralph/task-8")
    )
    pr = client.create_or_update_pr(
        PullRequestRequest(
            repo="qts/agentic-os",
            branch="ralph/task-8",
            base="main",
            title="Task 8",
            body="Initial body.",
        )
    )
    approval = client.record_approval(
        repo="qts/agentic-os",
        pr_number=pr.pr_number,
        approver="@technical-lead",
    )
    client.merge_pr(
        MergeRequest(
            repo="qts/agentic-os",
            pr_number=pr.pr_number,
            commit_title="Merge task 8",
            expected_head_sha=approval.current_head_sha,
        )
    )

    with pytest.raises(ValueError, match="already merged"):
        client.post_comment(
            CommentRequest(repo="qts/agentic-os", pr_number=pr.pr_number, body="After merge")
        )


def test_fake_record_approval_to_merged_pr_is_rejected() -> None:
    client = FakeGitHubClient()
    client.create_branch(
        BranchRequest(repo="qts/agentic-os", base_ref="main", branch="ralph/task-8")
    )
    pr = client.create_or_update_pr(
        PullRequestRequest(
            repo="qts/agentic-os",
            branch="ralph/task-8",
            base="main",
            title="Task 8",
            body="Initial body.",
        )
    )
    approval = client.record_approval(
        repo="qts/agentic-os",
        pr_number=pr.pr_number,
        approver="@technical-lead",
    )
    client.merge_pr(
        MergeRequest(
            repo="qts/agentic-os",
            pr_number=pr.pr_number,
            commit_title="Merge task 8",
            expected_head_sha=approval.current_head_sha,
        )
    )

    with pytest.raises(ValueError, match="already merged"):
        client.record_approval(
            repo="qts/agentic-os",
            pr_number=pr.pr_number,
            approver="@qa",
        )


def test_fake_post_comment_appends_ordered_comments() -> None:
    client = FakeGitHubClient()
    client.create_branch(
        BranchRequest(repo="qts/agentic-os", base_ref="main", branch="ralph/task-8")
    )
    pr = client.create_or_update_pr(
        PullRequestRequest(
            repo="qts/agentic-os",
            branch="ralph/task-8",
            base="main",
            title="Task 8",
            body="Initial body.",
        )
    )

    first = client.post_comment(
        CommentRequest(repo="qts/agentic-os", pr_number=pr.pr_number, body="First")
    )
    second = client.post_comment(
        CommentRequest(repo="qts/agentic-os", pr_number=pr.pr_number, body="Second")
    )

    assert first.comment_number == 1
    assert first.comments == ("First",)
    assert second.comment_number == 2
    assert second.comments == ("First", "Second")
