"""The bootstrap and heartbeat wire between a Runner and the control plane (PRD issue 41).

ADR-0013 §8 and map ticket 06 §2/§4: a Runner presents a **Runner Registration Token** once,
receives a durable identity, its namespace and its first Runner Token, and from then on says
what it is every 30 s over the same outbound-only stream. Nothing here ever travels inbound into
the org's network -- the Runner dials out, always.

It lives in the *contracts* distribution because this envelope **is** what the Runner
compatibility floor is about (ADR-0013 §7): ``agentic_runner_contracts.__version__`` ticks
when these shapes change, which is exactly what makes "one minor behind warns, two behind
holds" a statement about the wire rather than about a control-plane deploy.

Three rules are enforced by the schema itself rather than by a reviewer:

* **A Runner states its Tags once, at bootstrap, and never in a heartbeat body** (map
  ticket 25 §9). :class:`HeartbeatEnvelope` has no tag field and forbids extras, so a body
  carrying one fails validation before any handler sees it;
  :attr:`HeartbeatEnvelope.tag_set_version` is how a Runner says *which* tag set it is
  running. The control plane owns them after that: :attr:`HeartbeatAck.tags` is the set
  the console edited (ADR-0022 §13).
* **Self-reported facts only, scrubbed of content** (06 §4 as refined by 07). A Directive
  outcome carries a status, an error code, an exception *type* and redacted frames --
  never a payload, a message or a repository path. The patterns below are what "scrubbed"
  means operationally.
* **Host party is never here** (17 A3). It is fixed at registration from the Runner
  Registration Token used and is a routing rule the control plane owns; a Runner cannot assert it.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Annotated, Any, Final, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    StringConstraints,
    model_serializer,
    model_validator,
)

from agentic_runner_contracts.channel_messages import TranscriptRequest
from agentic_runner_contracts.llm_access import LlmProvider
from agentic_runner_contracts.llm_key import Fingerprint
from agentic_runner_contracts.llm_usage import UsageRecord
from agentic_runner_contracts.oauth_connector import (
    OAuthAuthorizeUrl,
    OAuthDisconnect,
    OAuthOutcome,
    OAuthStart,
    SealedOAuthCode,
)
from agentic_runner_contracts.runner_limit import RunnerLimit
from agentic_runner_contracts.sealed_credential import (
    OpenedCredential,
    SealedCredential,
)
from agentic_runner_contracts.user_sources import SourceStatus, UserSourceAssignment

# 23 item 4 (Buildkite's own numbers): heartbeat every 30 s, stale at 3 min. Stale is a
# *read* -- "runner offline since" -- never a suspension: a closed laptop lid is an outage.
HEARTBEAT_INTERVAL: Final = timedelta(seconds=30)
HEARTBEAT_STALE_AFTER: Final = timedelta(minutes=3)

# One heartbeat's worth of Usage Records (ADR-0013 §11, PRD issue 43). A Directive makes
# far fewer than this in 30 s; the bound is what stops a Runner that was offline for a
# day from shipping its whole backlog in one body -- the outbox keeps the rest and the
# next heartbeat takes them.
USAGE_BATCH_MAX: Final = 200

# "last-N Directive outcomes" (06 §4). Ten is one Ralph Loop's worth of Directives, which
# is the window a support question ("why did this Work Record stall") is actually asked in.
RECENT_OUTCOMES_MAX: Final = 10

# How many Agents one Runner may hold snapshots for (PRD issue 44). It bounds a single
# exchange -- an Organisation past it has the rest pushed on the next beat, 30 s away,
# because the control plane caps the *pushes* and not the Agents eligible for one -- and
# it bounds the Runner's own store with it: a Runner that held more than this could not
# acknowledge what it holds, so `HeartbeatLink` evicts its least recently read entry
# instead. `max_concurrent_directives` has the same ceiling, so a Runner never evicts an
# Agent it is currently working for.
SNAPSHOT_PUSH_MAX: Final = 64

# The Contracts one ack asks a Runner to report Credential References for, and the
# references in each (ADR-0022 §7). Their product bounds the envelope's report, so a
# Runner never builds a `credentials` list the control plane refuses.
CREDENTIAL_MANIFESTS_MAX: Final = 64
CREDENTIAL_REFERENCES_MAX: Final = 64

_EXCEPTION_TYPE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")
# ``path/to/module.py:123`` and nothing else -- no function name, no source line, no
# message. A frame that carries a repository path still carries only a path shape, which
# is why the *count* is capped too.
_REDACTED_FRAME = re.compile(r"^[A-Za-z0-9_./-]{1,200}:\d{1,6}$")
_OS_RELEASE = re.compile(r"^[a-z][a-z0-9_]{0,15}( [0-9A-Za-z._+-]{1,48})?$")
_TAG_KEY = re.compile(r"^[a-z][a-z0-9_.-]{0,62}$")

ExceptionType = Annotated[str, StringConstraints(pattern=_EXCEPTION_TYPE.pattern)]
RedactedFrame = Annotated[str, StringConstraints(pattern=_REDACTED_FRAME.pattern)]
TagKey = Annotated[str, StringConstraints(pattern=_TAG_KEY.pattern)]
Version = Annotated[str, StringConstraints(pattern=r"^\d+\.\d+\.\d+$")]
# A build is 64 hex since runner-repo 06; 12 is still accepted because a Runner released
# before it reports the first 16.
BuildId = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{12,64}$")]
RegistrationToken = Annotated[str, StringConstraints(min_length=16, max_length=256)]
# A workstation Runner's name (QTS-1773): one path segment, launchd label and systemd
# instance name on the machine, and what the console lists the Runner under.
RUNNER_NAME_PATTERN: Final = r"^[a-z0-9][a-z0-9-]{0,31}$"
RunnerName = Annotated[str, StringConstraints(pattern=RUNNER_NAME_PATTERN)]


class IsolationMode(StrEnum):
    """How a Runner separates one Contract's work from another's (17 A2).

    Declared, never auto-detected: a Runner claiming ``contract_uid`` that cannot
    ``setuid`` refuses to start, and the control plane routes at most one Contract at a
    time to a ``none`` Runner.
    """

    CONTRACT_UID = "contract_uid"
    NONE = "none"


class EgressPosture(StrEnum):
    """What the Runner can reach outbound (06 §4, ticket 05's rejected all-or-nothing toggle).

    ``UNENFORCED`` (PRD issue 58): the host cannot hold a Directive to its Profile's
    egress allow-list -- a workstation, where the attempt's proxy is honoured by the CLIs
    but nothing underneath stops a raw socket -- so it says so rather than claiming the
    list binds.
    """

    UNRESTRICTED = "unrestricted"
    ALLOWLISTED = "allowlisted"
    BLOCKED = "blocked"
    UNENFORCED = "unenforced"


class FolderPosture(StrEnum):
    """How far one harness is held to the Runner Limit's folders (ADR-0022 §55).

    ``CHECKED``: both the Runner and the harness refuse a denied path. ``UNENFORCED``: the
    harness can read a path without asking, so only what it asks about is refused.
    """

    CHECKED = "checked"
    UNENFORCED = "unenforced"


class RunnerLimitPosture(BaseModel):
    """How strongly each part of the Runner Limit binds on this host.

    ``tools`` is always ``enforced``: every verb needs a credential only the Runner holds
    (§53). ``networks`` is the Runner's ``egress_posture`` (§54). ``folders`` is per
    ``cli_kind`` the Runner serves (§55).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tools: Literal["enforced"] = "enforced"
    networks: EgressPosture
    folders: dict[
        Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,31}$")], FolderPosture
    ] = Field(default_factory=dict, max_length=8)


