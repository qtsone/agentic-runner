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
import getpass
import hashlib
import logging
import os
import shutil
import signal
import sys
import tempfile
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from ipaddress import IPv4Address, IPv6Address, ip_address
from pathlib import Path
from typing import Any, Final, Literal
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, UUID, uuid5

import httpx
from pydantic import ValidationError
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
from agentic_runner.attempts import AttemptRecords
from agentic_runner.auth_mode import runs_subscriptions
from agentic_runner.build import build_id
from agentic_runner.child_watcher import install_stop_tolerant_child_watcher
from agentic_runner.config import RunnerConfig
from agentic_runner.config import load as load_config
from agentic_runner.connections import (
    ConnectionSlots,
    ConnectionWorkers,
    GracefulWorker,
    MachineSlots,
)
from agentic_runner.credentials import (
    CredentialResolver,
    DirectoryCredentialStore,
    HostCredentialStore,
    LayeredCredentialStore,
)
from agentic_runner.harness_self_test import DirectivesInFlight, HarnessSelfTests
from agentic_runner.heartbeat_link import WAKE_DIVERGENCE, HeartbeatLink
from agentic_runner.hooks import AttemptFacts, HookName, HookRunner
from agentic_runner.host_store import FileCredentialStore
from agentic_runner.integrations.git.workspace import LocalGitWorkspace
from agentic_runner.integrations.github.gh_client import GitHubAppClient
from agentic_runner.integrations.oauth import HttpOAuthProvider
from agentic_runner.lifecycle import LIFECYCLE_FILENAME, LifecycleOutbox
from agentic_runner.llm_contracts import (
    LLM_CONTRACTS_FILENAME,
    load_llm_contracts,
    save_llm_contracts,
)
from agentic_runner.llm_proxy import (
    LLM_SLOT_REFERENCES,
    CeilingStore,
    LlmEndpoint,
    LlmProxy,
    SlotStore,
    UsageOutbox,
    llm_slot_references,
)
from agentic_runner.mcp import ToolServerHealthLog
from agentic_runner.message_store import MESSAGES_DIR, MessageStore, TemporalWorkflowSignaller
from agentic_runner.oauth_connectors import OAuthConnectors
from agentic_runner.organisation_name import save_organisation_name
from agentic_runner.pool_key import (
    POOL_KEY_ENV,
    POOL_KEY_FILENAME,
    LlmKeyOpener,
    PoolKey,
    PoolKeyError,
    load_pool_key,
)
from agentic_runner.private_state import (
    UnsafeStateError,
    ensure_private_dir,
    private_write,
    require_private_files,
)
from agentic_runner.registration import (
    REGISTRATION_TOKEN_REFUSED_HINT,
    RUNNER_REVOKED_REASON,
    STATE_FILENAME,
    RunnerRegistrationClient,
    RunnerRegistrationError,
    RunnerState,
    SignedIntakeStream,
    can_separate_uids,
    load_state,
    require_isolation_supported,
    save_state,
)
from agentic_runner.runner_limit import (
    EMPTY_RUNNER_LIMIT,
    AppliedRunnerLimit,
    DeniedFolders,
    RunnerLimitError,
)
from agentic_runner.sealed_box import (
    RECIPIENT_KEY_FILENAME,
    RecipientKeyStore,
    SealedCredentialStream,
)
from agentic_runner.sign_in import KeyStore, SignIn
from agentic_runner.tiny_http import HttpRequest, read_request, write_json
from agentic_runner.triage_activities import RunnerTriageActivities
from agentic_runner.usage_windows import (
    ClaudeUsageScreenProbe,
    CodexRateLimitsProbe,
    UsageWindowProbe,
    UsageWindows,
)
from agentic_runner.user_sources import ProxyTriage, UserSourcePoller
from agentic_runner.workers.acp_runtime import ACP_BRIDGES, AcpRuntime, profile_acp_executables
from agentic_runner.workers.agent_runtime import AgentRuntime
from agentic_runner.workers.claude_runtime import ClaudeRuntime
from agentic_runner.workers.claude_sign_in import OAUTH_TOKEN, ClaudeSignInError, ClaudeSignIns
from agentic_runner.workers.codex_runtime import CodexRuntime
from agentic_runner.workers.contract_isolation import UID_MAP_FILENAME, ContractIsolation
from agentic_runner.workers.fastapi_client import DirectiveTokenSource, RunnerFastApiClient
from agentic_runner.workers.harness_logins import (
    CodexSignInError,
    CodexSignIns,
    holds_login,
    subscription_report,
)
from agentic_runner.workers.settings import WorkerSettings, get_worker_settings
from agentic_runner_contracts import __version__ as contracts_version
from agentic_runner_contracts.channel_messages import TranscriptDelivery
from agentic_runner_contracts.llm_access import PROVIDER_KEY_NAMES, provider_for
from agentic_runner_contracts.public_metadata import NAMESPACE_PREFIX
from agentic_runner_contracts.routing import RunnerRoutingIdentity
from agentic_runner_contracts.runner_login import (
    AUTHORIZATION_PENDING,
    EXPIRED_TOKEN,
    SLOW_DOWN,
    DeviceLoginRequest,
)
from agentic_runner_contracts.runner_registration import (
    RECENT_OUTCOMES_MAX,
    AcpBridgeVersion,
    CliVersion,
    ContractLlmAccess,
    ContractLlmTerms,
    CredentialManifest,
    CredentialValidity,
    DirectiveOutcome,
    EgressPosture,
    FolderPosture,
    HarnessVersion,
    HeartbeatAck,
    HeartbeatEnvelope,
    HostAttestation,
    IsolationMode,
    LifecycleKind,
    PoolTerms,
    ProviderKeyReport,
    RecipientKey,
    ResourcePressure,
    RunnerConnection,
    RunnerInstall,
    RunnerLimitReport,
    RuntimeSubscriptionReport,
    SlotProbe,
    SubscriptionState,
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
    "REGISTRATION_TOKEN_ENV",
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
    "own_store",
    "run",
    "sign_in",
    "run_runner_lifecycle_hook",
    "run_worker_until_terminated",
    "with_profile_acp_cli_kinds",
]

_logger = logging.getLogger(__name__)

# The Helm values and the workstation installer both land here as environment (ADR-0013
# §8). The Runner Registration Token is read from the environment and never from a flag: a
# flag would put it in the process table for every other uid on the host to read.
REGISTRATION_TOKEN_ENV = "AGENTIC_RUNNER_REGISTRATION_TOKEN"
# Its name before ADR-0021, still read so an installed Runner upgrades without an edit.
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
# How often due harness self-tests are looked for; each is due every 15 minutes at most.
SELF_TEST_SWEEP_SECONDS = 60.0
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
# What `run` exits with once the control plane revokes this Runner: non-zero, as the
# conformance suite asks, and its own value so a workstation login agent can tell it from a
# crash its service manager should restart (QTS-2207).
REVOKED_EXIT_CODE = 3

# What `agentic-runner status <name>` reads (PRD issue 47): the live pid, and a file the
# process touches on every acknowledged heartbeat, whose mtime is the heartbeat age.
PIDFILE_NAME = "runner.pid"
HEARTBEAT_STAMP_NAME = "last-heartbeat"
# The keys `agentic-runner sign-in` stores on a container Runner (`own_store`).
OWN_STORE_DIR = "credentials"
# How long a store key the provider refused waits before it is probed again.
HOST_KEY_REPROBE_SECONDS = 300.0
# Every top-level state file the Runner reads back. A workstation's state directory also
# holds files the Runner only writes or the OS service manager owns (`runner.log`), so the
# start check names these rather than refusing whatever else sits beside them.
_PRIVATE_STATE_FILES = (
    STATE_FILENAME,
    RECIPIENT_KEY_FILENAME,
    LIFECYCLE_FILENAME,
    UID_MAP_FILENAME,
    LLM_CONTRACTS_FILENAME,
    POOL_KEY_FILENAME,
)


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


def registration_token_from_env() -> str:
    """``AGENTIC_RUNNER_REGISTRATION_TOKEN``, else the deprecated ``AGENTIC_AGENT_TOKEN``.

    An empty string when neither is set, so a caller that can ask for it still asks.
    """

    if token := os.environ.get(REGISTRATION_TOKEN_ENV, "").strip():
        return token
    if token := os.environ.get(AGENT_TOKEN_ENV, "").strip():
        print(
            f"warning: {AGENT_TOKEN_ENV} is deprecated; set {REGISTRATION_TOKEN_ENV} instead",
            file=sys.stderr,
            flush=True,
        )
        return token
    return ""


