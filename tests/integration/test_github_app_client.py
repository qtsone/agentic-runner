from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import jwt
import pytest

from agentic_runner.integrations.github.auth import (
    GitHubAppAuthenticationError,
    GitHubAppAuthProvider,
)
from agentic_runner.integrations.github.gh_client import GitHubAppClient
from agentic_runner_contracts.github_port import (
    ApprovalRequest,
    ApprovalState,
    BranchRequest,
    CommentRequest,
    MergeRequest,
    PullRequestFilesRequest,
    PullRequestReadyRequest,
    PullRequestRequest,
    PullRequestReviewRequest,
    RepositoryReadRequest,
    RepositoryReadResponse,
    ReviewEvent,
    ReviewRequest,
)

APP_ID = "123456"
INSTALLATION_ID = "987654"
INSTALLATION_TOKEN = "ghs_secret_installation_token"
INSTALLATION_TOKEN_REFRESH = "ghs_secret_refreshed_installation_token"
CURRENT_TIME = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
CURRENT_HEAD_SHA = "a" * 40
OLD_HEAD_SHA = "b" * 40
NEWER_HEAD_SHA = "c" * 40
STALE_HEAD_SHA = "d" * 40
MERGE_COMMIT_SHA = "e" * 40
PRIVATE_KEY_PEM = """-----BEGIN RSA PRIVATE KEY-----
MIIEpAIBAAKCAQEApWwpogjzP1FViQsfkxPQaXOxsn8TNnXF2YhDMQJBxWqRN+XJ
eU2H0gi1bstJ4DNKJMQjg8QezwNRcr5RyzqP04mw/5zqHw/9I1tnVNHcMPBhK15x
QcvFS6eQBZgTO2PXHOyvUUyynGtqvtF6WGJFChLxkt7NNW7E1F1Dux6v/tPx8yR1
NgL9N0nwJLZnrtbwzVk2vpKt7VFc5vVA/gkXHpP4I/74PVqYxAq5bf1oRmjSeLcQ
jfrwr1VRiNrmDoX+vscauTLkBO+jODhknXt+dfi0YPAAa+lv0CudprSpz0oxEIWX
GyO8hkUl+HEBz07JIVhNG46Fw5cx2lMhAX5D6wIDAQABAoIBAEb2eX2rPT5CU+Ew
RmE/tL4oBWi/HqzUJQXGcJyLjU91Acrq5l0FJ2iwl7RpvM1S81GGWn3iGh1QHRaO
EmSOQLjMboOY+s5Me5k5UsCOLllIJUcHgqppEb/8p8nejRGDKPqdhi/oKQ70/ZvS
HRvhPCCwM7V/oqRzWjiHsdCJv5IfA/7DvZMYE0ErSE+5sk43YsdyBUln+mtBhGgx
bL1HZMINOot1KpQn+EjGYj4DXGN/JOdCeW+uDP5MN115SmIUM6Hh21lfqxv2bQ63
cHFnL0CRhihZ+bFjetfh5iNm/fusgd+NfWrdysLhrshPPMlmNDdUkGE6B4EmySCx
AuRItaECgYEA50IYzbWfBTvhRaPGJm4sTFfHN5w0ZYJFS8vO8umxcyqe2w3oOn48
wkME4ENp00/dP/D/QXKcWbJrqEI7JhLXlvBto0jaM07/e9Ux6V5CiLnyrUt5mP0r
6RXHGH29V5xZyBwZxpxiFho6Ux64G62XoWg/xFLkd1C00hJ2UqR2t2kCgYEAtx7m
bUDagS07FlM+37r/GT4ShvYaSFDcBxxMac8scTh4Me6p1fALyUaOwOxQk+93FKqU
otn1TXb0pVhR15dP3zNIWsyPgLCt32eANxdzqwPAeJkA4mciq9SznOC6jf/sjaHf
uOPD1r/ZDfJ0EUTkJChl2FV47uVEu3ZmopIsqjMCgYAW+LvaA0aOkIoqDsCqJJuF
4dpKLdwOkUgs5UvjWU9lL0CkZddBqDSE339mf4vNj8tchKX2bFoXlt+W0S1q9Mgx
mCRr6dqy6g/6zwysL87QIhh3Gl4z0kJAXwdt6V+bik5o0FHHJtWfeG9+vjhvl2jO
gbqD1/AV4hB0JZ1XTDr2sQKBgQCFYFZJQTFlYQJmcl+bKWJgiluIPXxLK8n2y9/E
OYePN6gkBkdhcaPECEY1smnGNmavgMceDk6jC3+JZtjFhIpCceHDcLcc7pLV41b5
yXUQHH112UtRm/ke2p+wJeb7QmqThlGjIxIjOjzn8a4kXd8ljt8PQMICjq8PM1/y
DTHHDQKBgQDHu+/Ba6iFMjMph+3wX8B/NGpr/Dnoy+ChDcX7cW+i8WNgtB9FQu8t
5JxvFm5pv77rUXyTbrofL7RfgvR3HhkG8bGlztXxrP94RfaXBkLLAYOD/0OaawM5
VHM2Ys5gkUvXVio4wSqE1yESv49Sz0zSk0yZ50Wj10mFXu0zyOmFrA==
-----END RSA PRIVATE KEY-----"""


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> GitHubAppClient:
    return GitHubAppClient(
        app_id=APP_ID,
        installation_id=INSTALLATION_ID,
        private_key_pem=PRIVATE_KEY_PEM,
        api_base_url="https://github.test/api",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def _auth_provider(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    clock: Callable[[], datetime] | None = None,
) -> GitHubAppAuthProvider:
    kwargs: dict[str, Any] = {}
    if clock is not None:
        kwargs["clock"] = clock
    return GitHubAppAuthProvider(
        app_id=APP_ID,
        installation_id=INSTALLATION_ID,
        private_key_pem=PRIVATE_KEY_PEM,
        api_base_url="https://github.test/api",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        **kwargs,
    )


def _visible_chain_messages(error: BaseException) -> list[str]:
    messages: list[str] = []
    current: BaseException | None = error
    while current is not None:
        messages.append(str(current))
        if current.__cause__ is not None:
            current = current.__cause__
        elif current.__suppress_context__:
            current = None
        else:
            current = current.__context__
    return messages


def _assert_sanitized(error: BaseException, forbidden: Iterable[str]) -> None:
    joined = "\n".join(_visible_chain_messages(error))
    for secret in forbidden:
        assert secret not in joined
    assert "Authorization" not in joined
    assert "Bearer" not in joined
    assert "raw-body" not in joined


def _client_that_records_requests(seen_requests: list[httpx.Request]) -> GitHubAppClient:
    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        return httpx.Response(500, text="raw-body should not be requested")

    return _client(handler)


def _installation_token_payload(
    token: str = INSTALLATION_TOKEN,
    *,
    expires_at: datetime | None = None,
) -> dict[str, str]:
    expires_at = expires_at or CURRENT_TIME + timedelta(hours=1)
    return {"token": token, "expires_at": expires_at.isoformat().replace("+00:00", "Z")}


def _installation_token_response(request: httpx.Request) -> httpx.Response | None:
    if request.url.path == f"/api/app/installations/{INSTALLATION_ID}/access_tokens":
        return httpx.Response(201, json=_installation_token_payload())
    return None


def _pr_response(head_sha: str = CURRENT_HEAD_SHA) -> httpx.Response:
    return httpx.Response(200, json={"head": {"sha": head_sha}, "state": "open", "merged": False})


def _closed_pr_response(
    *,
    merged: bool,
    head_sha: str = CURRENT_HEAD_SHA,
    merged_by: Any = None,
    merge_commit_sha: Any = None,
) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "head": {"sha": head_sha},
            "state": "closed",
            "merged": merged,
            "merged_by": merged_by,
            "merge_commit_sha": merge_commit_sha,
        },
    )


def _review_response(
    state: str,
    commit_id: str,
    submitted_at: str,
    login: str = "technical-lead",
    user_type: str = "User",
) -> dict[str, Any]:
    return {
        "state": state,
        "commit_id": commit_id,
        "submitted_at": submitted_at,
        "user": {"login": login, "type": user_type},
    }


def test_installation_token_provider_mints_token_when_configured() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        response = _installation_token_response(request)
        assert response is not None
        return response

    provider = _client(handler).installation_token_provider()

    assert provider is not None
    assert provider() == INSTALLATION_TOKEN


def test_installation_token_provider_returns_none_when_unconfigured() -> None:
    assert GitHubAppClient().installation_token_provider() is None


def test_wait_for_approval_rejects_invalid_pr_head_sha_from_api_response() -> None:
    seen_requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if token_response := _installation_token_response(request):
            return token_response
        seen_requests.append((request.method, request.url.path))
        if request.url.path == "/api/repos/qts/agentic-os/pulls/42":
            return _pr_response("current-head-sha")
        return httpx.Response(200, json=[])

    client = _client(handler)

    with pytest.raises(RuntimeError, match="commit SHA"):
        client.wait_for_approval(ApprovalRequest(repo="qts/agentic-os", pr_number=42))

    assert seen_requests == [("GET", "/api/repos/qts/agentic-os/pulls/42")]


def test_wait_for_approval_rejects_invalid_review_commit_id_from_api_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if token_response := _installation_token_response(request):
            return token_response
        if request.url.path == "/api/repos/qts/agentic-os/pulls/42":
            return _pr_response(CURRENT_HEAD_SHA)
        return httpx.Response(
            200,
            json=[_review_response("APPROVED", "current-head-sha", "2026-06-09T10:00:00Z")],
        )

    client = _client(handler)

    with pytest.raises(RuntimeError, match="commit SHA"):
        client.wait_for_approval(ApprovalRequest(repo="qts/agentic-os", pr_number=42))