class RunnerLimitReport(BaseModel):
    """The Runner Limit and its posture, as registration and each heartbeat carry it (§57)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    limit: RunnerLimit
    posture: RunnerLimitPosture


class SlotProbe(StrEnum):
    """The probe result for one LLM key: a delivered slot (22 A10) or the key in the
    Runner's own store (Spec: Runners rev 3)."""

    VALID = "valid"
    INVALID = "invalid"
    UNPROBED = "unprobed"


class FloorState(StrEnum):
    """Where a Runner's contracts version sits against the floor (ADR-0013 §7, 25 §7).

    ``OK`` same minor or newer; ``WARN`` exactly one minor behind (banner + one
    Notification); ``HOLD`` two or more behind (no new Directives, in-flight ones finish);
    ``INCOMPATIBLE`` a different major, which is refused at bootstrap outright.
    """

    OK = "ok"
    WARN = "warn"
    HOLD = "hold"
    INCOMPATIBLE = "incompatible"


HOLD_REASON: Final = "Runner below version floor"


class RecipientKey(BaseModel):
    """The public half of the host-generated Recipient Key (22 A4).

    One per *installation*, not per Runner: a Helm release's replicas share the host
    Secret and register the same key, a workstation process is its own installation. The
    platform relays a key it did not mint; sealed Credential Delivery (issue 48) is what
    later encrypts to it.
    """

    model_config = ConfigDict(extra="forbid")

    key_id: Annotated[str, StringConstraints(min_length=8, max_length=128)]
    public_key: Annotated[str, StringConstraints(min_length=32, max_length=4096)]


class BootstrapRequest(BaseModel):
    """What a Runner process presents on first boot (ADR-0013 §8).

    ``tags`` is the one place a Runner states its own Tags: free-form ``key=value`` the
    operator declared in config, used for routing (issue 42) and never in a queue name, a
    Search Attribute or a heartbeat body. Edits after registration come back on
    :attr:`HeartbeatAck.tags`.

    ``build_id`` is the same digest the heartbeat's attestation carries, here so a
    platform that admits only published builds can refuse before it issues an identity
    (runner-repo 06). Optional because a Runner older than this field sends none.

    The Runner Registration Token travels as ``registration_token``; ``agent_token`` is
    its name before ADR-0021, still accepted so a Runner below 3.16 keeps registering.
    Exactly one of the two, and :attr:`token` is the value whichever it was.
    """

    model_config = ConfigDict(extra="forbid")

    registration_token: RegistrationToken | None = None
    agent_token: RegistrationToken | None = None
    tags: dict[TagKey, Annotated[str, StringConstraints(max_length=256)]] = Field(
        default_factory=dict, max_length=32
    )
    isolation_mode: IsolationMode
    contracts_version: Version
    runner_version: Version
    recipient_key: RecipientKey
    max_concurrent_directives: int = Field(default=1, ge=1, le=64)
    build_id: BuildId | None = None
    # ADR-0022 §57-§58: what this Runner's host denies its Agents, and how far it binds.
    # Optional so a Runner below 3.21 still registers.
    runner_limit: RunnerLimitReport | None = None

    @model_validator(mode="after")
    def _exactly_one_token(self) -> BootstrapRequest:
        if (self.registration_token is None) == (self.agent_token is None):
            raise ValueError("send exactly one of registration_token and agent_token")
        return self

    @model_serializer(mode="wrap")
    def _omit_absent_fields(self, handler: SerializerFunctionWrapHandler) -> Any:
        # A control plane below 3.16 forbids extra keys and requires `agent_token`, so a
        # body that names the field it does not know is refused whatever its value --
        # upgrade the control plane before the Runner. Likewise `runner_limit` below 3.21.
        body = handler(self)
        if isinstance(body, dict):
            for name in ("registration_token", "agent_token", "runner_limit"):
                if body.get(name) is None:
                    body.pop(name, None)
        return body

    @property
    def token(self) -> str:
        """The Runner Registration Token, under whichever name the Runner sent it."""

        token = self.registration_token or self.agent_token
        assert token is not None  # _exactly_one_token
        return token


