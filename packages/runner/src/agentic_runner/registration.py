"""The Runner's half of registration and heartbeat (PRD issue 41, ADR-0013 §8).

Outbound-only: this process dials the control plane, presents its Agent Token once, keeps
what comes back in its own state directory, and then says what it is every 30 s. Nothing
listens on a port here, which is also why revocation is entirely the control plane's --
the lever it pulls is the refused heartbeat.

Two things fail closed on this side, and both fail *before* anything registers:

* **Isolation mode** (17 A2). A Runner configured ``contract_uid`` that cannot actually
  separate uids refuses to start, rather than silently degrading to a shared uid after
  someone edits a ``securityContext``. Auto-detection was rejected for exactly that.
* **A payload newer than this Runner's contracts** (25 §7). The control plane holds
  Directives to a Runner two minors behind, but the Runner is the backstop: it refuses to
  execute a payload whose contracts version it cannot claim to understand, so a floor that
  was misconfigured on the platform side still cannot make it guess.
"""

from __future__ import annotations

import base64
import contextlib
import os
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import BaseModel, ConfigDict

from agentic_runner import __version__ as runner_version
from agentic_runner_contracts import __version__ as contracts_version
from agentic_runner_contracts.channel_messages import TRANSCRIPT_DELIVERIES_PATH, TranscriptDelivery
from agentic_runner_contracts.runner_registration import (
    BootstrapRequest,
    BootstrapResponse,
    DirectiveTokenRequest,
    DirectiveTokenResponse,
    HeartbeatAck,
    HeartbeatEnvelope,
    IsolationMode,
    RecipientKey,
)
from agentic_runner_contracts.runner_signature import (
    RUNNER_ID_HEADER,
    SIGNATURE_HEADER,
    SIGNED_AT_HEADER,
    canonical_request,
    format_signed_at,
)
from agentic_runner_contracts.user_sources import (
    IntakeIgnoredReport,
    IntakeWorkRequest,
    IntakeWorkResponse,
)

BOOTSTRAP_PATH = "/api/runner/v1/runners/bootstrap"
HEARTBEAT_PATH = "/api/runner/v1/runners/heartbeat"
DIRECTIVE_TOKEN_PATH = "/api/runner/v1/runners/directive-token"
INTAKE_WORK_RECORDS_PATH = "/api/runner/v1/runners/intake/work-records"
INTAKE_IGNORED_PATH = "/api/runner/v1/runners/intake/ignored"

# The refusal a revoked Runner's heartbeat earns (06 §2: revocation *is* the refused
# heartbeat). The one reason this process stops on rather than retries.
RUNNER_REVOKED_REASON = "runner_revoked"

# What the state directory holds after a successful bootstrap. One file, mode 0600: the
# private key is in it, and a Runner that loses it re-bootstraps rather than recovering.
STATE_FILENAME = "runner-identity.json"


class RunnerRegistrationError(RuntimeError):
    """The Runner cannot register or heartbeat; ``reason`` is the control plane's code."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class RunnerState(BaseModel):
    """What this process is, once registered. Persisted; reread on every restart.

    A model rather than a dataclass so the state file round-trips through one
    ``model_validate_json``/``model_dump_json`` pair: a hand-written reader would spell
    ``task_queue`` as a literal key, and a Public Metadata name is the builder's to spell.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    runner_id: UUID
    identity_id: str
    private_key_pem: str
    temporal_namespace: str
    task_queue: str
    # What this Runner asserts every Directive's routing against (PRD issue 42): the host
    # party the control plane registered it as, and the Tags it registered with. Both
    # default so a state file written before issue 42 still loads -- such a process makes
    # no routing assertion until it re-bootstraps, which is the introduce-before-require
    # posture every other seam here takes.
    host_party: str = ""
    tags: dict[str, str] = {}
    max_concurrent_directives: int = 1
    # The control plane's cadence, as bootstrap told it (06 §4's 30 s by default); a
    # state file written before issue 47 carries none and keeps the default.
    heartbeat_interval_seconds: int = 30

    def signer(self) -> Ed25519PrivateKey:
        key = serialization.load_pem_private_key(
            self.private_key_pem.encode("utf-8"), password=None
        )
        if not isinstance(key, Ed25519PrivateKey):
            raise RunnerRegistrationError(
                "identity_unusable", "the persisted Runner identity is not an Ed25519 key"
            )
        return key

    def signed_headers(
        self, method: str, url: str, body: bytes, *, now: datetime | None = None
    ) -> dict[str, str]:
        """The signed Runner envelope for one request (issue 71): every signing site's only path.

        The path signed is the one on the wire -- ``url``'s raw target, query included --
        so a base URL carrying a path prefix signs what the platform actually receives.
        """

        signed_at = format_signed_at(now or datetime.now(UTC))
        message = canonical_request(
            method=method,
            path=httpx.URL(url).raw_path.decode("ascii"),
            signed_at=signed_at,
            body=body,
        )
        return {
            RUNNER_ID_HEADER: str(self.runner_id),
            SIGNATURE_HEADER: base64.b64encode(self.signer().sign(message)).decode("ascii"),
            SIGNED_AT_HEADER: signed_at,
        }


