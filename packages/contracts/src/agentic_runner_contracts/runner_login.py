"""``agentic-runner login`` and ``connect``: a person's Runner (Spec: Runners rev 14).

A person's Runner belongs to the person, like a GitHub App: ``login`` makes it theirs with a
device flow, and ``connect`` asks for a connection to an Organisation they belong to. The
Organisation's own Runners keep the Runner Registration Token (:mod:`runner_registration`).

**Login** is RFC 8628's device authorization grant over the Runner surface. The Runner posts
:class:`DeviceLoginRequest` -- what a bootstrap carries, minus the token -- and shows the
:class:`DeviceAuthorization`'s ``user_code`` while the browser opens its verification URI.
It then polls :data:`LOGIN_TOKEN_PATH` with the ``device_code`` every ``interval`` seconds.
Until the person confirms, the control plane refuses with one of the RFC's codes in
``{"detail": {"reason": ...}}``: :data:`AUTHORIZATION_PENDING` (poll again),
:data:`SLOW_DOWN` (poll again, five seconds slower), :data:`EXPIRED_TOKEN` or
:data:`ACCESS_DENIED` (stop). Once confirmed it answers :class:`LoginResponse`: the Runner's
identity, with the person as its operator and no connection. No token is ever copied: the
``device_code`` is the only secret, and it lives in this one process.

**Connecting** is signed with the identity login handed out, like every later request.
:data:`ORGANISATIONS_PATH` lists the Organisations the operator belongs to, each with
whether this Runner may connect there; :data:`CONNECTIONS_PATH` asks for one and answers
the :class:`~runner_registration.RunnerConnection` made, without its token. The running
Runner picks the connection up from its next heartbeat ack
(:attr:`runner_registration.HeartbeatAck.connections`), which is the only place a
connection's Runner Token travels.
"""

from __future__ import annotations

from typing import Annotated, Final
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from agentic_runner_contracts.runner_registration import (
    CONNECTIONS_MAX,
    BuildId,
    DisplayName,
    FloorState,
    IsolationMode,
    RecipientKey,
    RunnerIdentityMaterial,
    RunnerName,
    TagKey,
    Version,
)

__all__ = [
    "ACCESS_DENIED",
    "AUTHORIZATION_PENDING",
    "CONNECTIONS_PATH",
    "EXPIRED_TOKEN",
    "LOGIN_DEVICE_PATH",
    "LOGIN_TOKEN_PATH",
    "ORGANISATIONS_PATH",
    "RUNNER_LIMIT_REASON",
    "SLOW_DOWN",
    "ConnectRequest",
    "ConnectableOrganisation",
    "ConnectableOrganisations",
    "DeviceAuthorization",
    "DeviceLoginRequest",
    "DeviceTokenRequest",
    "LoginResponse",
]

LOGIN_DEVICE_PATH: Final = "/api/runner/v1/login/device"
LOGIN_TOKEN_PATH: Final = "/api/runner/v1/login/token"
ORGANISATIONS_PATH: Final = "/api/runner/v1/runners/organisations"
CONNECTIONS_PATH: Final = "/api/runner/v1/runners/connections"

# RFC 8628 §3.5's error codes, as the refusal's `reason`.
AUTHORIZATION_PENDING: Final = "authorization_pending"
SLOW_DOWN: Final = "slow_down"
EXPIRED_TOKEN: Final = "expired_token"
ACCESS_DENIED: Final = "access_denied"
# A person has at most 5 live Runners; the sixth login is refused with 409 (rev 14).
RUNNER_LIMIT_REASON: Final = "runner_limit"

DeviceCode = Annotated[str, StringConstraints(min_length=16, max_length=256)]


class DeviceLoginRequest(BaseModel):
    """What ``login`` presents, unauthenticated: a bootstrap without its token.

    ``name`` is the ``login`` argument, or the container's install name; None leaves it to
    the control plane, which names it uniquely among the person's Runners.
    """

    model_config = ConfigDict(extra="forbid")

    name: RunnerName | None = None
    tags: dict[TagKey, Annotated[str, StringConstraints(max_length=256)]] = Field(
        default_factory=dict, max_length=32
    )
    isolation_mode: IsolationMode
    contracts_version: Version
    runner_version: Version
    recipient_key: RecipientKey
    max_concurrent_directives: int = Field(default=1, ge=1, le=64)
    build_id: BuildId | None = None


class DeviceAuthorization(BaseModel):
    """RFC 8628 §3.2's answer: the code to show, where to confirm it, and how to poll."""

    model_config = ConfigDict(extra="forbid")

    device_code: DeviceCode
    user_code: Annotated[str, StringConstraints(min_length=4, max_length=32)]
    verification_uri: Annotated[str, StringConstraints(min_length=8, max_length=2048)]
    verification_uri_complete: (
        Annotated[str, StringConstraints(min_length=8, max_length=2048)] | None
    ) = None
    expires_in: int = Field(ge=1)
    interval: int = Field(default=5, ge=1)


class DeviceTokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_code: DeviceCode


class LoginResponse(BaseModel):
    """The confirmed login: a Runner the person operates, connected nowhere yet.

    ``task_queue`` is ``runner.{runner_id}``, the queue it polls in every namespace it is
    connected to; ``name`` is the one the control plane stored, unique among the person's
    Runners. The private key exists here and nowhere else, as at bootstrap.
    """

    model_config = ConfigDict(extra="forbid")

    identity: RunnerIdentityMaterial
    operator_user_profile_id: UUID
    name: RunnerName
    task_queue: str
    tag_set_version: int = 1
    floor_state: FloorState
    contracts_floor: Version
    heartbeat_interval_seconds: int = 30
    stale_after_seconds: int = 180
    temporal_address: str = ""


class ConnectableOrganisation(BaseModel):
    """One Organisation the operator belongs to, and whether this Runner may connect there.

    ``refusal`` is None when it may, else why not, in the console's words (for example the
    Organisation's setting "Members can connect their own Runners" is off).
    """

    model_config = ConfigDict(extra="forbid")

    organisation_id: UUID
    name: DisplayName
    connected: bool = False
    refusal: Annotated[str, StringConstraints(max_length=512)] | None = None


class ConnectableOrganisations(BaseModel):
    model_config = ConfigDict(extra="forbid")

    organisations: list[ConnectableOrganisation] = Field(
        default_factory=list, max_length=CONNECTIONS_MAX
    )


class ConnectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    organisation_id: UUID