class RunnerIdentityMaterial(BaseModel):
    """The durable identity, handed back exactly once (06 §2).

    ``private_key_pem`` exists in this response and nowhere else -- the control plane keeps
    only the public half -- so a Runner that loses its state dir re-bootstraps rather than
    recovering it. It persists to its own Secret/PVC and signs every later heartbeat with
    it, which is what makes the heartbeat unforgeable by anyone holding only the reusable
    Runner Registration Token.
    """

    model_config = ConfigDict(extra="forbid")

    runner_id: UUID
    identity_id: str
    private_key_pem: str


class BootstrapResponse(BaseModel):
    """The identity, the namespace, the first Runner Token and where to poll."""

    model_config = ConfigDict(extra="forbid")

    identity: RunnerIdentityMaterial
    temporal_namespace: str
    # `runner.{runner_id}` from `public_metadata.runner_task_queue` -- the Runner polls
    # what it is told here and builds no name of its own.
    task_queue: str
    runner_token: str
    runner_token_expires_at: datetime
    tag_set_version: int
    floor_state: FloorState
    contracts_floor: Version
    # Who the control plane registered this process as being hosted by, fixed from the
    # Runner Registration Token presented (17 A3). Told to the Runner, never declared by it:
    # it is what the Runner asserts a Directive's routing against (PRD issue 42), and a
    # Runner that could set it could reach another contractor's work. Defaulted so a Runner
    # on this wire still parses a response from a control plane that predates issue 42 -- it then
    # has nothing to assert with and simply performs no routing assertion.
    host_party: str = ""
    heartbeat_interval_seconds: int = int(HEARTBEAT_INTERVAL.total_seconds())
    stale_after_seconds: int = int(HEARTBEAT_STALE_AFTER.total_seconds())
    # ADR-0022 §4: the Temporal frontend the namespace lives on, so no install command
    # carries it. Blank from a control plane that predates the field; the Runner then
    # falls back to its own `TEMPORAL_ADDRESS`.
    temporal_address: str = ""


class SelfTestFailure(StrEnum):
    """Why a harness's ``--version`` under a Contract's uid did not answer (local-agents 17)."""

    NOT_FOUND = "not_found"
    BELOW_FLOOR = "below_floor"
    SPAWN_FAILED = "spawn_failed"
    TIMEOUT = "timeout"


class HarnessSelfTest(BaseModel):
    """The last ``--version`` run as the Contract would run it: a result, never its output."""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    at: datetime
    reason: SelfTestFailure | None = None


class UsageWindow(BaseModel):
    """One subscription usage window, as the harness itself last reported it (local-agents
    10): which window, how much of it is used, and when it resets. A percentage and a time
    -- never an account, a plan or anything the harness authenticated with."""

    model_config = ConfigDict(extra="forbid")

    # ``five_hour``, ``seven_day``, ``seven_day_opus``…: the vendor's own window name
    # where it has one.
    name: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,31}$")]
    used_percent: float = Field(ge=0.0, le=100.0)
    resets_at: datetime | None = None


_OPTIONAL_HARNESS_FIELDS: Final = ("auth_modes", "permission_mode", "self_test", "usage_windows")


class HarnessVersion(BaseModel):
    """One CLI as it stands under one Contract root (ADR-0015 §4, 23 item 5).

    ``auth_modes``, ``permission_mode`` and ``self_test`` are the runtime's descriptor
    (local-agents 17), self-reported like everything else here. The permission mode is
    what the Runner passes the harness, reported and never trusted as a gate (ADR-0011
    §12). ``usage_windows`` is present only for a Contract signed in to a subscription on
    a user-hosted Runner, once the harness has said how much of it is used (local-agents
    10).
    """

    model_config = ConfigDict(extra="forbid")

    contract_id: UUID
    cli_kind: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,31}$")]
    version: Annotated[str, StringConstraints(max_length=64)]
    auth_mode: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,31}$")]
    auth_modes: (
        list[Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,31}$")]] | None
    ) = Field(default=None, max_length=8)
    permission_mode: (
        Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_=;.-]{0,95}$")] | None
    ) = None
    self_test: HarnessSelfTest | None = None
    usage_windows: list[UsageWindow] | None = Field(default=None, max_length=8)

    @model_serializer(mode="wrap")
    def _omit_absent_optional_fields(self, handler: SerializerFunctionWrapHandler) -> Any:
        # An older control plane forbids extra keys, so a row without these is the body
        # it already parses (the descriptor arrived in 3.4, the usage windows in 3.7). A
        # row with one needs the control plane on that minor first -- upgrade the control
        # plane before the Runner.
        body = handler(self)
        if isinstance(body, dict):
            for name in _OPTIONAL_HARNESS_FIELDS:
                if body.get(name) is None:
                    body.pop(name, None)
        return body


class ResourcePressure(BaseModel):
    """Enough to answer "is this Runner wedged or merely busy" without a shell (06 §4)."""

    model_config = ConfigDict(extra="forbid")

    cpu_percent: float = Field(ge=0.0, le=100.0)
    memory_percent: float = Field(ge=0.0, le=100.0)
    disk_percent: float = Field(ge=0.0, le=100.0)


class DirectiveOutcome(BaseModel):
    """One finished Directive, as status and codes -- never as content (06 §4, 07)."""

    model_config = ConfigDict(extra="forbid")

    # Real activity names for every org (ticket 07's answer): type names are Public
    # Metadata and substituting codes would hide real failures from support.
    activity: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
    status: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,31}$")]
    error_code: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")] | None = None
    exception_type: ExceptionType | None = None
    redacted_frames: list[RedactedFrame] = Field(default_factory=list, max_length=20)


