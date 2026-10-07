"""``agentic-runner run``: the Runner process itself (PRD issue 46, ADR-0013 §8).

One process, one identity, many concurrent Directives. It registers once (or re-reads
the identity a previous boot persisted), heartbeats every 30 s over the outbound-only
stream, polls its own ``runner.{runner_id}`` queue for the activities the locality rule
assigns it, and serves its Directives the relocated LLM proxy and the callback socket.
Nothing here is platform code: the loop, the policy authority and the Evidence sink stay
where ADR-0013 §1 put them, and this module imports none of them.

Start-up fails closed before anything registers (17 A2): a Runner declared
``contract_uid`` that cannot change uid exits non-zero naming ``CAP_SETUID``, and its
readiness probe never turns green. Auto-detecting ``none`` instead was rejected for
exactly the case that would make it tempting -- a ``securityContext`` someone edited.

The composition helpers below (``build_*``) are the single place a concrete runtime,
store or isolation is named.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import signal
import tempfile
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

import httpx
from temporalio import activity
from temporalio.client import Client
from temporalio.worker import (
    ActivityInboundInterceptor,
    ExecuteActivityInput,
    Interceptor,
    Worker,
)

from agentic_runner import __version__ as runner_version
from agentic_runner.activities import RunnerRalphActivities
from agentic_runner.child_watcher import install_stop_tolerant_child_watcher
from agentic_runner.config import RunnerConfig
from agentic_runner.config import load as load_config
from agentic_runner.credentials import (
    CredentialResolver,
    DirectoryCredentialStore,
    EmptyCredentialStore,
    HostCredentialStore,
)
from agentic_runner.device_login_activities import ContractDeviceLoginActivities
from agentic_runner.heartbeat_link import WAKE_DIVERGENCE, HeartbeatLink
from agentic_runner.hooks import AttemptFacts, HookName, HookRunner
from agentic_runner.integrations.git.workspace import LocalGitWorkspace
from agentic_runner.integrations.github.gh_client import GitHubAppClient
from agentic_runner.lifecycle import LifecycleOutbox
from agentic_runner.llm_proxy import CeilingStore, LlmProxy, SlotStore, UsageOutbox
from agentic_runner.mcp import ToolServerHealthLog
from agentic_runner.message_store import MESSAGES_DIR, MessageStore, TemporalWorkflowSignaller
from agentic_runner.registration import (
    RunnerRegistrationClient,
    RunnerRegistrationError,
    RunnerState,
    SignedIntakeStream,
    can_separate_uids,
    load_state,
    require_isolation_supported,
    save_state,
)
from agentic_runner.sealed_box import RecipientKeyStore, SealedCredentialStream
from agentic_runner.tiny_http import HttpRequest, read_request, write_json
from agentic_runner.triage_activities import RunnerTriageActivities
from agentic_runner.user_sources import ProxyTriage, UserSourcePoller
from agentic_runner.workers.agent_runtime import AgentRuntime
from agentic_runner.workers.claude_runtime import ClaudeRuntime
from agentic_runner.workers.codex_runtime import CodexRuntime
from agentic_runner.workers.contract_isolation import ContractIsolation
from agentic_runner.workers.fastapi_client import DirectiveTokenSource, RunnerFastApiClient
from agentic_runner.workers.settings import WorkerSettings, get_worker_settings
from agentic_runner_contracts import __version__ as contracts_version
from agentic_runner_contracts.channel_messages import TranscriptDelivery
from agentic_runner_contracts.routing import RunnerRoutingIdentity
from agentic_runner_contracts.runner_registration import (
    RECENT_OUTCOMES_MAX,
    CliVersion,
    DirectiveOutcome,
    EgressPosture,
    HarnessVersion,
    HeartbeatAck,
    HeartbeatEnvelope,
    HostAttestation,
    IsolationMode,
    LifecycleKind,
    RecipientKey,
    ResourcePressure,
)
from agentic_runner_contracts.runtime_context import WorkerRuntimeContextResolver
from agentic_runner_contracts.sealed_credential import key_fingerprint

__all__ = [
    "AGENT_TOKEN_ENV",
    "CONTROL_PLANE_ENV",
    "EGRESS_POSTURE_ENV",
    "ISOLATION_ENV",
    "READINESS_PORT_ENV",
    "RECIPIENT_KEY_ID_ENV",
    "RECIPIENT_PUBLIC_KEY_ENV",
    "TAGS_ENV",
    "TEMPORAL_TLS_ENV",
    "ControlPlaneStream",
    "GracefulWorker",
    "Readiness",
    "build_agent_runtimes",
    "build_contract_isolation",
    "build_credential_resolver",
    "build_hook_runner",
    "build_socket_dir",
    "graceful_shutdown_timeout",
    "parse_tags",
    "recipient_key",
    "register",
    "run",
    "run_runner_lifecycle_hook",
    "run_worker_until_terminated",
]

_logger = logging.getLogger(__name__)

# The Helm values and the workstation installer both land here as environment (ADR-0013
# §8). The Agent Token is read from the environment and never from a flag: a flag would
# put it in the process table for every other uid on the host to read.
AGENT_TOKEN_ENV = "AGENTIC_AGENT_TOKEN"
CONTROL_PLANE_ENV = "AGENTIC_CONTROL_PLANE_URL"
TAGS_ENV = "AGENTIC_RUNNER_TAGS"
ISOLATION_ENV = "AGENTIC_RUNNER_ISOLATION"
EGRESS_POSTURE_ENV = "AGENTIC_RUNNER_EGRESS_POSTURE"
MAX_CONCURRENT_DIRECTIVES_ENV = "AGENTIC_RUNNER_MAX_CONCURRENT_DIRECTIVES"
READINESS_PORT_ENV = "AGENTIC_RUNNER_READINESS_PORT"
TEMPORAL_TLS_ENV = "AGENTIC_TEMPORAL_TLS"
# How often a user-connected Source is read (PRD issue 50). A minute is how often a person
# glances at their inbox; anything tighter spends IMAP logins, not attention.
SOURCE_POLL_SECONDS_ENV = "AGENTIC_RUNNER_SOURCE_POLL_SECONDS"
DEFAULT_SOURCE_POLL_SECONDS = 60.0
# The Recipient Key is normally *generated here*, once per installation, and kept in the
# state directory (PRD issue 48, 22 A4). These two exist for an installer that mints it
# outside this process and are read only when both are set; the Helm chart takes the
# other route -- `recipient-key ensure-secret` installs the release's key into the
# state directory before this process starts, so the store below finds it.
RECIPIENT_KEY_ID_ENV = "AGENTIC_RECIPIENT_KEY_ID"
RECIPIENT_PUBLIC_KEY_ENV = "AGENTIC_RECIPIENT_PUBLIC_KEY"

# The verifier's own Temporal activity ceiling (the platform's workflows/ralph.py,
# _LONG_RUNNING_ACTIVITY_TIMEOUTS["run_verifier"]). A constant rather than an import:
# it bounds a *process* shutdown budget, and the Runner imports no workflow module.
_VERIFIER_ACTIVITY_BUDGET_SECONDS = 20 * 60
# Margin so the Temporal SDK's own graceful-shutdown timer expires and Worker.run()
# returns before Kubernetes SIGKILLs the pod at terminationGracePeriodSeconds (the
# chart's value and this must move together).
_SHUTDOWN_HEADROOM_SECONDS = 60

# What `agentic-runner status <org>` reads (PRD issue 47): the live pid, and a file the
# process touches on every acknowledged heartbeat, whose mtime is the heartbeat age.
PIDFILE_NAME = "runner.pid"
HEARTBEAT_STAMP_NAME = "last-heartbeat"


# ------------------------------------------------------------------ environment


def parse_tags(raw: str) -> dict[str, str]:
    """``region=eu-west-1,gpu=none`` -> a Runner Tag set (25 §9).

    Free-form by design, and sent exactly once at bootstrap: a tag never enters a queue
    name, a Search Attribute or a heartbeat body.
    """

    tags: dict[str, str] = {}
    for pair in raw.split(","):
        if not pair.strip():
            continue
        key, separator, value = pair.partition("=")
        if not separator:
            raise ValueError(f"Runner Tag {pair!r} is not key=value")
        tags[key.strip()] = value.strip()
    return tags


def isolation_mode() -> IsolationMode:
    return IsolationMode(os.environ.get(ISOLATION_ENV, "").strip() or IsolationMode.CONTRACT_UID)


def recipient_key(state_dir: Path) -> tuple[RecipientKey, str]:
    """This installation's Recipient Key and its fingerprint (22 A4).

    Generated on first call and never again: the private half stays in the state directory
    at 0600 and the platform only ever relays the public one. An operator-supplied pair
    (both env vars) wins, because an installer that minted the key elsewhere is stating
    which key this installation is.
    """

    key_id = os.environ.get(RECIPIENT_KEY_ID_ENV, "").strip()
    public_key = os.environ.get(RECIPIENT_PUBLIC_KEY_ENV, "").strip()
    if key_id and public_key:
        return RecipientKey(key_id=key_id, public_key=public_key), key_fingerprint(public_key)
    generated = RecipientKeyStore(state_dir).current()
    return (
        RecipientKey(key_id=generated.key_id, public_key=generated.public_key),
        generated.fingerprint,
    )


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RunnerRegistrationError("configuration_missing", f"{name} is not set")
    return value


def _max_concurrent_directives() -> int:
    return int(os.environ.get(MAX_CONCURRENT_DIRECTIVES_ENV, "").strip() or 1)


async def register(
    *,
    state_dir: Path,
    can_separate_uids: bool,
    client: httpx.AsyncClient | None = None,
    control_plane: str | None = None,
    agent_token: str | None = None,
    isolation: IsolationMode | None = None,
) -> tuple[RunnerState, str]:
    """First boot (PRD issue 41): exchange the Agent Token for this process's identity.

    Idempotent by state file -- a restart re-reads what is already there rather than
    registering a second Runner against the Organisation's cap (21 A7). The keyword
    overrides are the workstation ``install`` (issue 47), which takes the Agent Token
    pasted at a prompt rather than from a Helm-set environment.
    """

    existing = load_state(state_dir)
    if existing is not None:
        return existing, f"already registered {existing.runner_id} on {existing.task_queue}"
    recipient, fingerprint = recipient_key(state_dir)
    registration = RunnerRegistrationClient(
        base_url=control_plane or _require_env(CONTROL_PLANE_ENV), client=client
    )
    state, response = await registration.bootstrap(
        agent_token=agent_token or _require_env(AGENT_TOKEN_ENV),
        tags=parse_tags(os.environ.get(TAGS_ENV, "")),
        isolation_mode=isolation or isolation_mode(),
        recipient_key=recipient,
        max_concurrent_directives=_max_concurrent_directives(),
        can_separate_uids=can_separate_uids,
    )
    save_state(state_dir, state)
    return state, (
        f"registered {state.runner_id} in namespace {state.temporal_namespace} "
        f"on {state.task_queue}; contracts floor {response.contracts_floor} "
        f"({response.floor_state.value}); Recipient Key {fingerprint}"
    )


# ------------------------------------------------------------------ composition


def build_contract_isolation(
    settings: WorkerSettings, *, can_separate_uids: bool | None = None
) -> ContractIsolation:
    """The Runner's uid allocator and Contract directory owner (ADR-0015 §1-§3)."""

    return ContractIsolation(
        workspace_root=_setting(settings, "WORKSPACE_ROOT", Path("/var/lib/agentic-os/workspaces")),
        state_dir=_setting(settings, "WORKER_STATE_DIR", Path("/var/lib/agentic-os/state")),
        uid_min=_setting(settings, "CONTRACT_UID_MIN", 60_000),
        uid_max=_setting(settings, "CONTRACT_UID_MAX", 60_999),
        max_processes=_setting(settings, "CONTRACT_MAX_PROCESSES", 512),
        memory_limit_bytes=_setting(settings, "CONTRACT_MEMORY_LIMIT_BYTES", 4 * 1024**3),
        can_separate_uids=can_separate_uids,
    )


