"""The fake control plane: the Runner's side of the platform, answered the way it answers.

One oracle for every test that needs a control plane (runner-repo issue 07): the unit
tests mount it as an ``httpx.MockTransport``, the chart, Docker and workstation tests
serve it over HTTP from the Runner's own install (``python -m agentic_runner.testing``),
and the conformance scenarios do both. Standard library plus the Runner's own
dependencies, so it runs anywhere a Runner does.

It serves the public Runner surface only -- the ``/api/runner/v1`` prefix of issue 85
(ADR-0016) -- and refuses anything else, so a Runner build that reaches for a platform
route outside that surface fails here before it fails in production. What it checks is
what the platform checks on that surface: the bootstrap body, the Ed25519 signature of
every signed request against the identity it handed out, revocation, and the hosted
queue. What it answers is the same shape the platform answers, refusals included
(``{"detail": {"reason", "detail"}}``). Everything it saw is kept for the test to read,
and ``GET /stats`` -- the one route outside the prefix, and the fake's own -- summarises
it for a shell script.

Local-agents 04b adds what one Directive on a shared Runner needs: :meth:`deliver` seals a
funder's value to every Runner's Recipient Key and pushes it on each ack, the runtime
context and usage routes answer for one Contract, and ``/v1`` -- also outside the prefix
-- is the LLM provider a Runner is pointed at, recording the credential each call
presented, so a test sees the proxy spent the delivered key and not the attempt bearer.

Contracts 3.25 (Spec: Runners rev 3): every ack names the one Contract this fake serves in
``llm_contracts``, with ``subscription`` as its right, so a Runner reports the LLM access it
holds for it; :attr:`FakeControlPlane.llm_access` keeps each report. ``subscription``
``None`` answers as a control plane below 3.25 does: no Contract terms at all.

Contracts 3.26: :attr:`FakeControlPlane.pool` is the ack's pool and its current Pool Key;
None, the default, answers for a personal Runner or a control plane below 3.26.

Contracts 3.31 (Spec: Runners rev 14): every ack names the Runner's connections. A Runner a
Runner Registration Token registered has one, to :attr:`FakeControlPlane.namespace`'s
Organisation, carrying the ack's own Runner Token. A Runner ``login`` made is the
:attr:`FakeControlPlane.operator`'s, connected nowhere until ``connect`` asks for one of
:attr:`FakeControlPlane.organisations`; each connection names one Contract in the ack's
``llm_contracts``. The device flow waits for :meth:`FakeControlPlane.confirm_login`, or
confirms at the first poll with ``confirm_logins`` (the served mode, which a shell script
cannot confirm). ``connections_on_ack=False`` answers as a control plane below 3.31.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from uuid import UUID, uuid4

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from pydantic import BaseModel, ValidationError

from agentic_runner.registration import (
    BOOTSTRAP_PATH,
    DIRECTIVE_TOKEN_PATH,
    HEARTBEAT_PATH,
    RUNNER_REVOKED_REASON,
)
from agentic_runner.sealed_box import seal
from agentic_runner_contracts import __version__ as contracts_version
from agentic_runner_contracts.public_metadata import namespace as organisation_namespace
from agentic_runner_contracts.public_metadata import runner_task_queue
from agentic_runner_contracts.runner_login import (
    AUTHORIZATION_PENDING,
    CONNECTIONS_PATH,
    EXPIRED_TOKEN,
    LOGIN_DEVICE_PATH,
    LOGIN_TOKEN_PATH,
    ORGANISATIONS_PATH,
    RUNNER_LIMIT_REASON,
    ConnectableOrganisation,
    ConnectableOrganisations,
    ConnectRequest,
    DeviceAuthorization,
    DeviceLoginRequest,
    DeviceTokenRequest,
    LoginResponse,
)
from agentic_runner_contracts.runner_registration import (
    BootstrapRequest,
    BootstrapResponse,
    ContractLlmTerms,
    DirectiveTokenRequest,
    DirectiveTokenResponse,
    FloorState,
    HeartbeatAck,
    HeartbeatEnvelope,
    PoolTerms,
    RunnerConnection,
    RunnerIdentityMaterial,
    SubscriptionRight,
)
from agentic_runner_contracts.runner_signature import (
    RUNNER_ID_HEADER,
    SIGNATURE_HEADER,
    SIGNED_AT_HEADER,
    canonical_request,
)
from agentic_runner_contracts.runtime_context import (
    ProductVerifierCommandSource,
    WorkerRuntimeContext,
)
from agentic_runner_contracts.sealed_credential import SealedCredential, delivery_binding

__all__ = [
    "DEFAULT_AGENT_ID",
    "DEFAULT_OPERATOR",
    "DEFAULT_CONTRACT_ID",
    "DEFAULT_NAMESPACE",
    "RUNNER_PREFIX",
    "Evidence",
    "FakeControlPlane",
    "ProviderCall",
    "Refusal",
]

RUNNER_PREFIX = "/api/runner/v1"
STATS_PATH = "/stats"
PROVIDER_PREFIX = "/v1"
DEFAULT_CONTRACT_ID = UUID("00000000-0000-4000-8000-0000000000c1")
DEFAULT_AGENT_ID = UUID("00000000-0000-4000-8000-0000000000a1")
# The namespace the chart, Docker and workstation tests start their Temporal dev server
# with: one Organisation, whose id is the first non-nil UUID.
DEFAULT_NAMESPACE = "org-00000000-0000-0000-0000-000000000001"
# The person `login` signs in as, and the Organisations they belong to.
DEFAULT_OPERATOR = UUID("00000000-0000-4000-8000-0000000000e1")
DEFAULT_ORGANISATIONS: Mapping[UUID, str] = {
    UUID("00000000-0000-0000-0000-000000000001"): "Acme",
    UUID("00000000-0000-0000-0000-000000000002"): "Globex",
}
# A person holds at most this many live Runners (rev 14).
RUNNERS_PER_PERSON = 5
_EVIDENCE_PATH_SUFFIX = "/evidence"
_RUNTIME_CONTEXT_PATH_SUFFIX = "/runtime-context"
_DIRECTIVE_USAGE_PATH = f"{RUNNER_PREFIX}/usage/work-records/"
_HARNESS_USAGE_PATH = f"{RUNNER_PREFIX}/usage/harness"
_WORK_RECORDS_PATH = f"{RUNNER_PREFIX}/work-records/"
_TOKEN_TTL = timedelta(hours=1)
_DIRECTIVE_TOKEN_TTL = timedelta(minutes=15)


@dataclass(frozen=True)
class Evidence:
    """One Evidence write as received; ``credential`` is ``directive`` or ``signed``."""

    work_record_id: str
    actor: str
    source: str
    payload: dict[str, Any]
    credential: str


@dataclass(frozen=True)
class Refusal:
    method: str
    path: str
    status: int
    reason: str


@dataclass(frozen=True)
class ProviderCall:
    path: str
    authorization: str


@dataclass(frozen=True)
class _Answer:
    status: int
    body: dict[str, Any] | None = None
    # A provider stream: sent as is, as ``text/event-stream``, in place of ``body``.
    stream: bytes | None = None


class _RefusedError(Exception):
    def __init__(self, status: int, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.reason = reason


@dataclass
class _Connection:
    organisation_id: UUID
    paused: bool = False


@dataclass
class _Registered:
    public_key: Ed25519PublicKey
    revoked: bool = False
    tokens: int = 0
    operator: UUID | None = None
    connections: list[_Connection] = field(default_factory=list)


@dataclass
class _PendingLogin:
    request: DeviceLoginRequest
    user_code: str
    confirmed: bool = False
    expired: bool = False


@dataclass
class FakeControlPlane:
    """Bootstrap, heartbeat, Directive token and Evidence, answered the way the platform does.

    ``heartbeat_interval_seconds`` is the cadence bootstrap hands out; a scenario that
    waits on beats sets it low rather than waiting 30 s per beat.
    """

    namespace: str = DEFAULT_NAMESPACE
    # Whether a Runner bootstrapped here is a person's own (`operator` below operates it),
    # not the Organisation's: what a person's own Runner decides subscriptions on.
    bootstraps_operated: bool = False
    heartbeat_interval_seconds: int = 30
    contracts_floor: str = contracts_version
    contract_id: UUID = DEFAULT_CONTRACT_ID
    agent_id: UUID = DEFAULT_AGENT_ID
    cli_kind: str = "codex_cli"
    # Blank answers as a control plane from before ADR-0022 §4 does: no address at all.
    temporal_address: str = ""
    # The ack's word on whether the person may sign this Contract in with their own
    # subscription; None leaves `llm_contracts` out, as a control plane below 3.25 does.
    subscription: SubscriptionRight | None = SubscriptionRight.NO_RIGHT
    pool: PoolTerms | None = None
    # Rev 14: who `login` signs in as, the Organisations they belong to (id: name), and
    # why one refuses a connection (id: reason, as the setting the console names).
    operator: UUID = DEFAULT_OPERATOR
    organisations: Mapping[UUID, str] = field(default_factory=lambda: dict(DEFAULT_ORGANISATIONS))
    connect_refusals: Mapping[UUID, str] = field(default_factory=dict)
    confirm_logins: bool = False
    connections_on_ack: bool = True

    bootstraps: list[BootstrapRequest] = field(default_factory=list)
    runner_ids: list[UUID] = field(default_factory=list)
    heartbeats: list[HeartbeatEnvelope] = field(default_factory=list)
    heartbeat_runner_ids: list[UUID] = field(default_factory=list)
    directive_tokens: list[str] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    refusals: list[Refusal] = field(default_factory=list)
    provider_calls: list[ProviderCall] = field(default_factory=list)
    slots: list[dict[str, Any]] = field(default_factory=list)
    usage: list[dict[str, Any]] = field(default_factory=list)
    llm_access: list[dict[str, Any]] = field(default_factory=list)
    logins: list[DeviceLoginRequest] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._lock = threading.Lock()
        self._registered: dict[UUID, _Registered] = {}
        self._minted: set[str] = set()
        self._delivered: dict[str, str] = {}
        # One ciphertext per (Recipient Key, slot): the ack is authoritative, and a fresh
        # seal every beat would read as a new value at the same version.
        self._sealed: dict[tuple[str, str], str] = {}
        self._logins: dict[str, _PendingLogin] = {}

    # ---------------------------------------------------------------- the test's levers

    def revoke(self, runner_id: UUID) -> None:
        """Revoke a Runner: from now on its signed requests are refused ``runner_revoked``."""

        with self._lock:
            self._registered[runner_id].revoked = True

    def confirm_login(self, user_code: str) -> None:
        """The person confirms ``user_code`` in the browser."""

        with self._lock:
            next(p for p in self._logins.values() if p.user_code == user_code).confirmed = True

    def expire_logins(self) -> None:
        """Every code still waiting runs out, as one does after ``expires_in``."""

        with self._lock:
            for pending in self._logins.values():
                pending.expired = True

    def pause(self, runner_id: UUID, organisation_id: UUID, *, paused: bool = True) -> None:
        """The operator pauses (or resumes) one connection; the next ack says so."""

        with self._lock:
            self._connection(runner_id, organisation_id).paused = paused

    def disconnect(self, runner_id: UUID, organisation_id: UUID) -> None:
        """One connection removed; the next ack no longer names it."""

        with self._lock:
            runner = self._registered[runner_id]
            runner.connections.remove(self._connection(runner_id, organisation_id))

    def contract_for(self, organisation_id: UUID) -> UUID:
        """The operator's Contract in ``organisation_id``, as the ack's terms name it."""

        return UUID(int=organisation_id.int ^ self.operator.int)

    def deliver(self, slot: str, value: str) -> None:
        """A funder delivers ``value`` to ``contract_id`` under ``slot``, at version 1."""

        with self._lock:
            self._delivered[slot] = value

    async def wait_for(self, condition: Callable[[], bool], *, timeout: float = 10.0) -> None:
        """Poll ``condition`` until it holds; works whether the fake is mounted or served."""

        deadline = time.monotonic() + timeout
        while not condition():
            if time.monotonic() > deadline:
                raise TimeoutError(f"fake control plane: condition not met in {timeout}s")
            await asyncio.sleep(0.02)

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "bootstraps": len(self.bootstraps),
                "runner_ids": [str(runner_id) for runner_id in self.runner_ids],
                "logins": len(self.logins),
                "connections": {
                    str(runner_id): [str(c.organisation_id) for c in runner.connections]
                    for runner_id, runner in self._registered.items()
                },
                "recipient_key_ids": sorted({b.recipient_key.key_id for b in self.bootstraps}),
                "isolation_modes": sorted({b.isolation_mode.value for b in self.bootstraps}),
                "heartbeats": len(self.heartbeats),
                "heartbeat_runner_ids": sorted({str(r) for r in self.heartbeat_runner_ids}),
                "bootstrap_build_ids": sorted({b.build_id or "" for b in self.bootstraps}),
                "heartbeat_build_ids": sorted(
                    {h.attestation.build_id if h.attestation else "" for h in self.heartbeats}
                ),
                "directive_tokens": len(self.directive_tokens),
                "evidence": len(self.evidence),
                "refusals": [f"{r.method} {r.path} {r.status} {r.reason}" for r in self.refusals],
                "contract_id": str(self.contract_id),
                "provider_calls": [
                    {"path": c.path, "authorization": c.authorization} for c in self.provider_calls
                ],
                "slots": self.slots[-4:],
                "llm_access": self.llm_access[-4:],
                "usage": self.usage,
                "evidence_events": [{"source": e.source, **e.payload} for e in self.evidence],
            }

    # ---------------------------------------------------------------- the two mounts

    def transport(self) -> httpx.MockTransport:
        """In-process: hand this to ``httpx.AsyncClient(transport=...)``."""

        def handle(request: httpx.Request) -> httpx.Response:
            answer = self.handle(
                request.method,
                request.url.raw_path.decode("ascii"),
                request.headers,
                request.content,
            )
            if answer.stream is not None:
                return httpx.Response(
                    answer.status,
                    content=answer.stream,
                    headers={"content-type": "text/event-stream"},
                )
            return httpx.Response(
                answer.status,
                json=answer.body,
                headers={"date": formatdate(usegmt=True)},
            )

        return httpx.MockTransport(handle)

    @contextmanager
    def serving(self, host: str = "127.0.0.1", port: int = 0) -> Iterator[str]:
        """Over HTTP, on a background thread; yields the base URL. Port 0 picks a free one."""

        server = self.server(host, port)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://{host}:{server.server_address[1]}"
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def server(self, host: str, port: int) -> ThreadingHTTPServer:
        plane = self

        class Handler(BaseHTTPRequestHandler):
            def _answer(self) -> None:
                length = int(self.headers.get("content-length", "0") or 0)
                answer = plane.handle(
                    self.command, self.path, dict(self.headers), self.rfile.read(length)
                )
                if answer.stream is not None:
                    raw, content_type = answer.stream, "text/event-stream"
                else:
                    raw = b"" if answer.body is None else json.dumps(answer.body).encode()
                    content_type = "application/json"
                # send_response adds the `Date` header the Runner's clock-skew reading uses.
                self.send_response(answer.status)
                self.send_header("content-type", content_type)
                self.send_header("content-length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            do_GET = _answer  # noqa: N815 - http.server's contract
            do_POST = _answer  # noqa: N815 - http.server's contract
            do_PUT = _answer  # noqa: N815 - http.server's contract
            do_DELETE = _answer  # noqa: N815 - http.server's contract

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                print(f"fake control plane: {format % args}", flush=True)

        return ThreadingHTTPServer((host, port), Handler)

    # ---------------------------------------------------------------- the routes

    def handle(self, method: str, target: str, headers: Mapping[str, str], body: bytes) -> _Answer:
        """One request, as received: the raw target (query included) and the exact body."""

        path = target.partition("?")[0]
        if method == "GET" and path == STATS_PATH:
            return _Answer(200, self.stats())
        headers = {key.lower(): value for key, value in headers.items()}
        if path.startswith(f"{PROVIDER_PREFIX}/"):
            return self._provider(method, path, headers)
        try:
            with self._lock:
                return self._route(method, target, path, headers, body)
        except _RefusedError as refused:
            with self._lock:
                self.refusals.append(Refusal(method, path, refused.status, refused.reason))
            return _Answer(
                refused.status, {"detail": {"reason": refused.reason, "detail": str(refused)}}
            )

    def _route(
        self, method: str, target: str, path: str, headers: dict[str, str], body: bytes
    ) -> _Answer:
        if not path.startswith(f"{RUNNER_PREFIX}/"):
            raise _RefusedError(
                404,
                "outside_runner_prefix",
                f"{path} is not on the Runner surface ({RUNNER_PREFIX}); a Runner calls "
                "nothing else",
            )
        if method == "POST" and path == BOOTSTRAP_PATH:
            return self._bootstrap(body)
        if method == "POST" and path == LOGIN_DEVICE_PATH:
            return self._start_login(headers, body)
        if method == "POST" and path == LOGIN_TOKEN_PATH:
            return self._finish_login(body)
        if method == "GET" and path == ORGANISATIONS_PATH:
            return self._organisations(method, target, headers, body)
        if method == "POST" and path == CONNECTIONS_PATH:
            return self._connect(method, target, headers, body)
        if method == "POST" and path == HEARTBEAT_PATH:
            return self._heartbeat(method, target, headers, body)
        if method == "POST" and path == DIRECTIVE_TOKEN_PATH:
            return self._directive_token(method, target, headers, body)
        if (
            method == "POST"
            and path.startswith(_WORK_RECORDS_PATH)
            and path.endswith(_EVIDENCE_PATH_SUFFIX)
        ):
            work_record_id = path[len(_WORK_RECORDS_PATH) : -len(_EVIDENCE_PATH_SUFFIX)]
            return self._evidence(work_record_id, method, target, headers, body)
        if (
            method == "GET"
            and path.startswith(_WORK_RECORDS_PATH)
            and path.endswith(_RUNTIME_CONTEXT_PATH_SUFFIX)
        ):
            self._directive_bearer(headers)
            work_record_id = path[len(_WORK_RECORDS_PATH) : -len(_RUNTIME_CONTEXT_PATH_SUFFIX)]
            return _Answer(200, self._runtime_context(work_record_id))
        if method == "GET" and path.startswith(_DIRECTIVE_USAGE_PATH):
            self._directive_bearer(headers)
            return _Answer(200, {"tokens": 0})
        if method == "POST" and path == _HARNESS_USAGE_PATH:
            self._directive_bearer(headers)
            return _Answer(200, {"records": []})
        raise _RefusedError(
            404, "not_served", f"the fake control plane does not serve {method} {path}"
        )

    def _bootstrap(self, body: bytes) -> _Answer:
        request = _parse(BootstrapRequest, body)
        runner_id = uuid4()
        key = Ed25519PrivateKey.generate()
        self._registered[runner_id] = _Registered(
            public_key=key.public_key(),
            connections=[_Connection(organisation_id=self._own_organisation())],
        )
        self.bootstraps.append(request)
        self.runner_ids.append(runner_id)
        print(f"bootstrap {runner_id} isolation={request.isolation_mode.value}", flush=True)
        response = BootstrapResponse(
            identity=_identity(runner_id, key),
            temporal_namespace=self.namespace,
            task_queue=runner_task_queue(runner_id),
            runner_token=f"runner-token-{runner_id}-0",
            runner_token_expires_at=datetime.now(UTC) + _TOKEN_TTL,
            tag_set_version=1,
            floor_state=FloorState.OK,
            contracts_floor=self.contracts_floor,
            heartbeat_interval_seconds=self.heartbeat_interval_seconds,
            temporal_address=self.temporal_address,
            operator_user_profile_id=self.operator if self.bootstraps_operated else None,
        )
        return _Answer(
            200,
            response.model_dump(
                mode="json", exclude=None if self.temporal_address else {"temporal_address"}
            ),
        )

    def _heartbeat(self, method: str, target: str, headers: dict[str, str], body: bytes) -> _Answer:
        runner_id, runner = self._signed(method, target, headers, body)
        envelope = _parse(HeartbeatEnvelope, body)
        expected = runner_task_queue(runner_id)
        if envelope.hosted_task_queue != expected:
            raise _RefusedError(
                409,
                "hosted_queue_mismatch",
                f"Runner {runner_id} reports polling {envelope.hosted_task_queue!r}; "
                f"Directives are dispatched to {expected!r}",
            )
        self.heartbeats.append(envelope)
        self.heartbeat_runner_ids.append(runner_id)
        self.slots.extend(
            {"runner_id": str(runner_id), **slot.model_dump(mode="json")} for slot in envelope.slots
        )
        self.usage.extend(record.model_dump(mode="json") for record in envelope.usage)
        self.llm_access.extend(
            {"runner_id": str(runner_id), **report.model_dump(mode="json")}
            for report in envelope.llm_access or []
        )
        runner.tokens += 1
        token = f"runner-token-{runner_id}-{runner.tokens}"
        ack = HeartbeatAck(
            runner_id=runner_id,
            runner_token=token,
            runner_token_expires_at=datetime.now(UTC) + _TOKEN_TTL,
            floor_state=FloorState.OK,
            contracts_floor=self.contracts_floor,
            tag_set_version=envelope.tag_set_version,
            accepts_new_directives=True,
            # Every Usage Record committed, so the Runner's outbox drops exactly these.
            usage_accepted=[f"{u.directive_id}:{u.sequence}" for u in envelope.usage],
            sealed_credentials=self._sealed_credentials(runner_id),
            llm_contracts=self._llm_contracts(runner),
            pool=self.pool,
            operator_user_profile_id=runner.operator if self.connections_on_ack else None,
            connections=(
                [self._connection_terms(runner_id, c, token) for c in runner.connections]
                if self.connections_on_ack
                else None
            ),
        )
        return _Answer(200, ack.model_dump(mode="json"))

    def _llm_contracts(self, runner: _Registered) -> list[ContractLlmTerms] | None:
        if self.subscription is None:
            return None
        if runner.operator is None:
            # Wherever the ack names connections it names each Contract's Organisation:
            # the Runner serves a Contract only on its own Organisation's connection.
            return [
                ContractLlmTerms(
                    contract_id=self.contract_id,
                    subscription=self.subscription,
                    organisation_id=self._own_organisation() if self.connections_on_ack else None,
                )
            ]
        # One Contract per connection: the operator's own in that Organisation.
        return [
            ContractLlmTerms(
                contract_id=self.contract_for(c.organisation_id),
                subscription=self.subscription,
                organisation_id=c.organisation_id,
                organisation_name=self.organisations.get(c.organisation_id),
            )
            for c in runner.connections
        ]

    def _connection_terms(
        self, runner_id: UUID, connection: _Connection, token: str
    ) -> RunnerConnection:
        organisation_id = connection.organisation_id
        return RunnerConnection(
            organisation_id=organisation_id,
            organisation_name=self.organisations.get(organisation_id),
            temporal_namespace=(
                self.namespace
                if organisation_id == self._own_organisation()
                else organisation_namespace(organisation_id)
            ),
            task_queue=runner_task_queue(runner_id),
            # One token per namespace, as the Temporal gateway admits one per connection.
            runner_token=f"{token}-{organisation_id}"
            if self._registered[runner_id].operator is not None
            else token,
            runner_token_expires_at=datetime.now(UTC) + _TOKEN_TTL,
            paused=connection.paused,
        )

    def _own_organisation(self) -> UUID:
        """The Organisation of :attr:`namespace`, where a token registers a Runner."""

        return UUID(self.namespace.removeprefix("org-"))

    def _connection(self, runner_id: UUID, organisation_id: UUID) -> _Connection:
        return next(
            c
            for c in self._registered[runner_id].connections
            if c.organisation_id == organisation_id
        )

    def _start_login(self, headers: Mapping[str, str], body: bytes) -> _Answer:
        request = _parse(DeviceLoginRequest, body)
        device_code = f"device-code-{uuid4()}"
        user_code = f"CODE-{len(self._logins) + 1:04d}"
        self._logins[device_code] = _PendingLogin(request=request, user_code=user_code)
        authorization = DeviceAuthorization(
            device_code=device_code,
            user_code=user_code,
            # On the host the Runner dialled: it opens no page anywhere else.
            verification_uri=f"https://{headers['host']}/device",
            verification_uri_complete=f"https://{headers['host']}/device?code={user_code}",
            expires_in=600,
            interval=1,
        )
        return _Answer(200, authorization.model_dump(mode="json"))

    def _finish_login(self, body: bytes) -> _Answer:
        device_code = _parse(DeviceTokenRequest, body).device_code
        pending = self._logins.get(device_code)
        if pending is None or pending.expired:
            raise _RefusedError(400, EXPIRED_TOKEN, "the device code expired")
        if not (pending.confirmed or self.confirm_logins):
            raise _RefusedError(400, AUTHORIZATION_PENDING, "waiting for the person to confirm")
        live = [
            r for r in self._registered.values() if r.operator == self.operator and not r.revoked
        ]
        if len(live) >= RUNNERS_PER_PERSON:
            raise _RefusedError(
                409, RUNNER_LIMIT_REASON, f"a person has at most {RUNNERS_PER_PERSON} Runners"
            )
        del self._logins[device_code]
        runner_id = uuid4()
        key = Ed25519PrivateKey.generate()
        self._registered[runner_id] = _Registered(
            public_key=key.public_key(), operator=self.operator
        )
        self.logins.append(pending.request)
        # The heartbeat seals to the Recipient Key a login presented, as to a bootstrap's.
        self.bootstraps.append(
            BootstrapRequest(
                registration_token=device_code,
                isolation_mode=pending.request.isolation_mode,
                contracts_version=pending.request.contracts_version,
                runner_version=pending.request.runner_version,
                recipient_key=pending.request.recipient_key,
                build_id=pending.request.build_id,
            )
        )
        self.runner_ids.append(runner_id)
        response = LoginResponse(
            identity=_identity(runner_id, key),
            operator_user_profile_id=self.operator,
            name=pending.request.name or f"runner-{len(live) + 1}",
            task_queue=runner_task_queue(runner_id),
            floor_state=FloorState.OK,
            contracts_floor=self.contracts_floor,
            heartbeat_interval_seconds=self.heartbeat_interval_seconds,
            temporal_address=self.temporal_address,
        )
        return _Answer(200, response.model_dump(mode="json"))

    def _organisations(
        self, method: str, target: str, headers: dict[str, str], body: bytes
    ) -> _Answer:
        _, runner = self._signed(method, target, headers, body)
        connected = {c.organisation_id for c in runner.connections}
        listed = ConnectableOrganisations(
            organisations=[
                ConnectableOrganisation(
                    organisation_id=organisation_id,
                    name=name,
                    connected=organisation_id in connected,
                    refusal=self.connect_refusals.get(organisation_id),
                )
                for organisation_id, name in self.organisations.items()
            ]
        )
        return _Answer(200, listed.model_dump(mode="json"))

    def _connect(self, method: str, target: str, headers: dict[str, str], body: bytes) -> _Answer:
        runner_id, runner = self._signed(method, target, headers, body)
        organisation_id = _parse(ConnectRequest, body).organisation_id
        if runner.operator is None:
            raise _RefusedError(
                403, "runner_not_operated", "an Organisation's own Runner has its one connection"
            )
        if organisation_id not in self.organisations:
            raise _RefusedError(404, "organisation_not_found", "not an Organisation of yours")
        if reason := self.connect_refusals.get(organisation_id):
            raise _RefusedError(403, "connect_refused", reason)
        if all(c.organisation_id != organisation_id for c in runner.connections):
            runner.connections.append(_Connection(organisation_id=organisation_id))
        connection = self._connection_terms(
            runner_id, self._connection(runner_id, organisation_id), ""
        ).model_copy(update={"runner_token": None, "runner_token_expires_at": None})
        return _Answer(201, connection.model_dump(mode="json"))

    def _directive_token(
        self, method: str, target: str, headers: dict[str, str], body: bytes
    ) -> _Answer:
        self._signed(method, target, headers, body)
        request = _parse(DirectiveTokenRequest, body)
        token = f"directive-token-{len(self.directive_tokens) + 1}"
        self._minted.add(token)
        self.directive_tokens.append(request.directive_id)
        response = DirectiveTokenResponse(
            directive_id=request.directive_id,
            token=token,
            expires_at=datetime.now(UTC) + _DIRECTIVE_TOKEN_TTL,
        )
        return _Answer(200, response.model_dump(mode="json"))

    def _evidence(
        self,
        work_record_id: str,
        method: str,
        target: str,
        headers: dict[str, str],
        body: bytes,
    ) -> _Answer:
        # Either credential the platform accepts on this route: the Directive token the
        # activity pulled, or -- before one exists -- the signed Runner envelope.
        scheme, _, token = headers.get("authorization", "").partition(" ")
        if scheme.lower() == "bearer" and token in self._minted:
            credential = "directive"
        else:
            self._signed(method, target, headers, body)
            credential = "signed"
        try:
            payload = json.loads(body)
            entry = Evidence(
                work_record_id=work_record_id,
                actor=str(payload["actor"]),
                source=str(payload["source"]),
                payload=dict(payload["payload"]),
                credential=credential,
            )
        except (ValueError, KeyError, TypeError) as error:
            raise _RefusedError(
                422, "evidence_invalid", f"not an Evidence write: {error}"
            ) from None
        self.evidence.append(entry)
        return _Answer(
            201,
            {
                "evidence_event_id": str(uuid4()),
                "work_record_id": work_record_id,
                "sequence": len(self.evidence),
                "created": True,
                "idempotency_key": payload.get("idempotency_key"),
            },
        )

    def _sealed_credentials(self, runner_id: UUID) -> list[SealedCredential]:
        bootstrap = self.bootstraps[self.runner_ids.index(runner_id)]
        key = bootstrap.recipient_key
        sealed = []
        for slot, value in sorted(self._delivered.items()):
            if (key.key_id, slot) not in self._sealed:
                self._sealed[(key.key_id, slot)] = seal(
                    public_key=key.public_key,
                    binding=delivery_binding(
                        contract_id=self.contract_id, slot=slot, recipient_key_id=key.key_id
                    ),
                    plaintext=value,
                )
            sealed.append(
                SealedCredential(
                    contract_id=self.contract_id,
                    slot=slot,
                    recipient_key_id=key.key_id,
                    version=1,
                    ciphertext=self._sealed[(key.key_id, slot)],
                )
            )
        return sealed

    def _runtime_context(self, work_record_id: str) -> dict[str, Any]:
        context = WorkerRuntimeContext(
            work_record_id=work_record_id,
            contract_id=str(self.contract_id),
            agent_id=str(self.agent_id),
            profile_slug="learner",
            cli_kind=self.cli_kind,
            repo="conformance/repo",
            base_branch="main",
            task_queue="",
            worker_secret_refs=[],
            product_verifier_command_source=ProductVerifierCommandSource(
                source="runtime-profile", available=True, metadata={"command": "true"}
            ),
        )
        return context.model_dump(mode="json")

    def _provider(self, method: str, path: str, headers: dict[str, str]) -> _Answer:
        with self._lock:
            self.provider_calls.append(ProviderCall(path, headers.get("authorization", "")))
        if method == "GET" and path == f"{PROVIDER_PREFIX}/models":
            return _Answer(200, {"object": "list", "data": [{"id": "gpt-5", "object": "model"}]})
        if method == "POST" and path == f"{PROVIDER_PREFIX}/responses":
            return _Answer(200, stream=_responses_stream())
        return _Answer(404, {"detail": f"the fake provider does not serve {method} {path}"})

    def _directive_bearer(self, headers: dict[str, str]) -> None:
        scheme, _, token = headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or token not in self._minted:
            raise _RefusedError(401, "directive_token_invalid", "no Directive token minted here")

    def _signed(
        self, method: str, target: str, headers: dict[str, str], body: bytes
    ) -> tuple[UUID, _Registered]:
        """The signed Runner envelope, checked the way the platform checks it (issue 71)."""

        try:
            runner_id = UUID(headers.get(RUNNER_ID_HEADER.lower(), ""))
        except ValueError:
            raise _RefusedError(
                401, "signature_invalid", "no Runner id on a signed request"
            ) from None
        runner = self._registered.get(runner_id)
        if runner is None:
            raise _RefusedError(404, "runner_not_found", f"Runner {runner_id} not found")
        try:
            signature = base64.b64decode(headers.get(SIGNATURE_HEADER.lower(), ""), validate=True)
            runner.public_key.verify(
                signature,
                canonical_request(
                    method=method,
                    path=target,
                    signed_at=headers.get(SIGNED_AT_HEADER.lower(), ""),
                    body=body,
                ),
            )
        except (binascii.Error, InvalidSignature):
            raise _RefusedError(
                401, "signature_invalid", f"Runner {runner_id}: signature does not verify"
            ) from None
        if runner.revoked:
            raise _RefusedError(403, RUNNER_REVOKED_REASON, f"Runner {runner_id} is revoked")
        return runner_id, runner


def _identity(runner_id: UUID, key: Ed25519PrivateKey) -> RunnerIdentityMaterial:
    return RunnerIdentityMaterial(
        runner_id=runner_id,
        identity_id=f"identity-{runner_id}",
        private_key_pem=key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode("ascii"),
    )


def _parse[Model: BaseModel](model: type[Model], body: bytes) -> Model:
    try:
        return model.model_validate_json(body)
    except ValidationError as error:
        raise _RefusedError(422, "request_invalid", str(error)) from None


def _responses_stream() -> bytes:
    """The smallest Responses stream Codex finishes a turn on."""

    events: list[dict[str, Any]] = [
        {"type": "response.created", "response": {"id": "resp_fake"}},
        {
            "type": "response.output_item.done",
            "item": {
                "type": "message",
                "role": "assistant",
                "id": "msg_fake",
                "content": [{"type": "output_text", "text": "No lessons."}],
            },
        },
        {
            "type": "response.completed",
            "response": {
                "id": "resp_fake",
                "usage": {
                    "input_tokens": 42,
                    "input_tokens_details": None,
                    "output_tokens": 7,
                    "output_tokens_details": None,
                    "total_tokens": 49,
                },
            },
        },
    ]
    return b"".join(
        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode() for event in events
    )