class CredentialValidity(BaseModel):
    """A Credential Reference and whether the Runner can currently use it. A boolean, by
    design: the value never crosses the stream in the clear (ADR-0013 §11 as amended)."""

    model_config = ConfigDict(extra="forbid")

    reference: Annotated[str, StringConstraints(max_length=128)]
    valid: bool
    # ADR-0022 §7: usability is per Contract, because a value sealed for one Contract
    # resolves for it alone. Required: a Runner on 3.18 or earlier never filled this
    # list, so no envelope that parsed before carries an entry.
    contract_id: UUID


class CredentialManifest(BaseModel):
    """One Contract's Credential References, which the Runner reports on (ADR-0022 §7).

    Sent by the control plane because the Runner learns a manifest only from a Directive's
    runtime context, and routing needs the report before the first Directive arrives.
    """

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _drop_retired_funding_party(cls, data: Any) -> Any:
        # A control plane below 3.25 still names who funds the Contract. The Runner's own
        # store now serves every Contract it runs (ADR-0022 §36 went with `funding`), so
        # the key says nothing a Runner on 3.25 reads. Remove once the control plane is
        # on 3.25.
        if isinstance(data, dict) and "funding_party" in data:
            return {key: value for key, value in data.items() if key != "funding_party"}
        return data

    contract_id: UUID
    references: list[Annotated[str, StringConstraints(max_length=128)]] = Field(
        default_factory=list, max_length=CREDENTIAL_REFERENCES_MAX
    )


class ToolServerHealth(BaseModel):
    """Whether a Tool Server the Runner last started for a Directive came up (console-v2
    issue 29). The slug, one boolean and when -- never a command line, URL, env or error
    text, which is where a credential would leak (slice 18's scrubbed boolean)."""

    model_config = ConfigDict(extra="forbid")

    # The platform's `mcp_servers.slug` is String(64).
    slug: Annotated[str, StringConstraints(max_length=64)]
    started: bool
    last_started_at: datetime


class SlotStatus(BaseModel):
    """One credential slot as the funder sees it (22 A10)."""

    model_config = ConfigDict(extra="forbid")

    contract_id: UUID
    runtime_kind: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,31}$")]
    present: bool
    key_id: Annotated[str, StringConstraints(max_length=128)] | None = None
    delivered_at: datetime | None = None
    last_used_at: datetime | None = None
    probe: SlotProbe = SlotProbe.UNPROBED


class AppliedSnapshot(BaseModel):
    """One Grant snapshot this Runner currently holds, as it acknowledges it (issue 44).

    The version, not the snapshot: this is how the control plane tells *applied* from
    *sent*, and how it knows which Agents it need not push again. A Runner that has
    applied nothing sends an empty list and is pushed everything it holds.

    ``contract_id`` is the push's own, echoed back: None for a snapshot pushed without one,
    by a control plane below 3.23.
    """

    model_config = ConfigDict(extra="forbid")

    agent_id: UUID
    version: Annotated[str, StringConstraints(min_length=8, max_length=64)]
    contract_id: UUID | None = None

    @model_serializer(mode="wrap")
    def _omit_absent_contract(self, handler: SerializerFunctionWrapHandler) -> Any:
        # A control plane below 3.23 forbids the key, and pushes nothing a Runner would
        # echo it for.
        body = handler(self)
        if isinstance(body, dict) and body.get("contract_id") is None:
            body.pop("contract_id", None)
        return body


class GrantPush(BaseModel):
    """One Agent's Grant snapshot under one Contract, pushed on change (ADR-0011 §11).

    ``snapshot`` is ``services.agents.GrantSnapshotRead`` as JSON -- the same shape the
    ``grant-snapshot`` endpoint serves, because only the *source* changed: the Runner
    parses it with ``GrantSnapshot.from_payload`` and the evaluator is untouched.

    ``contract_id`` is the Work Records' Contract the snapshot decides for (QTS-1706): an
    Organisation Agent runs under each Owner's Contract, so the Runner holds one snapshot
    per Agent and Contract and decides a verb on its own Work Record's. None from a
    control plane below 3.23, which holds one snapshot per Agent.
    """

    model_config = ConfigDict(extra="forbid")

    agent_id: UUID
    version: Annotated[str, StringConstraints(min_length=8, max_length=64)]
    snapshot: dict[str, object]
    contract_id: UUID | None = None

    @model_serializer(mode="wrap")
    def _omit_absent_contract(self, handler: SerializerFunctionWrapHandler) -> Any:
        # A Runner below 3.23 forbids the key, so the control plane sends it only to a
        # Runner whose heartbeat names contracts 3.23 or later.
        body = handler(self)
        if isinstance(body, dict) and body.get("contract_id") is None:
            body.pop("contract_id", None)
        return body


class CeilingPush(BaseModel):
    """One Contract's monthly ceilings, on the same channel as the Grants (12 B4).

    ``used`` is the control plane's month-to-date figure at the moment of the push; the
    relocated proxy adds what it has metered since (PRD issue 43's ``CeilingStore``).
    """

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _read_retired_org_funded(cls, data: Any) -> Any:
        # A control plane below 3.25 pushes the Organisation's ceiling for every Contract
        # and says with `org_funded` whether it applies. From 3.25 the control plane
        # pushes it only where it applies (the Organisation's ceiling counts the usage on
        # the Organisation's Runners), so an old push that said "not org-funded" is read
        # as no Organisation ceiling. Remove once the control plane is on 3.25.
        if isinstance(data, dict) and "org_funded" in data:
            org_funded = data["org_funded"] is True
            data = {key: value for key, value in data.items() if key != "org_funded"}
            if not org_funded:
                data["organisation_limit"] = None
        return data

    contract_id: UUID
    contract_limit: int | None = None
    contract_used: int = 0
    # Set only where the Organisation's ceiling applies to this Runner's usage.
    organisation_limit: int | None = None
    organisation_used: int = 0