def _max_concurrent_directives() -> int:
    return int(os.environ.get(MAX_CONCURRENT_DIRECTIVES_ENV, "").strip() or 1)


async def register(
    *,
    state_dir: Path,
    can_separate_uids: bool,
    client: httpx.AsyncClient | None = None,
    control_plane: str | None = None,
    registration_token: str | None = None,
    isolation: IsolationMode | None = None,
    runner_limit: RunnerLimitReport | None = None,
) -> tuple[RunnerState, str]:
    """First boot (PRD issue 41): exchange the Runner Registration Token for this
    process's identity.

    Idempotent by state file -- a restart re-reads what is already there rather than
    registering a second Runner against the Organisation's cap (21 A7). The keyword
    overrides are the workstation ``install`` (issue 47), which takes the Runner
    Registration Token pasted at a prompt rather than from a Helm-set environment.
    """

    existing = load_state(state_dir)
    if existing is not None:
        return existing, f"already registered {existing.runner_id} on {existing.task_queue}"
    recipient, fingerprint = recipient_key(state_dir)
    registration = RunnerRegistrationClient(
        base_url=control_plane or _require_env(CONTROL_PLANE_ENV), client=client
    )
    token = registration_token or registration_token_from_env()
    if not token:
        raise RunnerRegistrationError(
            "configuration_missing", f"{REGISTRATION_TOKEN_ENV} is not set"
        )
    state, response = await registration.bootstrap(
        registration_token=token,
        tags=parse_tags(os.environ.get(TAGS_ENV, "")),
        isolation_mode=isolation or isolation_mode(),
        recipient_key=recipient,
        max_concurrent_directives=_max_concurrent_directives(),
        can_separate_uids=can_separate_uids,
        runner_limit=runner_limit,
    )
    save_state(state_dir, state)
    return state, (
        f"registered {state.runner_id} in namespace {state.temporal_namespace} "
        f"on {state.task_queue}; contracts floor {response.contracts_floor} "
        f"({response.floor_state.value}); Recipient Key {fingerprint}"
    )


# RFC 8628 §3.5: each `slow_down` adds five seconds to the polling interval.
_SLOW_DOWN_SECONDS = 5


async def login(
    *,
    state_dir: Path,
    control_plane: str,
    name: str | None,
    say: Callable[[str], None],
    open_browser: Callable[[str], object],
    client: httpx.AsyncClient | None = None,
    isolation: IsolationMode | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> tuple[RunnerState, str]:
    """``agentic-runner login``: a device flow makes this Runner the person's (rev 14).

    The Runner shows a code and opens the browser; the person signs in and confirms, and
    the control plane creates the Runner with them as its operator and no connection. No
    token is copied: the device code never leaves this process. A state directory that
    already holds a Runner is refused, as ``install`` refuses to register over one.
    """

    existing = load_state(state_dir)
    if existing is not None:
        raise RunnerRegistrationError(
            "already_registered",
            f"{state_dir} already holds Runner {existing.runner_id}; `connect` it to an "
            "Organisation, or log in under another name",
        )
    recipient, _ = recipient_key(state_dir)
    registration = RunnerRegistrationClient(base_url=control_plane, client=client)
    tags = parse_tags(os.environ.get(TAGS_ENV, ""))
    max_concurrent = _max_concurrent_directives()
    authorization = await registration.start_login(
        DeviceLoginRequest(
            name=name,
            tags=tags,
            isolation_mode=isolation or isolation_mode(),
            contracts_version=contracts_version,
            runner_version=runner_version,
            recipient_key=recipient,
            max_concurrent_directives=max_concurrent,
            build_id=build_id(),
        )
    )
    page = _verification_page(
        authorization.verification_uri_complete or authorization.verification_uri, control_plane
    )
    say(f"code           {_printable(authorization.user_code)}")
    say(f"confirm at     {page}")
    open_browser(page)
    interval = float(authorization.interval)
    deadline = monotonic() + authorization.expires_in
    while True:
        await sleep(interval)
        try:
            response = await registration.finish_login(authorization.device_code)
            break
        except RunnerRegistrationError as error:
            if error.reason == SLOW_DOWN:
                interval += _SLOW_DOWN_SECONDS
            elif error.reason != AUTHORIZATION_PENDING:
                raise
        if monotonic() >= deadline:
            raise RunnerRegistrationError(
                EXPIRED_TOKEN, "the code expired before it was confirmed; run login again"
            )
    state = RunnerState(
        **response.identity.model_dump(),
        task_queue=response.task_queue,
        operator_user_profile_id=response.operator_user_profile_id,
        tags=dict(tags),
        max_concurrent_directives=max_concurrent,
        heartbeat_interval_seconds=response.heartbeat_interval_seconds,
        temporal_address=response.temporal_address,
    )
    save_state(state_dir, state)
    return state, (
        f"logged in {response.name} ({state.runner_id}); connect it to an Organisation with "
        "`agentic-runner connect`"
    )


async def connect(
    *,
    state_dir: Path,
    control_plane: str,
    organisation: str | None,
    ask: Callable[[str], str],
    say: Callable[[str], None],
    client: httpx.AsyncClient | None = None,
) -> int:
    """``agentic-runner connect [organisation]``: ask for a connection (rev 14).

    ``organisation`` is an Organisation's id or its name; without it, the Organisations
    the operator belongs to are listed and one is asked for. The running Runner takes the
    connection up from its next heartbeat ack, so nothing here touches Temporal.
    """

    state = load_state(state_dir)
    if state is None or state.operator_user_profile_id is None:
        say(
            "refusing to connect: only a Runner you logged in with `agentic-runner login` "
            "connects to Organisations; an Organisation's own Runner has its one connection"
        )
        return 1
    registration = RunnerRegistrationClient(base_url=control_plane, client=client)
    listed = (await registration.organisations(state)).organisations
    if not listed:
        say("you belong to no Organisation yet; connect once someone invites you")
        return 1
    if organisation is None:
        for number, entry in enumerate(listed, start=1):
            note = f" ({entry.refusal})" if entry.refusal else ""
            if entry.connected:
                note = " (connected)"
            say(f"  [{number}] {_printable(entry.name)}{note}")
        answer = ask("Organisation to connect to [number]: ").strip()
        chosen = _numbered(listed, answer)
    else:
        chosen = next((e for e in listed if organisation in {str(e.organisation_id), e.name}), None)
    if chosen is None:
        say("refusing to connect: no such Organisation among yours")
        return 1
    if chosen.connected:
        say(f"already connected to {_printable(chosen.name)}")
        return 0
    if chosen.refusal:
        say(f"refusing to connect to {_printable(chosen.name)}: {chosen.refusal}")
        return 1
    await registration.connect(state, chosen.organisation_id)
    say(
        f"connected to {_printable(chosen.name)}; the Runner polls its work from its next heartbeat"
    )
    return 0


def _verification_page(page: str, control_plane: str) -> str:
    """The page the control plane asks the person to confirm on, once it is safe to open.

    The browser opens whatever the answer names, so a spoofed or hostile control plane
    could launch a ``file:`` or custom-scheme URL, or a look-alike sign-in page. Only
    ``https`` (``http`` on loopback, for the fake control plane) on the control plane's
    own host, or a parent domain of it, is opened: the console sits at the parent of the
    Runner edge (``agentic.<zone>`` over ``api.agentic.<zone>``, ADR-0016).
    """

    target = urlsplit(page)
    host = (target.hostname or "").lower()
    dialled = (urlsplit(control_plane).hostname or "").lower()
    secure = target.scheme == "https" or (target.scheme == "http" and _loopback(host))
    parent = _address(dialled) is None and "." in host and dialled.endswith(f".{host}")
    own = host == dialled or parent
    if not (secure and own) or _printable(page) != page:
        raise RunnerRegistrationError(
            "verification_uri_refused",
            f"refusing to open {_printable(page)!r}: the control plane at {dialled} must "
            "send an https page on its own domain",
        )
    return page


def _loopback(host: str) -> bool:
    address = _address(host)
    return host == "localhost" or (address is not None and address.is_loopback)


def _address(host: str) -> IPv4Address | IPv6Address | None:
    try:
        return ip_address(host)
    except ValueError:
        return None


def _numbered[T](items: Sequence[T], answer: str) -> T | None:
    """The item a person picked by its 1-based number, or None for anything else."""

    if answer.isdigit() and 0 < int(answer) <= len(items):
        return items[int(answer) - 1]
    return None


def _printable(name: str) -> str:
    # A name someone typed in the console: an escape sequence in it would reach the terminal.
    return "".join(character for character in name if character.isprintable())


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


def own_store(config: RunnerConfig) -> FileCredentialStore:
    """Where ``agentic-runner sign-in`` keeps a key typed on a container Runner, whose
    configured store is a read-only mounted Secret: ``0600`` files in the state directory.
    """

    return FileCredentialStore(config.state_dir / OWN_STORE_DIR)


def build_credential_resolver(
    config: RunnerConfig, *, host_store: HostCredentialStore | None = None
) -> CredentialResolver:
    """The host store a Directive resolves its Credential References from (22 A1), and the
    LLM proxy its keys by the provider's key name (Spec: Runners rev 3).

    A workstation passes its OS store, which ``sign-in`` and ``credential set`` both write.
    A container reads the keys ``sign-in`` stored first, then the mounted Secret; with
    neither holding a value, "nothing is installed" is the fail-closed reading. Values a
    funder sealed land in this same resolver, under the same reference name, from the
    heartbeat stream.
    """

    if host_store is not None:
        return CredentialResolver(store=host_store)
    stores: list[HostCredentialStore] = [own_store(config)]
    if config.credential_store is not None:
        stores.append(DirectoryCredentialStore(config.credential_store))
    return CredentialResolver(store=LayeredCredentialStore(*stores))


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
    settings: WorkerSettings,
    clis: Sequence[CliVersion],
    *,
    denied_folders: DeniedFolders | None = None,
) -> dict[str, AgentRuntime]:
    """Every Agent Runtime this process serves, by the Profile's `cli_kind` (ADR-0006).

    The one place a concrete runtime is named; everything downstream depends only on the
    port. One process serves every CLI it found that clears its floor, and each Directive
    picks from here by the Work Record's `cli_kind` (local-agents 01) -- there is no
    process-wide choice. The same `clis` ride the heartbeat's attestation, so what this
    Runner serves and what routing believes it serves are one list.

    Which runtime serves a kind -- its pinned ACP bridge or the per-CLI one -- is this
    Runner's setting (`ACP_CLI_KINDS`), never the payload's (local-agents 12 items 6, 7).
    A kind the host opted in to run a Profile-named command for (`ACP_PROFILE_CLI_KINDS`,
    local-agents 18) reaches `clis` through :func:`with_profile_acp_cli_kinds`.
    ``denied_folders`` is the Runner Limit's folders part: the ACP permission seam is where
    a harness asks before it reads a path, so the ACP runtimes are the ones that take it.
    """

    pinned_kinds = _pinned_acp_cli_kinds(settings)
    profile_kinds = set(profile_acp_executables(settings))
    return {
        cli.cli_kind: (
            AcpRuntime(cli_kind=cli.cli_kind, settings=settings, denied_folders=denied_folders)
            if cli.cli_kind in profile_kinds or cli.cli_kind in pinned_kinds
            else _RUNTIMES[cli.cli_kind](settings)
        )
        for cli in clis
        if cli.meets_floor and (cli.cli_kind in _RUNTIMES or cli.cli_kind in profile_kinds)
    }


