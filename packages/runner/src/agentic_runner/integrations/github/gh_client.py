from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from agentic_runner.integrations.github.auth import (
    GitHubAppAuthenticationError,
    GitHubAppAuthProvider,
)
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
    ReviewRequest,
    ReviewResponse,
    is_human_reviewer,
)

_DEFAULT_API_BASE_URL = "https://api.github.com"
_REQUEST_TIMEOUT_SECONDS = 10.0
_REPO_PATH_SEGMENT_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")
_URL_CONTROL_CHARS = frozenset(chr(value) for value in range(0x20)) | {chr(0x7F)}
_GITHUB_BRANCH_REF_FORBIDDEN_CHARS = _URL_CONTROL_CHARS | set(" ?#~^*[:\\+")
_GITHUB_BRANCH_REF_FORBIDDEN_VALUES = frozenset(
    {"-", "--all", "--delete", "--mirror", "all", "delete", "head", "mirror"}
)
_GITHUB_REVIEWS_PAGE_SIZE = 100
_GITHUB_PR_FILES_PAGE_SIZE = 100
_COMMIT_SHA_PATTERN = re.compile(r"^[0-9A-Fa-f]{40}$")
# REST cannot take a pull request out of draft — PATCH /pulls/{n} silently ignores
# `draft` — so the `pr.review` seam goes through the one API that supports it.
_MARK_READY_MUTATION = (
    "mutation($pullRequestId: ID!) { "
    "markPullRequestReadyForReview(input: {pullRequestId: $pullRequestId}) "
    "{ pullRequest { number isDraft } } }"
)