class SubscriptionState(StrEnum):
    """One Agent Runtime's subscription sign-in in one Contract's harness root.

    ``NONE``: no sign-in there. ``EXPIRED``: a sign-in whose lifetime the Runner can read
    has run out. ``AT_LIMIT``: a usage window is used up until its reset. Read from the
    harness root and the vendor CLI's own usage report; no probe calls a vendor endpoint
    with the person's bearer.
    """

    SIGNED_IN = "signed_in"
    EXPIRED = "expired"
    AT_LIMIT = "at_limit"
    NONE = "none"


class ProviderKeyReport(BaseModel):
    """Whether the Runner's own store holds a provider's key, and that key's probe."""

    model_config = ConfigDict(extra="forbid")

    provider: LlmProvider
    held: bool
    probe: SlotProbe = SlotProbe.UNPROBED


class RuntimeSubscriptionReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cli_kind: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,31}$")]
    state: SubscriptionState
    # Set for `at_limit` only: when the used-up window resets.
    resets_at: datetime | None = None


class ContractLlmAccess(BaseModel):
    """The LLM access this Runner holds for one Contract it serves (Spec: Runners rev 3).

    The one record of LLM access: the Contract gate, routing and the console read this
    and nothing else. The store's keys are the host's, so on a pool Runner they repeat for
    every Contract; a subscription lives in one Contract's harness root.
    """

    model_config = ConfigDict(extra="forbid")

    contract_id: UUID
    keys: list[ProviderKeyReport] = Field(default_factory=list, max_length=8)
    subscriptions: list[RuntimeSubscriptionReport] = Field(default_factory=list, max_length=8)


class InstallTarget(StrEnum):
    """Which of the three install paths this Runner came from, so the console prints the
    command that reaches it: ``kubectl exec``, ``docker exec``, or none on a workstation."""

    HELM = "helm"
    DOCKER = "docker"
    WORKSTATION = "workstation"


class RunnerInstall(BaseModel):
    """Where to run a command on this Runner. ``hostname`` is the pod name under Helm and
    the container id under Docker; a workstation's, which can name its person, is never
    sent, and the schema refuses one. ``name`` is what a workstation Runner was installed
    as, the person's own choice, which every local command on it takes (QTS-1773)."""

    model_config = ConfigDict(extra="forbid")

    target: InstallTarget
    hostname: (
        Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$")] | None
    ) = None
    name: RunnerName | None = None

    @model_validator(mode="after")
    def _hostname_only_in_a_container(self) -> RunnerInstall:
        if self.target == InstallTarget.WORKSTATION and self.hostname is not None:
            raise ValueError("a workstation Runner never reports its hostname")
        return self

    @model_serializer(mode="wrap")
    def _omit_no_name(self, handler: SerializerFunctionWrapHandler) -> Any:
        # A control plane below 3.27 forbids the key; a container Runner has no name.
        body = handler(self)
        if isinstance(body, dict) and body.get("name") is None:
            body.pop("name", None)
        return body


class SubscriptionRight(StrEnum):
    """Whether the person may sign this Contract in with their own subscription, and if
    not, why -- so ``agentic-runner sign-in`` can say which of the two is missing."""

    ALLOWED = "allowed"
    # The Contract lacks `allows_own_subscription`.
    NO_RIGHT = "no_right"
    # The right is on, and the person has not acknowledged the subscription terms.
    NOT_ACKNOWLEDGED = "not_acknowledged"


class ContractLlmTerms(BaseModel):
    """One Contract this Runner serves, as the ack names it."""

    model_config = ConfigDict(extra="forbid")

    contract_id: UUID
    subscription: SubscriptionRight


class PoolTerms(BaseModel):
    """The pool a Runner belongs to, as the ack names it (Spec: Runners rev 3).

    ``pool_key_fingerprint`` is the pool's current Pool Key, None while the pool has
    none. A Runner holding another one opens none of the pool's LLM Keys.
    """

    model_config = ConfigDict(extra="forbid")

    pool_key_fingerprint: Fingerprint | None = None


class InstallChannel(StrEnum):
    """How this Runner was installed, as it says so itself (23 item 5)."""

    HELM = "helm"
    HOMEBREW = "homebrew"
    SYSTEMD_USER = "systemd_user"
    WINDOWS_USER = "windows_user"
    MANUAL = "manual"


class SessionKind(StrEnum):
    """What the process runs under. ``login_agent`` is the workstation shape (23 item 1):
    a per-user service that dies with the login session's keychain, never a daemon."""

    LOGIN_AGENT = "login_agent"
    CONTAINER = "container"
    INTERACTIVE = "interactive"


class StoreKind(StrEnum):
    """Where this Runner's Credential Reference values live (23 item 5)."""

    KEYCHAIN = "keychain"
    SECRET_SERVICE = "secret_service"
    CREDENTIAL_MANAGER = "credential_manager"
    # The `0600` fallback when no OS store is reachable, and the mounted `0400` Secret.
    FILE = "file"
    NONE = "none"


class AcpBridgeVersion(BaseModel):
    """The ACP bridge a Runner pins in front of one CLI (local-agents 12 item 2): its npm
    package and version, the Runner's own constants -- never a path."""

    model_config = ConfigDict(extra="forbid")

    package: Annotated[
        str,
        StringConstraints(pattern=r"^(@[a-z0-9][a-z0-9._-]{0,63}/)?[a-z0-9][a-z0-9._-]{0,127}$"),
    ]
    version: Annotated[str, StringConstraints(pattern=r"^[0-9][0-9A-Za-z.+-]{0,63}$")]