def _setting(settings: object, name: str, default: Any) -> Any:
    return getattr(settings, name, default)


def build_hook_runner(config: RunnerConfig) -> HookRunner:
    """Load the Runner Hooks once, at the composition root (PRD issue 45).

    Once, not per Directive: `load_hooks` refuses a filename that is not a catalogue slot,
    and an operator's typo should stop the Runner starting rather than fail one Work
    Record at a time, hours later, on whichever pod happened to pick it up.
    """

    return HookRunner(hooks_path=config.hooks_path)


def build_credential_resolver(
    config: RunnerConfig, *, host_store: HostCredentialStore | None = None
) -> CredentialResolver:
    """The host store a Directive resolves its Credential References from (22 A1).

    No store configured resolves to `EmptyCredentialStore` rather than to no resolver at
    all -- "nothing is installed" is the fail-closed reading. Values a funder sealed land
    in this same resolver, under the same reference name, from the heartbeat stream.
    """

    store: HostCredentialStore = host_store or (
        DirectoryCredentialStore(config.credential_store)
        if config.credential_store is not None
        else EmptyCredentialStore()
    )
    return CredentialResolver(store=store)


def build_socket_dir(config: RunnerConfig) -> Path:
    """Where per-attempt callback sockets live, resolved once.

    ``sun_path`` is ~104 bytes, so the sockets need a short root of their own. A Runner
    that cannot create the configured one (a read-only `/run`, a developer's laptop)
    falls back to a private temp directory here, at start-up, so a callback socket is
    never the reason a Work Record fails.
    """

    try:
        config.socket_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        fallback = Path(tempfile.mkdtemp(prefix="agentic-runner-sockets-"))
        # 0755, not mkdtemp's 0700: the per-attempt subdirectory below is chowned to the
        # Contract's uid, and that uid cannot traverse a parent only the Runner may enter.
        fallback.chmod(0o755)
        _logger.warning("callback socket dir %s unusable; using %s", config.socket_dir, fallback)
        return fallback
    return config.socket_dir