def runner_limit_report(
    limit: AppliedRunnerLimit,
    runtimes: Mapping[str, AgentRuntime],
    *,
    egress_posture: EgressPosture,
) -> RunnerLimitReport | None:
    """The Runner Limit with how far each part binds here (ADR-0022 §53-§55).

    Folders are ``checked`` only for a runtime whose harness asks before it reads a path;
    every other harness reads what it likes, so the list is ``unenforced`` against it.
    """

    return limit.report(
        egress_posture=egress_posture,
        folders={
            kind: (
                FolderPosture.CHECKED
                if isinstance(runtime, AcpRuntime) and runtime.folders_checked
                else FolderPosture.UNENFORCED
            )
            for kind, runtime in runtimes.items()
        },
    )


def egress_posture_from_env() -> EgressPosture:
    return EgressPosture(
        os.environ.get(EGRESS_POSTURE_ENV, "").strip() or EgressPosture.UNRESTRICTED
    )


def _pinned_acp_cli_kinds(settings: WorkerSettings) -> set[str]:
    """The kinds `ACP_CLI_KINDS` routes through a bridge this Runner pins."""

    named = {kind.strip() for kind in settings.ACP_CLI_KINDS.split(",") if kind.strip()}
    return named & ACP_BRIDGES.keys()


def with_acp_bridges(
    attestation: HostAttestation | None, settings: WorkerSettings
) -> HostAttestation | None:
    """The attestation, with each kind this Runner serves through its pinned ACP bridge
    carrying that bridge's package and version (QTS-1322), so the console shows what
    actually drives the CLI. A kind below its floor is not served, so it names no bridge.
    """

    if attestation is None:
        return None
    pinned_kinds = _pinned_acp_cli_kinds(settings)
    clis = [
        cli.model_copy(
            update={
                "acp_bridge": AcpBridgeVersion(
                    package=ACP_BRIDGES[cli.cli_kind].package,
                    version=ACP_BRIDGES[cli.cli_kind].version,
                )
            }
        )
        if cli.meets_floor and cli.cli_kind in pinned_kinds
        else cli
        for cli in attestation.clis
    ]
    return attestation.model_copy(update={"clis": clis})


def with_profile_acp_cli_kinds(
    attestation: HostAttestation | None, settings: WorkerSettings
) -> HostAttestation | None:
    """The attestation, plus each kind the host opted in to run a Profile-named ACP command
    for, so routing sends that kind here and the registry serves it (local-agents 18).

    The Runner does not start the host's executable just to ask its version, so the
    version reported is the Runner's own: what serves the kind is this build's ACP runtime.
    No attestation stays none -- such a Runner is routed nothing at all.
    """

    if attestation is None:
        return None
    found = {cli.cli_kind for cli in attestation.clis}
    added = [
        CliVersion(cli_kind=kind, version=runner_version, meets_floor=True)
        for kind in profile_acp_executables(settings)
        if kind not in found
    ]
    # Validated, not copied: the attestation's cap on `clis` holds for these too.
    return HostAttestation.model_validate(
        {**attestation.model_dump(), "clis": [*attestation.clis, *added]}
    )


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