def test_merge_pr_rejects_invalid_merge_response_sha() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if token_response := _installation_token_response(request):
            return token_response
        if request.url.path == "/api/repos/qts/agentic-os/pulls/42":
            return _pr_response(CURRENT_HEAD_SHA)
        if request.url.path == "/api/repos/qts/agentic-os/pulls/42/reviews":
            return httpx.Response(
                200,
                json=[_review_response("APPROVED", CURRENT_HEAD_SHA, "2026-06-09T10:00:00Z")],
            )
        return httpx.Response(200, json={"merged": True, "sha": "merge-commit-sha"})

    client = _client(handler)

    with pytest.raises(RuntimeError, match="commit SHA"):
        client.merge_pr(
            MergeRequest(
                repo="qts/agentic-os",
                pr_number=42,
                commit_title="Merge task 21",
                expected_head_sha=CURRENT_HEAD_SHA,
            )
        )


def test_merge_pr_rejects_invalid_expected_head_sha_before_merge_endpoint() -> None:
    seen_requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if token_response := _installation_token_response(request):
            return token_response
        seen_requests.append((request.method, request.url.path))
        if request.url.path == "/api/repos/qts/agentic-os/pulls/42":
            return _pr_response(CURRENT_HEAD_SHA)
        if request.url.path == "/api/repos/qts/agentic-os/pulls/42/reviews":
            return httpx.Response(
                200,
                json=[_review_response("APPROVED", CURRENT_HEAD_SHA, "2026-06-09T10:00:00Z")],
            )
        raise AssertionError("merge endpoint should not be called")

    client = _client(handler)

    with pytest.raises(RuntimeError, match="commit SHA"):
        client.merge_pr(
            MergeRequest(
                repo="qts/agentic-os",
                pr_number=42,
                commit_title="Merge task 21",
                expected_head_sha="current-head-sha",
            )
        )

    assert seen_requests == [
        ("GET", "/api/repos/qts/agentic-os/pulls/42"),
        ("GET", "/api/repos/qts/agentic-os/pulls/42/reviews"),
    ]


def test_auth_provider_generates_valid_rs256_jwt_claims() -> None:
    provider = GitHubAppAuthProvider(
        app_id=APP_ID,
        installation_id=INSTALLATION_ID,
        private_key_pem=PRIVATE_KEY_PEM,
    )

    app_jwt = provider.generate_app_jwt()

    assert jwt.get_unverified_header(app_jwt)["alg"] == "RS256"
    claims = jwt.decode(app_jwt, options={"verify_signature": False})
    assert claims["iss"] == APP_ID
    assert claims["iat"] <= int(time.time())
    assert claims["exp"] <= claims["iat"] + 600
    assert claims["exp"] > claims["iat"]


def test_auth_provider_exchanges_installation_token_with_mock_transport() -> None:
    seen_requests: list[tuple[str, str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        authorization = request.headers["Authorization"]
        seen_requests.append((request.method, request.url.path, authorization))
        assert authorization.startswith("Bearer ")
        app_jwt = authorization.removeprefix("Bearer ")
        claims = jwt.decode(app_jwt, options={"verify_signature": False})
        assert claims["iss"] == APP_ID
        return httpx.Response(201, json=_installation_token_payload())

    provider = _auth_provider(handler, clock=lambda: CURRENT_TIME)

    assert provider.get_installation_token() == INSTALLATION_TOKEN
    assert seen_requests == [
        (
            "POST",
            f"/api/app/installations/{INSTALLATION_ID}/access_tokens",
            seen_requests[0][2],
        )
    ]


def test_auth_provider_reuses_valid_unexpired_installation_token() -> None:
    token_exchange_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_exchange_count
        assert request.url.path == f"/api/app/installations/{INSTALLATION_ID}/access_tokens"
        token_exchange_count += 1
        return httpx.Response(
            201,
            json=_installation_token_payload(expires_at=CURRENT_TIME + timedelta(hours=1)),
        )

    provider = _auth_provider(handler, clock=lambda: CURRENT_TIME)

    assert provider.get_installation_token() == INSTALLATION_TOKEN
    assert provider.get_installation_token() == INSTALLATION_TOKEN
    assert token_exchange_count == 1


def test_auth_provider_refreshes_expired_installation_token() -> None:
    current_time = CURRENT_TIME
    tokens = [INSTALLATION_TOKEN, INSTALLATION_TOKEN_REFRESH]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/api/app/installations/{INSTALLATION_ID}/access_tokens"
        token = tokens.pop(0)
        return httpx.Response(
            201,
            json=_installation_token_payload(token, expires_at=current_time + timedelta(hours=1)),
        )

    provider = _auth_provider(handler, clock=lambda: current_time)

    assert provider.get_installation_token() == INSTALLATION_TOKEN
    current_time = CURRENT_TIME + timedelta(hours=2)
    assert provider.get_installation_token() == INSTALLATION_TOKEN_REFRESH
    assert tokens == []


def test_auth_provider_refreshes_installation_token_within_refresh_skew() -> None:
    current_time = CURRENT_TIME
    tokens = [INSTALLATION_TOKEN, INSTALLATION_TOKEN_REFRESH]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/api/app/installations/{INSTALLATION_ID}/access_tokens"
        token = tokens.pop(0)
        return httpx.Response(
            201,
            json=_installation_token_payload(token, expires_at=current_time + timedelta(minutes=4)),
        )

    provider = _auth_provider(handler, clock=lambda: current_time)

    assert provider.get_installation_token() == INSTALLATION_TOKEN
    assert provider.get_installation_token() == INSTALLATION_TOKEN_REFRESH
    assert tokens == []


def test_auth_provider_rejects_missing_installation_token_expires_at_with_sanitized_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/api/app/installations/{INSTALLATION_ID}/access_tokens"
        return httpx.Response(201, json={"token": INSTALLATION_TOKEN})

    provider = _auth_provider(handler)

    with pytest.raises(RuntimeError) as error:
        provider.get_installation_token()

    assert error.value.__cause__ is None
    assert error.value.__suppress_context__ is True
    _assert_sanitized(error.value, (APP_ID, INSTALLATION_ID, PRIVATE_KEY_PEM, INSTALLATION_TOKEN))


def test_auth_provider_rejects_invalid_installation_token_expires_at_with_sanitized_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/api/app/installations/{INSTALLATION_ID}/access_tokens"
        return httpx.Response(
            201,
            json={
                "token": INSTALLATION_TOKEN,
                "expires_at": "not-a-valid-timestamp",
                "raw": f"raw-body {PRIVATE_KEY_PEM} {INSTALLATION_TOKEN}",
            },
        )

    provider = _auth_provider(handler, clock=lambda: CURRENT_TIME)

    with pytest.raises(RuntimeError) as error:
        provider.get_installation_token()

    assert error.value.__cause__ is None
    assert error.value.__suppress_context__ is True
    _assert_sanitized(
        error.value,
        (APP_ID, INSTALLATION_ID, PRIVATE_KEY_PEM, INSTALLATION_TOKEN, "not-a-valid-timestamp"),
    )


def test_auth_provider_errors_are_sanitized_and_suppress_secret_exception_chains() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(
            f"raw-body {PRIVATE_KEY_PEM} {INSTALLATION_TOKEN} {request.headers}",
            request=request,
        )

    provider = _auth_provider(handler)

    with pytest.raises(RuntimeError) as error:
        provider.get_installation_token()

    assert error.value.__cause__ is None
    assert error.value.__suppress_context__ is True
    _assert_sanitized(error.value, (APP_ID, INSTALLATION_ID, PRIVATE_KEY_PEM, INSTALLATION_TOKEN))


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("app_id", ""),
        ("installation_id", ""),
        ("private_key_pem", ""),
    ),
)
def test_constructor_rejects_empty_credentials_with_sanitized_errors(
    field: str, value: str
) -> None:
    kwargs = {
        "app_id": APP_ID,
        "installation_id": INSTALLATION_ID,
        "private_key_pem": PRIVATE_KEY_PEM,
    }
    kwargs[field] = value

    with pytest.raises(ValueError) as error:
        GitHubAppClient(**kwargs)

    assert field in str(error.value)
    _assert_sanitized(error.value, (APP_ID, INSTALLATION_ID, PRIVATE_KEY_PEM))


def test_generated_app_jwt_uses_rs256_and_expected_github_claims() -> None:
    client = GitHubAppClient(
        app_id=APP_ID,
        installation_id=INSTALLATION_ID,
        private_key_pem=PRIVATE_KEY_PEM,
    )

    app_jwt = client._generate_app_jwt()

    assert jwt.get_unverified_header(app_jwt)["alg"] == "RS256"
    claims = jwt.decode(app_jwt, options={"verify_signature": False})
    assert claims["iss"] == APP_ID
    assert claims["iat"] <= int(time.time())
    assert claims["exp"] <= claims["iat"] + 600
    assert claims["exp"] > claims["iat"]


