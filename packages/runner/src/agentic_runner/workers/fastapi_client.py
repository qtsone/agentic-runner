from __future__ import annotations

import json
from collections.abc import Mapping
from contextvars import ContextVar
from types import TracebackType
from typing import Any, Protocol, Self, cast
from urllib.parse import quote

import httpx

from agentic_runner.registration import RunnerState

# The Directive token for the activity execution under way (PRD issue 63, 25 §11).
# Task-local by construction: Temporal runs each activity execution in its own asyncio
# task, so a value set inside one is discarded with it -- never an attribute on the
# client or the activities object, never a cache keyed by Work Record (ADR-0013 §3),
# and never in an activity payload. The Runner package reads no ``INTERNAL_SERVICE_TOKEN``
# and sends no ``X-Internal-Service-Token``: that shared secret is the platform worker's
# (the platform's own ``workers.fastapi_client``), and a client-hosted Runner never holds it.
directive_token: ContextVar[str | None] = ContextVar("directive_token", default=None)


class DirectiveTokenSource(Protocol):
    """One pull of a Directive's control-plane token over the Runner's signed stream."""

    async def directive_token(self, directive_id: str) -> str: ...


_INVALID_WORK_RECORD_TRANSITION_CODE = "invalid_work_record_transition"


class WorkerFastApiClientError(RuntimeError):
    """Raised when the worker cannot complete an internal FastAPI request."""

    def __init__(
        self,
        message: str,
        *,
        method: str | None = None,
        path: str | None = None,
        status_code: int | None = None,
        detail: object | None = None,
    ) -> None:
        super().__init__(message)
        self.method = method
        self.path = path
        self.status_code = status_code
        self.detail = detail


class WorkerFastApiClientRejectionError(WorkerFastApiClientError):
    """Deterministic backend rejection: a 4xx (other than 408/429) that a retry
    replays verbatim. RalphWorkflow lists this type in its activity retry policy's
    non_retryable_error_types so a poisoned request fails the activity immediately
    instead of re-running its full Directive on every retry (work record f6223d2a:
    38 overnight execute_fix_directive attempts against a permanent 422)."""


# 408/429 are the transient members of the 4xx family (server-side request timeout
# and rate limiting); every other 4xx is deterministic for an identical request.
_TRANSIENT_4XX_STATUS_CODES = frozenset({408, 429})


def _error_class_for_status(status_code: int) -> type[WorkerFastApiClientError]:
    if 400 <= status_code < 500 and status_code not in _TRANSIENT_4XX_STATUS_CODES:
        return WorkerFastApiClientRejectionError
    return WorkerFastApiClientError