async def run_worker_until_terminated(
    worker: GracefulWorker, *, stop: asyncio.Event | None = None
) -> None:
    """Poll until SIGTERM (or ``stop``), then drain: stop polling, let the running
    activity finish.

    Python's default SIGTERM disposition kills the process outright, discarding whatever
    Directive is mid-run. The chart's ``preStop`` hook (issue 02) signals termination as
    early as possible; this handler is what turns that into ``Worker.shutdown()`` -- stop
    polling for new work, wait for the in-flight activity, then let ``run()`` return.

    A caller that passes ``stop`` owns SIGTERM already (``run`` installs it before it
    starts up, QTS-1536); one set before the Worker polls never starts it.
    """

    if stop is not None and stop.is_set():
        return
    async with _sigterm_sets(stop) as stop_event:
        run_task = asyncio.ensure_future(worker.run())
        stop_task = asyncio.ensure_future(stop_event.wait())
        await asyncio.wait({run_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
        if not stop_task.done():
            stop_task.cancel()
        if not run_task.done():
            _logger.info("Worker received SIGTERM: draining in-flight activity before exit")
            await worker.shutdown()
        await run_task


@contextlib.asynccontextmanager
async def _sigterm_sets(stop: asyncio.Event | None) -> AsyncIterator[asyncio.Event]:
    """``stop`` as given, or a new event SIGTERM sets for the length of the block."""

    if stop is not None:
        yield stop
        return
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    loop.add_signal_handler(signal.SIGTERM, stop_event.set)
    try:
        yield stop_event
    finally:
        loop.remove_signal_handler(signal.SIGTERM)


class _StoppedBeforeStartError(Exception):
    """SIGTERM (or ``stop``) arrived before the Worker polled: no Directive is in flight."""


async def _before_stop[T](awaitable: Awaitable[T], stop: asyncio.Event) -> T:
    """``awaitable``'s result, unless ``stop`` is set first; then it is cancelled.

    Start-up waits on the network -- registration, the first heartbeat, the Temporal
    connection -- for as long as the far end takes to answer. A stop during that wait
    must not wait with it.
    """

    work = asyncio.ensure_future(awaitable)
    stopped = asyncio.ensure_future(stop.wait())
    try:
        await asyncio.wait({work, stopped}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        stopped.cancel()
        if not work.done():
            work.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await work
    if work.cancelled():
        raise _StoppedBeforeStartError
    return work.result()


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
    # Local-agents 17: each runtime's descriptor, the harness roots on disk and the last
    # self-test under each.
    runtimes: Mapping[str, AgentRuntime] = field(default_factory=dict)
    contracts: ContractIsolation | None = None
    self_tests: HarnessSelfTests | None = None
    # Local-agents 10: each subscription's usage windows; None off a user-hosted Runner.
    usage_windows: UsageWindows | None = None
    # Local-agents 04b and rev 3: which key names fill the LLM proxy's slots, delivered or
    # from the host's own store, and where each is spent.
    llm_slots: Mapping[str, LlmEndpoint] = field(default_factory=lambda: dict(LLM_SLOT_REFERENCES))
    # Rev 3: where a command reaches this Runner, and where the ack's Contract terms are
    # kept for `agentic-runner sign-in`.
    install: RunnerInstall | None = None
    state_dir: Path | None = None
    # Rev 3: the fingerprint of the Pool Key this Runner was given; None when it was given
    # none. Reported only once an ack named the pool (`_pool`), which a control plane on
    # 3.26 or later does.
    pool_key_fingerprint: str | None = None
    # Console-v2 issue 30: the OAuth Connectors whose code, tokens and verifier stay here.
    oauth: OAuthConnectors | None = None
    # ADR-0022 §57: the Runner Limit read at start and its posture; None while it is empty.
    runner_limit: RunnerLimitReport | None = None
    _delivered: set[tuple[str, str]] = field(default_factory=set)
    # ADR-0022 §7: the Contracts the last ack asked this Runner to report references for.
    _manifests: list[CredentialManifest] = field(default_factory=list)
    _offered_to_proxy: dict[tuple[str, str], str] = field(default_factory=dict)
    # The host store's keys offered to the proxy, by key name, as a digest: a changed
    # value is probed again, an unchanged one is not.
    _offered_host: dict[str, str] = field(default_factory=dict)
    _host_probed_at: dict[str, float] = field(default_factory=dict)
    # Rev 3: the Contracts the last ack said this Runner serves; None until an ack from a
    # control plane on 3.25 named them, and the LLM access report is sent only after.
    _llm_contracts: list[ContractLlmTerms] | None = None
    _pool: PoolTerms | None = None
    # QTS-1773: the control plane's contracts version, from its ack; None until an ack from
    # one on 3.27 or later, the first to send it and to parse `RunnerInstall.name`.
    _control_plane_contracts: str | None = None
    # QTS-1909: the name in the copy `list` reads, None while there is none. "unwritten"
    # until this process first writes it, so the first ack replaces what a previous one left.
    _organisation_name: str | None | Literal["unwritten"] = "unwritten"
    _last_seen: tuple[float, datetime] | None = None
    # Rev 14: the connections the last ack named; None until an ack from a control plane
    # on 3.31 or later named them.
    _connections: list[RunnerConnection] | None = None

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
        """Per Contract: the CLI's version and whether it runs on a key the proxy holds
        (22 A8) or on a sign-in in the harness root -- read off the slots and by ``stat``,
        never by opening the root. A Contract with a harness root and no slot is listed
        too, so a login-only Contract gets its descriptor (local-agents 17).

        ``auth_mode`` is ``api_key``, ``subscription`` for a sign-in, or ``none``; it
        replaced ``login``, which said only that no key was delivered (rev 3). The LLM
        access report is the record routing reads; this row is the harness's descriptor.
        """

        if self.attestation is None:
            return []
        versions = {cli.cli_kind: cli.version for cli in self.attestation.clis}
        slots = {(str(slot.contract_id), slot.runtime_kind) for slot in self.proxy.slots.statuses()}
        roots = self.contracts.harness_roots() if self.contracts is not None else []
        return [
            self._harness(contract_id, cli_kind, versions)
            for contract_id, cli_kind in sorted(slots | set(roots))
        ]

    def _harness(
        self, contract_id: str, cli_kind: str, versions: Mapping[str, str]
    ) -> HarnessVersion:
        runtime = self.runtimes.get(cli_kind)
        capabilities = runtime.capabilities() if runtime is not None else None
        provider = provider_for(cli_kind)
        keyed = self.proxy.slots.holds(UUID(contract_id), provider) if provider else False
        auth_mode = "api_key" if keyed else self._signed_in_mode(contract_id, cli_kind)
        return HarnessVersion(
            contract_id=UUID(contract_id),
            cli_kind=cli_kind,
            version=versions.get(cli_kind, "unknown"),
            auth_mode=auth_mode,
            auth_modes=(sorted(capabilities.auth_modes) if capabilities is not None else None),
            permission_mode=capabilities.permission_mode if capabilities is not None else None,
            self_test=(
                self.self_tests.result(contract_id, cli_kind)
                if self.self_tests is not None
                else None
            ),
            usage_windows=(
                self.usage_windows.windows(contract_id, cli_kind)
                if self.usage_windows is not None
                else None
            ),
        )

    def _signed_in_mode(self, contract_id: str, cli_kind: str) -> str:
        if self.contracts is None:
            return "none"
        report = self._subscription(contract_id, cli_kind)
        return "none" if report.state == SubscriptionState.NONE else "subscription"

    def _subscription(self, contract_id: str, cli_kind: str) -> RuntimeSubscriptionReport:
        assert self.contracts is not None
        return subscription_report(
            self.contracts,
            contract_id,
            cli_kind,
            windows=(
                self.usage_windows.windows(contract_id, cli_kind)
                if self.usage_windows is not None
                else None
            ),
            now=self.clock(),
        )

    def _llm_access_report(self) -> list[ContractLlmAccess] | None:
        """Per Contract the last ack named: the store's key and its probe per provider,
        and the subscription per Agent Runtime served (Spec: Runners rev 3).

        The store's keys are the host's and serve every Contract, so they repeat; a
        subscription is one Contract's harness root's. Sent only once an ack named the
        Contracts -- a control plane below 3.25 refuses the field.
        """

        if self._llm_contracts is None:
            return None
        keys = [
            ProviderKeyReport(
                provider=provider,
                held=key_name in self._offered_host,
                probe=self.proxy.slots.host_probe(provider.value) or SlotProbe.UNPROBED,
            )
            for provider, key_name in PROVIDER_KEY_NAMES.items()
        ]
        return [
            ContractLlmAccess(
                contract_id=terms.contract_id,
                keys=keys,
                subscriptions=(
                    [
                        self._subscription(str(terms.contract_id), cli_kind)
                        for cli_kind in sorted(self.runtimes)
                    ]
                    if self.contracts is not None
                    else []
                ),
            )
            for terms in self._llm_contracts
        ]

    def _install_report(self) -> RunnerInstall | None:
        """Where a command reaches this Runner, sent once an ack named the Contracts -- a
        control plane below 3.25 refuses the field -- and with its name only once an ack
        came from a control plane on 3.27, since one below refuses that key (QTS-1773)."""

        if self.install is None or self._llm_contracts is None:
            return None
        if self._control_plane_contracts is None:
            return self.install.model_copy(update={"name": None})
        return self.install

    def _credential_report(self) -> list[CredentialValidity]:
        """Per Contract the last ack named, whether each reference would resolve (§7).

        Asked of the same resolver a Directive resolves through, so the report and the
        Directive cannot disagree on what a funder sealed or what the host lends (§36).
        """

        return [
            CredentialValidity(
                contract_id=manifest.contract_id,
                reference=reference,
                valid=self.credentials.usable(str(manifest.contract_id), reference),
            )
            for manifest in self._manifests
            for reference in manifest.references
        ]

    async def directive_token(self, directive_id: str) -> str:
        """One activity execution's control-plane token, by Directive id (PRD issue 63)."""

        return (await self.client.request_directive_token(self.state, directive_id)).token

    async def exchange(self, applied: Sequence[Any]) -> HeartbeatAck:
        now = self.clock()
        self._observe_sleep(now)
        await self._sync_host_keys()
        lifecycle = self.lifecycle.pending() if self.lifecycle is not None else []
        renewed, resealed = self.sealed.renew(now)
        if self.oauth is not None:
            await self.oauth.before_beat()
        envelope = HeartbeatEnvelope(
            runner_version=runner_version,
            contracts_version=contracts_version,
            resource_pressure=resource_pressure(Path.cwd()),
            max_concurrent_directives=self.state.max_concurrent_directives,
            current_load=self.load.in_flight,
            recent_outcomes=list(self.load.outcomes),
            credentials=self._credential_report(),
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
            oauth_authorizations=self.oauth.take_authorizations() if self.oauth else [],
            oauth_tokens=self.oauth.uploads() if self.oauth else [],
            oauth_outcomes=self.oauth.take_outcomes() if self.oauth else [],
            runner_limit=self.runner_limit,
            llm_access=self._llm_access_report(),
            install=self._install_report(),
            pool_key_fingerprint=self.pool_key_fingerprint if self._pool is not None else None,
        )
        ack = await self.client.heartbeat(self.state, envelope)
        if self.sources is not None:
            self.sources.assign(ack.user_sources)
        await self._deliver_transcripts(ack)
        if self.oauth is not None:
            # Before the sealed values are applied: a Disconnect revokes with the tokens
            # this Runner holds, and the same ack may already have withdrawn their ciphertext.
            await self.oauth.apply(
                starts=ack.oauth_starts, codes=ack.oauth_codes, disconnects=ack.oauth_disconnects
            )
        if self.lifecycle is not None:
            self.lifecycle.acknowledge(lifecycle)
        if self.heartbeat_stamp is not None:
            self.heartbeat_stamp.touch(mode=0o600)
        self.proxy.outbox.acknowledge(ack.usage_accepted)
        for refused in self.sealed.apply(ack.sealed_credentials):
            _logger.warning(
                "sealed credential %s/%s could not be opened with this installation's key",
                refused.contract_id,
                refused.slot,
            )
        self._sync_delivered()
        self._manifests = list(ack.credential_manifests)
        self._take_llm_contracts(ack.llm_contracts)
        self._take_pool(ack.pool)
        if ack.contracts_version is not None:
            self._control_plane_contracts = ack.contracts_version
        self._take_organisation_name(ack.organisation_name)
        await self._sync_llm_slots()
        if ack.runner_token is not None:
            self.runner_token = ack.runner_token
            self.runner_token_expires_at = ack.runner_token_expires_at
        if ack.connections is not None:
            self._connections = list(ack.connections)
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

        plaintext = self.oauth.resolved() if self.oauth is not None else self.sealed.plaintext
        for (contract_id, slot), value in plaintext.items():
            self.credentials.deliver(contract_id, slot, value)
        for contract_id, slot in self._delivered - set(plaintext):
            self.credentials.drop(contract_id, slot)
        self._delivered = set(plaintext)

    def connections(self) -> list[RunnerConnection]:
        """What this Runner polls: the last ack's connections (rev 14), or, from a control
        plane below 3.31, the one namespace it registered in with the ack's Runner Token.
        A Runner ``login`` made and nobody connected polls nothing."""

        if self._connections is not None:
            return list(self._connections)
        if not self.state.temporal_namespace or self.runner_token is None:
            return []
        return [
            RunnerConnection(
                organisation_id=_namespace_organisation(self.state.temporal_namespace),
                temporal_namespace=self.state.temporal_namespace,
                task_queue=self.state.task_queue,
                runner_token=self.runner_token,
                runner_token_expires_at=self.runner_token_expires_at,
            )
        ]

    @property
    def acks_connections(self) -> bool:
        return self._connections is not None

    @property
    def llm_contracts(self) -> list[ContractLlmTerms] | None:
        return None if self._llm_contracts is None else list(self._llm_contracts)

    def _take_llm_contracts(self, terms: list[ContractLlmTerms] | None) -> None:
        """Keep the ack's Contract terms, and the copy ``sign-in`` reads when they change.
        An ack without them (a control plane below 3.25) leaves what is held."""

        if terms is None or terms == self._llm_contracts:
            return
        self._llm_contracts = list(terms)
        if self.state_dir is not None:
            save_llm_contracts(self.state_dir, terms)

    def _take_organisation_name(self, name: str | None) -> None:
        """Keep the copy ``list`` reads when the ack's name changes. Unlike the Contract
        terms, an ack without one clears it: only the name, never the id, can go stale."""

        if self.state_dir is None or name == self._organisation_name:
            return
        save_organisation_name(self.state_dir, name)
        self._organisation_name = name

    def _take_pool(self, pool: PoolTerms | None) -> None:
        """Keep the ack's pool. An ack without one (a personal Runner, or a control plane
        below 3.26) leaves what is held."""

        if pool is None or pool == self._pool:
            return
        self._pool = pool
        current = pool.pool_key_fingerprint
        if current is not None and current != self.pool_key_fingerprint:
            # The operator's signal: this Runner opens none of the pool's LLM Keys until
            # its install carries the Pool Key now on the Runners page.
            _logger.warning(
                "this Runner holds Pool Key %s, not the pool's current %s; it opens none of "
                "the pool's LLM Keys until %s is updated",
                self.pool_key_fingerprint or "none",
                current,
                POOL_KEY_ENV,
            )

    async def _sync_host_keys(self) -> None:
        """The host store's keys, by the provider's key name, become the proxy's host slots
        (Spec: Runners rev 3), serving every Contract this Runner runs.

        Read every beat, so a key put straight into the store (``credential set``, the
        chart's Secret, a mounted folder, ``sign-in``) reaches the proxy within one beat.
        Read on a thread: a workstation's store is the OS keychain, one subprocess per key.
        Probed when the value changed, and a refused one again after
        ``HOST_KEY_REPROBE_SECONDS`` -- the probe also answers no on a network fault, and
        nobody re-delivers a store key. A removed key stops serving at once.
        """

        for key_name, endpoint in sorted(self.llm_slots.items()):
            value = await asyncio.to_thread(self.credentials.host_store.get, key_name)
            if value is None:
                if self._offered_host.pop(key_name, None) is not None:
                    self._host_probed_at.pop(key_name, None)
                    self.proxy.slots.drop_host(endpoint.name)
                continue
            digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
            refused = self.proxy.slots.host_probe(endpoint.name) == SlotProbe.INVALID
            due = self.monotonic() - self._host_probed_at.get(key_name, 0.0)
            if self._offered_host.get(key_name) == digest and not (
                refused and due >= HOST_KEY_REPROBE_SECONDS
            ):
                continue
            self._offered_host[key_name] = digest
            self._host_probed_at[key_name] = self.monotonic()
            await self.proxy.slots.put_host(
                endpoint.slot(reference=key_name, key_id=f"{key_name}@host", value=value)
            )

    async def _sync_llm_slots(self) -> None:
        """A delivered LLM key becomes its Contract's proxy slot for its provider
        (local-agents 04b, rev 3).

        Offered once per value, not once per beat: :meth:`SlotStore.put` probes the
        provider, and a value the probe refused stays refused until the funder delivers a
        new one -- the heartbeat's slot fields already say why. A renewal re-seals the same
        value, so it costs no probe either. A withdrawn reference wipes that provider's
        slot alone (22 A9); the Contract's other provider keeps serving.
        """

        delivered = {
            key: value for key, value in self.sealed.plaintext.items() if key[1] in self.llm_slots
        }
        withdrawn = set(self._offered_to_proxy) - set(delivered)
        for contract_id, reference in withdrawn:
            self.proxy.slots.drop(UUID(contract_id), self.llm_slots[reference].name)
        offered = {key: v for key, v in self._offered_to_proxy.items() if key not in withdrawn}
        for (contract_id, reference), value in sorted(delivered.items()):
            if offered.get((contract_id, reference)) == value:
                continue
            version = self.sealed.version(contract_id, reference)
            await self.proxy.slots.put(
                UUID(contract_id),
                self.llm_slots[reference].slot(
                    reference=reference, key_id=f"{reference}@v{version}", value=value
                ),
            )
        self._offered_to_proxy = delivered


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


async def sign_in(which: str, *, store: KeyStore, method: str = OAUTH_TOKEN) -> int:
    """``agentic-runner sign-in claude|codex`` (``sign_in.SignIn``), composed here.

    Runs with the Runner's environment (``kubectl exec``, ``docker exec``, or a
    workstation's own), so a subscription gets the uid and the harness root the Contract's
    Directives already run under. ``store`` is where a typed key goes: the workstation's
    OS store, or ``own_store`` in a container. Nothing reaches the control plane.
    """

    state_dir = load_config().state_dir
    state = load_state(state_dir)

    async def subscription(contract_id: str, cli_kind: str) -> int:
        mode = isolation_mode()
        can = can_separate_uids()
        try:
            require_isolation_supported(mode, can_separate_uids=can)
            # `run` gives the settings this default; a sign-in must build the same ones, or a
            # workstation, whose environment sets only the control plane, is refused (QTS-2157).
            _default_callback_url(os.environ.get(CONTROL_PLANE_ENV, "").strip())
            settings = get_worker_settings()
        except RunnerRegistrationError as error:
            print(f"refusing to sign in ({error.reason}): {error}", file=sys.stderr)
            return 1
        except ValidationError as error:
            print(
                f"refusing to sign in: {_invalid_settings(error)}; run this with the Runner's "
                "environment (`docker exec`, `kubectl exec`, or on the workstation that runs it)",
                file=sys.stderr,
            )
            return 2
        isolation = build_contract_isolation(settings, can_separate_uids=can)
        try:
            if cli_kind == "codex_cli":
                return await CodexSignIns(isolation).sign_in(contract_id)
            return await ClaudeSignIns(isolation).sign_in(contract_id, method=method)
        except (ClaudeSignInError, CodexSignInError) as error:
            print(f"sign-in failed: {error}", file=sys.stderr)
            return 1

    return await SignIn(
        host_party=(state.host_party if state is not None else "") or None,
        operated=state is not None and state.operator_user_profile_id is not None,
        contracts=load_llm_contracts(state_dir),
        served=[kind for kind, cli in _SIGN_IN_CLIS.items() if shutil.which(cli)],
        store=store,
        subscription=subscription,
        ask=input,
        secret=getpass.getpass,
        say=lambda line: print(line, file=sys.stderr),
    ).run(which)


_SIGN_IN_CLIS: Final[Mapping[str, str]] = {"claude_code": "claude", "codex_cli": "codex"}


def _default_callback_url(control_plane: str) -> None:
    # The control plane the activities call back is the one this process registered
    # with, unless the operator points them at an internal address explicitly.
    if control_plane:
        os.environ.setdefault("INTERNAL_FASTAPI_BASE_URL", control_plane)


def _invalid_settings(error: ValidationError) -> str:
    """``INTERNAL_FASTAPI_BASE_URL (Field required)``, one per field, never the value."""

    return ", ".join(
        f"{'.'.join(str(part) for part in detail['loc'])} ({detail['msg']})"
        for detail in error.errors(include_input=False, include_url=False)
    )


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
    install: RunnerInstall | None = None,
) -> int:
    """The Runner process. Returns the exit code.

    The seams are injectable for the test that stands two of these up against a fake
    control plane: the HTTP transport, the Temporal connection, the Worker and the
    capability check. Production passes none of them. ``host_store`` and
    ``attestation`` are the workstation's (issue 47): its OS credential store and what
    it says about itself. ``install`` is where a command reaches this Runner, which the
    heartbeat reports so the console can print ``sign-in`` for it (rev 3).
    ``heartbeat_interval`` defaults to the cadence bootstrap gave.
    """

    loop = asyncio.get_running_loop()
    # Before the first await: in a container this process is PID 1, and the kernel drops
    # a signal PID 1 has no handler for, so a SIGTERM during start-up was lost and
    # `docker stop` waited out its grace period to SIGKILL (QTS-1536).
    stop = stop or asyncio.Event()
    loop.add_signal_handler(signal.SIGTERM, stop.set)
    try:
        return await _run(
            stop=stop,
            http_client=http_client,
            connect=connect,
            worker_factory=worker_factory,
            can_change_uid=can_change_uid,
            heartbeat_interval=heartbeat_interval,
            readiness=readiness,
            host_store=host_store,
            attestation=attestation,
            install=install,
        )
    finally:
        loop.remove_signal_handler(signal.SIGTERM)


async def _run(
    *,
    stop: asyncio.Event,
    http_client: httpx.AsyncClient | None,
    connect: Connector,
    worker_factory: WorkerFactory,
    can_change_uid: bool | None,
    heartbeat_interval: float | None,
    readiness: Readiness | None,
    host_store: HostCredentialStore | None,
    attestation: HostAttestation | None,
    install: RunnerInstall | None = None,
) -> int:
    install_stop_tolerant_child_watcher(asyncio.get_running_loop())
    try:
        config = load_config()
    except (ValidationError, ValueError, OSError) as error:
        # A `[limits]` table (or any other setting) that does not parse stops the Runner
        # here, rather than starting it with a limit the host did not write (ADR-0022 §57).
        print(f"refusing to start (configuration_invalid): {error}", flush=True)
        return 1
    mode = isolation_mode()
    can = can_separate_uids() if can_change_uid is None else can_change_uid
    try:
        require_isolation_supported(mode, can_separate_uids=can)
        control_plane = _require_env(CONTROL_PLANE_ENV)
        ensure_private_dir(config.state_dir)
        # Some of these are first read only after registration (the lifecycle outbox, the
        # uid map); one left 0644 by an earlier release must refuse here, not crash later.
        require_private_files(config.state_dir, _PRIVATE_STATE_FILES)
    except (RunnerRegistrationError, UnsafeStateError) as error:
        print(f"refusing to start ({error.reason}): {error}", flush=True)
        return 1
    _default_callback_url(control_plane)
    settings = get_worker_settings()
    try:
        attestation = with_profile_acp_cli_kinds(with_acp_bridges(attestation, settings), settings)
    except ValueError as error:
        print(f"refusing to start: {error}", flush=True)
        return 1
    try:
        runner_limit = AppliedRunnerLimit.from_config(
            config.limits, workspace_roots=(config.workspace_root, settings.WORKSPACE_ROOT)
        )
    except RunnerLimitError as error:
        print(f"refusing to start (runner_limit): {error}", flush=True)
        return 1
    state_dir = config.state_dir
    try:
        pool_key = load_pool_key(state_dir)
    except PoolKeyError as error:
        # Refused rather than started without it: the pool's LLM Keys would then fail
        # every Directive routed to them, far from the typo that caused it.
        print(f"refusing to start (pool_key_invalid): {error}", flush=True)
        return 1

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
            install=install,
            runner_limit=runner_limit,
            pool_key=pool_key,
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
    stop: asyncio.Event,
    heartbeat_interval: float | None,
    host_store: HostCredentialStore | None,
    attestation: HostAttestation | None,
    install: RunnerInstall | None = None,
    runner_limit: AppliedRunnerLimit = EMPTY_RUNNER_LIMIT,
    pool_key: PoolKey | None = None,
) -> int:
    limit_report = runner_limit_report(
        runner_limit,
        build_agent_runtimes(
            settings,
            attestation.clis if attestation is not None else [],
            denied_folders=runner_limit.folders,
        ),
        egress_posture=egress_posture_from_env(),
    )
    try:
        state, outcome = await _before_stop(
            register(
                state_dir=state_dir,
                can_separate_uids=can_change_uid,
                client=http_client,
                runner_limit=limit_report,
            ),
            stop,
        )
    except _StoppedBeforeStartError:
        print("stopped before registration completed", flush=True)
        return 0
    except RunnerRegistrationError as error:
        print(f"registration refused ({error.reason}): {error}", flush=True)
        if error.refuses_registration_token:
            print(REGISTRATION_TOKEN_REFUSED_HINT, flush=True)
        return 1
    except UnsafeStateError as error:
        print(f"refusing to start ({error.reason}): {error}", flush=True)
        return 1
    print(outcome, flush=True)
    if os.environ.get(MAX_CONCURRENT_DIRECTIVES_ENV, "").strip():
        # Rev 14: the machine's limit is its own config, read at every start, not only the
        # value it registered or logged in with.
        state = state.model_copy(update={"max_concurrent_directives": _max_concurrent_directives()})
    temporal_address = state.temporal_address or settings.TEMPORAL_ADDRESS
    if not temporal_address:
        print(
            "refusing to start (configuration_missing): the control plane gave no Temporal "
            "address and TEMPORAL_ADDRESS is not set",
            flush=True,
        )
        return 1
    interval = (
        heartbeat_interval if heartbeat_interval is not None else state.heartbeat_interval_seconds
    )
    # `status` reads these two (issue 47); the process is the only writer of either.
    pidfile = state_dir / PIDFILE_NAME
    private_write(pidfile, str(os.getpid()).encode("utf-8"))
    lifecycle = LifecycleOutbox(state_dir)
    lifecycle.add(LifecycleKind.START, datetime.now(UTC))
    try:
        return await _serve_registered(
            config=config,
            settings=settings,
            state=state,
            temporal_address=temporal_address,
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
            install=install,
            lifecycle=lifecycle,
            runner_limit=runner_limit,
            pool_key=pool_key,
        )
    except _StoppedBeforeStartError:
        # No beat with a 10 s timeout on the way out (`_report_stop`): the control plane
        # may be what start-up was waiting for. The next start carries the stop.
        lifecycle.add(LifecycleKind.STOP, datetime.now(UTC))
        print("stopped before the Worker started", flush=True)
        return 0
    finally:
        pidfile.unlink(missing_ok=True)


async def _serve_registered(
    *,
    config: RunnerConfig,
    settings: WorkerSettings,
    state: RunnerState,
    temporal_address: str,
    mode: IsolationMode,
    can_change_uid: bool,
    control_plane: str,
    state_dir: Path,
    readiness: Readiness,
    http_client: httpx.AsyncClient | None,
    connect: Connector,
    worker_factory: WorkerFactory,
    stop: asyncio.Event,
    interval: float,
    host_store: HostCredentialStore | None,
    attestation: HostAttestation | None,
    lifecycle: LifecycleOutbox,
    install: RunnerInstall | None = None,
    runner_limit: AppliedRunnerLimit = EMPTY_RUNNER_LIMIT,
    pool_key: PoolKey | None = None,
) -> int:
    load = _LoadInterceptor()
    proxy = LlmProxy(slots=SlotStore(), ceilings=CeilingStore(), outbox=UsageOutbox())
    credentials = build_credential_resolver(config, host_store=host_store)
    isolation = build_contract_isolation(settings, can_separate_uids=can_change_uid)
    runtimes = build_agent_runtimes(
        settings,
        attestation.clis if attestation is not None else [],
        denied_folders=runner_limit.folders,
    )
    egress_posture = egress_posture_from_env()
    in_flight = DirectivesInFlight()
    self_tests = HarnessSelfTests(
        sandbox_for=lambda contract_id, cli_kind: isolation.existing_sandbox(
            contract_id, runtime_kind=cli_kind
        ),
        in_flight=in_flight,
        served=runtimes,
        monotonic=time.monotonic,
    )
    usage_windows = build_usage_windows(
        host_party=state.host_party, runtimes=runtimes, isolation=isolation, in_flight=in_flight
    )
    # One store for both: it caches the key file, and a renewal through the stream must
    # be the key the LLM Key opener reads too.
    recipient_keys = RecipientKeyStore(state_dir)
    llm_slots = llm_slot_references(
        {
            name: url
            for name, url in (
                ("openai", config.openai_base_url),
                ("anthropic", config.anthropic_base_url),
            )
            if url
        }
    )
    llm_keys = LlmKeyOpener(pool_key=pool_key, recipient_keys=recipient_keys, endpoints=llm_slots)
    stream = ControlPlaneStream(
        client=RunnerRegistrationClient(base_url=control_plane, client=http_client),
        state=state,
        isolation=mode,
        proxy=proxy,
        sealed=SealedCredentialStream(recipient_keys),
        credentials=credentials,
        llm_slots=llm_slots,
        load=load,
        egress_posture=egress_posture,
        attestation=attestation,
        lifecycle=lifecycle,
        heartbeat_stamp=state_dir / HEARTBEAT_STAMP_NAME,
        runtimes=runtimes,
        contracts=isolation,
        self_tests=self_tests,
        usage_windows=usage_windows,
        runner_limit=runner_limit_report(runner_limit, runtimes, egress_posture=egress_posture),
        install=install,
        state_dir=state_dir,
        pool_key_fingerprint=llm_keys.fingerprint,
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
    stream.oauth = OAuthConnectors(
        sealed=stream.sealed, provider=HttpOAuthProvider(), credentials=credentials
    )
    tool_servers = ToolServerHealthLog()
    stream.tool_servers = tool_servers
    link = HeartbeatLink(stream, ceilings=proxy.ceilings)
    # The first beat is the one that hands back a Runner Token: a persisted identity has
    # none, and the Temporal connection below cannot be opened without it.
    while True:
        try:
            await _before_stop(link.exchange(), stop)
            break
        except _StoppedBeforeStartError:
            raise
        except Exception as error:  # noqa: BLE001 - keep trying until the control plane answers
            if _revoked(error):
                return REVOKED_EXIT_CODE
            _logger.warning("heartbeat failed before start: %s", error)
            await _before_stop(asyncio.sleep(interval), stop)

    tls = os.environ.get(TEMPORAL_TLS_ENV, "").strip().lower() in {"1", "true", "yes"}
    hooks = build_hook_runner(config)
    async with (
        proxy,
        RunnerFastApiClient(str(settings.INTERNAL_FASTAPI_BASE_URL), state=state) as fastapi,
    ):

        async def connect_client(namespace: str, token: str) -> Client:
            return await connect(temporal_address, namespace=namespace, api_key=token, tls=tls)

        def build_worker(
            client: Client, connection: RunnerConnection, slots: ConnectionSlots
        ) -> GracefulWorker:
            return worker_factory(
                client,
                task_queue=connection.task_queue,
                activities=_activities(
                    fastapi,
                    settings=settings,
                    config=config,
                    state=state,
                    isolation=isolation,
                    agent_runtimes=runtimes,
                    directives_in_flight=in_flight,
                    hooks=hooks,
                    proxy=proxy,
                    credentials=credentials,
                    link=link,
                    token_source=stream,
                    messages=messages,
                    tool_servers=tool_servers,
                    usage_windows=usage_windows,
                    runner_limit=runner_limit,
                    llm_contracts=lambda: stream.llm_contracts,
                    llm_keys=llm_keys,
                    # The wake signal rides the connection the Directive came in on: its
                    # Organisation's own namespace (PRD issue 52, map ticket 15 §2).
                    signaller=TemporalWorkflowSignaller(client),
                    # A legacy connection's Organisation is read off its namespace's name;
                    # only an ack that names connections says which Contracts are whose.
                    connection_organisation=lambda: (
                        connection.organisation_id if stream.acks_connections else None
                    ),
                ),
                interceptors=[load],
                graceful_shutdown_timeout=graceful_shutdown_timeout(settings),
                tuner=machine.tuner(slots),
            )

        # Rev 14: one limit for the machine, whatever the number of connections.
        machine = MachineSlots(state.max_concurrent_directives)
        workers = ConnectionWorkers(
            machine=machine,
            connect_client=connect_client,
            build_worker=build_worker,  # type: ignore[arg-type]
        )
        try:
            await _before_stop(workers.reconcile(stream.connections()), stop)
        except _StoppedBeforeStartError:
            await workers.shutdown()
            raise
        revoked = asyncio.Event()
        beat = asyncio.create_task(
            _heartbeat_forever(link, stream, workers, interval, stop=stop, revoked=revoked)
        )
        poll = asyncio.create_task(_poll_sources_forever(stream.sources, _source_poll_seconds()))
        self_test = asyncio.create_task(
            _self_test_forever(self_tests, isolation, SELF_TEST_SWEEP_SECONDS)
        )
        sweeps = [beat, poll, self_test]
        if usage_windows is not None:
            sweeps.append(
                asyncio.create_task(
                    _usage_windows_forever(usage_windows, isolation, SELF_TEST_SWEEP_SECONDS)
                )
            )
        readiness.ready = True
        await run_runner_lifecycle_hook(hooks, HookName.RUNNER_STARTUP)
        try:
            await _serve_until_stopped(workers, stop)
        finally:
            readiness.ready = False
            for task in sweeps:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await run_runner_lifecycle_hook(hooks, HookName.RUNNER_SHUTDOWN)
            await _report_stop(link, lifecycle)
    return REVOKED_EXIT_CODE if revoked.is_set() else 0


def _namespace_organisation(namespace: str) -> UUID:
    """The Organisation an ``org-{id}`` namespace isolates; any other name keys as itself."""

    try:
        return UUID(namespace.removeprefix(NAMESPACE_PREFIX))
    except ValueError:
        return uuid5(NAMESPACE_URL, namespace)


def _revoked(error: Exception) -> bool:
    """Whether a failed heartbeat was the control plane revoking this Runner.

    Every other failure is a lost beat and the next tick retries it; a revocation is
    final, so retrying would only dial a control plane that has already said no.
    """

    if isinstance(error, RunnerRegistrationError) and error.reason == RUNNER_REVOKED_REASON:
        print(f"revoked by the control plane: {error}", flush=True)
        return True
    return False


async def _report_stop(link: HeartbeatLink, lifecycle: LifecycleOutbox) -> None:
    """One last beat carrying ``stop``; if it cannot land, the next start carries it."""

    lifecycle.add(LifecycleKind.STOP, datetime.now(UTC))
    try:
        await asyncio.wait_for(link.exchange(), timeout=10)
    except Exception as error:  # noqa: BLE001 - the fact stays queued in the state dir
        _logger.warning("stop not reported yet (%s); the next start carries it", error)


async def _serve_until_stopped(workers: ConnectionWorkers, stop: asyncio.Event) -> None:
    """Poll until SIGTERM (or ``stop``), then drain every connection's Worker: stop
    polling, let the running activities finish. A Worker that fails on its own stops the
    process with its error, as the one Worker's did before connections."""

    stopped = asyncio.ensure_future(stop.wait())
    failed = asyncio.ensure_future(workers.failed.wait())
    try:
        await asyncio.wait({stopped, failed}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        stopped.cancel()
        failed.cancel()
    _logger.info("draining in-flight activities before exit")
    await workers.shutdown()
    if workers.error is not None:
        raise workers.error


async def _heartbeat_forever(
    link: HeartbeatLink,
    stream: ControlPlaneStream,
    workers: ConnectionWorkers,
    interval: float,
    *,
    stop: asyncio.Event,
    revoked: asyncio.Event,
) -> None:
    """Every 30 s, for as long as the process lives; a missed beat is logged, not fatal.

    Staleness is the link's own property (issue 44): after three minutes of failures the
    verb seams refuse on their own, so there is nothing for this loop to do about a
    failure except try again on the next tick. A revocation is the exception: it drains
    the Workers exactly as SIGTERM would, and the process exits non-zero. Each ack's
    connections are reconciled into the Workers (rev 14): a new one starts polling, a
    renewed token reaches its client, and a gone one drains.
    """

    while True:
        await asyncio.sleep(interval)
        try:
            await link.exchange()
        except Exception as error:  # noqa: BLE001 - any failed exchange is a lost beat
            if _revoked(error):
                revoked.set()
                stop.set()
                return
            _logger.warning("heartbeat failed: %s", error)
            continue
        try:
            await workers.reconcile(stream.connections())
        except Exception as error:  # noqa: BLE001 - a connection that cannot open retries
            _logger.warning("a connection could not start: %s", error)


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


async def _self_test_forever(
    self_tests: HarnessSelfTests, isolation: ContractIsolation, interval: float
) -> None:
    """Each harness root's self-test at start, then whenever it falls due (local-agents 17).

    Only roots already on disk: a Contract's uid is allocated by its first Directive,
    which records the allocation, and a self-test must not be the call that does it.
    """

    while True:
        try:
            await self_tests.run_due(isolation.harness_roots())
        except Exception as error:  # noqa: BLE001 - a lost sweep is retried on the next one
            _logger.warning("harness self-test sweep failed: %s", error)
        await asyncio.sleep(interval)


def build_usage_windows(
    *,
    host_party: str,
    runtimes: Mapping[str, AgentRuntime],
    isolation: ContractIsolation,
    in_flight: DirectivesInFlight,
) -> UsageWindows | None:
    """A probe per served harness, or None on a Runner that runs no subscription
    (local-agents 10): only where one can run is there a usage window anyone's to read
    (``auth_mode.runs_subscriptions``, QTS-1450).
    """

    if not runs_subscriptions(host_party, separates_contract_uids=isolation.can_separate_uids):
        return None
    probes: dict[str, UsageWindowProbe] = {
        "codex_cli": CodexRateLimitsProbe(),
        "claude_code": ClaudeUsageScreenProbe(),
    }
    return UsageWindows(
        probes={kind: probe for kind, probe in probes.items() if kind in runtimes},
        sandbox_for=lambda contract_id, cli_kind: isolation.existing_sandbox(
            contract_id, runtime_kind=cli_kind
        ),
        in_flight=in_flight,
        monotonic=time.monotonic,
    )


async def _usage_windows_forever(
    usage_windows: UsageWindows, isolation: ContractIsolation, interval: float
) -> None:
    """Each signed-in harness root's usage windows, whenever its probe falls due
    (local-agents 10). Whether a key is also held does not matter: from rev 3 the control
    plane chooses the subscription or the key per Directive, and a usage limit is what
    sends the work elsewhere, so the heartbeat reports it either way."""

    while True:
        try:
            await usage_windows.run_due(
                [
                    (contract_id, cli_kind)
                    for contract_id, cli_kind in isolation.harness_roots()
                    if holds_login(isolation, contract_id, cli_kind)
                ]
            )
        except Exception as error:  # noqa: BLE001 - a lost sweep is retried on the next one
            _logger.warning("usage window sweep failed: %s", error)
        await asyncio.sleep(interval)


def _activities(
    fastapi: RunnerFastApiClient,
    *,
    settings: WorkerSettings,
    config: RunnerConfig,
    state: RunnerState,
    isolation: ContractIsolation,
    agent_runtimes: Mapping[str, AgentRuntime],
    directives_in_flight: DirectivesInFlight,
    hooks: HookRunner,
    proxy: LlmProxy,
    credentials: CredentialResolver,
    link: HeartbeatLink,
    token_source: DirectiveTokenSource | None = None,
    messages: MessageStore | None = None,
    signaller: TemporalWorkflowSignaller | None = None,
    tool_servers: ToolServerHealthLog | None = None,
    usage_windows: UsageWindows | None = None,
    runner_limit: AppliedRunnerLimit | None = None,
    llm_contracts: Callable[[], list[ContractLlmTerms] | None] | None = None,
    llm_keys: LlmKeyOpener | None = None,
    connection_organisation: Callable[[], UUID | None] | None = None,
) -> list[Callable[..., Any]]:
    github_client = GitHubAppClient(
        app_id=os.getenv("GITHUB_APP_ID"),
        installation_id=os.getenv("GITHUB_APP_INSTALLATION_ID"),
        private_key_pem=os.getenv("GITHUB_APP_PRIVATE_KEY_PEM"),
        api_base_url=os.getenv("GITHUB_API_BASE_URL", "https://api.github.com"),
    )
    static_git_token = os.getenv("AGENTIC_OS_GIT_TOKEN") or None
    attempt_records = AttemptRecords(config.state_dir, workspace_root=settings.WORKSPACE_ROOT)
    # Only the dead go: a live record is a harness this process lost on restart, still
    # writing its Workspace, and the next attempt must keep being refused (RR-05).
    attempt_records.sweep()
    ralph = RunnerRalphActivities(
        fastapi,
        runtime_context_resolver=WorkerRuntimeContextResolver(fastapi),
        agent_runtimes=agent_runtimes,
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
        attempt_records=attempt_records,
        directives_in_flight=directives_in_flight,
        usage_windows=usage_windows,
        runner_limit=runner_limit,
        llm_contracts=llm_contracts,
        llm_keys=llm_keys,
        connection_organisation=connection_organisation,
    )
    return [
        *ralph.activity_callables(),
        *RunnerTriageActivities(
            proxy=proxy,
            model=settings.TRIAGE_MODEL,
            timeout_seconds=settings.TRIAGE_DIRECTIVE_TIMEOUT_SECONDS,
            host_party=state.host_party,
            llm_contracts=llm_contracts,
            connection_organisation=connection_organisation,
        ).activity_callables(),
    ]