def test_token_exchange_uses_app_jwt_and_subsequent_request_uses_installation_token() -> None:
    seen_requests: list[tuple[str, str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        authorization = request.headers["Authorization"]
        seen_requests.append((request.method, request.url.path, authorization))
        if request.url.path == f"/api/app/installations/{INSTALLATION_ID}/access_tokens":
            assert authorization.startswith("Bearer ")
            app_jwt = authorization.removeprefix("Bearer ")
            claims = jwt.decode(app_jwt, options={"verify_signature": False})
            assert claims["iss"] == APP_ID
            return httpx.Response(201, json=_installation_token_payload())
        assert request.url.path == "/api/repos/qts/agentic-os"
        assert authorization == f"Bearer {INSTALLATION_TOKEN}"
        return httpx.Response(200, json={"full_name": "qts/agentic-os"})

    client = _client(handler)

    assert client._request_json("GET", "/repos/qts/agentic-os") == {"full_name": "qts/agentic-os"}
    assert seen_requests[0][0:2] == (
        "POST",
        f"/api/app/installations/{INSTALLATION_ID}/access_tokens",
    )
    assert seen_requests[1] == ("GET", "/api/repos/qts/agentic-os", f"Bearer {INSTALLATION_TOKEN}")


@pytest.mark.parametrize(
    "response",
    (
        httpx.Response(500, text="raw-body contains ghs_secret_installation_token"),
        httpx.Response(201, text="raw-body is not json"),
        httpx.Response(201, json={"expires_at": "2026-01-01T00:00:00Z"}),
    ),
)
def test_token_exchange_failures_are_sanitized(response: httpx.Response) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return response

    client = _client(handler)

    with pytest.raises(RuntimeError) as error:
        client._request_json("GET", "/repos/qts/agentic-os")

    _assert_sanitized(error.value, (APP_ID, INSTALLATION_ID, PRIVATE_KEY_PEM, INSTALLATION_TOKEN))


def test_network_exceptions_during_token_exchange_are_sanitized() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(
            f"raw-body {PRIVATE_KEY_PEM} {INSTALLATION_TOKEN}", request=_request
        )

    client = _client(handler)

    with pytest.raises(RuntimeError) as error:
        client._request_json("GET", "/repos/qts/agentic-os")

    _assert_sanitized(error.value, (APP_ID, INSTALLATION_ID, PRIVATE_KEY_PEM, INSTALLATION_TOKEN))


def test_authenticated_request_failures_are_sanitized_after_token_is_stored() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/api/app/installations/{INSTALLATION_ID}/access_tokens":
            return httpx.Response(201, json=_installation_token_payload())
        return httpx.Response(
            403,
            json={
                "message": "raw-body denied",
                "token": INSTALLATION_TOKEN,
                "request": dict(request.headers),
            },
        )

    client = _client(handler)

    with pytest.raises(RuntimeError) as error:
        client._request_json("GET", "/repos/qts/agentic-os")

    _assert_sanitized(error.value, (APP_ID, INSTALLATION_ID, PRIVATE_KEY_PEM, INSTALLATION_TOKEN))


def test_authenticated_request_requires_json_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/api/app/installations/{INSTALLATION_ID}/access_tokens":
            return httpx.Response(201, json=_installation_token_payload())
        return httpx.Response(200, content=b"raw-body is not json")

    client = _client(handler)

    with pytest.raises(RuntimeError) as error:
        client._request_json("GET", "/repos/qts/agentic-os")

    _assert_sanitized(error.value, (APP_ID, INSTALLATION_ID, PRIVATE_KEY_PEM, INSTALLATION_TOKEN))


def test_authenticated_request_returns_json_object_not_arrays() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/api/app/installations/{INSTALLATION_ID}/access_tokens":
            return httpx.Response(201, json=_installation_token_payload())
        return httpx.Response(200, json=["raw-body"])

    client = _client(handler)

    with pytest.raises(RuntimeError) as error:
        client._request_json("GET", "/repos/qts/agentic-os")

    _assert_sanitized(error.value, (APP_ID, INSTALLATION_ID, PRIVATE_KEY_PEM, INSTALLATION_TOKEN))


def test_authenticated_request_accepts_json_payloads() -> None:
    payload: dict[str, Any] = {"title": "Task 21"}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/api/app/installations/{INSTALLATION_ID}/access_tokens":
            return httpx.Response(201, json=_installation_token_payload())
        assert json.loads(request.content) == payload
        return httpx.Response(200, json={"ok": True})

    client = _client(handler)

    assert client._request_json("PATCH", "/repos/qts/agentic-os", json_body=payload) == {"ok": True}


def test_create_branch_reads_base_ref_then_posts_work_ref() -> None:
    seen_requests: list[tuple[str, str, dict[str, Any] | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/api/app/installations/{INSTALLATION_ID}/access_tokens":
            return httpx.Response(201, json=_installation_token_payload())
        body = json.loads(request.content) if request.content else None
        seen_requests.append((request.method, request.url.path, body))
        if request.url.path == "/api/repos/qts/agentic-os/git/ref/heads/main":
            return httpx.Response(200, json={"object": {"sha": "base-sha"}})
        assert request.url.path == "/api/repos/qts/agentic-os/git/refs"
        return httpx.Response(201, json={"ref": "refs/heads/ralph/task-21"})

    client = _client(handler)

    response = client.create_branch(
        BranchRequest(repo="qts/agentic-os", base_ref="main", branch="ralph/task-21")
    )

    assert response.repo == "qts/agentic-os"
    assert response.base_ref == "main"
    assert response.branch == "ralph/task-21"
    assert response.created is True
    assert seen_requests == [
        ("GET", "/api/repos/qts/agentic-os/git/ref/heads/main", None),
        (
            "POST",
            "/api/repos/qts/agentic-os/git/refs",
            {"ref": "refs/heads/ralph/task-21", "sha": "base-sha"},
        ),
    ]


def test_create_branch_preserves_branch_slashes_in_encoded_base_ref_path() -> None:
    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/api/app/installations/{INSTALLATION_ID}/access_tokens":
            return httpx.Response(201, json=_installation_token_payload())
        seen_urls.append(str(request.url))
        if request.method == "GET":
            assert str(request.url).endswith(
                "/api/repos/qts/agentic-os/git/ref/heads/release%2F2026-06"
            )
            return httpx.Response(200, json={"object": {"sha": "base-sha"}})
        assert request.url.path == "/api/repos/qts/agentic-os/git/refs"
        return httpx.Response(201, json={"ref": "refs/heads/ralph/task-21"})

    client = _client(handler)

    response = client.create_branch(
        BranchRequest(repo="qts/agentic-os", base_ref="release/2026-06", branch="ralph/task-21")
    )

    assert response.repo == "qts/agentic-os"
    assert response.base_ref == "release/2026-06"
    assert seen_urls == [
        "https://github.test/api/repos/qts/agentic-os/git/ref/heads/release%2F2026-06",
        "https://github.test/api/repos/qts/agentic-os/git/refs",
    ]


@pytest.mark.parametrize(
    "repo",
    (
        "owner/repo?x=1",
        "owner/repo#frag",
        "owner/repo/extra",
        "../repo",
        "owner/../repo",
    ),
)
@pytest.mark.parametrize(
    "operation",
    ("create_branch", "create_or_update_pr", "request_review", "post_comment"),
)
def test_lifecycle_methods_reject_malformed_repo_before_http_request(
    repo: str, operation: str
) -> None:
    seen_requests: list[httpx.Request] = []
    client = _client_that_records_requests(seen_requests)

    with pytest.raises(RuntimeError) as error:
        if operation == "create_branch":
            client.create_branch(BranchRequest(repo=repo, base_ref="main", branch="ralph/task-21"))
        elif operation == "create_or_update_pr":
            client.create_or_update_pr(
                PullRequestRequest(
                    repo=repo,
                    branch="ralph/task-21",
                    base="main",
                    title="Task 21",
                    body="Lifecycle methods.",
                )
            )
        elif operation == "request_review":
            client.request_review(ReviewRequest(repo=repo, pr_number=42, reviewer="technical-lead"))
        else:
            client.post_comment(CommentRequest(repo=repo, pr_number=42, body="Ready for review."))

    assert seen_requests == []
    assert error.value.__cause__ is None
    assert error.value.__suppress_context__ is True
    _assert_sanitized(error.value, (APP_ID, INSTALLATION_ID, PRIVATE_KEY_PEM, INSTALLATION_TOKEN))
    _assert_sanitized(error.value, (repo,))


def test_create_branch_rejects_malformed_base_ref_before_http_request() -> None:
    seen_requests: list[httpx.Request] = []
    client = _client_that_records_requests(seen_requests)

    with pytest.raises(RuntimeError) as error:
        client.create_branch(
            BranchRequest(repo="qts/agentic-os", base_ref="main?x=1", branch="ralph/task-21")
        )

    assert seen_requests == []
    assert error.value.__cause__ is None
    assert error.value.__suppress_context__ is True
    _assert_sanitized(error.value, (APP_ID, INSTALLATION_ID, PRIVATE_KEY_PEM, INSTALLATION_TOKEN))
    _assert_sanitized(error.value, ("main?x=1",))


@pytest.mark.parametrize(
    ("branch", "base_ref"),
    (
        ("feature?x=1", "main"),
        ("refs/tags/v1", "main"),
        ("feature/../main", "main"),
        ("HEAD", "main"),
        ("ralph/task-21", "main#frag"),
        ("ralph/task-21", "+refs/heads/main:refs/heads/main"),
    ),
)
def test_create_branch_rejects_malformed_branch_refs_before_http_request(
    branch: str, base_ref: str
) -> None:
    seen_requests: list[httpx.Request] = []
    client = _client_that_records_requests(seen_requests)

    with pytest.raises(RuntimeError) as error:
        client.create_branch(BranchRequest(repo="qts/agentic-os", base_ref=base_ref, branch=branch))

    assert seen_requests == []
    assert error.value.__cause__ is None
    assert error.value.__suppress_context__ is True
    _assert_sanitized(error.value, (APP_ID, INSTALLATION_ID, PRIVATE_KEY_PEM, INSTALLATION_TOKEN))
    _assert_sanitized(error.value, (branch, base_ref))


def test_create_branch_treats_existing_work_ref_as_idempotent() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/api/app/installations/{INSTALLATION_ID}/access_tokens":
            return httpx.Response(201, json=_installation_token_payload())
        if request.method == "GET":
            return httpx.Response(200, json={"object": {"sha": "base-sha"}})
        return httpx.Response(422, json={"message": "Reference already exists"})

    client = _client(handler)

    response = client.create_branch(
        BranchRequest(repo="qts/agentic-os", base_ref="main", branch="ralph/task-21")
    )

    assert response.created is False
    assert response.repo == "qts/agentic-os"
    assert response.branch == "ralph/task-21"
    assert response.base_ref == "main"


def test_create_or_update_pr_creates_when_no_open_pr_exists_for_branch() -> None:
    seen_requests: list[tuple[str, str, dict[str, Any] | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/api/app/installations/{INSTALLATION_ID}/access_tokens":
            return httpx.Response(201, json=_installation_token_payload())
        body = json.loads(request.content) if request.content else None
        seen_requests.append((request.method, request.url.path, body))
        if request.method == "GET":
            assert str(request.url).endswith(
                "/api/repos/qts/agentic-os/pulls?head=qts%3Aralph%2Ftask-21&state=open"
            )
            return httpx.Response(200, json=[])
        assert request.url.path == "/api/repos/qts/agentic-os/pulls"
        return httpx.Response(
            201,
            json={
                "number": 42,
                "head": {"ref": "ralph/task-21"},
                "base": {"ref": "main"},
                "title": "Task 21",
                "body": "Lifecycle methods.",
                "merged": False,
            },
        )

    client = _client(handler)

    response = client.create_or_update_pr(
        PullRequestRequest(
            repo="qts/agentic-os",
            branch="ralph/task-21",
            base="main",
            title="Task 21",
            body="Lifecycle methods.",
        )
    )

    assert response.pr_number == 42
    assert response.created is True
    assert response.branch == "ralph/task-21"
    assert response.base == "main"
    assert response.title == "Task 21"
    assert response.body == "Lifecycle methods."
    assert response.merged is False
    assert seen_requests[1] == (
        "POST",
        "/api/repos/qts/agentic-os/pulls",
        {
            "head": "ralph/task-21",
            "base": "main",
            "title": "Task 21",
            "body": "Lifecycle methods.",
            "draft": False,
        },
    )


def test_create_or_update_pr_updates_existing_open_pr_for_branch() -> None:
    seen_requests: list[tuple[str, str, dict[str, Any] | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/api/app/installations/{INSTALLATION_ID}/access_tokens":
            return httpx.Response(201, json=_installation_token_payload())
        body = json.loads(request.content) if request.content else None
        seen_requests.append((request.method, request.url.path, body))
        if request.method == "GET":
            return httpx.Response(200, json=[{"number": 42}])
        assert request.url.path == "/api/repos/qts/agentic-os/pulls/42"
        return httpx.Response(
            200,
            json={
                "number": 42,
                "head": {"ref": "ralph/task-21"},
                "base": {"ref": "main"},
                "title": "Task 21 updated",
                "body": "Updated body.",
                "merged": False,
            },
        )

    client = _client(handler)

    response = client.create_or_update_pr(
        PullRequestRequest(
            repo="qts/agentic-os",
            branch="ralph/task-21",
            base="main",
            title="Task 21 updated",
            body="Updated body.",
        )
    )

    assert response.pr_number == 42
    assert response.created is False
    assert response.title == "Task 21 updated"
    assert response.body == "Updated body."
    assert seen_requests[1] == (
        "PATCH",
        "/api/repos/qts/agentic-os/pulls/42",
        {"base": "main", "title": "Task 21 updated", "body": "Updated body."},
    )


def test_create_or_update_pr_preserves_valid_branch_and_base_refs_with_slashes() -> None:
    seen_requests: list[tuple[str, str, dict[str, Any] | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/api/app/installations/{INSTALLATION_ID}/access_tokens":
            return httpx.Response(201, json=_installation_token_payload())
        body = json.loads(request.content) if request.content else None
        seen_requests.append((request.method, str(request.url), body))
        if request.method == "GET":
            assert str(request.url).endswith(
                "/api/repos/qts/agentic-os/pulls?"
                "head=qts%3Aagent%2F123e4567-e89b-12d3-a456-426614174000-task-21"
                "&state=open"
            )
            return httpx.Response(200, json=[])
        assert request.url.path == "/api/repos/qts/agentic-os/pulls"
        return httpx.Response(
            201,
            json={
                "number": 42,
                "head": {"ref": "agent/123e4567-e89b-12d3-a456-426614174000-task-21"},
                "base": {"ref": "release/2026-06"},
                "title": "Task 21",
                "body": "Lifecycle methods.",
                "merged": False,
            },
        )

    client = _client(handler)

    response = client.create_or_update_pr(
        PullRequestRequest(
            repo="qts/agentic-os",
            branch="agent/123e4567-e89b-12d3-a456-426614174000-task-21",
            base="release/2026-06",
            title="Task 21",
            body="Lifecycle methods.",
        )
    )

    assert response.branch == "agent/123e4567-e89b-12d3-a456-426614174000-task-21"
    assert response.base == "release/2026-06"
    assert seen_requests[1] == (
        "POST",
        "https://github.test/api/repos/qts/agentic-os/pulls",
        {
            "head": "agent/123e4567-e89b-12d3-a456-426614174000-task-21",
            "base": "release/2026-06",
            "title": "Task 21",
            "body": "Lifecycle methods.",
            "draft": False,
        },
    )


@pytest.mark.parametrize(
    ("branch", "base"),
    (
        ("feature?x=1", "main"),
        ("refs/tags/v1", "main"),
        ("feature/../main", "main"),
        ("HEAD", "main"),
        ("ralph/task-21", "main#frag"),
        ("ralph/task-21", "+refs/heads/main:refs/heads/main"),
    ),
)
def test_create_or_update_pr_rejects_malformed_branch_refs_before_http_request(
    branch: str, base: str
) -> None:
    seen_requests: list[httpx.Request] = []
    client = _client_that_records_requests(seen_requests)

    with pytest.raises(RuntimeError) as error:
        client.create_or_update_pr(
            PullRequestRequest(
                repo="qts/agentic-os",
                branch=branch,
                base=base,
                title="Task 21",
                body="Lifecycle methods.",
            )
        )

    assert seen_requests == []
    assert error.value.__cause__ is None
    assert error.value.__suppress_context__ is True
    _assert_sanitized(error.value, (APP_ID, INSTALLATION_ID, PRIVATE_KEY_PEM, INSTALLATION_TOKEN))
    _assert_sanitized(error.value, (branch, base))


def test_request_review_posts_requested_reviewers_body() -> None:
    seen_requests: list[tuple[str, str, dict[str, Any] | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/api/app/installations/{INSTALLATION_ID}/access_tokens":
            return httpx.Response(201, json=_installation_token_payload())
        body = json.loads(request.content) if request.content else None
        seen_requests.append((request.method, request.url.path, body))
        return httpx.Response(
            201,
            json={"requested_reviewers": [{"login": "technical-lead"}]},
        )

    client = _client(handler)

    response = client.request_review(
        ReviewRequest(repo="qts/agentic-os", pr_number=42, reviewer="technical-lead")
    )

    assert response.requested is True
    assert response.reviewers == ("technical-lead",)
    assert seen_requests == [
        (
            "POST",
            "/api/repos/qts/agentic-os/pulls/42/requested_reviewers",
            {"reviewers": ["technical-lead"]},
        )
    ]


@pytest.mark.parametrize("pr_number", ("42?x=1", "42/extra", 0, -1, True))
def test_request_review_rejects_malformed_pr_number_before_http_request(
    pr_number: Any,
) -> None:
    seen_requests: list[httpx.Request] = []
    client = _client_that_records_requests(seen_requests)

    with pytest.raises(RuntimeError) as error:
        client.request_review(
            ReviewRequest(repo="qts/agentic-os", pr_number=pr_number, reviewer="technical-lead")
        )

    assert seen_requests == []
    assert error.value.__cause__ is None
    assert error.value.__suppress_context__ is True
    _assert_sanitized(error.value, (APP_ID, INSTALLATION_ID, PRIVATE_KEY_PEM, INSTALLATION_TOKEN))
    _assert_sanitized(error.value, (str(pr_number),))


@pytest.mark.parametrize(
    "response_json",
    (
        {"users": [{"login": "technical-lead"}], "raw": INSTALLATION_TOKEN},
        {"requested_reviewers": {"login": "technical-lead"}, "raw": INSTALLATION_TOKEN},
        {"requested_reviewers": [{"login": 42}], "raw": INSTALLATION_TOKEN},
    ),
)
def test_request_review_rejects_malformed_requested_reviewers_with_sanitized_error(
    response_json: dict[str, Any],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/api/app/installations/{INSTALLATION_ID}/access_tokens":
            return httpx.Response(201, json=_installation_token_payload())
        return httpx.Response(201, json=response_json)

    client = _client(handler)

    with pytest.raises(RuntimeError) as error:
        client.request_review(
            ReviewRequest(repo="qts/agentic-os", pr_number=42, reviewer="technical-lead")
        )

    assert error.value.__cause__ is None
    assert error.value.__suppress_context__ is True
    _assert_sanitized(error.value, (APP_ID, INSTALLATION_ID, PRIVATE_KEY_PEM, INSTALLATION_TOKEN))


def test_wait_for_approval_returns_approved_for_current_head_approval() -> None:
    seen_requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if token_response := _installation_token_response(request):
            return token_response
        seen_requests.append((request.method, request.url.path))
        if request.url.path == "/api/repos/qts/agentic-os/pulls/42":
            return _pr_response(CURRENT_HEAD_SHA)
        assert request.url.path == "/api/repos/qts/agentic-os/pulls/42/reviews"
        return httpx.Response(
            200,
            json=[
                _review_response(
                    "APPROVED",
                    CURRENT_HEAD_SHA,
                    "2026-06-09T10:00:00Z",
                )
            ],
        )

    client = _client(handler)

    response = client.wait_for_approval(ApprovalRequest(repo="qts/agentic-os", pr_number=42))

    assert response.state == ApprovalState.APPROVED
    assert response.approver == "technical-lead"
    assert response.current_head_sha == CURRENT_HEAD_SHA
    assert response.approved_head_sha == CURRENT_HEAD_SHA
    assert seen_requests == [
        ("GET", "/api/repos/qts/agentic-os/pulls/42"),
        ("GET", "/api/repos/qts/agentic-os/pulls/42/reviews"),
    ]


def test_wait_for_approval_counts_distinct_humans_and_never_the_apps_own_review() -> None:
    # What `required_human_approvals` is counted from (ADR-0011 §6). The Agent has no
    # GitHub identity of its own — it reviews through the installation — so the App's own
    # approval is present, decisive for `state`, and worth nothing to the four-eyes count.
    def handler(request: httpx.Request) -> httpx.Response:
        if token_response := _installation_token_response(request):
            return token_response
        if request.url.path == "/api/repos/qts/agentic-os/pulls/42":
            return _pr_response(CURRENT_HEAD_SHA)
        return httpx.Response(
            200,
            json=[
                _review_response("APPROVED", CURRENT_HEAD_SHA, "2026-06-09T10:00:00Z"),
                _review_response(
                    "APPROVED",
                    CURRENT_HEAD_SHA,
                    "2026-06-09T10:01:00Z",
                    login="second-human",
                ),
                _review_response(
                    "APPROVED",
                    CURRENT_HEAD_SHA,
                    "2026-06-09T10:02:00Z",
                    login="agentic-os[bot]",
                    user_type="Bot",
                ),
                _review_response(
                    "APPROVED",
                    OLD_HEAD_SHA,
                    "2026-06-09T10:03:00Z",
                    login="stale-human",
                ),
            ],
        )

    response = _client(handler).wait_for_approval(
        ApprovalRequest(repo="qts/agentic-os", pr_number=42)
    )

    assert response.state == ApprovalState.APPROVED
    assert response.approving_reviewers == ("second-human", "technical-lead")


def test_wait_for_approval_ignores_old_head_approval() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if token_response := _installation_token_response(request):
            return token_response
        if request.url.path == "/api/repos/qts/agentic-os/pulls/42":
            return _pr_response(CURRENT_HEAD_SHA)
        return httpx.Response(
            200,
            json=[
                _review_response("APPROVED", OLD_HEAD_SHA, "2026-06-09T10:00:00Z"),
                _review_response("COMMENTED", CURRENT_HEAD_SHA, "2026-06-09T10:01:00Z"),
            ],
        )

    client = _client(handler)

    response = client.wait_for_approval(ApprovalRequest(repo="qts/agentic-os", pr_number=42))

    assert response.state == ApprovalState.PENDING
    assert response.approver is None
    assert response.current_head_sha == CURRENT_HEAD_SHA
    assert response.approved_head_sha is None


def test_wait_for_approval_blocks_when_changes_requested_after_approval() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if token_response := _installation_token_response(request):
            return token_response
        if request.url.path == "/api/repos/qts/agentic-os/pulls/42":
            return _pr_response(CURRENT_HEAD_SHA)
        return httpx.Response(
            200,
            json=[
                _review_response("APPROVED", CURRENT_HEAD_SHA, "2026-06-09T10:00:00Z"),
                _review_response(
                    "CHANGES_REQUESTED",
                    CURRENT_HEAD_SHA,
                    "2026-06-09T10:01:00Z",
                ),
            ],
        )

    client = _client(handler)

    response = client.wait_for_approval(ApprovalRequest(repo="qts/agentic-os", pr_number=42))

    # The explicit rejection state: still blocks merge, and (unlike PENDING) lets the
    # Reviewer Gate terminate as a reviewer denial instead of waiting to its deadline.
    assert response.state == ApprovalState.CHANGES_REQUESTED
    assert response.approver == "technical-lead"
    assert response.current_head_sha == CURRENT_HEAD_SHA
    assert response.approved_head_sha is None


def test_wait_for_approval_keeps_changes_request_active_over_later_approval_by_other() -> None:
    # GitHub's decision is per reviewer: reviewer B's later approval must not override
    # reviewer A's standing changes-request (the single-latest-review shortcut did).
    def handler(request: httpx.Request) -> httpx.Response:
        if token_response := _installation_token_response(request):
            return token_response
        if request.url.path == "/api/repos/qts/agentic-os/pulls/42":
            return _pr_response(CURRENT_HEAD_SHA)
        return httpx.Response(
            200,
            json=[
                _review_response(
                    "CHANGES_REQUESTED",
                    CURRENT_HEAD_SHA,
                    "2026-06-09T10:00:00Z",
                    login="blocking-reviewer",
                ),
                _review_response(
                    "APPROVED",
                    CURRENT_HEAD_SHA,
                    "2026-06-09T10:01:00Z",
                    login="approving-reviewer",
                ),
            ],
        )

    client = _client(handler)

    response = client.wait_for_approval(ApprovalRequest(repo="qts/agentic-os", pr_number=42))

    assert response.state == ApprovalState.CHANGES_REQUESTED
    assert response.approver == "blocking-reviewer"


def test_wait_for_approval_dismissal_clears_only_that_reviewers_own_decision() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if token_response := _installation_token_response(request):
            return token_response
        if request.url.path == "/api/repos/qts/agentic-os/pulls/42":
            return _pr_response(CURRENT_HEAD_SHA)
        return httpx.Response(
            200,
            json=[
                _review_response(
                    "APPROVED",
                    CURRENT_HEAD_SHA,
                    "2026-06-09T10:00:00Z",
                    login="fickle-reviewer",
                ),
                _review_response(
                    "APPROVED",
                    CURRENT_HEAD_SHA,
                    "2026-06-09T10:00:30Z",
                    login="steady-reviewer",
                ),
                _review_response(
                    "DISMISSED",
                    CURRENT_HEAD_SHA,
                    "2026-06-09T10:01:00Z",
                    login="fickle-reviewer",
                ),
            ],
        )

    client = _client(handler)

    response = client.wait_for_approval(ApprovalRequest(repo="qts/agentic-os", pr_number=42))

    assert response.state == ApprovalState.APPROVED
    assert response.approver == "steady-reviewer"
    assert response.approved_head_sha == CURRENT_HEAD_SHA


def test_wait_for_approval_stays_pending_when_no_reviews_exist() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if token_response := _installation_token_response(request):
            return token_response
        if request.url.path == "/api/repos/qts/agentic-os/pulls/42":
            return _pr_response(CURRENT_HEAD_SHA)
        return httpx.Response(200, json=[])

    client = _client(handler)

    response = client.wait_for_approval(ApprovalRequest(repo="qts/agentic-os", pr_number=42))

    assert response.state == ApprovalState.PENDING
    assert response.approver is None


def test_wait_for_approval_reports_externally_merged_pr_without_fetching_reviews() -> None:
    seen_requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if token_response := _installation_token_response(request):
            return token_response
        seen_requests.append((request.method, request.url.path))
        assert request.url.path == "/api/repos/qts/agentic-os/pulls/42"
        return _closed_pr_response(
            merged=True,
            merged_by={"login": "release-manager"},
            merge_commit_sha=MERGE_COMMIT_SHA,
        )

    client = _client(handler)

    response = client.wait_for_approval(ApprovalRequest(repo="qts/agentic-os", pr_number=42))

    # The lifecycle outcome supersedes any review verdict, so the reviews endpoint is
    # never consulted and the review-derived state stays PENDING.
    assert seen_requests == [("GET", "/api/repos/qts/agentic-os/pulls/42")]
    assert response.pr_merged is True
    assert response.pr_closed is False
    assert response.state == ApprovalState.PENDING
    assert response.merged_by == "release-manager"
    assert response.merge_commit_sha == MERGE_COMMIT_SHA
    assert response.current_head_sha == CURRENT_HEAD_SHA


def test_wait_for_approval_reports_closed_unmerged_pr() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if token_response := _installation_token_response(request):
            return token_response
        return _closed_pr_response(merged=False)

    client = _client(handler)

    response = client.wait_for_approval(ApprovalRequest(repo="qts/agentic-os", pr_number=42))

    assert response.pr_closed is True
    assert response.pr_merged is False
    assert response.state == ApprovalState.PENDING
    assert response.merged_by is None
    assert response.merge_commit_sha is None


@pytest.mark.parametrize(
    ("merged_by", "merge_commit_sha"),
    (
        (None, None),
        ({"login": 42}, "not-a-sha"),
        ("release-manager", 7),
    ),
)
def test_wait_for_approval_tolerates_missing_or_malformed_merge_attribution(
    merged_by: Any, merge_commit_sha: Any
) -> None:
    # Attribution is enrichment, never gating: malformed merged_by/merge_commit_sha
    # must degrade to an unattributed external merge, not fail the poll into the
    # ~3-day approval_timeout this outcome exists to prevent.
    def handler(request: httpx.Request) -> httpx.Response:
        if token_response := _installation_token_response(request):
            return token_response
        return _closed_pr_response(
            merged=True, merged_by=merged_by, merge_commit_sha=merge_commit_sha
        )

    client = _client(handler)

    response = client.wait_for_approval(ApprovalRequest(repo="qts/agentic-os", pr_number=42))

    assert response.pr_merged is True
    assert response.merged_by is None
    assert response.merge_commit_sha is None


def test_wait_for_approval_rejects_invalid_pull_request_state() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if token_response := _installation_token_response(request):
            return token_response
        return httpx.Response(
            200, json={"head": {"sha": CURRENT_HEAD_SHA}, "state": "banana", "merged": False}
        )

    client = _client(handler)

    with pytest.raises(RuntimeError, match="pull request state"):
        client.wait_for_approval(ApprovalRequest(repo="qts/agentic-os", pr_number=42))


def test_wait_for_approval_treats_stale_head_changes_request_as_pending() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if token_response := _installation_token_response(request):
            return token_response
        if request.url.path == "/api/repos/qts/agentic-os/pulls/42":
            return _pr_response(CURRENT_HEAD_SHA)
        return httpx.Response(
            200,
            json=[
                _review_response("CHANGES_REQUESTED", OLD_HEAD_SHA, "2026-06-09T10:00:00Z"),
            ],
        )

    client = _client(handler)

    response = client.wait_for_approval(ApprovalRequest(repo="qts/agentic-os", pr_number=42))

    assert response.state == ApprovalState.PENDING


def test_merge_pr_refuses_when_current_head_approval_is_dismissed() -> None:
    seen_requests: list[tuple[str, str, dict[str, Any] | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if token_response := _installation_token_response(request):
            return token_response
        body = json.loads(request.content) if request.content else None
        seen_requests.append((request.method, request.url.path, body))
        if request.url.path == "/api/repos/qts/agentic-os/pulls/42":
            return _pr_response(CURRENT_HEAD_SHA)
        if request.url.path == "/api/repos/qts/agentic-os/pulls/42/reviews":
            return httpx.Response(
                200,
                json=[
                    _review_response(
                        "APPROVED",
                        CURRENT_HEAD_SHA,
                        "2026-06-09T10:00:00Z",
                    ),
                    _review_response(
                        "DISMISSED",
                        CURRENT_HEAD_SHA,
                        "2026-06-09T10:01:00Z",
                    ),
                ],
            )
        assert request.url.path == "/api/repos/qts/agentic-os/pulls/42/merge"
        return httpx.Response(200, json={"merged": True, "sha": MERGE_COMMIT_SHA})

    client = _client(handler)

    with pytest.raises(RuntimeError, match="requires approval"):
        client.merge_pr(
            MergeRequest(
                repo="qts/agentic-os",
                pr_number=42,
                commit_title="Merge task 21",
                expected_head_sha=CURRENT_HEAD_SHA,
            )
        )

    assert seen_requests == [
        ("GET", "/api/repos/qts/agentic-os/pulls/42", None),
        ("GET", "/api/repos/qts/agentic-os/pulls/42/reviews", None),
    ]


def test_wait_for_approval_allows_later_approval_after_changes_requested() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if token_response := _installation_token_response(request):
            return token_response
        if request.url.path == "/api/repos/qts/agentic-os/pulls/42":
            return _pr_response(CURRENT_HEAD_SHA)
        # The SAME reviewer re-reviews: their later approval supersedes their own
        # changes-request. (A different reviewer's approval must not — see
        # test_wait_for_approval_keeps_changes_request_active_over_later_approval_by_other.)
        return httpx.Response(
            200,
            json=[
                _review_response(
                    "CHANGES_REQUESTED",
                    CURRENT_HEAD_SHA,
                    "2026-06-09T10:00:00Z",
                    login="lead-reviewer",
                ),
                _review_response(
                    "APPROVED",
                    CURRENT_HEAD_SHA,
                    "2026-06-09T10:01:00Z",
                    login="lead-reviewer",
                ),
            ],
        )

    client = _client(handler)

    response = client.wait_for_approval(ApprovalRequest(repo="qts/agentic-os", pr_number=42))

    assert response.state == ApprovalState.APPROVED
    assert response.approver == "lead-reviewer"
    assert response.current_head_sha == CURRENT_HEAD_SHA
    assert response.approved_head_sha == CURRENT_HEAD_SHA


def test_merge_pr_refuses_before_merge_endpoint_when_not_approved() -> None:
    seen_requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if token_response := _installation_token_response(request):
            return token_response
        seen_requests.append((request.method, request.url.path))
        if request.url.path == "/api/repos/qts/agentic-os/pulls/42":
            return _pr_response(CURRENT_HEAD_SHA)
        assert request.url.path == "/api/repos/qts/agentic-os/pulls/42/reviews"
        return httpx.Response(200, json=[])

    client = _client(handler)

    with pytest.raises(RuntimeError, match="requires approval") as error:
        client.merge_pr(
            MergeRequest(
                repo="qts/agentic-os",
                pr_number=42,
                commit_title="Merge task 21",
                expected_head_sha=CURRENT_HEAD_SHA,
            )
        )

    assert seen_requests == [
        ("GET", "/api/repos/qts/agentic-os/pulls/42"),
        ("GET", "/api/repos/qts/agentic-os/pulls/42/reviews"),
    ]
    assert error.value.__cause__ is None
    assert error.value.__suppress_context__ is True
    _assert_sanitized(error.value, (APP_ID, INSTALLATION_ID, PRIVATE_KEY_PEM, INSTALLATION_TOKEN))


def test_merge_pr_refuses_when_later_review_page_requests_changes() -> None:
    seen_requests: list[tuple[str, str, dict[str, Any] | None]] = []
    review_urls: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if token_response := _installation_token_response(request):
            return token_response
        body = json.loads(request.content) if request.content else None
        seen_requests.append((request.method, request.url.path, body))
        if request.url.path == "/api/repos/qts/agentic-os/pulls/42":
            return _pr_response(CURRENT_HEAD_SHA)
        if request.url.path == "/api/repos/qts/agentic-os/pulls/42/reviews":
            review_urls.append(request.url)
            if request.url.params.get("page") == "2":
                return httpx.Response(
                    200,
                    json=[
                        _review_response(
                            "CHANGES_REQUESTED",
                            CURRENT_HEAD_SHA,
                            "2026-06-09T10:01:00Z",
                        )
                    ],
                )
            return httpx.Response(
                200,
                headers={
                    "Link": (
                        "<https://github.test/api/repos/qts/agentic-os/pulls/42/reviews"
                        '?per_page=100&page=2>; rel="next"'
                    )
                },
                json=[
                    _review_response(
                        "APPROVED",
                        CURRENT_HEAD_SHA,
                        "2026-06-09T10:00:00Z",
                    )
                ],
            )
        assert request.url.path == "/api/repos/qts/agentic-os/pulls/42/merge"
        return httpx.Response(200, json={"merged": True})

    client = _client(handler)

    with pytest.raises(RuntimeError, match="requires approval"):
        client.merge_pr(
            MergeRequest(
                repo="qts/agentic-os",
                pr_number=42,
                commit_title="Merge task 21",
                expected_head_sha=CURRENT_HEAD_SHA,
            )
        )

    assert [request.path for request in review_urls] == [
        "/api/repos/qts/agentic-os/pulls/42/reviews",
        "/api/repos/qts/agentic-os/pulls/42/reviews",
    ]
    assert review_urls[0].params["per_page"] == "100"
    assert review_urls[1].params["per_page"] == "100"
    assert review_urls[1].params["page"] == "2"
    assert seen_requests == [
        ("GET", "/api/repos/qts/agentic-os/pulls/42", None),
        ("GET", "/api/repos/qts/agentic-os/pulls/42/reviews", None),
        ("GET", "/api/repos/qts/agentic-os/pulls/42/reviews", None),
    ]


def test_merge_pr_calls_merge_endpoint_after_approval_and_maps_success_response() -> None:
    seen_requests: list[tuple[str, str, dict[str, Any] | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if token_response := _installation_token_response(request):
            return token_response
        body = json.loads(request.content) if request.content else None
        seen_requests.append((request.method, request.url.path, body))
        if request.url.path == "/api/repos/qts/agentic-os/pulls/42":
            return _pr_response(CURRENT_HEAD_SHA)
        if request.url.path == "/api/repos/qts/agentic-os/pulls/42/reviews":
            return httpx.Response(
                200,
                json=[
                    _review_response(
                        "APPROVED",
                        CURRENT_HEAD_SHA,
                        "2026-06-09T10:00:00Z",
                    )
                ],
            )
        assert request.url.path == "/api/repos/qts/agentic-os/pulls/42/merge"
        return httpx.Response(200, json={"merged": True, "sha": MERGE_COMMIT_SHA})

    client = _client(handler)

    response = client.merge_pr(
        MergeRequest(
            repo="qts/agentic-os",
            pr_number=42,
            commit_title="Merge task 21",
            expected_head_sha=CURRENT_HEAD_SHA,
        )
    )

    assert response.repo == "qts/agentic-os"
    assert response.pr_number == 42
    assert response.commit_title == "Merge task 21"
    assert response.merged is True
    assert response.merge_commit_sha == MERGE_COMMIT_SHA
    assert seen_requests == [
        ("GET", "/api/repos/qts/agentic-os/pulls/42", None),
        ("GET", "/api/repos/qts/agentic-os/pulls/42/reviews", None),
        (
            "PUT",
            "/api/repos/qts/agentic-os/pulls/42/merge",
            {"commit_title": "Merge task 21", "sha": CURRENT_HEAD_SHA},
        ),
    ]


def test_merge_pr_rejects_stale_expected_head_before_merge_endpoint() -> None:
    seen_requests: list[tuple[str, str, dict[str, Any] | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if token_response := _installation_token_response(request):
            return token_response
        body = json.loads(request.content) if request.content else None
        seen_requests.append((request.method, request.url.path, body))
        if request.url.path == "/api/repos/qts/agentic-os/pulls/42":
            return _pr_response(CURRENT_HEAD_SHA)
        assert request.url.path == "/api/repos/qts/agentic-os/pulls/42/reviews"
        return httpx.Response(
            200,
            json=[
                _review_response(
                    "APPROVED",
                    CURRENT_HEAD_SHA,
                    "2026-06-09T10:00:00Z",
                )
            ],
        )

    client = _client(handler)

    with pytest.raises(RuntimeError, match="expected head SHA"):
        client.merge_pr(
            MergeRequest(
                repo="qts/agentic-os",
                pr_number=42,
                commit_title="Merge task 21",
                expected_head_sha=STALE_HEAD_SHA,
            )
        )

    assert seen_requests == [
        ("GET", "/api/repos/qts/agentic-os/pulls/42", None),
        ("GET", "/api/repos/qts/agentic-os/pulls/42/reviews", None),
    ]


def test_merge_pr_allows_later_paginated_approval_after_changes_requested() -> None:
    seen_requests: list[tuple[str, str, dict[str, Any] | None]] = []
    review_urls: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if token_response := _installation_token_response(request):
            return token_response
        body = json.loads(request.content) if request.content else None
        seen_requests.append((request.method, request.url.path, body))
        if request.url.path == "/api/repos/qts/agentic-os/pulls/42":
            return _pr_response(CURRENT_HEAD_SHA)
        if request.url.path == "/api/repos/qts/agentic-os/pulls/42/reviews":
            review_urls.append(request.url)
            if request.url.params.get("page") == "2":
                return httpx.Response(
                    200,
                    json=[
                        _review_response(
                            "APPROVED",
                            CURRENT_HEAD_SHA,
                            "2026-06-09T10:01:00Z",
                            login="lead-reviewer",
                        )
                    ],
                )
            return httpx.Response(
                200,
                headers={
                    "Link": (
                        "<https://github.test/api/repos/qts/agentic-os/pulls/42/reviews"
                        '?per_page=100&page=2>; rel="next"'
                    )
                },
                json=[
                    # Same reviewer as the page-2 approval: the later self-approval
                    # supersedes this changes-request across the page boundary.
                    _review_response(
                        "CHANGES_REQUESTED",
                        CURRENT_HEAD_SHA,
                        "2026-06-09T10:00:00Z",
                        login="lead-reviewer",
                    )
                ],
            )
        assert request.url.path == "/api/repos/qts/agentic-os/pulls/42/merge"
        return httpx.Response(200, json={"merged": True, "sha": MERGE_COMMIT_SHA})

    client = _client(handler)

    response = client.merge_pr(
        MergeRequest(
            repo="qts/agentic-os",
            pr_number=42,
            commit_title="Merge task 21",
            expected_head_sha=CURRENT_HEAD_SHA,
        )
    )

    assert response.merged is True
    assert response.merge_commit_sha == MERGE_COMMIT_SHA
    assert review_urls[0].params["per_page"] == "100"
    assert review_urls[1].params["per_page"] == "100"
    assert review_urls[1].params["page"] == "2"
    assert seen_requests == [
        ("GET", "/api/repos/qts/agentic-os/pulls/42", None),
        ("GET", "/api/repos/qts/agentic-os/pulls/42/reviews", None),
        ("GET", "/api/repos/qts/agentic-os/pulls/42/reviews", None),
        (
            "PUT",
            "/api/repos/qts/agentic-os/pulls/42/merge",
            {"commit_title": "Merge task 21", "sha": CURRENT_HEAD_SHA},
        ),
    ]


def test_merge_pr_conflict_is_sanitized_and_does_not_retry_against_newer_head() -> None:
    seen_requests: list[tuple[str, str, dict[str, Any] | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if token_response := _installation_token_response(request):
            return token_response
        body = json.loads(request.content) if request.content else None
        seen_requests.append((request.method, request.url.path, body))
        if request.url.path == "/api/repos/qts/agentic-os/pulls/42":
            head_sha = NEWER_HEAD_SHA if len(seen_requests) > 3 else CURRENT_HEAD_SHA
            return _pr_response(head_sha)
        if request.url.path == "/api/repos/qts/agentic-os/pulls/42/reviews":
            return httpx.Response(
                200,
                json=[
                    _review_response(
                        "APPROVED",
                        CURRENT_HEAD_SHA,
                        "2026-06-09T10:00:00Z",
                    )
                ],
            )
        return httpx.Response(409, text=f"raw-body {INSTALLATION_TOKEN}")

    client = _client(handler)

    with pytest.raises(RuntimeError) as error:
        client.merge_pr(
            MergeRequest(
                repo="qts/agentic-os",
                pr_number=42,
                commit_title="Merge task 21",
                expected_head_sha=CURRENT_HEAD_SHA,
            )
        )

    assert error.value.__cause__ is None
    assert error.value.__suppress_context__ is True
    _assert_sanitized(error.value, (APP_ID, INSTALLATION_ID, PRIVATE_KEY_PEM, INSTALLATION_TOKEN))
    assert seen_requests == [
        ("GET", "/api/repos/qts/agentic-os/pulls/42", None),
        ("GET", "/api/repos/qts/agentic-os/pulls/42/reviews", None),
        (
            "PUT",
            "/api/repos/qts/agentic-os/pulls/42/merge",
            {"commit_title": "Merge task 21", "sha": CURRENT_HEAD_SHA},
        ),
    ]


@pytest.mark.parametrize(
    "malformed_response_path",
    ("pull", "reviews_object", "review_item", "merge"),
)
def test_approval_and_merge_reject_malformed_responses_with_sanitized_errors(
    malformed_response_path: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if token_response := _installation_token_response(request):
            return token_response
        if request.url.path == "/api/repos/qts/agentic-os/pulls/42":
            if malformed_response_path == "pull":
                return httpx.Response(
                    200,
                    json={"head": {"ref": "branch"}, "raw": INSTALLATION_TOKEN},
                )
            return _pr_response(CURRENT_HEAD_SHA)
        if request.url.path == "/api/repos/qts/agentic-os/pulls/42/reviews":
            if malformed_response_path == "reviews_object":
                return httpx.Response(200, json={"raw": INSTALLATION_TOKEN})
            if malformed_response_path == "review_item":
                return httpx.Response(
                    200,
                    json=[{"state": "APPROVED", "commit_id": CURRENT_HEAD_SHA}],
                )
            return httpx.Response(
                200,
                json=[
                    _review_response(
                        "APPROVED",
                        CURRENT_HEAD_SHA,
                        "2026-06-09T10:00:00Z",
                    )
                ],
            )
        assert request.url.path == "/api/repos/qts/agentic-os/pulls/42/merge"
        return httpx.Response(200, json={"sha": MERGE_COMMIT_SHA, "raw": INSTALLATION_TOKEN})

    client = _client(handler)

    with pytest.raises(RuntimeError) as error:
        if malformed_response_path == "merge":
            client.merge_pr(
                MergeRequest(
                    repo="qts/agentic-os",
                    pr_number=42,
                    commit_title="Merge task 21",
                    expected_head_sha=CURRENT_HEAD_SHA,
                )
            )
        else:
            client.wait_for_approval(ApprovalRequest(repo="qts/agentic-os", pr_number=42))

    assert error.value.__cause__ is None
    assert error.value.__suppress_context__ is True
    _assert_sanitized(error.value, (APP_ID, INSTALLATION_ID, PRIVATE_KEY_PEM, INSTALLATION_TOKEN))


@pytest.mark.parametrize("pr_number", ("42?x=1", "42/extra", 0, -1, True))
@pytest.mark.parametrize("operation", ("wait_for_approval", "merge_pr"))
def test_approval_and_merge_reject_malformed_pr_number_before_http_request(
    pr_number: Any,
    operation: str,
) -> None:
    seen_requests: list[httpx.Request] = []
    client = _client_that_records_requests(seen_requests)

    with pytest.raises(RuntimeError) as error:
        if operation == "wait_for_approval":
            client.wait_for_approval(ApprovalRequest(repo="qts/agentic-os", pr_number=pr_number))
        else:
            client.merge_pr(
                MergeRequest(
                    repo="qts/agentic-os",
                    pr_number=pr_number,
                    commit_title="Merge task 21",
                    expected_head_sha=CURRENT_HEAD_SHA,
                )
            )

    assert seen_requests == []
    assert error.value.__cause__ is None
    assert error.value.__suppress_context__ is True
    _assert_sanitized(error.value, (APP_ID, INSTALLATION_ID, PRIVATE_KEY_PEM, INSTALLATION_TOKEN))
    _assert_sanitized(error.value, (str(pr_number),))


def test_post_comment_posts_issue_comment_body() -> None:
    seen_requests: list[tuple[str, str, dict[str, Any] | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/api/app/installations/{INSTALLATION_ID}/access_tokens":
            return httpx.Response(201, json=_installation_token_payload())
        body = json.loads(request.content) if request.content else None
        seen_requests.append((request.method, request.url.path, body))
        return httpx.Response(201, json={"id": 7, "body": "Ready for review."})

    client = _client(handler)

    response = client.post_comment(
        CommentRequest(repo="qts/agentic-os", pr_number=42, body="Ready for review.")
    )

    assert response.comment_number == 7
    assert response.body == "Ready for review."
    assert response.comments == ("Ready for review.",)
    assert seen_requests == [
        (
            "POST",
            "/api/repos/qts/agentic-os/issues/42/comments",
            {"body": "Ready for review."},
        )
    ]


@pytest.mark.parametrize("pr_number", ("42?x=1", "42/extra", 0, -1, True))
def test_post_comment_rejects_malformed_pr_number_before_http_request(
    pr_number: Any,
) -> None:
    seen_requests: list[httpx.Request] = []
    client = _client_that_records_requests(seen_requests)

    with pytest.raises(RuntimeError) as error:
        client.post_comment(
            CommentRequest(repo="qts/agentic-os", pr_number=pr_number, body="Ready for review.")
        )

    assert seen_requests == []
    assert error.value.__cause__ is None
    assert error.value.__suppress_context__ is True
    _assert_sanitized(error.value, (APP_ID, INSTALLATION_ID, PRIVATE_KEY_PEM, INSTALLATION_TOKEN))
    _assert_sanitized(error.value, (str(pr_number),))


@pytest.mark.parametrize(
    "response",
    (
        httpx.Response(500, text=f"raw-body {INSTALLATION_TOKEN}"),
        httpx.Response(
            201, json={"number": "not-an-int", "body": f"raw-body {INSTALLATION_TOKEN}"}
        ),
    ),
)
def test_lifecycle_failures_are_sanitized(response: httpx.Response) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/api/app/installations/{INSTALLATION_ID}/access_tokens":
            return httpx.Response(201, json=_installation_token_payload())
        if request.method == "GET":
            return httpx.Response(200, json=[])
        return response

    client = _client(handler)

    with pytest.raises(RuntimeError) as error:
        client.create_or_update_pr(
            PullRequestRequest(
                repo="qts/agentic-os",
                branch="ralph/task-21",
                base="main",
                title="Task 21",
                body="Lifecycle methods.",
            )
        )

    assert error.value.__cause__ is None
    assert error.value.__suppress_context__ is True
    _assert_sanitized(error.value, (APP_ID, INSTALLATION_ID, PRIVATE_KEY_PEM, INSTALLATION_TOKEN))


def _installation_token_response(request: httpx.Request) -> httpx.Response | None:
    if request.url.path.endswith("/access_tokens"):
        return httpx.Response(
            201, json={"token": INSTALLATION_TOKEN, "expires_at": "2099-01-01T00:00:00Z"}
        )
    return None


def test_mark_pr_ready_for_review_takes_the_draft_out_of_draft_over_graphql() -> None:
    # REST has no way to clear `draft` — PATCH silently ignores it — so the `pr.review`
    # seam is the one GitHub API that can: markPullRequestReadyForReview.
    seen: list[tuple[str, str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        token_response = _installation_token_response(request)
        if token_response is not None:
            return token_response
        body = json.loads(request.content) if request.content else None
        seen.append((request.method, request.url.path, body))
        if request.url.path.endswith("/pulls/42"):
            return httpx.Response(200, json={"draft": True, "node_id": "PR_node_42"})
        return httpx.Response(200, json={"data": {"markPullRequestReadyForReview": {}}})

    response = _client(handler).mark_pr_ready_for_review(
        PullRequestReadyRequest(repo="qts/agentic-os", pr_number=42)
    )

    assert (response.ready, response.marked_ready) == (True, True)
    assert seen[0][:2] == ("GET", "/api/repos/qts/agentic-os/pulls/42")
    method, path, body = seen[1]
    assert (method, path) == ("POST", "/api/graphql")
    assert body is not None
    assert body["variables"] == {"pullRequestId": "PR_node_42"}


def test_mark_pr_ready_for_review_is_a_no_op_on_a_pr_already_out_of_draft() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        token_response = _installation_token_response(request)
        if token_response is not None:
            return token_response
        assert request.url.path.endswith("/pulls/42"), "no mutation for a non-draft PR"
        return httpx.Response(200, json={"draft": False, "node_id": "PR_node_42"})

    response = _client(handler).mark_pr_ready_for_review(
        PullRequestReadyRequest(repo="qts/agentic-os", pr_number=42)
    )

    assert (response.ready, response.marked_ready) == (True, False)


def test_mark_pr_ready_for_review_raises_on_a_graphql_error_inside_a_200() -> None:
    # GraphQL reports failures in the body, so a 200 alone proves nothing: without this
    # the gate would wait forever on a PR still in draft.
    def handler(request: httpx.Request) -> httpx.Response:
        token_response = _installation_token_response(request)
        if token_response is not None:
            return token_response
        if request.url.path.endswith("/pulls/42"):
            return httpx.Response(200, json={"draft": True, "node_id": "PR_node_42"})
        return httpx.Response(200, json={"errors": [{"message": "Resource not accessible"}]})

    with pytest.raises(GitHubAppAuthenticationError):
        _client(handler).mark_pr_ready_for_review(
            PullRequestReadyRequest(repo="qts/agentic-os", pr_number=42)
        )


def test_submit_review_posts_the_verdict_pinned_to_the_head_it_names() -> None:
    # The `pr.comment` seam's one GitHub call (PRD issue 54): a review, never an approval.
    seen: list[tuple[str, str, dict[str, Any] | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if token_response := _installation_token_response(request):
            return token_response
        seen.append(
            (
                request.method,
                request.url.path,
                json.loads(request.content) if request.content else None,
            )
        )
        return httpx.Response(
            200,
            json={
                "id": 77,
                "state": "CHANGES_REQUESTED",
                "user": {"login": "agentic-os[bot]", "type": "Bot"},
            },
        )

    response = _client(handler).submit_review(
        PullRequestReviewRequest(
            repo="qts/agentic-os",
            pr_number=42,
            commit_id=CURRENT_HEAD_SHA,
            event=ReviewEvent.REQUEST_CHANGES,
            body="findings",
        )
    )

    assert (response.review_id, response.state, response.reviewer_login) == (
        77,
        "CHANGES_REQUESTED",
        "agentic-os[bot]",
    )
    assert seen == [
        (
            "POST",
            "/api/repos/qts/agentic-os/pulls/42/reviews",
            {"commit_id": CURRENT_HEAD_SHA, "event": "REQUEST_CHANGES", "body": "findings"},
        )
    ]


def test_the_critics_changes_request_is_not_the_orgs_denial() -> None:
    # The Critic's `block` arrives as the App's REQUEST_CHANGES (PRD issue 54). It is the
    # veto the workflow holds, so it must not end the Work Record as a reviewer denial.
    def handler(request: httpx.Request) -> httpx.Response:
        if token_response := _installation_token_response(request):
            return token_response
        if request.url.path == "/api/repos/qts/agentic-os/pulls/42":
            return _pr_response(CURRENT_HEAD_SHA)
        return httpx.Response(
            200,
            json=[
                _review_response(
                    "CHANGES_REQUESTED",
                    CURRENT_HEAD_SHA,
                    "2026-06-09T10:00:00Z",
                    login="agentic-os[bot]",
                    user_type="Bot",
                ),
            ],
        )

    response = _client(handler).wait_for_approval(
        ApprovalRequest(repo="qts/agentic-os", pr_number=42)
    )

    assert response.state == ApprovalState.PENDING
    assert response.approving_reviewers == ()


def _paged_files_handler(
    *, pages: int, page_size: int, changed_files: int
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        token_response = _installation_token_response(request)
        if token_response is not None:
            return token_response
        if request.url.path == "/api/repos/qts/agentic-os/pulls/7":
            return httpx.Response(200, json={"changed_files": changed_files})
        assert request.url.path == "/api/repos/qts/agentic-os/pulls/7/files"
        page = int(request.url.params.get("page", "1"))
        files = [{"filename": f"f{page}-{index}.py"} for index in range(page_size)]
        if page == pages:
            return httpx.Response(200, json=files)
        next_page = f"https://github.test/api/repos/qts/agentic-os/pulls/7/files?page={page + 1}"
        return httpx.Response(200, json=files, headers={"Link": f'<{next_page}>; rel="next"'})

    return handler


def test_list_changed_files_returns_every_page_when_the_count_matches() -> None:
    listing = _client(
        _paged_files_handler(pages=2, page_size=2, changed_files=4)
    ).list_changed_files(PullRequestFilesRequest(repo="qts/agentic-os", pr_number=7))

    assert listing.changed_paths == ("f1-0.py", "f1-1.py", "f2-0.py", "f2-1.py")


def test_list_changed_files_refuses_a_listing_truncated_at_the_github_cap() -> None:
    """`/files` stops at 3000 entries without an error; a Protected Path past the cap
    must not read as unchanged (QTS-1262)."""

    client = _client(_paged_files_handler(pages=30, page_size=100, changed_files=3001))

    with pytest.raises(GitHubAppAuthenticationError, match="incomplete"):
        client.list_changed_files(PullRequestFilesRequest(repo="qts/agentic-os", pr_number=7))


def test_read_repository_proves_the_installation_can_read_it() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if token_response := _installation_token_response(request):
            return token_response
        seen.append(f"{request.method} {request.url.path}")
        assert request.headers["Authorization"] == f"Bearer {INSTALLATION_TOKEN}"
        return httpx.Response(200, json={"full_name": "acme/widgets", "default_branch": "trunk"})

    response = _client(handler).read_repository(RepositoryReadRequest(repo="acme/widgets"))

    assert response == RepositoryReadResponse(repo="acme/widgets", default_branch="trunk")
    assert seen == ["GET /api/repos/acme/widgets"]


def test_read_repository_outside_the_installation_raises_sanitized() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if token_response := _installation_token_response(request):
            return token_response
        return httpx.Response(404, text="raw-body Not Found")

    with pytest.raises(GitHubAppAuthenticationError, match="status 404") as error:
        _client(handler).read_repository(RepositoryReadRequest(repo="acme/widgets"))

    _assert_sanitized(error.value, [INSTALLATION_TOKEN, PRIVATE_KEY_PEM])