class GitHubAppClient:
    """Production GitHub App adapter with private auth transport helpers.

    Branch, pull request, review request, and comment lifecycle methods use authenticated
    GitHub REST calls including approval polling and merge gating.
    """

    def __init__(
        self,
        app_id: str | None = None,
        installation_id: str | None = None,
        private_key_pem: str | None = None,
        api_base_url: str = _DEFAULT_API_BASE_URL,
        *,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._configured = self._validate_configuration(app_id, installation_id, private_key_pem)
        self._api_base_url = api_base_url.rstrip("/")
        self._http_client = http_client or httpx.Client(timeout=_REQUEST_TIMEOUT_SECONDS)
        self._auth_provider: GitHubAppAuthProvider | None = None
        if self._configured:
            assert app_id is not None
            assert installation_id is not None
            assert private_key_pem is not None
            self._auth_provider = GitHubAppAuthProvider(
                app_id=app_id,
                installation_id=installation_id,
                private_key_pem=private_key_pem,
                api_base_url=api_base_url,
                http_client=self._http_client,
            )

    @staticmethod
    def _validate_configuration(
        app_id: str | None, installation_id: str | None, private_key_pem: str | None
    ) -> bool:
        provided_values = (app_id, installation_id, private_key_pem)
        if all(value is None for value in provided_values):
            return False

        missing_fields = [
            field
            for field, value in (
                ("app_id", app_id),
                ("installation_id", installation_id),
                ("private_key_pem", private_key_pem),
            )
            if value is None or value.strip() == ""
        ]
        if missing_fields:
            raise ValueError(
                "GitHub App credentials are required and must be non-empty: "
                + ", ".join(missing_fields)
            )
        return True

    def _ensure_configured(self) -> None:
        if not self._configured:
            raise GitHubAppAuthenticationError("GitHub App credentials are not configured")

    def _generate_app_jwt(self) -> str:
        self._ensure_configured()
        if self._auth_provider is None:
            raise GitHubAppAuthenticationError("GitHub App credentials are not configured")
        return self._auth_provider.generate_app_jwt()

    def _request_json(
        self, method: str, path: str, *, json_body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        token = self._get_installation_token()
        headers = self._github_headers(f"Bearer {token}")
        return self._send_json_request(method, path, headers=headers, json_body=json_body)

    def _get_installation_token(self) -> str:
        self._ensure_configured()
        if self._auth_provider is None:
            raise GitHubAppAuthenticationError("GitHub App credentials are not configured")
        return self._auth_provider.get_installation_token()

    def installation_token_provider(self) -> Callable[[], str] | None:
        """Return a callable minting fresh installation tokens, or None if unconfigured.

        The returned callable reuses this client's auth provider, so installation
        tokens are cached and refreshed in one place across REST calls and git pushes.
        """

        if self._auth_provider is None:
            return None
        return self._auth_provider.get_installation_token

    @staticmethod
    def _github_headers(authorization: str) -> dict[str, str]:
        return GitHubAppAuthProvider.github_headers(authorization)

    def _send_json_request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str],
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        response_data = self._send_json_request_value(
            method,
            path,
            headers=headers,
            json_body=json_body,
        )
        if not isinstance(response_data, dict):
            raise GitHubAppAuthenticationError(
                "GitHub API response JSON object was invalid"
            ) from None
        return response_data

    def _send_json_request_value(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str],
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        response = self._send_authenticated_request(
            method,
            path,
            headers=headers,
            json_body=json_body,
        )

        if response.status_code < 200 or response.status_code >= 300:
            raise GitHubAppAuthenticationError(
                f"GitHub API request failed with status {response.status_code}"
            ) from None

        return self._decode_json_response(response)

    def _send_authenticated_request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str],
        json_body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        url = f"{self._api_base_url}/{path.lstrip('/')}"
        try:
            return self._http_client.request(
                method,
                url,
                headers=headers,
                json=json_body,
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError:
            raise GitHubAppAuthenticationError("GitHub API request failed") from None

    @staticmethod
    def _decode_json_response(response: httpx.Response) -> Any:
        try:
            response_data = response.json()
        except ValueError:
            raise GitHubAppAuthenticationError("GitHub API response was not valid JSON") from None
        return response_data

    def _request_json_list(self, method: str, path: str) -> list[Any]:
        token = self._get_installation_token()
        headers = self._github_headers(f"Bearer {token}")
        response_data = self._send_json_request_value(method, path, headers=headers)
        if not isinstance(response_data, list):
            raise GitHubAppAuthenticationError(
                "GitHub API response JSON array was invalid"
            ) from None
        return response_data

    def _request_json_list_pages(self, method: str, path: str) -> list[Any]:
        token = self._get_installation_token()
        headers = self._github_headers(f"Bearer {token}")
        items: list[Any] = []
        next_path: str | None = path
        while next_path is not None:
            response = self._send_authenticated_request(method, next_path, headers=headers)
            if response.status_code < 200 or response.status_code >= 300:
                raise GitHubAppAuthenticationError(
                    f"GitHub API request failed with status {response.status_code}"
                ) from None
            response_data = self._decode_json_response(response)
            if not isinstance(response_data, list):
                raise GitHubAppAuthenticationError(
                    "GitHub API response JSON array was invalid"
                ) from None
            items.extend(response_data)
            next_path = self._next_page_path(response)
        return items

    def _request_response(
        self, method: str, path: str, *, json_body: dict[str, Any] | None = None
    ) -> httpx.Response:
        token = self._get_installation_token()
        headers = self._github_headers(f"Bearer {token}")
        return self._send_authenticated_request(method, path, headers=headers, json_body=json_body)

    def create_branch(self, request: BranchRequest) -> BranchResponse:
        base_ref_name = self._validated_github_branch_ref(request.base_ref)
        branch_name = self._validated_github_branch_ref(request.branch)
        repo_path = self._encoded_repo_path(request.repo)
        encoded_base_ref = self._encoded_git_ref_path_value(base_ref_name)
        base_ref = self._request_json(
            "GET",
            f"/repos/{repo_path}/git/ref/heads/{encoded_base_ref}",
        )
        base_sha = self._required_nested_str(base_ref, ("object", "sha"), "base ref sha")
        response = self._request_response(
            "POST",
            f"/repos/{repo_path}/git/refs",
            json_body={"ref": f"refs/heads/{branch_name}", "sha": base_sha},
        )

        if response.status_code == 422 and self._is_already_exists_response(response):
            return BranchResponse(
                repo=request.repo,
                branch=branch_name,
                base_ref=base_ref_name,
                created=False,
            )
        if response.status_code < 200 or response.status_code >= 300:
            raise GitHubAppAuthenticationError(
                f"GitHub API request failed with status {response.status_code}"
            ) from None

        response_data = self._decode_json_response(response)
        if not isinstance(response_data, dict):
            raise GitHubAppAuthenticationError(
                "GitHub API response JSON object was invalid"
            ) from None
        self._required_str(response_data, "ref", "created ref")
        return BranchResponse(
            repo=request.repo,
            branch=branch_name,
            base_ref=base_ref_name,
            created=True,
        )

    def create_or_update_pr(self, request: PullRequestRequest) -> PullRequestResponse:
        branch_name = self._validated_github_branch_ref(request.branch)
        base_name = self._validated_github_branch_ref(request.base)
        owner, _repo_name = self._split_repo(request.repo)
        repo_path = self._encoded_repo_path_from_parts(owner, _repo_name)
        encoded_head = quote(f"{owner}:{branch_name}", safe="")
        existing_pull_requests = self._request_json_list(
            "GET",
            f"/repos/{repo_path}/pulls?head={encoded_head}&state=open",
        )
        if existing_pull_requests:
            pr_number = self._required_int_from_mapping(
                existing_pull_requests[0], "number", "pull request number"
            )
            response_data = self._request_json(
                "PATCH",
                f"/repos/{repo_path}/pulls/{pr_number}",
                json_body={
                    "base": base_name,
                    "title": request.title,
                    "body": request.body,
                },
            )
            return self._to_pull_request_response(request.repo, response_data, created=False)

        response_data = self._request_json(
            "POST",
            f"/repos/{repo_path}/pulls",
            json_body={
                "head": branch_name,
                "base": base_name,
                "title": request.title,
                "body": request.body,
                "draft": request.draft,
            },
        )
        return self._to_pull_request_response(request.repo, response_data, created=True)

    def mark_pr_ready_for_review(
        self,
        request: PullRequestReadyRequest,
    ) -> PullRequestReadyResponse:
        repo_path = self._encoded_repo_path(request.repo)
        pr_number = self._validated_pr_number(request.pr_number)
        pull_request = self._request_json("GET", f"/repos/{repo_path}/pulls/{pr_number}")
        if not self._optional_bool(pull_request, "draft", default=False):
            return PullRequestReadyResponse(
                repo=request.repo,
                pr_number=pr_number,
                ready=True,
                marked_ready=False,
            )
        node_id = self._required_str(pull_request, "node_id", "pull request node id")
        response_data = self._request_json(
            "POST",
            "/graphql",
            json_body={"query": _MARK_READY_MUTATION, "variables": {"pullRequestId": node_id}},
        )
        if response_data.get("errors"):
            # GraphQL reports failures inside a 200, so the REST status check above is
            # blind to them; without this a refused mutation would read as success and
            # the gate would wait forever on a PR still in draft.
            raise GitHubAppAuthenticationError(
                "GitHub API request failed: markPullRequestReadyForReview was rejected"
            ) from None
        return PullRequestReadyResponse(
            repo=request.repo,
            pr_number=pr_number,
            ready=True,
            marked_ready=True,
        )

    def close_pr(self, request: PullRequestCloseRequest) -> PullRequestCloseResponse:
        """Close a PR without merging it. The branch is deliberately left in place.

        The ending an owner refusal or timeout at a PR verb produces (ADR-0011 s15): the
        work survives for whoever picks it up, the org just never gets asked to review it.
        A PR already merged or closed is reported as ``closed=False`` rather than PATCHed,
        so the terminal activity is safe to retry.
        """
        repo_path = self._encoded_repo_path(request.repo)
        pr_number = self._validated_pr_number(request.pr_number)
        pull_request = self._request_json("GET", f"/repos/{repo_path}/pulls/{pr_number}")
        state = self._required_str(pull_request, "state", "pull request state")
        if state != "open":
            return PullRequestCloseResponse(repo=request.repo, pr_number=pr_number, closed=False)
        self._request_json(
            "PATCH",
            f"/repos/{repo_path}/pulls/{pr_number}",
            json_body={"state": "closed"},
        )
        return PullRequestCloseResponse(repo=request.repo, pr_number=pr_number, closed=True)

    def retarget_pr(self, request: PullRequestRetargetRequest) -> PullRequestRetargetResponse:
        """Move an open PR onto a new base (a stack's base merged, PRD issue 59). A PR
        already merged or closed is reported ``retargeted=False`` rather than PATCHed."""

        repo_path = self._encoded_repo_path(request.repo)
        pr_number = self._validated_pr_number(request.pr_number)
        base_name = self._validated_github_branch_ref(request.base)
        pull_request = self._request_json("GET", f"/repos/{repo_path}/pulls/{pr_number}")
        state = self._required_str(pull_request, "state", "pull request state")
        if state != "open":
            return PullRequestRetargetResponse(
                repo=request.repo, pr_number=pr_number, base=base_name, retargeted=False
            )
        self._request_json(
            "PATCH", f"/repos/{repo_path}/pulls/{pr_number}", json_body={"base": base_name}
        )
        return PullRequestRetargetResponse(
            repo=request.repo, pr_number=pr_number, base=base_name, retargeted=True
        )

    def request_review(self, request: ReviewRequest) -> ReviewResponse:
        repo_path = self._encoded_repo_path(request.repo)
        pr_number = self._validated_pr_number(request.pr_number)
        response_data = self._request_json(
            "POST",
            f"/repos/{repo_path}/pulls/{pr_number}/requested_reviewers",
            json_body={"reviewers": [request.reviewer]},
        )
        reviewers = self._reviewers_from_response(response_data)
        return ReviewResponse(
            repo=request.repo,
            pr_number=pr_number,
            reviewer=request.reviewer,
            reviewers=reviewers,
            requested=True,
        )

    def wait_for_approval(self, request: ApprovalRequest) -> ApprovalResponse:
        repo_path = self._encoded_repo_path(request.repo)
        pr_number = self._validated_pr_number(request.pr_number)
        approval, _head_sha = self._current_head_approval(request.repo, repo_path, pr_number)
        return approval

    def merge_pr(self, request: MergeRequest) -> MergeResponse:
        repo_path = self._encoded_repo_path(request.repo)
        pr_number = self._validated_pr_number(request.pr_number)
        approval, _approved_head_sha = self._current_head_approval(
            request.repo, repo_path, pr_number
        )
        if approval.state != ApprovalState.APPROVED:
            raise GitHubAppAuthenticationError(
                "GitHub pull request merge requires approval"
            ) from None
        expected_head_sha = self._validated_commit_sha(
            request.expected_head_sha, "expected head commit SHA"
        )
        if expected_head_sha != approval.current_head_sha:
            raise GitHubAppAuthenticationError(
                "GitHub pull request merge expected head SHA did not match current head"
            ) from None

        response_data = self._request_json(
            "PUT",
            f"/repos/{repo_path}/pulls/{pr_number}/merge",
            json_body={"commit_title": request.commit_title, "sha": expected_head_sha},
        )
        merged = self._required_bool(response_data, "merged", "merge status")
        merge_commit_sha = self._required_commit_sha(response_data, "sha", "merge commit SHA")
        return MergeResponse(
            repo=request.repo,
            pr_number=pr_number,
            commit_title=request.commit_title,
            merged=merged,
            merge_commit_sha=merge_commit_sha,
        )

    def post_comment(self, request: CommentRequest) -> CommentResponse:
        repo_path = self._encoded_repo_path(request.repo)
        pr_number = self._validated_pr_number(request.pr_number)
        response_data = self._request_json(
            "POST",
            f"/repos/{repo_path}/issues/{pr_number}/comments",
            json_body={"body": request.body},
        )
        comment_number = self._required_int(response_data, "id", "comment id")
        body = self._required_str(response_data, "body", "comment body")
        return CommentResponse(
            repo=request.repo,
            pr_number=pr_number,
            comment_number=comment_number,
            body=body,
            comments=(body,),
        )

    def submit_review(self, request: PullRequestReviewRequest) -> PullRequestReviewResponse:
        repo_path = self._encoded_repo_path(request.repo)
        pr_number = self._validated_pr_number(request.pr_number)
        response_data = self._request_json(
            "POST",
            f"/repos/{repo_path}/pulls/{pr_number}/reviews",
            json_body={
                "commit_id": self._validated_commit_sha(request.commit_id, "review commit SHA"),
                "event": request.event.value,
                "body": request.body,
            },
        )
        return PullRequestReviewResponse(
            repo=request.repo,
            pr_number=pr_number,
            review_id=self._required_int(response_data, "id", "review id"),
            state=self._required_str(response_data, "state", "review state"),
            reviewer_login=self._required_nested_str(
                response_data, ("user", "login"), "review user login"
            ),
        )

    def list_changed_files(self, request: PullRequestFilesRequest) -> PullRequestFilesResponse:
        repo_path = self._encoded_repo_path(request.repo)
        pr_number = self._validated_pr_number(request.pr_number)
        files = self._request_json_list_pages(
            "GET",
            f"/repos/{repo_path}/pulls/{pr_number}/files?per_page={_GITHUB_PR_FILES_PAGE_SIZE}",
        )
        changed_paths = tuple(
            self._required_str_from_mapping(file_entry, "filename", "changed file path")
            for file_entry in files
        )
        # `/files` stops paginating at 3000 entries without an error, so a PR that large
        # could push a Protected Path past the cap and auto-merge (QTS-1262). The PR's own
        # `changed_files` count is the check: a listing that disagrees is refused, and the
        # Autonomy Policy holds an unreadable diff for human.
        pull_request = self._request_json("GET", f"/repos/{repo_path}/pulls/{pr_number}")
        changed_file_count = self._required_int(
            pull_request, "changed_files", "pull request changed file count"
        )
        if len(changed_paths) != changed_file_count:
            raise GitHubAppAuthenticationError(
                "GitHub pull request changed file list was incomplete"
            ) from None
        return PullRequestFilesResponse(
            repo=request.repo,
            pr_number=pr_number,
            changed_paths=changed_paths,
        )

    def read_repository(self, request: RepositoryReadRequest) -> RepositoryReadResponse:
        repo_path = self._encoded_repo_path(request.repo)
        repository = self._request_json("GET", f"/repos/{repo_path}")
        return RepositoryReadResponse(
            repo=request.repo,
            default_branch=self._required_str(repository, "default_branch", "default branch"),
        )

    @staticmethod
    def _split_repo(repo: str) -> tuple[str, str]:
        parts = repo.split("/")
        if len(parts) != 2:
            raise GitHubAppAuthenticationError("GitHub repository identifier was invalid") from None
        owner, repo_name = parts
        if not GitHubAppClient._is_safe_repo_path_segment(
            owner
        ) or not GitHubAppClient._is_safe_repo_path_segment(repo_name):
            raise GitHubAppAuthenticationError("GitHub repository identifier was invalid") from None
        return owner, repo_name

    @classmethod
    def _encoded_repo_path(cls, repo: str) -> str:
        owner, repo_name = cls._split_repo(repo)
        return cls._encoded_repo_path_from_parts(owner, repo_name)

    @staticmethod
    def _encoded_repo_path_from_parts(owner: str, repo_name: str) -> str:
        return f"{quote(owner, safe='')}/{quote(repo_name, safe='')}"

    @staticmethod
    def _is_safe_repo_path_segment(value: str) -> bool:
        return bool(_REPO_PATH_SEGMENT_PATTERN.fullmatch(value)) and value not in {".", ".."}

    @staticmethod
    def _encoded_git_ref_path_value(ref: str) -> str:
        return quote(GitHubAppClient._validated_github_branch_ref(ref), safe="")

    @staticmethod
    def _validated_github_branch_ref(ref: str) -> str:
        normalized_ref = ref.lower()
        ref_parts = ref.split("/")
        if (
            ref == ""
            or normalized_ref in _GITHUB_BRANCH_REF_FORBIDDEN_VALUES
            or normalized_ref.startswith("refs/")
            or normalized_ref.startswith("-")
            or ref.startswith(("/", "."))
            or ref.endswith(("/", "."))
            or ".." in ref
            or "@{" in ref
            or any(char in _GITHUB_BRANCH_REF_FORBIDDEN_CHARS for char in ref)
            or any(part in {"", ".", ".."} for part in ref_parts)
            or any(part.endswith(".lock") for part in ref_parts)
        ):
            raise GitHubAppAuthenticationError("GitHub ref identifier was invalid") from None
        return ref

    @staticmethod
    def _validated_pr_number(pr_number: int) -> int:
        if isinstance(pr_number, bool) or not isinstance(pr_number, int) or pr_number < 1:
            raise GitHubAppAuthenticationError("GitHub pull request number was invalid") from None
        return pr_number

    @staticmethod
    def _is_already_exists_response(response: httpx.Response) -> bool:
        try:
            response_data = response.json()
        except ValueError:
            return False
        if not isinstance(response_data, dict):
            return False
        message = response_data.get("message")
        return isinstance(message, str) and "already exists" in message.lower()

    @classmethod
    def _to_pull_request_response(
        cls,
        repo: str,
        response_data: dict[str, Any],
        *,
        created: bool,
    ) -> PullRequestResponse:
        pr_number = cls._required_int(response_data, "number", "pull request number")
        title = cls._required_str(response_data, "title", "pull request title")
        body = cls._required_str(response_data, "body", "pull request body")
        branch = cls._required_nested_str(response_data, ("head", "ref"), "pull request head ref")
        base = cls._required_nested_str(response_data, ("base", "ref"), "pull request base ref")
        merged = cls._optional_bool(response_data, "merged", default=False)
        draft = cls._optional_bool(response_data, "draft", default=False)
        return PullRequestResponse(
            repo=repo,
            pr_number=pr_number,
            branch=branch,
            base=base,
            title=title,
            body=body,
            created=created,
            merged=merged,
            draft=draft,
        )

    @classmethod
    def _reviewers_from_response(cls, response_data: dict[str, Any]) -> tuple[str, ...]:
        requested_reviewers = response_data.get("requested_reviewers")
        if not isinstance(requested_reviewers, list):
            raise GitHubAppAuthenticationError("GitHub API response missing reviewers") from None
        reviewers: list[str] = []
        for reviewer in requested_reviewers:
            reviewers.append(cls._required_str_from_mapping(reviewer, "login", "reviewer login"))
        return tuple(reviewers)

    def _current_head_approval(
        self,
        repo: str,
        repo_path: str,
        pr_number: int,
    ) -> tuple[ApprovalResponse, str]:
        pull_request = self._request_json("GET", f"/repos/{repo_path}/pulls/{pr_number}")
        head_sha = self._required_nested_commit_sha(
            pull_request, ("head", "sha"), "pull request head commit SHA"
        )
        pr_state = self._required_str(pull_request, "state", "pull request state")
        if pr_state not in {"open", "closed"}:
            raise GitHubAppAuthenticationError(
                "GitHub API response invalid pull request state"
            ) from None
        if pr_state == "closed":
            # The PR lifecycle supersedes any review verdict: reviews on a merged or
            # closed PR can no longer gate anything, so skip the reviews fetch and report
            # the lifecycle outcome. Merge attribution is read leniently — a malformed
            # merged_by/merge_commit_sha must degrade to an unattributed merge, not fail
            # the poll into the ~3-day approval_timeout this outcome exists to prevent.
            merged = self._required_bool(pull_request, "merged", "pull request merged flag")
            return (
                ApprovalResponse(
                    repo=repo,
                    pr_number=pr_number,
                    state=ApprovalState.PENDING,
                    current_head_sha=head_sha,
                    pr_merged=merged,
                    pr_closed=not merged,
                    merged_by=(
                        self._optional_login(pull_request.get("merged_by")) if merged else None
                    ),
                    merge_commit_sha=(
                        self._optional_commit_sha(pull_request.get("merge_commit_sha"))
                        if merged
                        else None
                    ),
                ),
                head_sha,
            )
        reviews = self._request_json_list_pages(
            "GET",
            f"/repos/{repo_path}/pulls/{pr_number}/reviews?per_page={_GITHUB_REVIEWS_PAGE_SIZE}",
        )

        # GitHub's review decision is per reviewer: each reviewer's LATEST decisive
        # review is their standing verdict, and one reviewer's later approval never
        # overrides another reviewer's active changes-request. Collapsing to a single
        # globally-latest review would let exactly that override happen — in both the
        # Reviewer Gate and the merge_pr guard, which share this computation.
        latest_by_reviewer: dict[str, tuple[str, int, str, str, str]] = {}
        # ``user.type`` is read leniently: a missing or odd value falls back to the
        # ``[bot]`` login suffix, which GitHub reserves for App accounts, so a malformed
        # field can only ever make the four-eyes count stricter.
        human_types: dict[str, str] = {}
        for index, review in enumerate(reviews):
            state = self._required_str_from_mapping(review, "state", "review state")
            if state not in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
                continue
            commit_id = self._required_commit_sha_from_mapping(
                review, "commit_id", "review commit SHA"
            )
            if commit_id != head_sha:
                continue
            submitted_at = self._required_str_from_mapping(
                review, "submitted_at", "review submitted timestamp"
            )
            approver = self._required_nested_str(review, ("user", "login"), "review user login")
            candidate = (submitted_at, index, state, approver, commit_id)
            user = review.get("user")
            if isinstance(user, dict) and isinstance(user.get("type"), str):
                human_types[approver] = str(user["type"])
            previous = latest_by_reviewer.get(approver)
            if previous is None or candidate[:2] >= previous[:2]:
                latest_by_reviewer[approver] = candidate

        # A reviewer whose standing verdict is DISMISSED contributes nothing — the
        # dismissal clears only that reviewer's own decision, never another reviewer's.
        # An Agent's changes-request is the Critic's veto (PRD issue 54), which the
        # workflow holds and the seams assert; it is not the org's denial, so it must
        # never end the Work Record through the Reviewer Gate.
        blocking = [
            candidate
            for candidate in latest_by_reviewer.values()
            if candidate[2] == "CHANGES_REQUESTED"
            and is_human_reviewer(candidate[3], user_type=human_types.get(candidate[3]))
        ]
        if blocking:
            newest_block = max(blocking, key=lambda candidate: candidate[:2])
            return (
                ApprovalResponse(
                    repo=repo,
                    pr_number=pr_number,
                    state=ApprovalState.CHANGES_REQUESTED,
                    current_head_sha=head_sha,
                    approver=newest_block[3],
                ),
                head_sha,
            )
        approvals = [
            candidate for candidate in latest_by_reviewer.values() if candidate[2] == "APPROVED"
        ]
        if not approvals:
            return (
                ApprovalResponse(
                    repo=repo,
                    pr_number=pr_number,
                    state=ApprovalState.PENDING,
                    current_head_sha=head_sha,
                ),
                head_sha,
            )
        newest_approval = max(approvals, key=lambda candidate: candidate[:2])
        return (
            ApprovalResponse(
                repo=repo,
                pr_number=pr_number,
                state=ApprovalState.APPROVED,
                current_head_sha=head_sha,
                approved_head_sha=newest_approval[4],
                approver=newest_approval[3],
                approving_reviewers=tuple(
                    sorted(
                        candidate[3]
                        for candidate in approvals
                        if is_human_reviewer(candidate[3], user_type=human_types.get(candidate[3]))
                    )
                ),
            ),
            head_sha,
        )

    def _next_page_path(self, response: httpx.Response) -> str | None:
        link_header = response.headers.get("Link")
        if link_header is None:
            return None
        for link_part in link_header.split(","):
            match = re.search(r'<([^>]+)>\s*;\s*rel="next"', link_part.strip())
            if match is not None:
                return self._api_relative_path_from_url(match.group(1))
        return None

    def _api_relative_path_from_url(self, url: str) -> str:
        if any(char in _URL_CONTROL_CHARS for char in url):
            raise GitHubAppAuthenticationError("GitHub API pagination link was invalid") from None

        parsed_url = urlsplit(url)
        if parsed_url.scheme == "" and parsed_url.netloc == "":
            path = parsed_url.path
        else:
            parsed_api_base_url = urlsplit(self._api_base_url)
            if (
                parsed_url.scheme != parsed_api_base_url.scheme
                or parsed_url.netloc != parsed_api_base_url.netloc
            ):
                raise GitHubAppAuthenticationError(
                    "GitHub API pagination link was invalid"
                ) from None

            base_path = parsed_api_base_url.path.rstrip("/")
            if base_path != "" and not (
                parsed_url.path == base_path or parsed_url.path.startswith(f"{base_path}/")
            ):
                raise GitHubAppAuthenticationError(
                    "GitHub API pagination link was invalid"
                ) from None
            path = parsed_url.path.removeprefix(base_path) if base_path != "" else parsed_url.path

        if path == "" or not path.startswith("/"):
            raise GitHubAppAuthenticationError("GitHub API pagination link was invalid") from None
        return f"{path}?{parsed_url.query}" if parsed_url.query else path

    @classmethod
    def _required_nested_str(
        cls,
        response_data: dict[str, Any],
        path: tuple[str, str],
        field_name: str,
    ) -> str:
        parent = response_data.get(path[0])
        if not isinstance(parent, dict):
            raise GitHubAppAuthenticationError(
                f"GitHub API response missing {field_name}"
            ) from None
        return cls._required_str(parent, path[1], field_name)

    @classmethod
    def _required_nested_commit_sha(
        cls,
        response_data: dict[str, Any],
        path: tuple[str, str],
        field_name: str,
    ) -> str:
        return cls._validated_commit_sha(
            cls._required_nested_str(response_data, path, field_name), field_name
        )

    @classmethod
    def _required_commit_sha(
        cls,
        response_data: dict[str, Any],
        key: str,
        field_name: str,
    ) -> str:
        value = cls._required_str(response_data, key, field_name)
        return cls._validated_commit_sha(value, field_name)

    @classmethod
    def _required_commit_sha_from_mapping(
        cls,
        response_data: Any,
        key: str,
        field_name: str,
    ) -> str:
        return cls._validated_commit_sha(
            cls._required_str_from_mapping(response_data, key, field_name), field_name
        )

    @staticmethod
    def _validated_commit_sha(value: str, field_name: str) -> str:
        if _COMMIT_SHA_PATTERN.fullmatch(value) is None:
            raise GitHubAppAuthenticationError(
                f"GitHub commit SHA was invalid for {field_name}"
            ) from None
        return value

    @staticmethod
    def _optional_login(value: Any) -> str | None:
        """Lenient user-object read for attribution fields: None over a raised error."""
        if not isinstance(value, dict):
            return None
        login = value.get("login")
        return login if isinstance(login, str) and login else None

    @staticmethod
    def _optional_commit_sha(value: Any) -> str | None:
        """Lenient commit-SHA read for attribution fields: None over a raised error."""
        if isinstance(value, str) and _COMMIT_SHA_PATTERN.fullmatch(value) is not None:
            return value
        return None

    @classmethod
    def _required_str(cls, response_data: dict[str, Any], key: str, field_name: str) -> str:
        return cls._required_str_from_mapping(response_data, key, field_name)

    @staticmethod
    def _required_str_from_mapping(response_data: Any, key: str, field_name: str) -> str:
        if not isinstance(response_data, dict):
            raise GitHubAppAuthenticationError(
                f"GitHub API response missing {field_name}"
            ) from None
        value = response_data.get(key)
        if not isinstance(value, str):
            raise GitHubAppAuthenticationError(
                f"GitHub API response missing {field_name}"
            ) from None
        return value

    @classmethod
    def _required_int(cls, response_data: dict[str, Any], key: str, field_name: str) -> int:
        return cls._required_int_from_mapping(response_data, key, field_name)

    @staticmethod
    def _required_int_from_mapping(response_data: Any, key: str, field_name: str) -> int:
        if not isinstance(response_data, dict):
            raise GitHubAppAuthenticationError(
                f"GitHub API response missing {field_name}"
            ) from None
        value = response_data.get(key)
        if not isinstance(value, int):
            raise GitHubAppAuthenticationError(
                f"GitHub API response missing {field_name}"
            ) from None
        return value

    @staticmethod
    def _optional_bool(response_data: dict[str, Any], key: str, *, default: bool) -> bool:
        value = response_data.get(key, default)
        if not isinstance(value, bool):
            raise GitHubAppAuthenticationError(f"GitHub API response invalid {key}") from None
        return value

    @staticmethod
    def _required_bool(response_data: dict[str, Any], key: str, field_name: str) -> bool:
        value = response_data.get(key)
        if not isinstance(value, bool):
            raise GitHubAppAuthenticationError(
                f"GitHub API response missing {field_name}"
            ) from None
        return value
