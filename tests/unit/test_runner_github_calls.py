"""The Runner-side GitHub calls the platform App used to make (QTS-1253a).

Each activity returns a fact or a typed failure, never an empty success: the Protected-Path
check reads ``list_pr_changed_files``, and an empty list on error would read as "nothing
changed" and could auto-merge.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from agentic_runner.activities import RunnerRalphActivities
from agentic_runner.integrations.github.auth import GitHubAppAuthenticationError
from agentic_runner.integrations.github.fake_client import FakeGitHubClient
from agentic_runner_contracts.activity_io import (
    ChangedFilesInput,
    ChangedFilesOutput,
    GitHubCallError,
    GitHubCallFailure,
    PullRequestCloseInput,
    PullRequestCommentInput,
    RepositoryProbeInput,
    RequestReviewInput,
)
from agentic_runner_contracts.github_port import (
    BranchRequest,
    PullRequestRequest,
    RepositoryReadRequest,
    RepositoryReadResponse,
)
from agentic_runner_contracts.routing import (
    DirectiveRouting,
    RoutingRefusedError,
    RunnerRoutingIdentity,
)

REPOSITORY = "acme/widgets"
WORK_RECORD_ID = "wr-github-calls"
RUNNER_ID = "11111111-1111-4111-8111-111111111111"


class _UnusedFastApiClient:
    """These activities make no control-plane call; fail loudly if one does."""

    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"unexpected FastAPI call: {name}")


class _RaisingGitHubClient(FakeGitHubClient):
    """Every call fails the way the real client does: a GitHubAppAuthenticationError."""

    def __init__(self, message: str = "GitHub API request failed with status 502") -> None:
        super().__init__()
        self.calls = 0
        self._message = message

    def _fail(self, *_: object) -> Any:
        self.calls += 1
        raise GitHubAppAuthenticationError(self._message)

    request_review = list_changed_files = post_comment = close_pr = read_repository = _fail  # type: ignore[assignment]


def _activities(github: FakeGitHubClient | None, **kwargs: Any) -> RunnerRalphActivities:
    return RunnerRalphActivities(
        _UnusedFastApiClient(),  # type: ignore[arg-type]
        github_client=github,
        **kwargs,
    )


def _open_pr(github: FakeGitHubClient, changed_paths: list[str] | None = None) -> int:
    github.create_branch(BranchRequest(repo=REPOSITORY, base_ref="main", branch="agent/x"))
    pr_number = github.create_or_update_pr(
        PullRequestRequest(repo=REPOSITORY, branch="agent/x", base="main", title="t", body="")
    ).pr_number
    if changed_paths is not None:
        github.record_changed_paths(
            repo=REPOSITORY, pr_number=pr_number, changed_paths=changed_paths
        )
    return pr_number


Call = Callable[[RunnerRalphActivities, int], Awaitable[Any]]

CALLS: dict[str, Call] = {
    "request_pr_review": lambda a, pr: a.request_pr_review(
        RequestReviewInput(WORK_RECORD_ID, REPOSITORY, pr, reviewer="octo-reviewer")
    ),
    "list_pr_changed_files": lambda a, pr: a.list_pr_changed_files(
        ChangedFilesInput(WORK_RECORD_ID, REPOSITORY, pr)
    ),
    "post_pr_comment": lambda a, pr: a.post_pr_comment(
        PullRequestCommentInput(WORK_RECORD_ID, REPOSITORY, pr, body="Verification failed")
    ),
    "close_pr": lambda a, pr: a.close_pr(PullRequestCloseInput(WORK_RECORD_ID, REPOSITORY, pr)),
    "probe_repository_access": lambda a, _pr: a.probe_repository_access(
        RepositoryProbeInput(REPOSITORY)
    ),
}


@pytest.mark.asyncio
async def test_request_pr_review_asks_the_reviewer_on_the_pr() -> None:
    github = FakeGitHubClient()
    pr_number = _open_pr(github)

    output = await CALLS["request_pr_review"](_activities(github), pr_number)

    assert output.failure is None
    assert (output.requested, output.reviewer) == (True, "octo-reviewer")
    assert github.review_requests == ((REPOSITORY, pr_number, "octo-reviewer"),)


@pytest.mark.asyncio
async def test_list_pr_changed_files_returns_the_full_list() -> None:
    github = FakeGitHubClient()
    paths = [f"src/file_{index}.py" for index in range(120)]
    pr_number = _open_pr(github, changed_paths=paths)

    output = await CALLS["list_pr_changed_files"](_activities(github), pr_number)

    assert output.failure is None
    assert output.changed_paths == tuple(paths)


@pytest.mark.asyncio
async def test_an_empty_diff_is_a_success_distinct_from_a_failure() -> None:
    github = FakeGitHubClient()
    pr_number = _open_pr(github)

    output = await CALLS["list_pr_changed_files"](_activities(github), pr_number)

    assert (output.changed_paths, output.failure) == ((), None)


@pytest.mark.asyncio
async def test_post_pr_comment_appends_the_comment() -> None:
    github = FakeGitHubClient()
    pr_number = _open_pr(github)

    output = await CALLS["post_pr_comment"](_activities(github), pr_number)

    assert output.failure is None
    assert output.comment_id == 1
    assert github.pull_requests[0].comments == ["Verification failed"]


@pytest.mark.asyncio
async def test_close_pr_closes_once_and_a_retry_is_a_no_op() -> None:
    github = FakeGitHubClient()
    pr_number = _open_pr(github)
    activities = _activities(github)

    first = await CALLS["close_pr"](activities, pr_number)
    retry = await CALLS["close_pr"](activities, pr_number)

    assert (first.closed, first.failure) == (True, None)
    assert (retry.closed, retry.failure) == (False, None)
    assert github.pull_requests[0].closed is True


@pytest.mark.asyncio
async def test_probe_repository_access_reads_the_repository() -> None:
    github = FakeGitHubClient()
    github.record_repository(repo=REPOSITORY, default_branch="trunk")

    output = await CALLS["probe_repository_access"](_activities(github), 0)

    assert (output.default_branch, output.failure) == ("trunk", None)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(CALLS))
async def test_no_github_client_is_a_typed_failure(name: str) -> None:
    output = await CALLS[name](_activities(None), 1)

    assert output.failure == GitHubCallError(reason=GitHubCallFailure.NO_GITHUB_CLIENT)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(CALLS))
async def test_a_github_error_is_a_typed_failure_never_an_empty_success(name: str) -> None:
    github = _RaisingGitHubClient()

    output = await CALLS[name](_activities(github), 1)

    assert github.calls == 1
    assert output.failure is not None
    assert output.failure.reason is GitHubCallFailure.GITHUB_ERROR
    assert "status 502" in output.failure.detail
    if isinstance(output, ChangedFilesOutput):
        assert output.changed_paths is None
    assert getattr(output, "requested", False) is False
    assert getattr(output, "comment_id", 0) == 0
    assert getattr(output, "closed", False) is False
    assert getattr(output, "default_branch", "") == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(CALLS))
async def test_a_pr_or_repository_github_does_not_know_is_a_typed_failure(name: str) -> None:
    output = await CALLS[name](_activities(FakeGitHubClient()), 404)

    assert output.failure is not None
    assert output.failure.reason is GitHubCallFailure.GITHUB_ERROR
    assert "not found" in output.failure.detail


@pytest.mark.asyncio
async def test_a_failure_detail_never_carries_a_token() -> None:
    token = "ghs_" + "a" * 36
    github = _RaisingGitHubClient(message=f"refused with token {token}")

    output = await CALLS["list_pr_changed_files"](_activities(github), 1)

    assert output.failure is not None
    assert token not in output.failure.detail


@pytest.mark.parametrize(
    ("changed_paths", "failure"),
    [((), GitHubCallError(reason=GitHubCallFailure.GITHUB_ERROR)), (None, None)],
)
def test_changed_files_output_carries_exactly_one_of_list_and_failure(
    changed_paths: tuple[str, ...] | None, failure: GitHubCallError | None
) -> None:
    with pytest.raises(ValueError, match="exactly one"):
        ChangedFilesOutput(REPOSITORY, 1, changed_paths=changed_paths, failure=failure)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(CALLS))
async def test_a_misrouted_call_is_refused_before_github_is_touched(name: str) -> None:
    github = _RaisingGitHubClient()
    activities = _activities(
        github,
        routing_identity=RunnerRoutingIdentity(runner_id=RUNNER_ID, host_party="organisation"),
    )
    activities._fastapi_client = _RecordingEvidence()  # type: ignore[assignment]

    with pytest.raises(RoutingRefusedError):
        await CALLS[name](activities, 1)

    assert github.calls == 0


@pytest.mark.asyncio
async def test_a_routed_probe_reaches_github() -> None:
    github = FakeGitHubClient()
    github.record_repository(repo=REPOSITORY)
    activities = _activities(
        github,
        routing_identity=RunnerRoutingIdentity(runner_id=RUNNER_ID, host_party="organisation"),
    )

    output = await activities.probe_repository_access(
        RepositoryProbeInput(
            REPOSITORY, routing=DirectiveRouting(runner_id=RUNNER_ID, host_party="organisation")
        )
    )

    assert output.failure is None


class _RecordingEvidence:
    """A refused Work Record call writes its routing Evidence before raising."""

    async def append_evidence(self, *_: object, **__: object) -> dict[str, Any]:
        return {}


def test_the_fake_port_reads_only_a_recorded_repository() -> None:
    github = FakeGitHubClient()
    github.record_repository(repo=REPOSITORY)

    assert github.read_repository(RepositoryReadRequest(repo=REPOSITORY)) == (
        RepositoryReadResponse(repo=REPOSITORY, default_branch="main")
    )
    with pytest.raises(KeyError):
        github.read_repository(RepositoryReadRequest(repo="acme/other"))
