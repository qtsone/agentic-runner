"""The bootstrap and heartbeat wire between a Runner and the control plane (PRD issue 41).

ADR-0013 §8 and map ticket 06 §2/§4: a Runner presents an **Agent Token** once, receives a
durable identity, its namespace and its first Runner Token, and from then on says what it
is every 30 s over the same outbound-only stream. Nothing here ever travels inbound into
the org's network -- the Runner dials out, always.

It lives in the *contracts* distribution because this envelope **is** what the Runner
compatibility floor is about (ADR-0013 §7): ``agentic_runner_contracts.__version__`` ticks
when these shapes change, which is exactly what makes "one minor behind warns, two behind
holds" a statement about the wire rather than about a control-plane deploy.

Three rules are enforced by the schema itself rather than by a reviewer:

* **Runner Tags travel once, at bootstrap, and never in a heartbeat body** (map ticket 25
  §9). :class:`HeartbeatEnvelope` has no tag field and forbids extras, so a body carrying
  one fails validation before any handler sees it; :attr:`HeartbeatEnvelope.tag_set_version`
  is how a Runner says *which* tag set it is running.
* **Self-reported facts only, scrubbed of content** (06 §4 as refined by 07). A Directive
  outcome carries a status, an error code, an exception *type* and redacted frames --
  never a payload, a message or a repository path. The patterns below are what "scrubbed"
  means operationally.
* **Host party is never here** (17 A3). It is fixed at registration from the Agent Token
  used and is a routing rule the control plane owns; a Runner cannot assert it.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Annotated, Any, Final
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    StringConstraints,
    model_serializer,
)

from agentic_runner_contracts.channel_messages import TranscriptRequest
from agentic_runner_contracts.llm_usage import UsageRecord
from agentic_runner_contracts.sealed_credential import OpenedCredential, SealedCredential
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


class SlotProbe(StrEnum):
    """The funder-visible probe result for one credential slot (22 A10)."""

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

    ``tags`` is the one and only place Runner Tags cross the wire: free-form
    ``key=value`` the operator declared in config, used for routing (issue 42) and never
    in a queue name, a Search Attribute or a heartbeat body.

    ``build_id`` is the same digest the heartbeat's attestation carries, here so a
    platform that admits only published builds can refuse before it issues an identity
    (runner-repo 06). Optional because a Runner older than this field sends none.
    """

    model_config = ConfigDict(extra="forbid")

    agent_token: Annotated[str, StringConstraints(min_length=16, max_length=256)]
    tags: dict[TagKey, Annotated[str, StringConstraints(max_length=256)]] = Field(
        default_factory=dict, max_length=32
    )
    isolation_mode: IsolationMode
    contracts_version: Version
    runner_version: Version
    recipient_key: RecipientKey
    max_concurrent_directives: int = Field(default=1, ge=1, le=64)
    build_id: BuildId | None = None


class RunnerIdentityMaterial(BaseModel):
    """The durable identity, handed back exactly once (06 §2).

    ``private_key_pem`` exists in this response and nowhere else -- the control plane keeps
    only the public half -- so a Runner that loses its state dir re-bootstraps rather than
    recovering it. It persists to its own Secret/PVC and signs every later heartbeat with
    it, which is what makes the heartbeat unforgeable by anyone holding only the reusable
    Agent Token.
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
    # Agent Token presented (17 A3). Told to the Runner, never declared by it: it is what
    # the Runner asserts a Directive's routing against (PRD issue 42), and a Runner that
    # could set it could reach another contractor's work. Defaulted so a Runner on this
    # wire still parses a response from a control plane that predates issue 42 -- it then
    # has nothing to assert with and simply performs no routing assertion.
    host_party: str = ""
    heartbeat_interval_seconds: int = int(HEARTBEAT_INTERVAL.total_seconds())
    stale_after_seconds: int = int(HEARTBEAT_STALE_AFTER.total_seconds())


class HarnessVersion(BaseModel):
    """One CLI as it stands under one Contract root (ADR-0015 §4, 23 item 5)."""

    model_config = ConfigDict(extra="forbid")

    contract_id: UUID
    cli_kind: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,31}$")]
    version: Annotated[str, StringConstraints(max_length=64)]
    auth_mode: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,31}$")]


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
    """

    model_config = ConfigDict(extra="forbid")

    agent_id: UUID
    version: Annotated[str, StringConstraints(min_length=8, max_length=64)]


class GrantPush(BaseModel):
    """One Agent's Grant snapshot, pushed on change (ADR-0011 §11, PRD issue 44).

    ``snapshot`` is ``services.agents.GrantSnapshotRead`` as JSON -- the same shape the
    ``grant-snapshot`` endpoint serves, because only the *source* changed: the Runner
    parses it with ``GrantSnapshot.from_payload`` and the evaluator is untouched.
    """

    model_config = ConfigDict(extra="forbid")

    agent_id: UUID
    version: Annotated[str, StringConstraints(min_length=8, max_length=64)]
    snapshot: dict[str, object]


class CeilingPush(BaseModel):
    """One Contract's monthly ceilings, on the same channel as the Grants (12 B4).

    ``used`` is the control plane's month-to-date figure at the moment of the push; the
    relocated proxy adds what it has metered since (PRD issue 43's ``CeilingStore``).
    """

    model_config = ConfigDict(extra="forbid")

    contract_id: UUID
    contract_limit: int | None = None
    contract_used: int = 0
    organisation_limit: int | None = None
    organisation_used: int = 0
    # The Organisation's ceiling covers its org-funded Contracts only (map 12 B3).
    org_funded: bool = False


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


class CliVersion(BaseModel):
    """One Agent Runtime CLI found on ``PATH`` -- never bundled on a workstation (23 item 2).

    A version string and whether it clears the Runner's floor for that CLI; never a path,
    which on a workstation carries the user's home directory.
    """

    model_config = ConfigDict(extra="forbid")

    cli_kind: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,31}$")]
    version: Annotated[str, StringConstraints(pattern=r"^[0-9][0-9A-Za-z.+-]{0,63}$")]
    meets_floor: bool


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
    credentials: list[CredentialValidity] = Field(default_factory=list, max_length=64)
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
    # What this Runner has applied, per Agent (PRD issue 44). The control plane pushes
    # exactly what is missing or stale from this list, so a snapshot is pushed once per
    # change per Runner and an unacknowledged one is pushed again on the next beat.
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

    @model_serializer(mode="wrap")
    def _omit_empty_tool_servers(self, handler: SerializerFunctionWrapHandler) -> Any:
        # A control plane on contracts 2.5 forbids extra keys, so a Runner that started no
        # Tool Server sends the body that control plane already parses. One that did needs
        # the control plane on 2.6 first.
        body = handler(self)
        if isinstance(body, dict) and not body.get("tool_servers"):
            body.pop("tool_servers", None)
        return body


class HeartbeatAck(BaseModel):
    """What the control plane says back: a refreshed token, the floor, and any hold."""

    model_config = ConfigDict(extra="forbid")

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
    # The control plane's tag-set version. Disagreeing with the envelope's means the
    # operator edited config; the Runner re-bootstraps rather than mutating tags in place.
    tag_set_version: int
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
