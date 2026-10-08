"""The conformance kit's fake control plane holds the Runner to the platform's surface.

Driven through the Runner's own clients, so what passes here is what the Runner sends:

* anything outside the ``/api/runner/v1`` prefix is refused, so a Runner that reaches
  for a platform route fails against the fake before it fails in production;
* a signed request whose signature does not verify is refused, as the platform does;
* a Directive token pulled over the signed stream is the credential an Evidence write
  bears, and before one exists the write is signed instead;
* a revoked Runner's signed requests are refused ``runner_revoked``;
* served over HTTP it answers the same, and ``/stats`` reports what it saw;
* a delivered value reaches each Runner sealed to its own Recipient Key, the runtime
  context is answered on a Directive token only, and ``/v1`` records the credential a
  provider call presented (local-agents 04b).
"""

from __future__ import annotations

import json
import urllib.request
from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio

from agentic_runner.registration import (
    RUNNER_REVOKED_REASON,
    RunnerRegistrationClient,
    RunnerRegistrationError,
    RunnerState,
)
from agentic_runner.sealed_box import RecipientKeyPair, generate_recipient_key, open_sealed
from agentic_runner.testing import FakeControlPlane
from agentic_runner.workers.fastapi_client import (
    RunnerFastApiClient,
    WorkerFastApiClientError,
    directive_token,
)
from agentic_runner_contracts.public_metadata import runner_task_queue
from agentic_runner_contracts.runner_registration import (
    EgressPosture,
    HeartbeatEnvelope,
    IsolationMode,
    RecipientKey,
    ResourcePressure,
)
from agentic_runner_contracts.sealed_credential import delivery_binding

CONTROL_PLANE = "http://control-plane.test"


@pytest_asyncio.fixture
async def http(plane: FakeControlPlane) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=plane.transport()) as client:
        yield client


@pytest.fixture
def plane() -> FakeControlPlane:
    return FakeControlPlane()


async def _register(
    http: httpx.AsyncClient,
    base_url: str = CONTROL_PLANE,
    key: RecipientKeyPair | None = None,
) -> RunnerState:
    key = key or generate_recipient_key()
    state, _ = await RunnerRegistrationClient(base_url=base_url, client=http).bootstrap(
        agent_token="agent-token-of-sixteen-plus-chars",
        tags={},
        isolation_mode=IsolationMode.NONE,
        recipient_key=RecipientKey(key_id=key.key_id, public_key=key.public_key),
        can_separate_uids=False,
    )
    return state


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/internal/work-records/wr-1/evidence"),
        ("GET", "/api/admin/runners"),
        ("POST", "/runners/heartbeat"),
        ("GET", "/healthz"),
    ],
)
async def test_a_request_outside_the_runner_prefix_is_refused(
    plane: FakeControlPlane, http: httpx.AsyncClient, method: str, path: str
) -> None:
    response = await http.request(method, f"{CONTROL_PLANE}{path}", json={})

    assert response.status_code == 404
    assert response.json()["detail"]["reason"] == "outside_runner_prefix"
    [refusal] = plane.refusals
    assert (refusal.path, refusal.reason) == (path, "outside_runner_prefix")


@pytest.mark.asyncio
async def test_a_signature_that_does_not_verify_is_refused(
    plane: FakeControlPlane, http: httpx.AsyncClient
) -> None:
    state = await _register(http)
    body = b'{"directive_id": "wr-1:1"}'
    headers = state.signed_headers(
        "POST", f"{CONTROL_PLANE}/api/runner/v1/runners/directive-token", body
    )

    response = await http.post(
        f"{CONTROL_PLANE}/api/runner/v1/runners/directive-token",
        content=b'{"directive_id": "wr-2:1"}',
        headers=headers,
    )

    assert response.status_code == 401
    assert response.json()["detail"]["reason"] == "signature_invalid"
    assert plane.directive_tokens == []


@pytest.mark.asyncio
async def test_evidence_bears_the_pulled_directive_token_or_else_the_signature(
    plane: FakeControlPlane, http: httpx.AsyncClient
) -> None:
    state = await _register(http)
    client = RunnerRegistrationClient(base_url=CONTROL_PLANE, client=http)
    token = await client.request_directive_token(state, "wr-1:1")
    fastapi = RunnerFastApiClient(CONTROL_PLANE, http_client=http, state=state)

    signed = await fastapi.append_evidence("wr-1", source="runner.test", payload={"n": 1})
    scope = directive_token.set(token.token)
    try:
        await fastapi.append_evidence("wr-1", source="runner.test", payload={"n": 2})
    finally:
        directive_token.reset(scope)

    assert plane.directive_tokens == ["wr-1:1"]
    assert [(e.work_record_id, e.payload, e.credential) for e in plane.evidence] == [
        ("wr-1", {"n": 1}, "signed"),
        ("wr-1", {"n": 2}, "directive"),
    ]
    assert signed["sequence"] == 1