class CliVersion(BaseModel):
    """One Agent Runtime CLI found on ``PATH`` -- never bundled on a workstation (23 item 2).

    A version string and whether it clears the Runner's floor for that CLI; never a path,
    which on a workstation carries the user's home directory.
    """

    model_config = ConfigDict(extra="forbid")

    cli_kind: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,31}$")]
    version: Annotated[str, StringConstraints(pattern=r"^[0-9][0-9A-Za-z.+-]{0,63}$")]
    meets_floor: bool
    # Set only for a kind this Runner serves through its pinned ACP bridge (QTS-1322).
    acp_bridge: AcpBridgeVersion | None = None

    @model_serializer(mode="wrap")
    def _omit_absent_acp_bridge(self, handler: SerializerFunctionWrapHandler) -> Any:
        # A control plane below 3.12 forbids the key, so a row without a bridge is the
        # body it already parses -- upgrade the control plane before a Runner sets
        # ``ACP_CLI_KINDS``.
        body = handler(self)
        if isinstance(body, dict) and body.get("acp_bridge") is None:
            body.pop("acp_bridge", None)
        return body


class HostAttestation(BaseModel):
    """What a Runner says about its own build and host (23 item 5, 12 B7 as refined).

    **Self-reported, all of it.** Nothing here is verified by the platform, which is why
    the consoles render it as *self-reported build identity* with the host party beside
    every figure. Enumerations and short patterned strings only: an OS *release*, not a
    hostname; a build digest, not a path.
    """

    model_config = ConfigDict(extra="forbid")

    build_id: BuildId
    install_channel: InstallChannel
    os: Annotated[
        str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,15}( [0-9A-Za-z._+-]{1,48})?$")
    ]
    session_kind: SessionKind
    store_kind: StoreKind
    clis: list[CliVersion] = Field(default_factory=list, max_length=8)


class LifecycleKind(StrEnum):
    INSTALL = "install"
    START = "start"
    STOP = "stop"
    # 23 item 4: a lid close is an outage, and the wake is the fact worth a record.
    WAKE = "wake"


class LifecycleEvent(BaseModel):
    """One process lifecycle fact, for the Organisation's Evidence trail (issue 47).

    Ids and timestamps only. Carried on the next heartbeat because an ``install`` or a
    ``stop`` happens when no heartbeat is in flight -- the Runner keeps them in its state
    directory until an ack takes them.
    """

    model_config = ConfigDict(extra="forbid")

    kind: LifecycleKind
    at: datetime
    # Set on ``wake`` only: when the process went under.
    slept_at: datetime | None = None