def require_isolation_supported(mode: IsolationMode, *, can_separate_uids: bool) -> None:
    """17 A2, the fail-closed half: a ``contract_uid`` Runner that cannot setuid stops.

    Declared, never detected. A Runner that quietly downgraded to ``none`` would keep
    taking Directives from several Contracts while sharing one uid between them, which is
    the exact failure the mode exists to prevent.
    """

    if mode is IsolationMode.CONTRACT_UID and not can_separate_uids:
        raise RunnerRegistrationError(
            "isolation_unavailable",
            "isolation is configured `contract_uid` but this process lacks CAP_SETUID and "
            "cannot separate uids; run it as root with SETUID, SETGID, CHOWN, FOWNER and "
            "DAC_OVERRIDE (ADR-0015 §1), or configure `none` and accept a single-Contract "
            "Runner",
        )


# CAP_SETUID's bit in the kernel's capability bitmask (`capability.h`).
_CAP_SETUID_BIT = 7


def can_separate_uids(*, proc_status: Path = Path("/proc/self/status")) -> bool:
    """Whether this process can actually change uid (17 A2's honest answer).

    Read from the effective capability set on Linux, because `geteuid() == 0` is not it:
    a container that runs as root with ALL dropped -- the `restricted` Pod Security
    level's shape -- is root and still cannot `setuid`. Elsewhere root is the only
    signal there is.
    """

    try:
        status = proc_status.read_text(encoding="utf-8")
    except OSError:
        return os.geteuid() == 0
    for line in status.splitlines():
        if line.startswith("CapEff:"):
            effective = int(line.partition(":")[2].strip(), 16)
            return bool(effective >> _CAP_SETUID_BIT & 1)
    return os.geteuid() == 0


def require_payload_supported(
    payload_contracts_version: str, *, own: str = contracts_version
) -> None:
    """The Runner-side backstop (25 §7): refuse a payload newer than our own contracts.

    Older is fine -- the platform may address an older wire deliberately. Newer means the
    payload can carry fields this build has never seen, and executing it would be a guess.
    """

    if _version(payload_contracts_version) > _version(own):
        raise RunnerRegistrationError(
            "payload_too_new",
            f"payload declares contracts {payload_contracts_version}, newer than this "
            f"Runner's {own}; refusing to execute it",
        )


def _version(value: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in value.strip().split("."))
    except ValueError as error:
        raise RunnerRegistrationError(
            "contracts_version_unreadable", f"cannot read a contracts version from {value!r}"
        ) from error


def load_state(state_dir: Path) -> RunnerState | None:
    """The persisted identity, or ``None`` on a first boot."""

    path = state_dir / STATE_FILENAME
    if not path.exists():
        return None
    return RunnerState.model_validate_json(path.read_text(encoding="utf-8"))


def save_state(state_dir: Path, state: RunnerState) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / STATE_FILENAME
    path.write_text(state.model_dump_json(), encoding="utf-8")
    # The private key is in this file; a shared PVC or a workstation home is not a place
    # to leave it group-readable.
    path.chmod(0o600)