@pytest.mark.asyncio
async def test_a_revoked_runner_is_refused(
    plane: FakeControlPlane, http: httpx.AsyncClient
) -> None:
    state = await _register(http)
    plane.revoke(state.runner_id)

    with pytest.raises(RunnerRegistrationError) as refused:
        await RunnerRegistrationClient(base_url=CONTROL_PLANE, client=http).request_directive_token(
            state, "wr-1:1"
        )

    assert refused.value.reason == RUNNER_REVOKED_REASON


@pytest.mark.asyncio
async def test_served_over_http_it_answers_the_same_and_reports_on_stats(
    plane: FakeControlPlane,
) -> None:
    with plane.serving() as url:
        async with httpx.AsyncClient() as http:
            state = await _register(http, base_url=url)
            await RunnerRegistrationClient(base_url=url, client=http).request_directive_token(
                state, "wr-1:1"
            )
        with urllib.request.urlopen(f"{url}/stats") as response:
            stats = json.loads(response.read())

    assert stats["bootstraps"] == 1
    assert stats["runner_ids"] == [str(state.runner_id)]
    assert stats["isolation_modes"] == ["none"]
    assert stats["directive_tokens"] == 1
    assert stats["refusals"] == []


def _envelope(state: RunnerState) -> HeartbeatEnvelope:
    return HeartbeatEnvelope(
        runner_version="1.0.0",
        contracts_version="1.0.0",
        resource_pressure=ResourcePressure(cpu_percent=0, memory_percent=0, disk_percent=0),
        max_concurrent_directives=1,
        current_load=0,
        egress_posture=EgressPosture.UNENFORCED,
        isolation_mode=IsolationMode.NONE,
        tag_set_version=1,
        hosted_task_queue=runner_task_queue(state.runner_id),
    )


@pytest.mark.asyncio
async def test_a_delivered_value_reaches_each_runner_sealed_to_its_own_recipient_key(
    plane: FakeControlPlane, http: httpx.AsyncClient
) -> None:
    plane.deliver("OPENAI_API_KEY", "sk-funder")
    key = generate_recipient_key()
    state = await _register(http, key=key)
    client = RunnerRegistrationClient(base_url=CONTROL_PLANE, client=http)

    first = await client.heartbeat(state, _envelope(state))
    second = await client.heartbeat(state, _envelope(state))

    [sealed] = first.sealed_credentials
    assert (sealed.contract_id, sealed.slot, sealed.version) == (
        plane.contract_id,
        "OPENAI_API_KEY",
        1,
    )
    assert second.sealed_credentials == first.sealed_credentials
    binding = delivery_binding(
        contract_id=sealed.contract_id, slot=sealed.slot, recipient_key_id=key.key_id
    )
    assert (
        open_sealed(private_key=key.private_key, binding=binding, ciphertext=sealed.ciphertext)
        == "sk-funder"
    )


@pytest.mark.asyncio
async def test_the_runtime_context_is_answered_on_a_directive_token_only(
    plane: FakeControlPlane, http: httpx.AsyncClient
) -> None:
    state = await _register(http)
    token = await RunnerRegistrationClient(
        base_url=CONTROL_PLANE, client=http
    ).request_directive_token(state, "wr-1:1")
    fastapi = RunnerFastApiClient(CONTROL_PLANE, http_client=http, state=state)

    with pytest.raises(WorkerFastApiClientError):
        await fastapi.get_runtime_context("wr-1")
    scope = directive_token.set(token.token)
    try:
        context = await fastapi.get_runtime_context("wr-1")
    finally:
        directive_token.reset(scope)

    assert (context["work_record_id"], context["contract_id"], context["cli_kind"]) == (
        "wr-1",
        str(plane.contract_id),
        "codex_cli",
    )


@pytest.mark.asyncio
async def test_the_provider_records_the_credential_each_call_presented(
    plane: FakeControlPlane, http: httpx.AsyncClient
) -> None:
    models = await http.get(
        f"{CONTROL_PLANE}/v1/models", headers={"Authorization": "Bearer sk-funder"}
    )
    responses = await http.post(f"{CONTROL_PLANE}/v1/responses", json={})

    assert models.status_code == 200
    assert responses.headers["content-type"] == "text/event-stream"
    assert b"response.completed" in responses.content
    assert plane.stats()["provider_calls"] == [
        {"path": "/v1/models", "authorization": "Bearer sk-funder"},
        {"path": "/v1/responses", "authorization": ""},
    ]