_RUNTIMES: dict[str, Callable[[WorkerSettings], AgentRuntime]] = {
    "claude_code": lambda settings: ClaudeRuntime(settings=settings),
    "codex_cli": lambda settings: CodexRuntime(settings=settings),
}


def build_agent_runtimes(
    settings: WorkerSettings, clis: Sequence[CliVersion]
) -> dict[str, AgentRuntime]:
    """Every Agent Runtime this process serves, by the Profile's `cli_kind` (ADR-0006).

    The one place a concrete runtime is named; everything downstream depends only on the
    port. One process serves every CLI it found that clears its floor, and each Directive
    picks from here by the Work Record's `cli_kind` (local-agents 01) -- there is no
    process-wide choice. The same `clis` ride the heartbeat's attestation, so what this
    Runner serves and what routing believes it serves are one list.
    """

    return {
        cli.cli_kind: _RUNTIMES[cli.cli_kind](settings)
        for cli in clis
        if cli.meets_floor and cli.cli_kind in _RUNTIMES
    }


async def run_runner_lifecycle_hook(hooks: HookRunner, name: HookName) -> None:
    """``runner_startup`` / ``runner_shutdown``: the two slots outside a Directive.

    They run as the Runner's own uid and in its own working directory, because there is
    no Contract yet and no Workspace to stand in. A non-zero exit is logged, never fatal:
    refusing to start a Runner on a bad cleanup script would take an Organisation's whole
    fleet offline.
    """

    run = await hooks.run(name, cwd=Path.cwd(), facts=AttemptFacts())
    if run is not None:
        _logger.info("%s hook exited %d in %dms", name.value, run.exit_code, run.duration_ms)