class RunnerRegistrationClient:
    """The two calls this Runner ever makes to register itself."""

    def __init__(self, *, base_url: str, client: httpx.AsyncClient | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = client
        # The control plane's `Date` on the last answer: the reference clock the
        # heartbeat's `clock_skew_seconds` is measured against (23 item 5).
        self.server_date: datetime | None = None

    async def bootstrap(
        self,
        *,
        agent_token: str,
        tags: dict[str, str],
        isolation_mode: IsolationMode,
        recipient_key: RecipientKey,
        max_concurrent_directives: int = 1,
        can_separate_uids: bool,
    ) -> tuple[RunnerState, BootstrapResponse]:
        """Exchange the Agent Token for this process's durable identity."""

        require_isolation_supported(isolation_mode, can_separate_uids=can_separate_uids)
        request = BootstrapRequest(
            agent_token=agent_token,
            tags=tags,
            isolation_mode=isolation_mode,
            contracts_version=contracts_version,
            runner_version=runner_version,
            recipient_key=recipient_key,
            max_concurrent_directives=max_concurrent_directives,
        )
        payload = await self._post(BOOTSTRAP_PATH, content=request.model_dump_json(), headers={})
        response = BootstrapResponse.model_validate(payload)
        state = RunnerState(
            **response.identity.model_dump(),
            temporal_namespace=response.temporal_namespace,
            task_queue=response.task_queue,
            host_party=response.host_party,
            tags=dict(tags),
            max_concurrent_directives=max_concurrent_directives,
            heartbeat_interval_seconds=response.heartbeat_interval_seconds,
        )
        return state, response

    async def heartbeat(self, state: RunnerState, envelope: HeartbeatEnvelope) -> HeartbeatAck:
        """Send one envelope, signed with the identity, and take back a fresh token."""

        return HeartbeatAck.model_validate(await self._signed(state, HEARTBEAT_PATH, envelope))

    async def request_directive_token(
        self, state: RunnerState, directive_id: str
    ) -> DirectiveTokenResponse:
        """Pull one Directive's control-plane token by id (PRD issue 63, 25 §11).

        Signed like the heartbeat; the refusal reasons (revoked, held, routed elsewhere)
        come back as the same :class:`RunnerRegistrationError`.
        """

        payload = await self._signed(
            state, DIRECTIVE_TOKEN_PATH, DirectiveTokenRequest(directive_id=directive_id)
        )
        return DirectiveTokenResponse.model_validate(payload)

    async def create_intake_work_record(
        self, state: RunnerState, request: IntakeWorkRequest
    ) -> IntakeWorkResponse:
        """The create-Work-Record seam (PRD issue 50), under the Runner identity."""

        payload = await self._signed(state, INTAKE_WORK_RECORDS_PATH, request)
        return IntakeWorkResponse.model_validate(payload)

    async def report_intake_ignored(self, state: RunnerState, report: IntakeIgnoredReport) -> None:
        await self._signed(state, INTAKE_IGNORED_PATH, report)

    async def deliver_transcript(self, state: RunnerState, delivery: TranscriptDelivery) -> None:
        """Answer one transcript pull-through (PRD issue 52), under the Runner identity."""

        await self._signed(state, TRANSCRIPT_DELIVERIES_PATH, delivery)

    async def _signed(self, state: RunnerState, path: str, model: BaseModel) -> dict[str, Any]:
        # Signed over these exact bytes, which is why the body is serialised once here and
        # never re-serialised on the way out.
        body = model.model_dump_json().encode("utf-8")
        return await self._post(
            path,
            content=body,
            headers=state.signed_headers("POST", f"{self._base_url}{path}", body),
        )

    async def _post(
        self, path: str, *, content: str | bytes, headers: dict[str, str]
    ) -> dict[str, Any]:
        client = self._client or httpx.AsyncClient()
        owned = self._client is None
        try:
            response = await client.post(
                f"{self._base_url}{path}",
                content=content,
                headers={"content-type": "application/json", **headers},
            )
        finally:
            if owned:
                await client.aclose()
        with contextlib.suppress(TypeError, ValueError):
            self.server_date = parsedate_to_datetime(response.headers.get("date", ""))
        if response.status_code >= 400:
            raise RunnerRegistrationError(*_refusal(response))
        if not response.content:
            return {}
        result: dict[str, Any] = response.json()
        return result


def _refusal(response: httpx.Response) -> tuple[str, str]:
    """Pull the control plane's ``{"reason", "detail"}`` back out of an error body."""

    try:
        detail = response.json().get("detail")
    except ValueError:
        detail = None
    if isinstance(detail, dict) and "reason" in detail:
        return str(detail["reason"]), str(detail.get("detail", detail["reason"]))
    return "refused", f"{response.status_code}: {response.text[:200]}"


class SignedIntakeStream:
    """:class:`agentic_runner.user_sources.IntakeStream` over the signed outbound stream."""

    def __init__(self, client: RunnerRegistrationClient, state: RunnerState) -> None:
        self._client = client
        self._state = state

    async def create_work_record(self, request: IntakeWorkRequest) -> IntakeWorkResponse:
        return await self._client.create_intake_work_record(self._state, request)

    async def report_ignored(self, report: IntakeIgnoredReport) -> None:
        await self._client.report_intake_ignored(self._state, report)