class HeartbeatEnvelope(BaseModel):
    """Everything a Runner says about itself every 30 s.

    ``extra="forbid"`` is load-bearing twice over: it is what makes a body carrying a
    Runner Tag a schema failure (map ticket 25 §9 -- tags ride bootstrap, a *version* rides
    here), and it is what stops a future Runner smuggling an unreviewed field past the
    scrubbing rules above.
    """

    model_config = ConfigDict(extra="forbid")

    runner_version: Version
    contracts_version: Version
    harnesses: list[HarnessVersion] = Field(default_factory=list, max_length=64)
    resource_pressure: ResourcePressure
    max_concurrent_directives: int = Field(ge=1, le=64)
    current_load: int = Field(ge=0)
    recent_outcomes: list[DirectiveOutcome] = Field(
        default_factory=list, max_length=RECENT_OUTCOMES_MAX
    )
    # One entry per reference of every Contract the last ack's `credential_manifests` named.
    credentials: list[CredentialValidity] = Field(
        default_factory=list, max_length=CREDENTIAL_MANIFESTS_MAX * CREDENTIAL_REFERENCES_MAX
    )
    slots: list[SlotStatus] = Field(default_factory=list, max_length=64)
    # Usage is an outbound stream item (ADR-0013 §11): the relocated proxy meters on the
    # host that holds the funder's key and ships the rows out here, batched. Counts,
    # prices and ids only -- never a prompt or a completion.
    usage: list[UsageRecord] = Field(default_factory=list, max_length=USAGE_BATCH_MAX)
    egress_posture: EgressPosture
    isolation_mode: IsolationMode
    tag_set_version: int = Field(ge=1)
    # 23 item 4: a lid close is an outage, so the Runner says when it went under and when
    # it came back rather than leaving a silent gap for support to guess at.
    last_sleep_at: datetime | None = None
    last_wake_at: datetime | None = None
    clock_skew_seconds: float = 0.0
    hosted_task_queue: str
    # Sealed Credential Delivery's Runner half (PRD issue 48, 22 A4/A7). Present only on
    # the heartbeat that follows a renewal or a reinstall: the Runner generated a new
    # Recipient Key and this is its public half, relayed by a platform that did not mint
    # it. Absent on every ordinary beat -- the key on the Runner row still stands.
    recipient_key: RecipientKey | None = None
    # The same slots, re-sealed to the key above (22 A7). The Runner pushes these with
    # the new key and only destroys the old private half once the ack shows every slot
    # stored under the new `recipient_key_id`.
    resealed: list[SealedCredential] = Field(default_factory=list, max_length=64)
    # 22 A10's "opened by Runner" Evidence. The slots this installation actually opened
    # since the last beat, as ids: the platform cannot observe it -- it holds ciphertext
    # and no key -- so the fact arrives here or not at all.
    opened: list[OpenedCredential] = Field(default_factory=list, max_length=64)
    # Issue 47: the self-reported build and host facts, collected once at start-up, and
    # the lifecycle events not yet acknowledged. Defaulted so an older Runner's envelope
    # still parses.
    attestation: HostAttestation | None = None
    lifecycle: list[LifecycleEvent] = Field(default_factory=list, max_length=32)
    # What this Runner has applied, per Agent and Contract (PRD issue 44, QTS-1706). The
    # control plane pushes exactly what is missing or stale from this list, so a snapshot
    # is pushed once per change per Runner and an unacknowledged one is pushed again on
    # the next beat.
    applied_snapshots: list[AppliedSnapshot] = Field(
        default_factory=list, max_length=SNAPSHOT_PUSH_MAX
    )
    # PRD issue 50: whether each user-connected Source this Runner reads could be
    # connected, as a code -- how the console learns a Slack workspace refused member
    # installs. Absent on an older Runner, which reads no user-connected Source.
    source_status: list[SourceStatus] = Field(default_factory=list, max_length=64)
    # Console-v2 issue 29: each Runner-hosted Tool Server's latest start, per slug. A
    # server not started since the process began is absent.
    tool_servers: list[ToolServerHealth] = Field(default_factory=list, max_length=64)
    # Console-v2 issue 30: the authorise URL for each start the last ack asked for, the
    # token ciphertext a connect or a refresh sealed to this installation's Recipient Key
    # -- stored in the Contract's slot like any sealed delivery -- and each step's outcome
    # as ids, the Evidence. No verifier, code or token in the clear (`oauth_connector`).
    oauth_authorizations: list[OAuthAuthorizeUrl] = Field(default_factory=list, max_length=16)
    oauth_tokens: list[SealedCredential] = Field(default_factory=list, max_length=64)
    oauth_outcomes: list[OAuthOutcome] = Field(default_factory=list, max_length=64)
    # ADR-0022 §57-§58: the Runner Limit read at start, on every beat. None from a Runner
    # below 3.21, which the console shows as "not reported" rather than as no limit.
    runner_limit: RunnerLimitReport | None = None
    # Spec: Runners rev 3: the LLM access held for each Contract the last ack's
    # `llm_contracts` named, and where a command reaches this Runner. Both None until an
    # ack has carried `llm_contracts`: that is how the Runner knows the control plane is on
    # 3.25 and parses them. From 3.25 a Runner that reports no LLM access gets no work.
    llm_access: list[ContractLlmAccess] | None = Field(
        default=None, max_length=CREDENTIAL_MANIFESTS_MAX
    )
    install: RunnerInstall | None = None
    # Spec: Runners rev 3: the fingerprint of the Pool Key this Runner holds, None when it
    # holds none. Sent only once an ack carried `pool`, which a control plane on 3.26 or
    # later sends; an older one refuses the field.
    pool_key_fingerprint: Fingerprint | None = None

    @model_serializer(mode="wrap")
    def _omit_empty_optional_fields(self, handler: SerializerFunctionWrapHandler) -> Any:
        # An older control plane forbids extra keys, so a Runner with nothing to say in
        # these sends the body that control plane already parses. One that has something
        # needs the control plane on the contracts minor that added the field first.
        body = handler(self)
        if isinstance(body, dict):
            for name in (
                "tool_servers",
                "oauth_authorizations",
                "oauth_tokens",
                "oauth_outcomes",
                "runner_limit",
            ):
                if not body.get(name):
                    body.pop(name, None)
            # An empty report is still sent: it says the Runner holds no LLM access.
            for name in ("llm_access", "install", "pool_key_fingerprint"):
                if body.get(name) is None:
                    body.pop(name, None)
        return body