class RunnerFastApiClient:
    """What a Runner activity reads from, and reports to, the control plane at run time.

    The Runner's half of the split ADR-0013 §2 asks for: it never calls the control plane
    for *bookkeeping* — its activities do the thing and return facts, and the workflow
    records them through platform activities (``PlatformRalphActivities``). What is left
    here is what an activity genuinely cannot do without: the Work Record's runtime
    context, the Directive's metered usage, Questions and Owner Confirmations, and the
    Evidence its verb seams write. Every path is on the public Runner surface; the
    authoritative list of the routes a Runner calls is this class plus
    ``registration.py``'s constants (tests/integration/test_runner_api_surface.py).

    Every call carries the credential for *that* call (PRD issue 63). Anything about a
    Work Record -- the runtime context, the Evidence a verb seam writes
    (``append_evidence``, ``transition_work_record``, ``record_verifier_result``),
    Questions, Owner Confirmations, usage -- bears the Directive token the activity
    pulled for its execution (``directive_token``). Anything about the Runner itself --
    the expired-workspace listing, the Evidence a routing refusal or a refused token pull
    writes before a token exists -- is signed with the Runner identity exactly as the
    heartbeat is (``state``). Nothing new may be added beside the three Evidence writes.
    """

    # The public Runner surface (issue 85, ADR-0016): every route here is served by
    # api.agentic.<zone> and nothing else is. The platform-hosted subclass overrides it.
    _prefix = "/api/runner/v1"

    def __init__(
        self,
        base_url: str,
        timeout_seconds: float = 30.0,
        http_client: httpx.AsyncClient | None = None,
        state: RunnerState | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._owns_http_client = http_client is None
        self._http_client = http_client or httpx.AsyncClient(timeout=timeout_seconds)
        self._state = state

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_http_client:
            await self._http_client.aclose()

    async def get_runtime_context(
        self, work_record_id: str, agent_id: str | None = None
    ) -> dict[str, Any]:
        path = f"{self._prefix}/work-records/{_path_segment(work_record_id)}/runtime-context"
        if agent_id:
            path = f"{path}?agent_id={_path_segment(agent_id)}"
        return await self._get(path)

    async def raise_owner_confirmation(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Ask the Agent's owner before a ``confirm`` verb acts (ADR-0011 §14-16).

        The control plane consults the Scoped Allowance first, so the answer is either
        "granted, proceed" or the raised request plus the window the workflow waits out.
        """

        return await self._post(f"{self._prefix}/owner-confirmations", payload)

    async def raise_question(
        self, work_record_id: str, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        """The `work.ask` seam (PRD issue 60): the control plane evaluates it on the Agent
        link, records the Question it allows and raises the owner's ask on `confirm`."""

        return await self._post(
            f"{self._prefix}/work-records/{_path_segment(work_record_id)}/questions", payload
        )

    async def get_question(self, question_id: str) -> dict[str, Any]:
        """The Question and its answer, for the Directive the hold wakes (PRD issue 60)."""

        return await self._get(f"{self._prefix}/questions/{_path_segment(question_id)}")

    async def get_directive_usage(self, work_record_id: str, directive_id: str) -> dict[str, Any]:
        """Read one Directive's metered token aggregate (PRD issue 13).

        What the activity puts in the Directive's output, so the deterministic loop
        decrements the token Budget from an activity result and never from the ledger.
        """

        return await self._get(
            f"{self._prefix}/usage/work-records/{_path_segment(work_record_id)}"
            f"/directives/{_path_segment(directive_id)}"
        )

    async def report_harness_usage(self, payload: Mapping[str, Any]) -> list[dict[str, Any]]:
        """One Usage Record per model, source ``"harness"`` (PRD issue 31, 17 A9): a
        device-login or setup-token Directive bypasses the proxy, so this is the worker's
        only way to meter it. ``payload`` is ``HarnessUsageReport``'s shape.
        """

        result = await self._post(f"{self._prefix}/usage/harness", payload)
        records = result.get("records")
        return records if isinstance(records, list) else []

    async def append_evidence(
        self,
        work_record_id: str,
        *,
        source: str,
        payload: Mapping[str, Any],
        actor: str = "temporal-worker",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        request_payload: dict[str, Any] = {
            "actor": actor,
            "source": source,
            "payload": dict(payload),
        }
        if idempotency_key is not None:
            request_payload["idempotency_key"] = idempotency_key
        return await self._post(
            f"{self._prefix}/work-records/{_path_segment(work_record_id)}/evidence",
            request_payload,
        )

    async def transition_work_record(
        self,
        work_record_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        return await self._post(
            f"{self._prefix}/work-records/{_path_segment(work_record_id)}/transition",
            payload,
        )

    async def record_verifier_result(
        self,
        work_record_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        return await self._post(
            f"{self._prefix}/work-records/{_path_segment(work_record_id)}/verifier-result",
            payload,
        )

    async def list_expired_workspaces(self, work_record_ids: list[str]) -> list[str]:
        """Which of these Work Records' Workspaces are past the retention period (issue 30).

        The Runner holds directories; whether a Work Record is terminal and how long ago
        it ended is a control-plane fact. The ids go up, the expired subset comes back —
        the delete itself stays on the Runner, where the files are.
        """

        payload = await self._post(
            f"{self._prefix}/workspaces/expired", {"work_record_ids": work_record_ids}
        )
        expired = payload.get("expired")
        return [str(entry) for entry in expired] if isinstance(expired, list) else []

    def _credential_headers(self, method: str, url: str, body: bytes) -> dict[str, str]:
        """The Directive token when one is in scope, else the signed Runner envelope."""

        token = directive_token.get()
        if token is not None:
            return {"Authorization": f"Bearer {token}"}
        if self._state is None:
            return {}
        return self._state.signed_headers(method, url, body)

    async def _get(self, path: str) -> dict[str, Any]:
        try:
            url = f"{self._base_url}{path}"
            response = await self._http_client.get(
                url, headers=self._credential_headers("GET", url, b"")
            )
        except httpx.HTTPError:
            raise WorkerFastApiClientError(
                f"FastAPI request failed: GET {path} could not complete",
                method="GET",
                path=path,
            ) from None

        if not 200 <= response.status_code < 300:
            raise _error_class_for_status(response.status_code)(
                f"FastAPI request failed: GET {path} returned {response.status_code}",
                method="GET",
                path=path,
                status_code=response.status_code,
                detail=_response_error_detail(response),
            ) from None

        try:
            response_payload = response.json()
        except ValueError:
            raise WorkerFastApiClientError(
                f"FastAPI request failed: GET {path} returned invalid JSON"
            ) from None

        if not isinstance(response_payload, dict):
            raise WorkerFastApiClientError(
                f"FastAPI request failed: GET {path} returned invalid JSON object"
            ) from None

        return cast(dict[str, Any], response_payload)

    async def _post(
        self,
        path: str,
        payload: Mapping[str, Any],
        *,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        # Serialised once, here: the Runner signature is over these exact bytes.
        body = json.dumps(dict(payload)).encode("utf-8")
        url = f"{self._base_url}{path}"
        try:
            response = await self._http_client.post(
                url,
                content=body,
                headers={
                    "content-type": "application/json",
                    **self._credential_headers("POST", url, body),
                    **(headers or {}),
                },
            )
        except httpx.HTTPError:
            raise WorkerFastApiClientError(
                f"FastAPI request failed: POST {path} could not complete",
                method="POST",
                path=path,
            ) from None

        if not 200 <= response.status_code < 300:
            raise _error_class_for_status(response.status_code)(
                f"FastAPI request failed: POST {path} returned {response.status_code}",
                method="POST",
                path=path,
                status_code=response.status_code,
                detail=_response_error_detail(response),
            ) from None

        try:
            response_payload = response.json()
        except ValueError:
            raise WorkerFastApiClientError(
                f"FastAPI request failed: POST {path} returned invalid JSON"
            ) from None

        if not isinstance(response_payload, dict):
            raise WorkerFastApiClientError(
                f"FastAPI request failed: POST {path} returned invalid JSON object"
            ) from None

        return cast(dict[str, Any], response_payload)


def _path_segment(value: str) -> str:
    return quote(value, safe="")


def _response_error_detail(response: httpx.Response) -> object | None:
    try:
        payload = response.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    return _sanitize_error_detail(payload.get("detail"))


def _sanitize_error_detail(detail: object) -> object | None:
    if isinstance(detail, list):
        return [_sanitize_error_detail_item(item) for item in detail]
    if isinstance(detail, dict):
        invalid_transition_detail = _sanitize_invalid_transition_detail(detail)
        if invalid_transition_detail is not None:
            return invalid_transition_detail
        return _sanitize_error_detail_item(detail)
    if isinstance(detail, str):
        return "<redacted>"
    if isinstance(detail, int | float | bool) or detail is None:
        return detail
    return "<redacted>"


def _sanitize_invalid_transition_detail(detail: dict[object, object]) -> dict[str, object] | None:
    if detail.get("code") != _INVALID_WORK_RECORD_TRANSITION_CODE:
        return None

    from_state = detail.get("from_state")
    to_state = detail.get("to_state")
    allowed_states = detail.get("allowed_states")
    if not isinstance(from_state, str):
        return None
    if not isinstance(to_state, str):
        return None
    if not isinstance(allowed_states, list):
        return None
    if not all(isinstance(state, str) for state in allowed_states):
        return None

    return {
        "code": _INVALID_WORK_RECORD_TRANSITION_CODE,
        "from_state": from_state,
        "to_state": to_state,
        "allowed_states": allowed_states,
    }


def _sanitize_error_detail_item(item: object) -> object:
    if not isinstance(item, dict):
        return "<redacted>"

    sanitized: dict[str, object] = {}
    for key in ("type", "loc", "msg"):
        value = item.get(key)
        if isinstance(value, str | int | float | bool) or value is None:
            sanitized[key] = value
        elif isinstance(value, list | tuple):
            sanitized[key] = [str(part) for part in value]
        else:
            sanitized[key] = "<redacted>"
    return sanitized