def graceful_shutdown_timeout(settings: WorkerSettings) -> timedelta:
    """One Directive's worst case: the CLI execution ceiling plus the verifier budget.

    Sized so ``Worker.shutdown()`` (issue 02) waits out a real in-flight Directive
    instead of cancelling it once this timer lapses.
    """

    cli_timeout_seconds = max(
        settings.CODEX_CLI_TIMEOUT_SECONDS, settings.CLAUDE_CLI_TIMEOUT_SECONDS
    )
    return timedelta(
        seconds=cli_timeout_seconds + _VERIFIER_ACTIVITY_BUDGET_SECONDS - _SHUTDOWN_HEADROOM_SECONDS
    )


class GracefulWorker(Protocol):
    """The subset of ``temporalio.worker.Worker`` the shutdown drain depends on."""

    async def run(self) -> None: ...
    async def shutdown(self) -> None: ...


async def run_worker_until_terminated(
    worker: GracefulWorker, *, stop: asyncio.Event | None = None
) -> None:
    """Poll until SIGTERM (or ``stop``), then drain: stop polling, let the running
    activity finish.

    Python's default SIGTERM disposition kills the process outright, discarding whatever
    Directive is mid-run. The chart's ``preStop`` hook (issue 02) signals termination as
    early as possible; this handler is what turns that into ``Worker.shutdown()`` -- stop
    polling for new work, wait for the in-flight activity, then let ``run()`` return.
    """

    loop = asyncio.get_running_loop()
    stop_event = stop or asyncio.Event()
    loop.add_signal_handler(signal.SIGTERM, stop_event.set)
    try:
        run_task = asyncio.ensure_future(worker.run())
        stop_task = asyncio.ensure_future(stop_event.wait())
        await asyncio.wait({run_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
        if not stop_task.done():
            stop_task.cancel()
        if not run_task.done():
            _logger.info("Worker received SIGTERM: draining in-flight activity before exit")
            await worker.shutdown()
        await run_task
    finally:
        loop.remove_signal_handler(signal.SIGTERM)


# ------------------------------------------------------------------ the stream


class _LoadInterceptor(Interceptor):
    """Counts the Directives in flight and keeps the last-N outcomes (06 §4)."""

    def __init__(self) -> None:
        self.in_flight = 0
        self.outcomes: list[DirectiveOutcome] = []

    def intercept_activity(self, next: ActivityInboundInterceptor) -> ActivityInboundInterceptor:
        return _LoadInbound(next, self)


class _LoadInbound(ActivityInboundInterceptor):
    def __init__(self, next: ActivityInboundInterceptor, load: _LoadInterceptor) -> None:
        super().__init__(next)
        self._load = load

    async def execute_activity(self, input: ExecuteActivityInput) -> Any:
        self._load.in_flight += 1
        name = activity.info().activity_type
        try:
            result = await self.next.execute_activity(input)
        except Exception as error:
            self._record(name, "failed", type(error).__name__)
            raise
        else:
            self._record(name, "succeeded", None)
            return result
        finally:
            self._load.in_flight -= 1

    def _record(self, name: str, status: str, exception_type: str | None) -> None:
        self._load.outcomes.append(
            DirectiveOutcome(activity=name, status=status, exception_type=exception_type)
        )
        del self._load.outcomes[:-RECENT_OUTCOMES_MAX]


def resource_pressure(path: Path) -> ResourcePressure:
    """Enough to answer "wedged or merely busy" (06 §4), from what the OS offers."""

    cpu = 0.0
    if hasattr(os, "getloadavg"):
        cpu = min(100.0, os.getloadavg()[0] / (os.cpu_count() or 1) * 100.0)
    memory = 0.0
    try:
        fields = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
        total = float(fields["MemTotal"].split()[0])
        available = float(fields["MemAvailable"].split()[0])
        memory = max(0.0, min(100.0, (1 - available / total) * 100.0))
    except (OSError, KeyError, ValueError, ZeroDivisionError):
        pass
    try:
        usage = shutil.disk_usage(path)
        disk = usage.used / usage.total * 100.0 if usage.total else 0.0
    except OSError:
        disk = 0.0
    return ResourcePressure(cpu_percent=cpu, memory_percent=memory, disk_percent=disk)


@dataclass
class ControlPlaneStream:
    """One heartbeat exchange, as ``HeartbeatLink`` drives it (PRD issues 41, 43, 44, 48).

    Everything that rides the stream is assembled here from the stores that produced it:
    the proxy's Usage Records and slot fields, the sealed values opened since the last
    beat, the applied Grant snapshots the link hands in. What comes back is applied in
    the same place -- usage acknowledged, sealed values opened into the resolver, the
    refreshed Runner Token kept for the Temporal connection.
    """

    client: RunnerRegistrationClient
    state: RunnerState
    isolation: IsolationMode
    proxy: LlmProxy
    sealed: SealedCredentialStream
    credentials: CredentialResolver
    load: _LoadInterceptor
    egress_posture: EgressPosture = EgressPosture.UNRESTRICTED
    tag_set_version: int = 1
    runner_token: str | None = None
    runner_token_expires_at: datetime | None = None
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    # Issue 47: the self-reported build and host facts, the lifecycle facts waiting for an
    # ack, and where `status` reads the heartbeat age from.
    attestation: HostAttestation | None = None
    lifecycle: LifecycleOutbox | None = None
    heartbeat_stamp: Path | None = None
    monotonic: Callable[[], float] = time.monotonic
    last_sleep_at: datetime | None = None
    last_wake_at: datetime | None = None
    # PRD issue 50: the user-connected Sources this Runner reads, re-assigned every ack.
    sources: UserSourcePoller | None = None
    # PRD issue 52: where a transcript pull-through relayed on the ack is answered from.
    messages: MessageStore | None = None
    # Console-v2 issue 29: the latest start of each Runner-hosted Tool Server.
    tool_servers: ToolServerHealthLog | None = None
    _delivered: set[tuple[str, str]] = field(default_factory=set)
    _last_seen: tuple[float, datetime] | None = None

    def _observe_sleep(self, now: datetime) -> None:
        """A wall clock that ran ahead of the monotonic one is a suspend (23 item 4).

        Recorded, never acted on here: the attempt a sleep outlived is failed by its own
        Temporal heartbeat timeout, and the verb seams re-heartbeat before acting (44).
        """

        monotonic = self.monotonic()
        if self._last_seen is not None:
            seen_monotonic, seen_wall = self._last_seen
            awake = monotonic - seen_monotonic
            if (now - seen_wall).total_seconds() - awake > WAKE_DIVERGENCE:
                self.last_sleep_at = seen_wall + timedelta(seconds=awake)
                self.last_wake_at = now
                if self.lifecycle is not None:
                    self.lifecycle.add(LifecycleKind.WAKE, now, slept_at=self.last_sleep_at)
        self._last_seen = (monotonic, now)

    def _clock_skew(self, now: datetime) -> float:
        server = self.client.server_date
        return 0.0 if server is None else round((now - server).total_seconds(), 1)

    def _harnesses(self) -> list[HarnessVersion]:
        """Per Contract: the CLI's version and whether it runs on a delivered key (22 A8)
        or on the login its funder created in the harness root -- read off the slot, never
        off the root, which the Runner does not open."""

        if self.attestation is None:
            return []
        versions = {cli.cli_kind: cli.version for cli in self.attestation.clis}
        return [
            HarnessVersion(
                contract_id=slot.contract_id,
                cli_kind=slot.runtime_kind,
                version=versions.get(slot.runtime_kind, "unknown"),
                auth_mode="api_key" if slot.present else "login",
            )
            for slot in self.proxy.slots.statuses()
        ]

    async def directive_token(self, directive_id: str) -> str:
        """One activity execution's control-plane token, by Directive id (PRD issue 63)."""

        return (await self.client.request_directive_token(self.state, directive_id)).token

    async def exchange(self, applied: Sequence[Any]) -> HeartbeatAck:
        now = self.clock()
        self._observe_sleep(now)
        lifecycle = self.lifecycle.pending() if self.lifecycle is not None else []
        renewed, resealed = self.sealed.renew(now)
        envelope = HeartbeatEnvelope(
            runner_version=runner_version,
            contracts_version=contracts_version,
            resource_pressure=resource_pressure(Path.cwd()),
            max_concurrent_directives=self.state.max_concurrent_directives,
            current_load=self.load.in_flight,
            recent_outcomes=list(self.load.outcomes),
            slots=self.proxy.slots.statuses(),
            usage=self.proxy.outbox.pending(),
            egress_posture=self.egress_posture,
            isolation_mode=self.isolation,
            tag_set_version=self.tag_set_version,
            hosted_task_queue=self.state.task_queue,
            recipient_key=renewed,
            resealed=resealed,
            opened=self.sealed.take_opened(),
            applied_snapshots=list(applied),
            harnesses=self._harnesses(),
            attestation=self.attestation,
            lifecycle=lifecycle,
            last_sleep_at=self.last_sleep_at,
            last_wake_at=self.last_wake_at,
            clock_skew_seconds=self._clock_skew(now),
            source_status=self.sources.statuses() if self.sources is not None else [],
            tool_servers=self.tool_servers.latest() if self.tool_servers is not None else [],
        )
        ack = await self.client.heartbeat(self.state, envelope)
        if self.sources is not None:
            self.sources.assign(ack.user_sources)
        await self._deliver_transcripts(ack)
        if self.lifecycle is not None:
            self.lifecycle.acknowledge(lifecycle)
        if self.heartbeat_stamp is not None:
            self.heartbeat_stamp.touch()
        self.proxy.outbox.acknowledge(ack.usage_accepted)
        for refused in self.sealed.apply(ack.sealed_credentials):
            _logger.warning(
                "sealed credential %s/%s could not be opened with this installation's key",
                refused.contract_id,
                refused.slot,
            )
        self._sync_delivered()
        if ack.runner_token is not None:
            self.runner_token = ack.runner_token
            self.runner_token_expires_at = ack.runner_token_expires_at
        self.tag_set_version = ack.tag_set_version
        if ack.hold_reason:
            _logger.warning("control plane holds new Directives: %s", ack.hold_reason)
        return ack

    async def _deliver_transcripts(self, ack: HeartbeatAck) -> None:
        """The pull-through's Runner half (PRD issue 52, map ticket 10).

        The bodies go back over this same signed stream and nowhere else; a request this
        Runner cannot answer (no store, a sealed Contract) is answered with the reason so
        the console can say so rather than time out.
        """

        for request in ack.transcript_requests:
            delivery = (
                self.messages.transcript(request)
                if self.messages is not None
                else TranscriptDelivery(
                    request_id=request.request_id,
                    work_record_id=request.work_record_id,
                    refused="no_store",
                )
            )
            try:
                await self.client.deliver_transcript(self.state, delivery)
            except Exception as error:  # noqa: BLE001 - a lost delivery is the console's timeout
                _logger.warning(
                    "transcript %s could not be delivered: %s", request.request_id, error
                )

    def _sync_delivered(self) -> None:
        """The resolver holds exactly what the last ack let this installation open."""

        plaintext = self.sealed.plaintext
        for (contract_id, slot), value in plaintext.items():
            self.credentials.deliver(contract_id, slot, value)
        for contract_id, slot in self._delivered - set(plaintext):
            self.credentials.drop(contract_id, slot)
        self._delivered = set(plaintext)


# ------------------------------------------------------------------ readiness


class Readiness:
    """``GET /healthz``: 503 until this Runner is registered, heard and polling."""

    def __init__(self, port: int) -> None:
        self._port = port
        self.ready = False
        self._server: asyncio.AbstractServer | None = None

    @property
    def port(self) -> int:
        return self._port

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._serve, host="0.0.0.0", port=self._port)
        self._port = self._server.sockets[0].getsockname()[1]

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = await read_request(reader)
            if not isinstance(request, HttpRequest):
                await write_json(writer, *request)
            elif self.ready:
                await write_json(writer, 200, {"ready": True})
            else:
                await write_json(writer, 503, {"ready": False})
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()


# ------------------------------------------------------------------ the process

Connector = Callable[..., Awaitable[Client]]
WorkerFactory = Callable[..., GracefulWorker]


def _temporal_worker(client: Client, **kwargs: Any) -> Worker:
    return Worker(client, **kwargs)


async def run(
    *,
    http_client: httpx.AsyncClient | None = None,
    connect: Connector = Client.connect,
    worker_factory: WorkerFactory = _temporal_worker,
    can_change_uid: bool | None = None,
    stop: asyncio.Event | None = None,
    heartbeat_interval: float | None = None,
    readiness: Readiness | None = None,
    host_store: HostCredentialStore | None = None,
    attestation: HostAttestation | None = None,
) -> int:
    """The Runner process. Returns the exit code.

    The seams are injectable for the test that stands two of these up against a fake
    control plane: the HTTP transport, the Temporal connection, the Worker and the
    capability check. Production passes none of them. ``host_store`` and
    ``attestation`` are the workstation's (issue 47): its OS credential store and what
    it says about itself. ``heartbeat_interval`` defaults to the cadence bootstrap gave.
    """

    install_stop_tolerant_child_watcher(asyncio.get_running_loop())
    config = load_config()
    mode = isolation_mode()
    can = can_separate_uids() if can_change_uid is None else can_change_uid
    try:
        require_isolation_supported(mode, can_separate_uids=can)
        control_plane = _require_env(CONTROL_PLANE_ENV)
    except RunnerRegistrationError as error:
        print(f"refusing to start ({error.reason}): {error}", flush=True)
        return 1
    # The control plane the activities call back is the one this process registered
    # with, unless the operator points them at an internal address explicitly.
    os.environ.setdefault("INTERNAL_FASTAPI_BASE_URL", control_plane)
    settings = get_worker_settings()
    state_dir = config.state_dir

    readiness = readiness or Readiness(int(os.environ.get(READINESS_PORT_ENV, "").strip() or 0))
    await readiness.start()
    try:
        return await _serve(
            config=config,
            settings=settings,
            mode=mode,
            can_change_uid=can,
            control_plane=control_plane,
            state_dir=state_dir,
            readiness=readiness,
            http_client=http_client,
            connect=connect,
            worker_factory=worker_factory,
            stop=stop,
            heartbeat_interval=heartbeat_interval,
            host_store=host_store,
            attestation=attestation,
        )
    finally:
        await readiness.close()


async def _serve(
    *,
    config: RunnerConfig,
    settings: WorkerSettings,
    mode: IsolationMode,
    can_change_uid: bool,
    control_plane: str,
    state_dir: Path,
    readiness: Readiness,
    http_client: httpx.AsyncClient | None,
    connect: Connector,
    worker_factory: WorkerFactory,
    stop: asyncio.Event | None,
    heartbeat_interval: float | None,
    host_store: HostCredentialStore | None,
    attestation: HostAttestation | None,
) -> int:
    try:
        state, outcome = await register(
            state_dir=state_dir, can_separate_uids=can_change_uid, client=http_client
        )
    except RunnerRegistrationError as error:
        print(f"registration refused ({error.reason}): {error}", flush=True)
        return 1
    print(outcome, flush=True)
    interval = (
        heartbeat_interval if heartbeat_interval is not None else state.heartbeat_interval_seconds
    )
    # `status` reads these two (issue 47); the process is the only writer of either.
    pidfile = state_dir / PIDFILE_NAME
    pidfile.write_text(str(os.getpid()), encoding="utf-8")
    lifecycle = LifecycleOutbox(state_dir)
    lifecycle.add(LifecycleKind.START, datetime.now(UTC))
    try:
        return await _serve_registered(
            config=config,
            settings=settings,
            state=state,
            mode=mode,
            can_change_uid=can_change_uid,
            control_plane=control_plane,
            state_dir=state_dir,
            readiness=readiness,
            http_client=http_client,
            connect=connect,
            worker_factory=worker_factory,
            stop=stop,
            interval=interval,
            host_store=host_store,
            attestation=attestation,
            lifecycle=lifecycle,
        )
    finally:
        pidfile.unlink(missing_ok=True)


async def _serve_registered(
    *,
    config: RunnerConfig,
    settings: WorkerSettings,
    state: RunnerState,
    mode: IsolationMode,
    can_change_uid: bool,
    control_plane: str,
    state_dir: Path,
    readiness: Readiness,
    http_client: httpx.AsyncClient | None,
    connect: Connector,
    worker_factory: WorkerFactory,
    stop: asyncio.Event | None,
    interval: float,
    host_store: HostCredentialStore | None,
    attestation: HostAttestation | None,
    lifecycle: LifecycleOutbox,
) -> int:
    load = _LoadInterceptor()
    proxy = LlmProxy(slots=SlotStore(), ceilings=CeilingStore(), outbox=UsageOutbox())
    credentials = build_credential_resolver(config, host_store=host_store)
    stream = ControlPlaneStream(
        client=RunnerRegistrationClient(base_url=control_plane, client=http_client),
        state=state,
        isolation=mode,
        proxy=proxy,
        sealed=SealedCredentialStream(RecipientKeyStore(state_dir)),
        credentials=credentials,
        load=load,
        egress_posture=EgressPosture(
            os.environ.get(EGRESS_POSTURE_ENV, "").strip() or EgressPosture.UNRESTRICTED
        ),
        attestation=attestation,
        lifecycle=lifecycle,
        heartbeat_stamp=state_dir / HEARTBEAT_STAMP_NAME,
    )
    stream.sources = UserSourcePoller(
        stream=SignedIntakeStream(stream.client, state),
        store=credentials.host_store,
        triage=ProxyTriage(
            proxy=proxy,
            model=settings.TRIAGE_MODEL,
            timeout_seconds=settings.TRIAGE_DIRECTIVE_TIMEOUT_SECONDS,
        ),
    )
    messages = MessageStore(state_dir / MESSAGES_DIR)
    stream.messages = messages
    tool_servers = ToolServerHealthLog()
    stream.tool_servers = tool_servers
    link = HeartbeatLink(stream, ceilings=proxy.ceilings)
    # The first beat is the one that hands back a Runner Token: a persisted identity has
    # none, and the Temporal connection below cannot be opened without it.
    while True:
        try:
            await link.exchange()
            break
        except Exception as error:  # noqa: BLE001 - keep trying until the control plane answers
            _logger.warning("heartbeat failed before start: %s", error)
            if stop is not None and stop.is_set():
                return 1
            await asyncio.sleep(interval)

    client = await connect(
        settings.TEMPORAL_ADDRESS,
        namespace=state.temporal_namespace,
        api_key=stream.runner_token,
        tls=os.environ.get(TEMPORAL_TLS_ENV, "").strip().lower() in {"1", "true", "yes"},
    )
    hooks = build_hook_runner(config)
    async with (
        proxy,
        RunnerFastApiClient(str(settings.INTERNAL_FASTAPI_BASE_URL), state=state) as fastapi,
    ):
        worker = worker_factory(
            client,
            task_queue=state.task_queue,
            activities=_activities(
                fastapi,
                settings=settings,
                config=config,
                state=state,
                can_change_uid=can_change_uid,
                hooks=hooks,
                proxy=proxy,
                credentials=credentials,
                link=link,
                attestation=attestation,
                token_source=stream,
                messages=messages,
                tool_servers=tool_servers,
                # The wake signal rides the connection this process polls with: one
                # namespace, the Organisation's own (PRD issue 52, map ticket 15 §2).
                signaller=TemporalWorkflowSignaller(client),
            ),
            interceptors=[load],
            graceful_shutdown_timeout=graceful_shutdown_timeout(settings),
            max_concurrent_activities=state.max_concurrent_directives,
        )
        beat = asyncio.create_task(_heartbeat_forever(link, stream, client, interval))
        poll = asyncio.create_task(_poll_sources_forever(stream.sources, _source_poll_seconds()))
        readiness.ready = True
        await run_runner_lifecycle_hook(hooks, HookName.RUNNER_STARTUP)
        try:
            await run_worker_until_terminated(worker, stop=stop)
        finally:
            readiness.ready = False
            for task in (beat, poll):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await run_runner_lifecycle_hook(hooks, HookName.RUNNER_SHUTDOWN)
            await _report_stop(link, lifecycle)
    return 0


async def _report_stop(link: HeartbeatLink, lifecycle: LifecycleOutbox) -> None:
    """One last beat carrying ``stop``; if it cannot land, the next start carries it."""

    lifecycle.add(LifecycleKind.STOP, datetime.now(UTC))
    try:
        await asyncio.wait_for(link.exchange(), timeout=10)
    except Exception as error:  # noqa: BLE001 - the fact stays queued in the state dir
        _logger.warning("stop not reported yet (%s); the next start carries it", error)


async def _heartbeat_forever(
    link: HeartbeatLink, stream: ControlPlaneStream, client: Client, interval: float
) -> None:
    """Every 30 s, for as long as the process lives; a missed beat is logged, not fatal.

    Staleness is the link's own property (issue 44): after three minutes of failures the
    verb seams refuse on their own, so there is nothing for this loop to do about a
    failure except try again on the next tick.
    """

    while True:
        await asyncio.sleep(interval)
        token_before = stream.runner_token
        try:
            await link.exchange()
        except Exception as error:  # noqa: BLE001 - any failed exchange is a lost beat
            _logger.warning("heartbeat failed: %s", error)
            continue
        if stream.runner_token is not None and stream.runner_token != token_before:
            client.api_key = stream.runner_token


def _source_poll_seconds() -> float:
    raw = os.environ.get(SOURCE_POLL_SECONDS_ENV, "").strip()
    return float(raw) if raw else DEFAULT_SOURCE_POLL_SECONDS


async def _poll_sources_forever(poller: UserSourcePoller, interval: float) -> None:
    """Read every assigned user-connected Source once per interval (PRD issue 50).

    Nothing to read until the first ack assigns something; a failed poll is the poller's
    own business (it records the Source's status), never this loop's.
    """

    while True:
        await asyncio.sleep(interval)
        await poller.poll()


def _activities(
    fastapi: RunnerFastApiClient,
    *,
    settings: WorkerSettings,
    config: RunnerConfig,
    state: RunnerState,
    can_change_uid: bool,
    hooks: HookRunner,
    proxy: LlmProxy,
    credentials: CredentialResolver,
    link: HeartbeatLink,
    attestation: HostAttestation | None,
    token_source: DirectiveTokenSource | None = None,
    messages: MessageStore | None = None,
    signaller: TemporalWorkflowSignaller | None = None,
    tool_servers: ToolServerHealthLog | None = None,
) -> list[Callable[..., Any]]:
    github_client = GitHubAppClient(
        app_id=os.getenv("GITHUB_APP_ID"),
        installation_id=os.getenv("GITHUB_APP_INSTALLATION_ID"),
        private_key_pem=os.getenv("GITHUB_APP_PRIVATE_KEY_PEM"),
        api_base_url=os.getenv("GITHUB_API_BASE_URL", "https://api.github.com"),
    )
    static_git_token = os.getenv("AGENTIC_OS_GIT_TOKEN") or None
    isolation = build_contract_isolation(settings, can_separate_uids=can_change_uid)
    ralph = RunnerRalphActivities(
        fastapi,
        runtime_context_resolver=WorkerRuntimeContextResolver(fastapi),
        agent_runtimes=build_agent_runtimes(
            settings, attestation.clis if attestation is not None else []
        ),
        git_workspace=LocalGitWorkspace(
            git_token=static_git_token,
            git_token_provider=(
                None if static_git_token else github_client.installation_token_provider()
            ),
            command_timeout_seconds=settings.CODEX_CLI_TIMEOUT_SECONDS,
            output_limit_bytes=settings.CODEX_CLI_OUTPUT_LIMIT_BYTES,
        ),
        github_client=github_client,
        workspace_root=settings.WORKSPACE_ROOT,
        contract_isolation=isolation,
        # The host party is the control plane's to assign (17 A3); a state file written
        # before issue 42 carries none, and such a process makes no routing assertion.
        routing_identity=(
            RunnerRoutingIdentity(
                runner_id=str(state.runner_id), host_party=state.host_party, tags=dict(state.tags)
            )
            if state.host_party
            else None
        ),
        hooks=hooks,
        socket_dir=build_socket_dir(config),
        llm_proxy=proxy,
        credentials=credentials,
        heartbeat_link=link,
        token_source=token_source,
        message_store=messages,
        workflow_signaller=signaller,
        tool_server_health=tool_servers,
    )
    return [
        *ralph.activity_callables(),
        *ContractDeviceLoginActivities(contract_isolation=isolation).activity_callables(),
        *RunnerTriageActivities(
            proxy=proxy,
            model=settings.TRIAGE_MODEL,
            timeout_seconds=settings.TRIAGE_DIRECTIVE_TIMEOUT_SECONDS,
        ).activity_callables(),
    ]