class HeartbeatAck(BaseModel):
    """What the control plane says back: a refreshed token, the floor, and any hold."""

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _drop_retired_empty_sign_in_codes(cls, data: Any) -> Any:
        # A control plane on contracts 3.13 sends `"sign_in_codes": []` on every ack. Empty,
        # it says nothing, so a Runner on 3.14 drops it rather than refuse every beat; a
        # code in it is still an extra key, and refused (QTS-1448). Remove once the
        # control plane is on 3.14.
        if isinstance(data, dict) and data.get("sign_in_codes") == []:
            return {key: value for key, value in data.items() if key != "sign_in_codes"}
        return data

    runner_id: UUID
    # Null while the Runner is held (ADR-0013 §7, issue 39's own refusal): it keeps the
    # token it already holds until that expires, which is exactly how in-flight Directives
    # finish while no new one is dispatched.
    runner_token: str | None = None
    runner_token_expires_at: datetime | None = None
    floor_state: FloorState
    contracts_floor: Version
    # Set only when `floor_state` holds: the reason the console banner and the Directive
    # refusal both read (25 §7).
    hold_reason: str | None = None
    # The control plane's tag-set version: it ticks on every edit of the Runner's Tags.
    tag_set_version: int
    # ADR-0022 §13: the Runner's Tags as the control plane routes on them, edited in the
    # console after registration. Authoritative, like `sealed_credentials`: the Runner
    # checks every Directive's selector against this set, not against the Tags it
    # bootstrapped with, or work routed on an added Tag is refused on every retry. None
    # from a control plane that predates the field; the Runner then keeps what it holds.
    # The ack forbids extra keys, so the control plane sends it only to a Runner whose
    # heartbeat names contracts 3.20 or later.
    tags: dict[TagKey, Annotated[str, StringConstraints(max_length=256)]] | None = Field(
        default=None, max_length=32
    )
    accepts_new_directives: bool
    # ``directive_id:sequence`` for every Usage Record this heartbeat committed. The
    # Runner's outbox drops exactly these and re-sends the rest, so a dropped ack costs a
    # re-send and not a row: the ledger is idempotent on the same pair (PRD issue 43).
    usage_accepted: list[Annotated[str, StringConstraints(max_length=160)]] = Field(
        default_factory=list, max_length=USAGE_BATCH_MAX
    )
    # Every sealed value this installation may currently open (PRD issue 48, 22 A5).
    # Authoritative, not incremental: the Runner replaces what it holds with this list,
    # which is how a terminated Contract's value is dropped on the next stream message
    # without a delete verb of its own (22 A9).
    sealed_credentials: list[SealedCredential] = Field(default_factory=list, max_length=64)
    # Push on change (ADR-0011 §11): the Grant snapshots this Runner does not hold at the
    # current version, and the ceilings of the Contracts behind them. Empty on the common
    # beat -- nothing changed, so nothing is pushed.
    grant_pushes: list[GrantPush] = Field(default_factory=list, max_length=SNAPSHOT_PUSH_MAX)
    ceiling_pushes: list[CeilingPush] = Field(default_factory=list, max_length=SNAPSHOT_PUSH_MAX)
    # PRD issue 50: the user-connected Sources this Runner reads. Authoritative, like
    # `sealed_credentials`: pushed only to a Runner its owner hosts, and a Source removed
    # in the console is simply absent from the next ack.
    user_sources: list[UserSourceAssignment] = Field(default_factory=list, max_length=64)
    # PRD issue 52: transcript pull-throughs waiting on this Runner. Relayed once each --
    # the control plane hands a request over on exactly one beat -- and answered on the
    # signed stream (`TRANSCRIPT_DELIVERIES_PATH`), never on the ack itself.
    transcript_requests: list[TranscriptRequest] = Field(default_factory=list, max_length=16)
    # Console-v2 issue 30: start an OAuth authorisation, the code the browser sealed for
    # one, and Disconnect. Relayed once each, like `transcript_requests`; what became of each
    # comes back on the envelope's `oauth_*` fields.
    oauth_starts: list[OAuthStart] = Field(default_factory=list, max_length=16)
    oauth_codes: list[SealedOAuthCode] = Field(default_factory=list, max_length=16)
    oauth_disconnects: list[OAuthDisconnect] = Field(default_factory=list, max_length=16)
    # ADR-0022 §7: the Contracts this Runner may serve that declare Credential
    # References. Authoritative, like `sealed_credentials`: the next envelope reports on
    # exactly these. The ack forbids extra keys, so the control plane sends it only to a
    # Runner whose heartbeat names contracts 3.19 or later.
    credential_manifests: list[CredentialManifest] = Field(
        default_factory=list, max_length=CREDENTIAL_MANIFESTS_MAX
    )
    # Spec: Runners rev 3: every Contract this Runner serves, with whether its person may
    # sign it in with their own subscription. Authoritative, like `credential_manifests`:
    # the next envelope reports LLM access for exactly these, and `agentic-runner sign-in`
    # reads it. A personal Runner serves one. None from a control plane below 3.25; an
    # empty list says the Runner serves no Contract yet. The ack forbids extra keys, so the
    # control plane sends it only to a Runner whose heartbeat names contracts 3.25 or later.
    llm_contracts: list[ContractLlmTerms] | None = Field(
        default=None, max_length=CREDENTIAL_MANIFESTS_MAX
    )
    # Spec: Runners rev 3: the pool this Runner belongs to, and its current Pool Key. None
    # from a control plane below 3.26 and for a personal Runner, which belongs to no pool;
    # the Runner reports the fingerprint it holds only once an ack carried this. The ack
    # forbids extra keys, so the control plane sends it only to a Runner whose heartbeat
    # names contracts 3.26 or later.
    pool: PoolTerms | None = None
    # QTS-1773: the control plane's own contracts version, so a Runner sends a field only
    # to a control plane that parses it -- from 3.27, `RunnerInstall.name`. None from a
    # control plane below 3.27, which forbids that key. The ack forbids extra keys, so the
    # control plane sends it only to a Runner whose heartbeat names contracts 3.27 or later.
    contracts_version: Version | None = None

    @model_serializer(mode="wrap")
    def _omit_unsent_fields(self, handler: SerializerFunctionWrapHandler) -> Any:
        # A Runner on 3.18 forbids extra keys, so an ack with nothing to ask sends the
        # body that Runner already parses; likewise `tags` for a Runner on 3.19,
        # `llm_contracts` for one on 3.24, `pool` for one on 3.25 and `contracts_version`
        # for one on 3.26. An empty tag set or Contract list is still sent: it says the
        # Runner has no Tags, or serves no Contract.
        body = handler(self)
        if isinstance(body, dict):
            if not body.get("credential_manifests"):
                body.pop("credential_manifests", None)
            for name in ("tags", "llm_contracts", "pool", "contracts_version"):
                if body.get(name) is None:
                    body.pop(name, None)
        return body


class DirectiveTokenRequest(BaseModel):
    """The Runner asks for one Directive's control-plane token, by id (25 §11).

    Pulled over this stream and never placed in an activity payload, so nothing
    credential-shaped enters Temporal history -- which is what keeps a workflow history
    readable by anyone with namespace access from being a credential store.
    ``directive_id`` is ``{work_record_id}:{directive_number}``, the same id the Usage
    Records carry, so minting can validate it against a Work Record the control plane
    owns and the Runner that Directive was routed to (PRD issue 42; a workflow a rogue
    Runner starts itself matches no such record and earns no token -- 15 §3).
    """

    model_config = ConfigDict(extra="forbid")

    directive_id: Annotated[str, StringConstraints(min_length=3, max_length=128)]


class DirectiveTokenResponse(BaseModel):
    """One Directive's token, which dies with the attempt (25 §11).

    Its TTL is a Directive's own order of magnitude, not a session's: a token that
    outlived the attempt would be a standing credential in a place nothing revokes.
    """

    model_config = ConfigDict(extra="forbid")

    directive_id: str
    token: str
    expires_at: datetime
