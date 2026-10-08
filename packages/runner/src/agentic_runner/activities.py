"""The Runner's half of the Ralph Loop: the activities that must run where the work is.

The locality rule (ADR-0013 §2): an activity runs on the Runner **iff** it touches the
workspace, the Agent Runtime subprocess, a verb seam, or a credential the Runner holds.
That is the eight activities registered here — the two Directive turns, the four verb
seams (``push`` / ``pr.open`` / ``pr.review`` / ``pr.merge``), the verifier, and the two
that delete workspaces. The platform's ``workflows.activity_registry`` is the table, and
its ``workflows.activities`` is the platform's half.

They do the thing and return facts. Nothing here is a bookkeeping call the workflow could
make through a platform activity instead, and nothing is cached between activities
(ADR-0013 §3): the workspace a clone produced and the head a push produced both arrive on
an input and leave on an output. The Grant snapshot is the one exception, and it is not a
cache: a registered Runner is *pushed* it over its heartbeat stream and holds it per Agent
in ``heartbeat_link`` (PRD issue 44), which is what makes a narrowing bite at the next verb
rather than the next Directive.

A Protected Path (ADR-0011 §13): this module holds the four verb seams, so a PR touching
it always hits a human gate.
"""

import asyncio
import contextlib
import hashlib
import json
import re
import secrets
import shlex
import shutil
import tempfile
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path
from typing import Any, NoReturn, Protocol
from uuid import UUID

from temporalio import activity
from temporalio.exceptions import ApplicationError

from agentic_runner.attempts import (
    AttemptRecords,
    PriorAttemptAliveError,
    fence_work_record,
    refuse_if_prior_attempt_alive,
)
from agentic_runner.callback import (
    CALLBACK_TOKEN_ENV,
    AnnotateRequest,
    AnnotateResponse,
    ArtifactRequest,
    ArtifactResponse,
    AskRequest,
    AskResponse,
    AttemptCallbackServer,
    CallbackHandlers,
    MessageListRequest,
    MessageListResponse,
    MessageSendRequest,
    MessageSendResponse,
    VerbRequest,
    VerbResponse,
)
from agentic_runner.credentials import (
    CREDENTIAL_RESOLVED_SOURCE,
    CREDENTIAL_UNRESOLVABLE_SOURCE,
    CredentialResolver,
    DirectiveCredentials,
    EmptyCredentialStore,
    UnresolvableCredentialReferenceError,
)
from agentic_runner.egress import EGRESS_REFUSED_SOURCE, EgressProxy, Resolver, host_of
from agentic_runner.heartbeat_link import HeartbeatLink, SeamUnavailableError
from agentic_runner.hooks import (
    HOOK_EVIDENCE_SOURCE,
    AttemptFacts,
    DirectiveHookSession,
    HookName,
    HookRefusedError,
    HookRun,
    HookRunner,
    prepare_attempt_dir,
)
from agentic_runner.integrations.git.contracts import (
    AdoptWorkspaceRequest,
    CheckoutWorkBranchRequest,
    CloneWorkspaceRequest,
    CommitAllRequest,
    FetchBranchRequest,
    GitWorkspace,
    PrepareVerifierWorkspaceRequest,
    PushBranchRequest,
)
from agentic_runner.integrations.git.evidence import read_head_commit
from agentic_runner.integrations.git.workspace import (
    GitWorkspacePolicyError,
    contract_workspace_path,
)
from agentic_runner.integrations.github.auth import GitHubAppAuthenticationError
from agentic_runner.llm_proxy import LlmProxy
from agentic_runner.mcp import (
    MCP_ASSEMBLY_SOURCE,
    McpPlan,
    RunnerHostedServer,
    Spawner,
    ToolServerHealthLog,
    entry_for,
    plan_mcp,
)
from agentic_runner.message_store import MessageStore, WorkflowSignaller
from agentic_runner.registration import RunnerRegistrationError
from agentic_runner.runtime import verifier_command
from agentic_runner.runtime.verifier_command import CommandValidationError
from agentic_runner.workers._runtime_support import (
    RESERVED_DIRECTIVE_ENV,
    bound_text,
    bound_text_tail,
)
from agentic_runner.workers.agent_runtime import (
    AgentRuntime,
    AuthModel,
    DirectiveRequest,
    DirectiveResult,
    ResumableAgentRuntime,
)
from agentic_runner.workers.contract_isolation import (
    NO_CONTRACT,
    ContractIsolation,
    DirectiveSandbox,
    contract_path_segment,
)
from agentic_runner.workers.fastapi_client import (
    DirectiveTokenSource,
    WorkerFastApiClientError,
    directive_token,
)
from agentic_runner.workers.harness_usage import extract_claude_result, extract_codex_turn_usage
from agentic_runner.workers.mcp_config import McpServerEntry
from agentic_runner.workers.skills import (
    SkillDelivery,
    delivery_for,
    first_digest_mismatch,
    remove_skills,
    skills_preamble,
    write_skills,
)
from agentic_runner_contracts.activity_io import (
    LEARNER_CHILDREN_BUDGET_BYTES,
    LEARNING_FAILED,
    LEARNING_PROPOSED,
    LEARNING_RESERVE_EXHAUSTED,
    ApprovalWaitInput,
    ApprovalWaitOutput,
    BranchPullRequestInput,
    BranchPullRequestOutput,
    ChangedFilesInput,
    ChangedFilesOutput,
    ContractResidueInput,
    ContractResidueOutput,
    DirectiveUsage,
    FixDirectiveInput,
    FixDirectiveOutput,
    GitHubCallError,
    GitHubCallFailure,
    LearningDirectiveInput,
    LearningDirectiveOutput,
    MemberDirectiveInput,
    MemberDirectiveOutput,
    OwnerConfirmationPending,
    ProposedLesson,
    PullRequestCloseInput,
    PullRequestCloseOutput,
    PullRequestCommentInput,
    PullRequestCommentOutput,
    PullRequestMergeInput,
    PullRequestMergeOutput,
    PullRequestReadyInput,
    PullRequestReadyOutput,
    PullRequestReviewInput,
    PullRequestReviewOutput,
    QuestionAsked,
    RalphActivities,  # noqa: F401  re-exported for backward-compatible imports
    RepositoryProbeInput,
    RepositoryProbeOutput,
    RequestReviewInput,
    RequestReviewOutput,
    VerifierRunInput,
    VerifierRunOutput,
    WorkspaceReassemblyInput,
    WorkspaceReassemblyOutput,
    WorkspaceRetentionOutput,
    learner_children_json,
)
from agentic_runner_contracts.channel_messages import (
    VERDICT_BLOCK,
    MessageEnvelope,
    PendingMessagesSignal,
)
from agentic_runner_contracts.epics import (
    END_REASON_NEEDS_HUMAN,
    KIND_REPORT,
    KIND_WORK,
    PLAN_CHILDREN_MAX,
    PLAN_EDGES_MAX,
    PLAN_FILE,
    PLAN_ID_MAX,
    PLAN_TEXT_MAX,
    PLAN_TITLE_MAX,
    EpicBoard,
    EpicPlan,
    PlannedChild,
    PlannedEnding,
)
from agentic_runner_contracts.github_port import (
    ApprovalRequest,
    ApprovalState,
    BranchRequest,
    CommentRequest,
    GitHubClient,
    MergeRequest,
    PullRequestCloseRequest,
    PullRequestFilesRequest,
    PullRequestReadyRequest,
    PullRequestRequest,
    PullRequestRetargetRequest,
    PullRequestReviewRequest,
    RepositoryReadRequest,
    ReviewEvent,
    ReviewRequest,
)
from agentic_runner_contracts.grants import (
    CHANNEL_READ_VERB,
    CHANNEL_RESOURCE_TYPE,
    CHANNEL_WRITE_VERB,
    PR_COMMENT_VERB,
    PR_MERGE_VERB,
    PR_OPEN_VERB,
    PR_REVIEW_VERB,
    PUSH_VERB,
    REPO_RESOURCE_TYPE,
    UNENFORCED_SNAPSHOT,
    Decision,
    GrantSnapshot,
)
from agentic_runner_contracts.grants.seam import VerbDecision, decide_verb
from agentic_runner_contracts.questions import WORK_ASK_VERB
from agentic_runner_contracts.redaction import redact_secret_like_text
from agentic_runner_contracts.routing import (
    RoutingRefusedError,
    RunnerRoutingIdentity,
    assert_routed,
)
from agentic_runner_contracts.runtime_context import (
    EGRESS_ALLOW_LIST_KEY,
    McpServerSpec,
    SkillVersionSpec,
    WorkerRuntimeContext,
    WorkerRuntimeContextResolver,
    work_branch_name,
)
from agentic_runner_contracts.swarm import ROLE_LEAD

_GIT_EVIDENCE_LIMIT_BYTES = 32_768

_VERIFIER_FAILURE_OUTPUT_LIMIT_BYTES = 8_192

_RUNTIME_EVIDENCE_STREAM_LIMIT_BYTES = 16_384

_PERSONA_INSTRUCTIONS_LIMIT_BYTES = 8_192

_MERGE_ERROR_SUMMARY_LIMIT = 512

_GRANT_EVALUATION_SOURCE = "ralph.grant_evaluation"

_DEFAULT_CLI_KIND = "codex_cli"

_CONTRACT_WIPE_SOURCE = "ralph.contract_wipe"

_CONTRACT_UID_SOURCE = "ralph.contract_uid"

_WORKSPACE_RETENTION_SOURCE = "ralph.workspace_retention"

_EXPIRED_WORKSPACE_BATCH = 2_000

_WIPEABLE_CONTRACT_STATES = frozenset({"terminated", "declined", "expired"})

_OWNER_CONFIRMATION_SOURCE = "ralph.owner_confirmation"

_OWNER_CONFIRMATION_FILE_LIST_LIMIT = 50

_GITHUB_FAILURE_DETAIL_LIMIT = 500

_MERGE_SEAM_SOURCE = "ralph.merge_seam"

_ANNOTATION_SOURCE = "runner.annotation"

_ARTIFACT_SOURCE = "runner.artifact"

# PRD issue 52: one Evidence Event per Message, its metadata and never its body.
_MESSAGE_SOURCE = "channel.message"
# PRD issue 53: a member Directive ran (who, why, how many Messages it consumed), and a
# `pr.*` verb refused because its issuer is not the Lead -- named by role, whatever the
# Grant says (ADR-0012 §1).
_SWARM_DIRECTIVE_SOURCE = "swarm.directive"
_ROLE_REFUSAL_SOURCE = "swarm.role_refusal"
# PRD issue 54: the Critic's veto, one Evidence stream for the workflow's records of it
# and this Runner's own refusal of a `pr.review` / `pr.merge` it was handed no clear for.
_CRITIC_VETO_SOURCE = "critic.veto"
# PRD issue 55: the Learning Directive's run, and where the Learner leaves its proposals.
# The file is in the Workspace because that is the one directory every harness sandbox
# lets the Directive write; the Runner reads it and deletes it before anything else runs.
_LEARNING_RUN_SOURCE = "learning.directive_run"
_LESSONS_FILE = Path(".agentic-learner") / "lessons.json"
# PRD issue 59: where a Lead's Directive writes its plan (children to open, children to
# end, whether it ends the Epic). Read and deleted before anything is committed, so the
# plan never lands on the branch; the control plane evaluates `work.open` / `work.end`.
_PLAN_FILE = Path(PLAN_FILE)
# The retargets a base's `pr.merge` seam performed on its dependents (19 A3).
_STACK_SOURCE = "stack"
_LESSONS_MAX = 10
_LESSON_TEXT_LIMIT = 8_000
_LEARNER_INPUT_LIMIT_BYTES = 60_000

_ANNOTATION_LIMIT_BYTES = 16_384

_UNCLEARED_OUTCOME_REASON = (
    "the Outcome ceiling was not cleared for this merge: neither the Autonomy Policy "
    "nor an org Approval authorised it (ADR-0011 s1)"
)

_SAFE_REPO_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")

_HEARTBEAT_INTERVAL_SECONDS = 15.0

_APPROVAL_POLL_INTERVAL_SECONDS = 30.0

_APPROVAL_POLLS_PER_ACTIVITY = 10

_APPROVAL_ACTIVITY_TIME_BUDGET_SECONDS = 360.0


@dataclass(frozen=True, slots=True)
class HarnessSession:
    """The harness session a Directive attempt started, and where it ran (local-agents 16).

    A retry may resume it only when the Contract, the Workspace path and the head all
    match exactly (ADR-0007, amendment 2026-10-03).
    """

    session_id: str
    contract_id: str | None
    workspace_path: str
    head: str | None

    @classmethod
    def from_heartbeat(cls, details: object) -> "HarnessSession | None":
        if not isinstance(details, Mapping):
            return None
        session_id = details.get("session_id")
        contract_id = details.get("contract_id")
        workspace_path = details.get("workspace_path")
        head = details.get("head")
        if (
            not isinstance(session_id, str)
            or not isinstance(workspace_path, str)
            or not isinstance(contract_id, str | None)
            or not isinstance(head, str | None)
        ):
            return None
        return cls(session_id, contract_id, workspace_path, head)


def _why_not_resume(
    prior: HarnessSession | None,
    here: HarnessSession,
    agent_runtime: AgentRuntime,
    sandbox: DirectiveSandbox | None,
) -> str | None:
    if prior is None:
        return "no_prior_session"
    if prior.contract_id != here.contract_id:
        return "contract_changed"
    if prior.workspace_path != here.workspace_path:
        return "workspace_changed"
    if here.head is None or prior.head != here.head:
        return "head_changed"
    if not isinstance(agent_runtime, ResumableAgentRuntime):
        return "runtime_cannot_resume"
    if not agent_runtime.has_session(prior.session_id, sandbox):
        return "session_not_found"
    return None


class _Liveness:
    """What one attempt's heartbeats carry: its first start, and its harness session.

    Both ride in the Temporal heartbeat details rather than in the Runner's process
    (ADR-0013 §3), so the retry that follows a lost pod still finds them.
    """

    def __init__(
        self, *, first_started: float, now: float, prior_session: HarnessSession | None
    ) -> None:
        self.earlier_attempts_seconds = max(0.0, now - first_started)
        self.prior_session = prior_session
        self._first_started = first_started
        self._session: HarnessSession | None = None

    def details(self) -> tuple[object, ...]:
        if self._session is None:
            return (self._first_started,)
        return (self._first_started, asdict(self._session))

    def record_session(self, session: HarnessSession) -> None:
        self._session = session
        # At once, not at the next beat: the attempt may be lost before then.
        if activity.in_activity():
            activity.heartbeat(*self.details())


@contextlib.asynccontextmanager
async def _liveness_heartbeats() -> AsyncIterator[_Liveness]:
    """Emit periodic activity heartbeats from a background task while the body runs.

    Async-activity heartbeats are delivered on the worker event loop, so the wrapped
    body must keep the loop free during its long stretches (the agent runtimes use
    asyncio subprocesses; the verifier subprocess runs via asyncio.to_thread). No-op
    outside an activity context so tests can invoke activity methods directly.

    Yields the seconds earlier attempts of this same activity already spent (PRD issue
    47, 23 item 4). Each beat carries the first attempt's wall-clock start as its
    heartbeat details, so the retry that follows a laptop's sleep -- the attempt Temporal
    failed on its heartbeat timeout -- charges the Work Record's wall-clock from there:
    sleep is an outage, and the Budget keeps running through it. Once the Agent Runtime
    names its session, the beats carry that too (local-agents 16).
    """
    now = time.time()
    if not activity.in_activity():
        yield _Liveness(first_started=now, now=now, prior_session=None)
        return

    details = activity.info().heartbeat_details
    liveness = _Liveness(
        first_started=float(details[0]) if details else now,
        now=now,
        prior_session=HarnessSession.from_heartbeat(details[1]) if len(details) > 1 else None,
    )

    async def beat() -> None:
        while True:
            activity.heartbeat(*liveness.details())
            await asyncio.sleep(_HEARTBEAT_INTERVAL_SECONDS)

    heartbeater = asyncio.create_task(beat())
    try:
        yield liveness
    finally:
        heartbeater.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeater


class RunnerRalphFastApiClient(Protocol):
    """What a Runner activity reads from the control plane at run time (ADR-0013 §2).

    Deliberately small. The Runner never calls the control plane for bookkeeping: the
    reads here are what an activity cannot do its work without — the Work Record's
    runtime context and what the Directive spent. The Grant snapshot is no longer among
    them (PRD issue 44): it is pushed on change over the heartbeat stream and read from
    ``heartbeat_link.HeartbeatLink``. The writes below are the Evidence a verb seam
    records, over HTTP under the Directive token the activity pulled for its execution
    (PRD issue 63) -- or, before one exists, the signed Runner envelope.
    """

    async def get_runtime_context(
        self, work_record_id: str, agent_id: str | None = None
    ) -> dict[str, Any]:
        """Load sanitized worker runtime context -- the Work Record's own Agent's, or
        the Swarm member ``agent_id`` names (PRD issue 53)."""

    async def raise_owner_confirmation(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Ask the Agent's owner before a ``confirm`` verb acts (ADR-0011 §14-16)."""

    async def raise_question(
        self, work_record_id: str, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        """The `work.ask` seam: evaluate, and record the Question it allows (PRD issue 60)."""

    async def get_question(self, question_id: str) -> dict[str, Any]:
        """A Question and its answer, for the Directive its hold wakes (PRD issue 60)."""

    async def get_directive_usage(
        self,
        work_record_id: str,
        directive_id: str,
    ) -> dict[str, Any]:
        """Read one Directive's metered token aggregate (PRD issue 13)."""

    async def report_harness_usage(self, payload: Mapping[str, Any]) -> list[dict[str, Any]]:
        """One Usage Record per model off a harness's own JSON (PRD issue 31, 17 A9)."""

    async def list_expired_workspaces(self, work_record_ids: list[str]) -> list[str]:
        """Which of these Work Records' Workspaces are past retention (PRD issue 30)."""

    async def append_evidence(
        self,
        work_record_id: str,
        *,
        source: str,
        payload: Mapping[str, Any],
        actor: str = "temporal-worker",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Append bounded worker evidence to the work record."""

    async def transition_work_record(
        self,
        work_record_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Advance the work record lifecycle through the control-plane API."""

    async def record_verifier_result(
        self,
        work_record_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Record verifier result evidence through the control-plane API."""


@dataclass(frozen=True)
class _SeamVerdict:
    """What one verb seam concluded: proceed, refuse, or ask the owner (ADR-0011 s8-16).

    Three outcomes, not two. ``owner_consented`` is a ``confirm`` the owner has already
    answered — through a Scoped Allowance found at the raise, or through a signal that
    reached the workflow on an earlier attempt — and is the only way a ``confirm`` ever
    becomes a proceed.
    """

    decision: VerbDecision
    owner_consented: bool = False
    pending: OwnerConfirmationPending | None = None

    @property
    def allowed(self) -> bool:
        return self.pending is None and (self.decision.allowed or self.owner_consented)

    @property
    def refused(self) -> bool:
        return self.pending is None and not self.allowed

    @property
    def refusal_summary(self) -> str:
        return self.decision.refusal_summary


@dataclass(frozen=True)
class _RuntimeContextState:
    reviewer: str | None
    repository: str
    verifier_argv: tuple[str, ...]
    work_branch: str
    completion_criteria: str
    base_branch: str = ""
    agent_id: str | None = None
    # The Contract the Work Record's Directives run under: the OS line on the Runner
    # (ADR-0015 §1). Read off the runtime context, not the grant snapshot, so a Work
    # Record with no Agent bound still lands in its own directory.
    contract_id: str | None = None
    # Which harness this Work Record runs, and so which per-Contract config root
    # (`{contract_id}/harness/{cli_kind}`) its Directives read (ADR-0015 §4).
    cli_kind: str = _DEFAULT_CLI_KIND
    # PRD issue 31: what the Agent is configured to run, for pricing a Codex harness
    # usage event that carries no model of its own.
    model: str | None = None
    # The Agent Runtime Profile's per-process memory ceiling (map ticket 17 A7), carried
    # on the Profile's `command_policy` blob — the sandbox floor already lives there, and
    # a ceiling is exactly a floor rule. None means the Runner's own default applies.
    memory_limit_bytes: int | None = None
    # Pushed on change and applied on receipt (ADR-0011 §11, PRD issue 44): the evaluator
    # is local, so a narrowing lands on the next verb this state is read for.
    grant_snapshot: GrantSnapshot = UNENFORCED_SNAPSHOT
    # The snapshot exactly as the control plane served it. Carried beside the parsed form
    # so a Runner activity can hand the same bytes to the next one through activity I/O
    # (ADR-0013 §3) without re-serialising a parsed model.
    grant_snapshot_payload: dict[str, Any] = field(default_factory=dict)
    persona_slug: str | None = None
    persona_instructions: str = ""
    # PRD issue 56: the control plane's rendered Experience payload and the ids in it.
    experience: str = ""
    experience_lesson_ids: tuple[str, ...] = ()
    experience_truncated: bool = False
    # Console-v2 issue 28: the Skill versions attached to the Agent.
    skills: tuple[SkillVersionSpec, ...] = ()
    workspace_path: Path | None = None
    branch_head_sha: str | None = None
    # The Autonomy Policy's tier source (PRD issue 14) -- see WorkerRuntimeContext for
    # what each field means and why every default reads as "hold for human".
    product_id: str | None = None
    data_class: str | None = None
    action_tier: str | None = None
    risk_tier: str | None = None
    requester_id: str | None = None
    tier_within_contract_ceiling: bool = False
    # PRD issue 48, map 22 A1: the Credential Reference names this Contract declares.
    # Names, never values -- what the Runner may look up in its host store, read at this
    # Directive boundary like the Grant snapshot beside it.
    credential_references: tuple[str, ...] = ()
    # PRD issue 58: the MCP registry rows bound to the Work Record's Product, decided per
    # Directive against the Grant, and the Profile's egress allow-list.
    mcp_servers: tuple[McpServerSpec, ...] = ()
    egress_allow_list: tuple[str, ...] = ()
    # Console-v2 issue 23: a `report` Work Record's Directives run in an empty directory --
    # no clone, nothing committed or pushed.
    kind: str = KIND_WORK


@dataclass(slots=True)
class _DirectiveAttempt:
    """One Directive attempt: its hook lifecycle and the environment it hands the Agent.

    ``extra_env`` starts as the callback socket and bearer the Runner just minted and
    grows by whatever the ``environment`` hook exported; it is what reaches the Agent
    Runtime subprocess on top of the environment the runtime builds for itself.
    """

    hooks: DirectiveHookSession
    extra_env: dict[str, str] = field(default_factory=dict)
    # PRD issue 48 / 17 A10: the Contract's Credential References, resolved once at the
    # start of this attempt. Deliberately *not* merged into `extra_env` -- that mapping
    # goes to the Agent Runtime subprocess and to every hook, and a credential value
    # reaching either is the invariant ADR-0011 §9 exists to hold.
    credentials: DirectiveCredentials | None = None
    # PRD issue 58: what config assembly wrote for the CLI, and where it may reach.
    mcp_servers: tuple[McpServerEntry, ...] | None = None
    egress_allow_list: tuple[str, ...] = ()
    # Console-v2 issue 28: the attached Skills, for a runtime with no skills directory to
    # write them into. Prefixed to the Directive prompt; empty when there is none.
    skills_preamble: str = ""

    async def run_environment_hook(self) -> None:
        self.extra_env.update(await self.hooks.environment())

    @property
    def env(self) -> tuple[tuple[str, str], ...]:
        return tuple(sorted(self.extra_env.items()))


def _egress_allow_list(network_policy: Mapping[str, object]) -> tuple[str, ...]:
    entries = network_policy.get(EGRESS_ALLOW_LIST_KEY)
    if not isinstance(entries, list):
        return ()
    return tuple(str(entry) for entry in entries if str(entry).strip())


def _optional_uuid(value: str | None) -> UUID | None:
    """A platform id as a UUID, or None. A malformed one is dropped rather than raised:
    attribution must never be the reason a Directive fails (PRD issue 13's posture)."""

    if not value:
        return None
    try:
        return UUID(value)
    except ValueError:
        return None


def _file_sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _changed_paths_from_git_status(status: str) -> tuple[str, ...]:
    """The paths a ``git status --porcelain`` block names, for the owner's file list.

    Porcelain v1 is ``XY <path>`` with a two-character status field, and a rename reads
    ``R  old -> new``; the destination is what the owner cares about. Anything that does
    not parse is dropped rather than guessed — the file list is a disclosure, not a
    decision input the seam depends on.
    """

    paths: list[str] = []
    for line in status.splitlines():
        candidate = line[3:].strip() if len(line) > 3 else ""
        if not candidate:
            continue
        _, separator, destination = candidate.partition(" -> ")
        paths.append(destination.strip() if separator else candidate)
        if len(paths) == _OWNER_CONFIRMATION_FILE_LIST_LIMIT:
            break
    return tuple(paths)


def _owner_confirmation_summary(
    *,
    verb: str,
    request: BranchPullRequestInput,
    changed_paths: tuple[str, ...],
) -> str:
    """What the owner reads instead of a link they may not be able to open (s14)."""

    what = (
        f"Push the work branch {request.branch_name} to {request.repository}"
        if verb == PUSH_VERB
        else f"Open a draft pull request on {request.repository} from {request.branch_name}"
    )
    return f"{what} ({len(changed_paths)} changed file(s)). {request.pr_title}"


def directive_id(*, work_record_id: str, directive_number: int) -> str:
    """The id one Directive's Usage Records are keyed by (map ticket 12 B2).

    Derived, not minted: the Runner stamps it on every LLM call it makes for the
    Directive and reads the aggregate back under the same key, and a re-run of Directive
    N (an owner's confirmation, ADR-0011 s15) is the same Directive spending more.
    """

    # The raw Work Record id, not the hashed token the idempotency keys use: this one is
    # read by humans in the ledger and joined against `work_records`. Bounded to the
    # column's 128 characters, which a UUID and a Directive number never approach.
    return f"{work_record_id}:{directive_number}"[:128]


def _runtime_verifier_argv(runtime_context: WorkerRuntimeContext) -> tuple[str, ...]:
    metadata = runtime_context.product_verifier_command_source.metadata
    if not runtime_context.product_verifier_command_source.available:
        raise RuntimeError("Product verifier command is unavailable")
    command = metadata.get("command")
    if not isinstance(command, str) or not command.strip():
        raise RuntimeError("Product verifier command is unavailable")
    try:
        argv = tuple(shlex.split(command))
    except ValueError:
        # Malformed product-owned command text must fail closed in the verifier
        # stage, after lifecycle transition and rejected evidence recording.
        # Use the verifier command validator's existing empty-argv rejection
        # rather than persisting or surfacing raw malformed command text here.
        return ()
    if not argv:
        raise RuntimeError("Product verifier command is unavailable")
    return argv


def _verifier_evidence_payload(result: Any) -> dict[str, object]:
    return {
        "command_hash": result.command_hash,
        "exit_code": result.exit_code,
        "passed": result.passed,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "elapsed_ms": result.elapsed_ms,
        "argv": result.argv,
        "env_keys": result.env_keys,
    }


def _verifier_summary(result: Any) -> str:
    status = "passed" if result.passed else "failed"
    return f"Verifier {status} with exit code {result.exit_code}; command {result.command_hash}"


def _verifier_failure_output(result: Any) -> str:
    """Bounded tail of the failing verifier's captured output for the fix Directive.

    The one-line summary alone forced fix Directives to re-run the entire suite in the
    workspace just to rediscover which tests failed (observed on work record
    f6223d2a-f2ee-43e2-b023-aefecda953f9: ~6 minutes of pure rediscovery). Capture
    already redacted the streams; re-redacting here is defense-in-depth at the
    prompt seam."""
    sections: list[str] = []
    for label, text in (("stdout", result.stdout), ("stderr", result.stderr)):
        if not text.strip():
            continue
        notes: list[str] = []
        tail = bound_text_tail(
            redact_secret_like_text(text),
            _VERIFIER_FAILURE_OUTPUT_LIMIT_BYTES,
            notes,
            label,
        )
        note_suffix = f" [{'; '.join(notes)}]" if notes else ""
        sections.append(f"--- verifier {label} (tail){note_suffix} ---\n{tail.strip()}")
    return "\n\n".join(sections)


def _verifier_workspace_attestation_failure_summary(exc: Exception) -> str:
    error = redact_secret_like_text(str(exc))
    return f"verifier workspace attestation failed before verifier: {error}"


def _verifier_workspace_attestation_failure_payload(
    *,
    request: VerifierRunInput,
    state: _RuntimeContextState,
    workspace_root: Path,
    error: Exception,
) -> dict[str, object]:
    workspace_path = state.workspace_path
    return {
        "reason": "verifier_workspace_attestation_failed",
        "repository": request.repository,
        "pr_number": request.pr_number,
        "merge_commit_sha": request.merge_commit_sha,
        "approved_head_sha": request.approved_head_sha,
        "work_branch": state.work_branch,
        "workspace_path": (
            _workspace_id(workspace_path, workspace_root)
            if workspace_path is not None
            else "<unavailable>"
        ),
        "passed": False,
        "exit_code": 126,
        "stdout": "",
        "stderr": redact_secret_like_text(str(error)),
    }


def _invalid_verifier_command_payload(request: VerifierRunInput) -> dict[str, object]:
    return {
        "reason": "invalid_verifier_command",
        "repository": request.repository,
        "pr_number": request.pr_number,
        "merge_commit_sha": request.merge_commit_sha,
        "approved_head_sha": request.approved_head_sha,
        "passed": False,
        "exit_code": 126,
        "stdout": "",
        "stderr": "invalid_verifier_command",
    }


def _approved_head_mismatch_summary(
    *,
    local_head_sha: str | None,
    approved_head_sha: str,
) -> str:
    local = local_head_sha if local_head_sha is not None else "<unknown>"
    return (
        "approved head mismatch before verifier: local head "
        f"{local} does not match approved head {approved_head_sha}"
    )


def _approved_head_mismatch_payload(
    *,
    request: VerifierRunInput,
    local_head_sha: str,
    verifier_summary: str,
) -> dict[str, object]:
    return {
        "reason": "approved_head_mismatch",
        "repository": request.repository,
        "pr_number": request.pr_number,
        "merge_commit_sha": request.merge_commit_sha,
        "approved_head_sha": request.approved_head_sha,
        "local_head_sha": local_head_sha,
        "verifier_summary": verifier_summary,
    }


def _approvals_pending_summary(observed: int, required: int) -> str:
    return (
        f"merge held: {observed} of {required} required approvals from distinct org "
        "humans stand on the verified head"
    )


def _merge_exception_summary(exc: Exception) -> str:
    prefix = "GitHub merge failed: "
    message_limit = _MERGE_ERROR_SUMMARY_LIMIT - len(prefix)
    return f"{prefix}{redact_secret_like_text(str(exc))[:message_limit]}"


def _persona_prompt_preamble(
    *,
    persona_slug: str | None,
    instructions: str,
    notes: list[str],
    experience: str = "",
) -> str:
    """The routed Persona's operator-authored instructions, prepended to every Directive
    prompt (persona-specialists issue 02). Empty instructions yield an empty preamble so
    the assembled prompt stays byte-identical to the pre-Persona form.

    The pipeline guardrail is restated *after* the instructions and named as taking
    precedence, so operator-authored text can never override the pipeline's ownership of
    commit/push/PR — including in fix Directives, whose base prompt has no guardrail of
    its own. Oversized instructions are head-truncated into ``notes`` (the head carries an
    operator's lead material) rather than failing the Directive.

    ``experience`` (PRD issue 56) is the control plane's rendered Experience payload,
    already bounded; it sits beside the instructions and under the same guardrail.
    """
    if not instructions.strip() and not experience:
        return ""
    persona_section = ""
    if instructions.strip():
        bounded = bound_text(
            instructions.strip(), _PERSONA_INSTRUCTIONS_LIMIT_BYTES, notes, "persona_instructions"
        )
        persona_section = f"Persona instructions ({persona_slug or 'unknown'}):\n{bounded}\n\n"
    return (
        f"{persona_section}{experience}"
        "The pipeline rules take precedence over the Persona instructions above: edit "
        "files only, do not run git commit or git push, and do not open a pull request "
        "— the pipeline commits, pushes, and manages the PR.\n\n"
    )


# Console-v2 issue 23: how a report ends, in every Swarm member's prompt on one.
_REPORT_RULES = (
    "This Work Record ends in a written report, not a pull request. This directory is "
    "scratch space with no repository checkout, and nothing in it is the Outcome. When the "
    "report is ready, the Lead posts it as a `result` Message on the Channel and then ends "
    f'the Work Record by writing {{"end_epic": true}} to `{PLAN_FILE}` (`work.end`): the '
    "report is the Lead's last `result` Message. To leave it for a person instead, write "
    '{"end_epic": true, "needs_human": true}.\n\n'
)

# PRD issue 60: how an Agent asks a human, in the words every Lead prompt shares.
_ASK_HINT = (
    'ask with `agentic-runner ask "<question>"`: after this Directive the work waits up '
    "to 24 hours, and the next Directive carries the answer."
)


def _directive_prompt(*, completion_criteria: str, pr_body: str, persona_preamble: str = "") -> str:
    """Prompt for the initial Directive: Persona preamble (possibly empty), the Work
    Record's task, and the pipeline rules."""
    if not completion_criteria.strip():
        return f"{persona_preamble}{pr_body}"
    return (
        f"{persona_preamble}"
        "Implement the following change in the current repository workspace.\n\n"
        f"Task:\n{completion_criteria.strip()}\n\n"
        "Edit files only. Do not run git commit or git push and do not open a pull "
        "request — the pipeline commits, pushes, and manages the PR. When only a person "
        f"can unblock you, {_ASK_HINT}\n\n"
        f"{pr_body}"
    )


def _fix_directive_prompt(
    verifier_summary: str,
    completion_criteria: str,
    *,
    verifier_output: str = "",
    persona_preamble: str = "",
) -> str:
    """Prompt for a fix Directive: read the (already-redacted) verifier failure summary
    and output tail, and repair the change so the verifier passes, keeping the diff
    minimal. ``verifier_output`` may be empty (e.g. a Directive queued before this
    field existed, or a verifier that produced no output)."""
    task_section = (
        f"\n\nOriginal task:\n{completion_criteria.strip()}" if completion_criteria.strip() else ""
    )
    output_section = f"\n\nVerifier output:\n{verifier_output}" if verifier_output.strip() else ""
    return (
        f"{persona_preamble}"
        "The verifier failed for the change on this branch. Read the failure summary "
        "below, fix the code so the verifier passes, and keep the change minimal.\n\n"
        f"Verifier failure:\n{verifier_summary}{output_section}{task_section}"
    )


def _member_directive_prompt(
    *,
    role: str,
    wake_reason: str,
    pending: list[MessageEnvelope],
    completion_criteria: str,
    persona_preamble: str = "",
    board: EpicBoard | None = None,
    question: Mapping[str, Any] | None = None,
    report: bool = False,
) -> str:
    """Prompt for a woken Swarm member (PRD issue 53): its role, why it was woken, every
    pending Message folded in, and the pipeline rules. A member speaks back with
    ``agentic-runner message send``; the Runner commits and pushes what it changed --
    except on a report (console-v2 issue 23), whose Outcome is the Lead's closing
    `result` Message and whose directory holds no checkout."""

    if pending:
        folded = "\n\n".join(
            f"[{envelope.kind.value} from Agent {envelope.sender_agent_id}"
            f" -- message {envelope.message_id}]\n{envelope.body}"
            for envelope in pending
        )
        messages = f"Messages addressed to you:\n\n{folded}\n\n"
    else:
        messages = "No Message is pending for you.\n\n"
    task = (
        f"Work Record task:\n{completion_criteria.strip()}\n\n"
        if completion_criteria.strip()
        else ""
    )
    coordination = _coordination_prompt(board) if board is not None else ""
    answered = _question_prompt(question) if question is not None else ""
    return (
        f"{persona_preamble}"
        f"You hold the {role or 'lead'} role in this Work Record's Swarm and were woken "
        f"because: {wake_reason or 'messages are pending'}.\n\n"
        f"{task}{answered}{messages}{coordination}"
        f"{_REPORT_RULES if report else ''}"
        "Act on what is asked of you in this workspace. Reply to the Swarm with "
        "`agentic-runner message send --channel <id> --kind result|request|note "
        "[--to <agent-id> | --role lead|liaison|builder|critic] --ref message=<id> ...` "
        "where a reply is due: a `result` answers the `message` it references, a `request` "
        "wakes the Agent or role holder it is addressed to. When only a person can unblock "
        f"you, {_ASK_HINT} "
        + (
            "Do not open a pull request."
            if report
            else "Edit files only. Do not run git commit or git push and do not open a pull "
            "request -- the pipeline commits, pushes, and manages the PR."
        )
    )


def _learning_prompt(request: LearningDirectiveInput, *, base_branch: str, transcript: str) -> str:
    """The Learner's one turn (PRD issue 55, map 09 "the learning flow").

    The children section is printed as the platform fitted it -- never cut here, so a
    child is never shown in half (issue 73) -- and every cut, the platform's and this
    prompt's own, is stated once in the closing ``Cut to fit`` line."""

    notes: list[str] = []

    def section(title: str, value: object) -> str:
        text = value if isinstance(value, str) else json.dumps(value, indent=1, default=str)
        return f"{title}:\n{bound_text_tail(text, _LEARNER_INPUT_LIMIT_BYTES, notes, title)}\n\n"

    evidence = section("Evidence", request.evidence)
    children = (
        "Children's Evidence (metadata only; `ended: false` marks a child still in the "
        f"state shown):\n{learner_children_json(request.children_evidence)}\n\n"
        if request.children_evidence
        else ""
    )
    if request.children_omitted or request.children_evidence_omitted:
        shown = len(request.children_evidence)
        cut = sum(1 for child in request.children_evidence if child.get("evidence_omitted"))
        notes.append(
            f"showing {shown} of {shown + request.children_omitted} children; "
            f"older Evidence cut for {cut} ({request.children_evidence_omitted} events)"
        )
    transcript_section = section("Conversation transcript", transcript or "(none)")
    lessons_section = section("Existing Lessons", request.existing_lessons or "(none)")
    cut_line = f"Cut to fit: {'; '.join(notes)}.\n\n" if notes else ""
    return (
        "You are the Learner. This Work Record has ended "
        f"({request.ending_kind}{' / ' + request.end_reason if request.end_reason else ''}). "
        "Propose Lessons a future Directive of the same Persona should know -- from a "
        "failure as much as from a success. Propose nothing rather than something vague, "
        "and never repeat an existing Lesson below.\n\n"
        f"The change is the branch checked out here: read it with `git diff "
        f"origin/{base_branch}...HEAD` and `git log origin/{base_branch}..HEAD`. Do not "
        "edit tracked files, commit, push or open anything.\n\n"
        + evidence
        + children
        + transcript_section
        + lessons_section
        + cut_line
        + "Write your proposals as JSON to "
        f"`{_LESSONS_FILE}` in this workspace: "
        '{"lessons": [{"kind": "convention|pitfall|procedure|preference", "title": "...", '
        '"body": "...", "org_note": "..."}]}. Write `body` in general form -- no repository, '
        "product, branch, person or organisation names -- and put any organisation-specific "
        f"detail in `org_note`. At most {_LESSONS_MAX} Lessons; an empty list is a fine answer."
    )


def _coordination_prompt(board: EpicBoard) -> str:
    """The Epic's board as structured input to the Lead's Directive (PRD issue 59, 19 A2),
    and the plan file it answers with. A child that already exists is named by id in
    ``blocked_by`` / ``based_on``; a new one by its plan key."""

    cards = [
        {
            "work_record_id": card.work_record_id,
            "title": card.title,
            "status": card.status,
            "pr_number": card.pr_number,
            "end_reason": card.end_reason,
            "blocked_by": list(card.blocked_by),
            "based_on": card.based_on,
        }
        for card in board.children
    ]
    return (
        "You lead an Epic: a Work Record whose Outcome is its children's completion. "
        "Chart the children up front and react to their endings; the platform starts "
        "each child when its blockers are satisfied and merges nothing for you.\n\n"
        f"Board (children, states, PRs, edges):\n{json.dumps(cards, indent=1)}\n\n"
        f"Write your decision as JSON to `{_PLAN_FILE}` in this workspace: "
        '{"children": [{"key": "a", "title": "...", "description": "...", '
        '"channel_id": "<Channel id>", "blocked_by": ["<key or id>"], '
        '"based_on": "<key or id>", "budget_max_tokens": N}], '
        '"endings": [{"work_record_id": "<id>", "end_reason": "needs_human"}], '
        '"end_epic": false}. `blocked_by` starts a child when the sibling is DONE; '
        "`based_on` stacks it on the sibling's branch and starts it when that PR is open. "
        "Leave `channel_id` or a Budget field out to take the Epic's defaults for its children. "
        "An ending with `needs_human` parks a child for a person. Set `end_epic` when "
        "the Epic should end with children parked or ended. Write nothing when there is "
        f"nothing to decide. Limits: a title of {PLAN_TITLE_MAX} characters, a description "
        f"of {PLAN_TEXT_MAX}, at most {PLAN_CHILDREN_MAX} children; a Budget field must be "
        "positive.\n\n"
    )


def _read_plan(path: Path) -> EpicPlan | None:
    """The Lead's plan, or ``None``: a missing or malformed file decides nothing.

    Clamped to the control plane's bounds (``PLAN_*`` in the contracts) rather than
    handed over as written: `submit_epic_plan` rejects a longer title or a non-positive
    Budget with a 422, which is non-retryable and ends the Epic in an Incident.
    """

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    children: list[PlannedChild] = []
    for index, entry in enumerate(raw.get("children") or []):
        if not isinstance(entry, dict) or index >= PLAN_CHILDREN_MAX:
            continue
        title = str(entry.get("title") or "")[:PLAN_TITLE_MAX]
        channel_id = str(entry.get("channel_id") or "")[:PLAN_ID_MAX]
        # An empty Channel is the Epic's children default, filled by the control plane.
        if not title:
            continue
        blocked_by = entry.get("blocked_by")
        children.append(
            PlannedChild(
                key=str(entry.get("key") or f"child-{index + 1}")[:PLAN_ID_MAX],
                title=title,
                description=str(entry.get("description") or "")[:PLAN_TEXT_MAX],
                channel_id=channel_id,
                blocked_by=tuple(
                    str(item)[:PLAN_ID_MAX]
                    for item in (blocked_by if isinstance(blocked_by, list) else [])[
                        :PLAN_EDGES_MAX
                    ]
                ),
                based_on=str(entry.get("based_on") or "")[:PLAN_ID_MAX],
                budget_max_directives=_positive_int(entry.get("budget_max_directives")),
                budget_max_wall_clock_seconds=_positive_float(
                    entry.get("budget_max_wall_clock_seconds")
                ),
                budget_max_tokens=_non_negative_int(entry.get("budget_max_tokens")),
            )
        )
    endings = [
        PlannedEnding(
            work_record_id=str(entry["work_record_id"])[:PLAN_ID_MAX],
            # `needs_human` is the one reason a Lead's `work.end` may give (19 A5); any
            # other would 500 at `_park`'s enum and be retried forever.
            end_reason=END_REASON_NEEDS_HUMAN,
        )
        for entry in (raw.get("endings") or [])[:PLAN_CHILDREN_MAX]
        if isinstance(entry, dict) and entry.get("work_record_id")
    ]
    end_epic = bool(raw.get("end_epic"))
    if not children and not endings and not end_epic:
        return None
    return EpicPlan(
        children=tuple(children),
        endings=tuple(endings),
        end_epic=end_epic,
        needs_human=bool(raw.get("needs_human")),
    )


def _with_question[Output: (BranchPullRequestOutput, FixDirectiveOutput)](
    output: Output, asked: list[QuestionAsked]
) -> Output:
    """The Question the attempt's Agent asked rides out on the output (PRD issue 60):
    the loop holds on it once the Directive has run, whatever else the Directive did."""

    return replace(output, question=asked[0]) if asked else output


def _question_prompt(question: Mapping[str, Any]) -> str:
    """What the woken Agent knows of its Question: the text, and the answer or its
    absence -- and, on silence, the two moves it has (PRD issue 60, 19: nothing ends on
    silence, the Agent decides)."""

    asked = f"You asked (Question {question.get('id')}):\n{question.get('text') or ''}\n\n"
    addressee = str(question.get("addressed_to") or "person")
    if question.get("outcome") == "answered":
        return f"{asked}The {addressee} answered:\n{question.get('answer_text') or ''}\n\n"
    return (
        f"{asked}No answer came from the {addressee} within the 24-hour window. Decide: "
        "carry on under an assumption you state explicitly (with `agentic-runner annotate`), "
        f'or park this Work Record for a person by writing {{"end_epic": true}} to '
        f"`{_PLAN_FILE}` (`work.end needs_human`: the PR and branch are kept).\n\n"
    )


def _take_plan(workspace: Path | None) -> EpicPlan | None:
    """Read the plan file and remove it, so it is never committed with the change."""

    if workspace is None:
        return None
    path = workspace / _PLAN_FILE
    plan = _read_plan(path)
    path.unlink(missing_ok=True)
    return plan


def _number(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _positive_int(value: object) -> int | None:
    number = _number(value)
    return int(number) if number is not None and number >= 1 else None


def _non_negative_int(value: object) -> int | None:
    number = _number(value)
    return int(number) if number is not None and number >= 0 else None


def _positive_float(value: object) -> float | None:
    number = _number(value)
    return number if number is not None and number > 0 else None


def _read_lessons(path: Path) -> list[ProposedLesson]:
    """The Learner's proposals, or none: a missing or malformed file proposes nothing."""

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    entries = raw.get("lessons") if isinstance(raw, dict) else raw
    if not isinstance(entries, list):
        return []
    lessons: list[ProposedLesson] = []
    for entry in entries[:_LESSONS_MAX]:
        if not isinstance(entry, dict):
            continue
        fields_ = {
            key: str(entry.get(key) or "")[:_LESSON_TEXT_LIMIT]
            for key in ("kind", "title", "body", "org_note")
        }
        if fields_["kind"] and fields_["title"] and fields_["body"]:
            lessons.append(ProposedLesson(**fields_))
    return lessons


@dataclass(frozen=True)
class _InPlaceDirective:
    """What distinguishes one in-place Directive kind from another (PRD issue 53).

    A fix Directive and a member Directive run the same way -- one runtime turn in the
    Workspace the first Directive cloned, then commit and push under the Agent's own
    `push` -- and differ only in what they are told, what the Evidence calls them, and
    whether a turn that changed nothing still pushes.
    """

    lifecycle_source: str
    run_source: str
    push_source: str
    commit_message: str
    push_summary: str
    prompt: Callable[[_RuntimeContextState, list[str]], Awaitable[str]]
    # A member that only spoke has nothing to push; a fix that changed nothing pushes
    # the same head again, which is how a stuck fix loop was always made visible.
    push_when_clean: bool = True


def _profile_memory_limit_bytes(command_policy: Mapping[str, object]) -> int | None:
    """The Agent Runtime Profile's `memory_limit_bytes`, or None for the Runner default."""

    value = command_policy.get("memory_limit_bytes")
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _contract_workspace_path(
    *,
    workspace_root: Path,
    contract_id: str | None,
    work_record_id: str,
) -> Path:
    """``{contract_id}/{work_record_id}`` (ADR-0015 §2), replacing ``{owner}/{repo}/...``.

    The pure path form, for the rehydration path that must name a Workspace without
    creating one. Creation goes through :meth:`FastApiRalphActivities._prepare_workspace`,
    which also sets the mode and the owner.
    """

    return contract_workspace_path(
        workspace_root=workspace_root,
        contract_id=contract_path_segment(contract_id),
        work_record_id=work_record_id,
    )


def _github_remote_url(repository: str) -> str:
    owner, repo_name = _split_repo(repository)
    return f"https://github.com/{owner}/{repo_name}.git"


def _pull_request_url(repository: str, pr_number: int) -> str:
    owner, repo_name = _split_repo(repository)
    return f"https://github.com/{owner}/{repo_name}/pull/{pr_number}"


def _split_repo(repository: str) -> tuple[str, str]:
    parts = repository.split("/")
    if len(parts) != 2:
        raise ValueError("repository must be in owner/name form")
    owner, repo_name = parts
    if not _safe_repo_segment(owner) or not _safe_repo_segment(repo_name):
        raise ValueError("repository contains unsafe path segments")
    return owner, repo_name


def _safe_repo_segment(value: str) -> bool:
    return bool(_SAFE_REPO_SEGMENT_RE.fullmatch(value)) and value not in {".", ".."}


def _workspace_id(workspace_path: Path, workspace_root: Path) -> str:
    try:
        return str(workspace_path.resolve().relative_to(workspace_root.resolve()))
    except ValueError:
        return workspace_path.name


def _is_guard_mode_refusal(codex_result: DirectiveResult) -> bool:
    """Whether a Codex result is a runtime guard-mode refusal (issue 05).

    Discriminates on ``evidence.guard_mode`` rather than exit code 126, which is
    overloaded (the verifier-startup failure path also uses 126). Only the worker
    runtime's ``_refused_result`` sets a ``guard_mode`` beginning with ``refused``.
    """
    return codex_result.evidence.guard_mode.startswith("refused")


def _persona_instructions_hash(instructions: str) -> str | None:
    """Content hash of the *full* resolved instructions — it names the registry version
    that drove the session (issue 02: a prompt change is attributable after the fact),
    independent of the injection bounding, which is recorded as a truncation note."""
    if not instructions.strip():
        return None
    return hashlib.sha256(instructions.encode()).hexdigest()


def _codex_evidence_payload(
    codex_result: DirectiveResult,
    *,
    persona_slug: str | None = None,
    persona_instructions: str = "",
    prompt_notes: list[str] | None = None,
) -> dict[str, Any]:
    # The internal evidence endpoint rejects payloads over MAX_SERIALIZED_BYTES
    # (65,536; api/internal_work_records.py) with a 422, while the runtime keeps up
    # to CODEX_CLI_OUTPUT_LIMIT_BYTES (65,536) PER stream — so one long transcript
    # made every append fail deterministically and the activity retry loop re-ran
    # the full Directive forever (work record f6223d2a: 38 attempts overnight).
    # Bound each stream here, keeping the tail — Codex puts its final message and
    # test runners put their failure summaries at the end. Persona instructions ride
    # as slug + content hash only (issue 02) — never the text, whose size the
    # evidence budget does not control.
    notes: list[str] = list(prompt_notes or [])
    payload: dict[str, Any] = {
        "exit_code": codex_result.exit_code,
        "stdout": bound_text_tail(
            codex_result.stdout, _RUNTIME_EVIDENCE_STREAM_LIMIT_BYTES, notes, "stdout"
        ),
        "stderr": bound_text_tail(
            codex_result.stderr, _RUNTIME_EVIDENCE_STREAM_LIMIT_BYTES, notes, "stderr"
        ),
        "error": bound_text_tail(
            codex_result.error, _RUNTIME_EVIDENCE_STREAM_LIMIT_BYTES, notes, "error"
        ),
        "command_hash": codex_result.command_hash,
        "workspace": asdict(codex_result.evidence),
        "persona_slug": persona_slug,
        "persona_instructions_hash": _persona_instructions_hash(persona_instructions),
    }
    if notes:
        payload["truncation_notes"] = notes
    return payload


def wipe_contract_residue(
    contract_isolation: ContractIsolation | None,
    contract_id: str,
    *,
    contract_state: str,
) -> ContractResidueOutput:
    """The termination wipe (map 17 A6), as counts an Evidence Event can carry.

    A suspension or a chain hold keeps the tree in place, readable only by that
    Contract's uid, and a Product-removal drain leaves the Contract ``active`` — so
    the state, not the fact that the loop drained, decides whether anything goes.
    """

    segment = contract_path_segment(contract_id or None)
    if (
        contract_isolation is None
        or contract_state not in _WIPEABLE_CONTRACT_STATES
        or segment == NO_CONTRACT
    ):
        return ContractResidueOutput(
            contract_id=segment,
            workspaces_wiped=0,
            harness_roots_wiped=0,
            uid_retired=False,
        )
    residue = contract_isolation.wipe(segment)
    return ContractResidueOutput(
        contract_id=residue.contract_id,
        workspaces_wiped=residue.workspaces_removed,
        harness_roots_wiped=residue.harness_roots_removed,
        uid_retired=residue.uid_retired,
    )


# Where a Runner-side routing refusal lands in the trail (PRD issue 42, 17 A11).
_ROUTING_EVIDENCE_SOURCE = "runner.routing"
PRIOR_ATTEMPT_ALIVE_EVIDENCE_SOURCE = "runner.prior_attempt_alive"
HARNESS_SESSION_EVIDENCE_SOURCE = "runner.harness_session"
DIRECTIVE_TOKEN_EVIDENCE_SOURCE = "runner.directive_token"
AGENT_RUNTIME_EVIDENCE_SOURCE = "runner.agent_runtime"
SKILLS_EVIDENCE_SOURCE = "runner.skills"


class _EvidenceWriter(Protocol):
    async def append_evidence(
        self,
        work_record_id: str,
        *,
        source: str,
        payload: Mapping[str, Any],
        actor: str = ...,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]: ...


async def pull_directive_token(
    source: DirectiveTokenSource,
    client: _EvidenceWriter,
    request: object,
    *,
    actor: str,
) -> None:
    """Pull the token for this activity execution and scope it to the execution (PRD issue 63).

    Keyed on the request's Work Record and current Directive number; a request that
    names no Directive (PR readiness, approval, merge, review, verifier, re-assembly)
    keys on ``0``, which the control plane reads as the Work Record's own step. A request
    naming no Work Record at all (a Contract wipe) is Runner-scoped and keeps the signed
    envelope. A refusal fails the activity closed with Evidence naming the control plane's
    reason, written under the signed envelope since no token exists; a transport failure
    simply raises, and Temporal's retry pulls afresh.
    """

    work_record_id = str(getattr(request, "work_record_id", "") or "")
    if not work_record_id:
        return
    directive = directive_id(
        work_record_id=work_record_id,
        directive_number=int(getattr(request, "directive_number", 0) or 0),
    )
    try:
        token = await source.directive_token(directive)
    except RunnerRegistrationError as refusal:
        await _raise_recorded(
            refusal,
            client,
            work_record_id,
            source=DIRECTIVE_TOKEN_EVIDENCE_SOURCE,
            actor=actor,
            payload={
                "event": "directive.token_refused",
                "reason": refusal.reason,
                "directive_id": directive,
                "detail": str(refusal),
            },
        )
    directive_token.set(token)


async def _raise_recorded(
    refusal: Exception,
    client: _EvidenceWriter,
    work_record_id: str,
    *,
    source: str,
    actor: str,
    payload: Mapping[str, Any],
) -> NoReturn:
    """Write a refusal's Evidence under the signed envelope, then raise the refusal.

    The refusal, reason intact, is what the activity fails with even when its Evidence
    cannot land: a revoked Runner's envelope is refused too, and its 403 must not stand
    in for ``runner_revoked`` (the control plane already wrote that one, on the
    Organisation's trail, when it refused the mint). The failed write rides as the cause.
    """

    try:
        await client.append_evidence(work_record_id, source=source, actor=actor, payload=payload)
    except WorkerFastApiClientError as unwritten:
        raise refusal from unwritten
    raise refusal


_EXPERIENCE_EVIDENCE_SOURCE = "experience"


class RunnerRalphActivities:
    """The Runner's Ralph activities: workspace, Agent Runtime, verb seams, verifier."""

    def __init__(
        self,
        fastapi_client: RunnerRalphFastApiClient,
        *,
        runtime_context_resolver: WorkerRuntimeContextResolver | None = None,
        agent_runtimes: Mapping[str, AgentRuntime] | None = None,
        git_workspace: GitWorkspace | None = None,
        github_client: GitHubClient | None = None,
        workspace_root: Path | None = None,
        contract_isolation: ContractIsolation | None = None,
        routing_identity: RunnerRoutingIdentity | None = None,
        hooks: HookRunner | None = None,
        socket_dir: Path | None = None,
        llm_proxy: LlmProxy | None = None,
        credentials: CredentialResolver | None = None,
        heartbeat_link: HeartbeatLink | None = None,
        token_source: DirectiveTokenSource | None = None,
        message_store: MessageStore | None = None,
        workflow_signaller: WorkflowSignaller | None = None,
        verifier_runner: Callable[..., Any] = verifier_command._subprocess_runner,
        monotonic: Callable[[], float] = time.monotonic,
        mcp_spawner: Spawner | None = None,
        egress_resolver: Resolver | None = None,
        tool_server_health: ToolServerHealthLog | None = None,
        attempt_records: AttemptRecords | None = None,
    ) -> None:
        self._fastapi_client = fastapi_client
        # Wall-clock source for per-Directive runtime, accrued against the Budget's
        # wall-clock dimension (ADR-0007). Activities run outside the Temporal sandbox,
        # so a real monotonic clock is allowed here (it must never be used in the
        # @workflow.defn module, which stays deterministic).
        self._monotonic = monotonic
        self._runtime_context_resolver = runtime_context_resolver or WorkerRuntimeContextResolver(
            fastapi_client
        )
        # Every Agent Runtime this process serves, by the Profile's `cli_kind`; each
        # Directive picks from here by its Work Record's kind (local-agents 01).
        self._agent_runtimes: dict[str, AgentRuntime] = dict(agent_runtimes or {})
        self._git_workspace = git_workspace
        self._github_client = github_client
        self._workspace_root = workspace_root
        self._contract_isolation = contract_isolation
        # What this process is, as the control plane registered it (PRD issue 42): its
        # Runner id, its host party and its config-declared Tags. Every activity payload
        # is checked against it before the body runs, so a stolen or misrouted task costs
        # no verb. ``None`` in a process that never registered -- it polls no
        # ``runner.{runner_id}`` queue, so nothing routes to it and there is nothing to
        # assert against; the fail-closed check is on every Runner the router can pick.
        self._routing_identity = routing_identity
        # Runner Hooks (PRD issue 45). An empty HookRunner is the default so every call
        # site is unconditional: no hooks installed is the common case, not a special one.
        self._hooks = hooks or HookRunner(hooks={})
        self._socket_dir = socket_dir
        # The relocated LLM proxy (PRD issue 43). None until an Organisation moves onto
        # the Helm Runner (issue 46) and the backend proxy is retired: a Directive with
        # no proxy here keeps calling the backend one, which is exactly the transition
        # the issue describes.
        self._llm_proxy = llm_proxy
        # Credential References (PRD issue 48, 22 A1). None means this Runner resolves
        # none: a Contract that declares a manifest then fails its Directives closed,
        # which is the right answer -- a Runner with no host store cannot host that
        # Contract's work and saying so beats running it credential-less.
        self._credentials = credentials
        # Push-on-change Grants and the liveness gate (PRD issue 44). None on a host that
        # is not a registered Runner and has no heartbeat stream to be pushed over: there
        # the snapshot still travels on the activity input assembled for the Directive,
        # exactly as issue 36 left it.
        self._heartbeat_link = heartbeat_link
        # Where each activity execution pulls its Directive token (PRD issue 63): the
        # same signed stream the heartbeat rides. None on the link-less platform host,
        # which keeps the service secret until issue 46; the token itself is never held
        # here -- it lives in the execution's own context (``directive_token``).
        self._token_source = token_source
        # The Message store and the wake signal (PRD issue 52). None on a process with
        # no store: `message send | list` then answer "no store on this Runner" and the
        # `channel.*` seam still evaluates and records -- the refusal is in the Evidence.
        self._message_store = message_store
        self._workflow_signaller = workflow_signaller
        self._verifier_runner = verifier_runner
        # PRD issue 58: how a Runner-hosted MCP server is spawned and how the egress
        # proxy resolves a name -- the process and the network, seamed for tests.
        self._mcp_spawner = mcp_spawner
        self._egress_resolver = egress_resolver
        self._tool_server_health = tool_server_health
        # The retry fence (runner-repo 05). None fences nothing: a process that never
        # registered has no state directory to keep the records in.
        self._attempt_records = attempt_records

    def activity_callables(self) -> list[Callable[..., Any]]:
        """Return decorated activity callables for Temporal worker registration."""
        return [
            self.create_or_update_branch_pr,
            self.execute_fix_directive,
            self.execute_member_directive,
            self.execute_learning_directive,
            self.mark_pr_ready_for_review,
            self.wait_for_approval,
            self.merge_pr,
            self.post_pr_review,
            self.request_pr_review,
            self.list_pr_changed_files,
            self.post_pr_comment,
            self.close_pr,
            self.probe_repository_access,
            self.run_verifier,
            self.wipe_contract_residue,
            self.delete_expired_workspaces,
            self.reassemble_workspace,
        ]

    async def _assert_routed(self, request: object) -> None:
        """Fail closed unless this Runner is the one routed to (PRD issue 42, 25 §9), then
        pull this execution's Directive token (PRD issue 63).

        Called first in every activity below, before any workspace, verb seam, runtime
        turn or control-plane call. The refusal is deliberately *retryable*: the task
        goes back on this Runner's own queue and the Evidence is what turns a persistent
        poller into a visible intra-Organisation incident rather than silence (17 A11).
        """

        if self._routing_identity is not None:
            routing = getattr(request, "routing", None)
            try:
                assert_routed(routing, self._routing_identity)
            except RoutingRefusedError as refusal:
                work_record_id = getattr(request, "work_record_id", "")
                if not work_record_id:
                    raise
                await _raise_recorded(
                    refusal,
                    self._fastapi_client,
                    str(work_record_id),
                    source=_ROUTING_EVIDENCE_SOURCE,
                    actor=f"runner:{self._routing_identity.runner_id}",
                    payload={
                        "event": "directive.routing_refused",
                        "reason": refusal.reason,
                        "runner_id": self._routing_identity.runner_id,
                        "routed_runner_id": None if routing is None else routing.runner_id,
                        "detail": str(refusal),
                    },
                )
        if self._token_source is not None:
            await pull_directive_token(
                self._token_source, self._fastapi_client, request, actor=self._actor()
            )

    def _actor(self) -> str:
        return (
            f"runner:{self._routing_identity.runner_id}"
            if self._routing_identity is not None
            else "runner"
        )

    @contextlib.asynccontextmanager
    async def _fenced(self, work_record_id: str) -> AsyncIterator[None]:
        """Refuse this attempt's spawns while an earlier attempt's group lives (RR-05).

        The refusal is retryable on purpose: the earlier attempt either finishes or is
        killed, and the next retry then finds the Workspace free.
        """

        if self._attempt_records is None:
            yield
            return
        attempt = activity.info().attempt if activity.in_activity() else 1
        try:
            with fence_work_record(self._attempt_records, work_record_id, attempt=attempt):
                yield
        except PriorAttemptAliveError as refusal:
            await _raise_recorded(
                refusal,
                self._fastapi_client,
                work_record_id,
                source=PRIOR_ATTEMPT_ALIVE_EVIDENCE_SOURCE,
                actor=self._actor(),
                payload={
                    "event": "directive.prior_attempt_alive",
                    "work_record_id": work_record_id,
                    "attempt": attempt,
                    "prior_attempt": refusal.prior.attempt,
                    "prior_pgid": refusal.prior.pgid,
                },
            )

    async def _harness_session(
        self,
        liveness: _Liveness,
        *,
        work_record_id: str,
        directive_number: int,
        agent_runtime: AgentRuntime,
        contract_id: str | None,
        workspace_path: Path,
        sandbox: DirectiveSandbox | None,
    ) -> tuple[str | None, Callable[[str], None]]:
        """Resume the session an earlier attempt of this Directive started, or start fresh.

        Returns the session to resume, if any, and the hook that files a fresh one in the
        heartbeat details. Every retry writes one Evidence event saying which, and why --
        never what the session holds (local-agents 16, ADR-0007 amendment 2026-10-03).
        """

        head = read_head_commit(
            workspace_path=workspace_path, workspace_root=self._require_workspace_root()
        )
        here = HarnessSession(
            session_id="",
            contract_id=contract_id,
            workspace_path=str(workspace_path),
            head=head,
        )

        def start_fresh(session_id: str) -> None:
            liveness.record_session(replace(here, session_id=session_id))

        attempt = activity.info().attempt if activity.in_activity() else 1
        if attempt == 1:
            return None, start_fresh
        # Made explicit rather than left to the spawn: the session to resume must not be
        # one a still-running harness is writing (RR-05). A refusal raises into `_fenced`.
        refuse_if_prior_attempt_alive()
        prior = liveness.prior_session
        reason = _why_not_resume(prior, here, agent_runtime, sandbox)
        await self._fastapi_client.append_evidence(
            work_record_id,
            source=HARNESS_SESSION_EVIDENCE_SOURCE,
            actor=self._actor(),
            payload={
                "event": "directive.harness_session",
                "work_record_id": work_record_id,
                "directive_number": directive_number,
                "attempt": attempt,
                "decision": "fresh" if reason else "resumed",
                "reason": reason or "contract_workspace_and_head_match",
            },
        )
        if reason is not None or prior is None:
            return None, start_fresh
        liveness.record_session(prior)
        return prior.session_id, start_fresh

    @activity.defn(name="create_or_update_branch_pr")
    async def create_or_update_branch_pr(
        self,
        request: BranchPullRequestInput,
    ) -> BranchPullRequestOutput:
        """Run Codex in git workspace, push branch, and create or update GitHub PR."""
        await self._assert_routed(request)
        asked: list[QuestionAsked] = []
        try:
            async with self._fenced(request.work_record_id):
                ran = await self._run_branch_pr_directive(request, asked)
            return _with_question(ran, asked)
        except HookRefusedError:
            # A Runner Hook at or before `pre_runtime` exited non-zero, `pre_directive`
            # being the reject gate (PRD issue 45). Refusing the Directive is the point,
            # so it returns the Runner's existing "this Directive was refused" shape --
            # which ends the loop with an attributable Incident -- rather than raising
            # into a retry that would re-run the same hook to the same answer forever.
            # The `runner.hook` and `runner.directive_rejected` Evidence is already
            # written by the hook session.
            return BranchPullRequestOutput(
                repository=request.repository,
                branch_name=request.branch_name,
                base_ref=request.base_ref,
                pr_number=0,
                pr_url="",
                branch_created=False,
                pr_created=False,
                guard_mode_refused=True,
                workspace_path=request.workspace_path,
                grant_snapshot=dict(request.grant_snapshot),
            )

    async def _run_branch_pr_directive(
        self,
        request: BranchPullRequestInput,
        asked: list[QuestionAsked],
    ) -> BranchPullRequestOutput:
        async with (
            _liveness_heartbeats() as liveness,
            contextlib.AsyncExitStack() as attempt_stack,
        ):
            if (
                not self._agent_runtimes
                or self._git_workspace is None
                or self._github_client is None
            ):
                return BranchPullRequestOutput(
                    repository=request.repository,
                    branch_name=request.branch_name,
                    base_ref=request.base_ref,
                    pr_number=0,
                    pr_url="",
                    branch_created=False,
                    pr_created=False,
                )

            directive_started = self._monotonic() - liveness.earlier_attempts_seconds
            await self._advance_work_record_lifecycle(
                request.work_record_id,
                to_state="IN_PROGRESS",
                source="ralph.create_or_update_branch_pr",
                details={
                    "repository": request.repository,
                    "branch_name": request.branch_name,
                    "base_ref": request.base_ref,
                },
            )

            workspace_root = self._require_workspace_root()
            # The runtime state first: the Workspace is keyed on the Contract, so the
            # Contract has to be known before the path exists (ADR-0015 §2). The snapshot
            # rides in on the input (assembly fetched it); nothing is cached here.
            runtime_state = await self._runtime_state(
                request.work_record_id,
                workspace_path=request.workspace_path,
                grant_snapshot=request.grant_snapshot,
                agent_id=request.agent_id,
            )
            agent_runtime = await self._agent_runtime_for(request.work_record_id, runtime_state)
            mcp_plan = await self._plan_mcp(
                request.work_record_id,
                runtime_state,
                consented=request.owner_confirmed_verbs,
                declined=request.owner_declined_verbs,
            )
            if isinstance(mcp_plan, OwnerConfirmationPending):
                return BranchPullRequestOutput(
                    repository=request.repository,
                    branch_name=request.branch_name,
                    base_ref=request.base_ref,
                    pr_number=0,
                    pr_url="",
                    branch_created=False,
                    pr_created=False,
                    owner_confirmation=mcp_plan,
                    workspace_path=request.workspace_path,
                    grant_snapshot=runtime_state.grant_snapshot_payload,
                )
            contract_id = runtime_state.contract_id
            await self._record_uid_allocation(request.work_record_id, contract_id)
            workspace_path = self._prepare_workspace(
                workspace_root=workspace_root,
                contract_id=contract_id,
                work_record_id=request.work_record_id,
            )
            runtime_state = replace(runtime_state, workspace_path=workspace_path)
            attempt = await attempt_stack.enter_async_context(
                self._directive_attempt(
                    work_record_id=request.work_record_id,
                    state=runtime_state,
                    workspace_path=workspace_path,
                    directive_number=request.directive_number,
                    sandbox=self._contract_sandbox(runtime_state),
                    mcp_plan=mcp_plan,
                    asked=asked,
                )
            )
            # The reject gate, then the exports that reach the Agent Runtime. `pre_exit`
            # is the exit stack's business: it runs on every return below, and on cancel.
            await attempt.hooks.phase(HookName.PRE_DIRECTIVE)
            await attempt.run_environment_hook()
            await attempt.hooks.phase(HookName.PRE_CHECKOUT)
            if attempt.hooks.installed(HookName.CHECKOUT):
                # The one override worth keeping (map ticket 26 §1): a mirror or a warm
                # cache in place of the Runner's clone. It holds no git credential
                # (17 A10), so it can only reach a source its own host already trusts.
                await attempt.hooks.phase(HookName.CHECKOUT)
                # The hook is a separate process and cannot reach the adapter's workspace
                # registry, so the checkout it left has to be handed back across the port
                # -- without this every later git call (`collect_git_evidence` first) is
                # refused on a workspace the Runner never cloned, and the override breaks
                # every Directive instead of replacing one clone. Validated, not trusted.
                checkout_workspace = self._git_workspace.adopt_existing_clone(
                    AdoptWorkspaceRequest(
                        repo_full_name=request.repository,
                        remote_url=_github_remote_url(request.repository),
                        workspace_root=workspace_root,
                        workspace_path=workspace_path,
                        work_branch=request.branch_name,
                    )
                ).workspace_path
            else:
                remote_url = _github_remote_url(request.repository)
                self._git_workspace.clone_repository(
                    CloneWorkspaceRequest(
                        repo_full_name=request.repository,
                        remote_url=remote_url,
                        workspace_root=workspace_root,
                        workspace_path=workspace_path,
                    )
                )
                self._git_workspace.fetch_base_branch(
                    FetchBranchRequest(
                        repo_full_name=request.repository,
                        workspace_path=workspace_path,
                        base_branch=request.base_ref,
                    )
                )
                checkout_workspace = self._git_workspace.checkout_work_branch(
                    CheckoutWorkBranchRequest(
                        repo_full_name=request.repository,
                        workspace_path=workspace_path,
                        base_branch=request.base_ref,
                        work_branch=request.branch_name,
                    )
                ).workspace_path
            await attempt.hooks.phase(HookName.POST_CHECKOUT)
            branch = self._github_client.create_branch(
                BranchRequest(
                    repo=request.repository,
                    base_ref=request.base_ref,
                    branch=request.branch_name,
                )
            )
            # The prompt is derived here (not in the workflow input) so an activity retry
            # always carries the current Work Record task — the first production Directive
            # got only PR-body metadata, asked what to do, and exited 0 with no edits.
            await self._record_experience_injected(
                request.work_record_id,
                directive_number=request.directive_number,
                state=runtime_state,
            )
            prompt_notes: list[str] = []
            await attempt.hooks.phase(HookName.PRE_RUNTIME)
            sandbox = self._directive_sandbox(runtime_state, checkout_workspace)
            resume_session_id, on_session_started = await self._harness_session(
                liveness,
                work_record_id=request.work_record_id,
                directive_number=request.directive_number,
                agent_runtime=agent_runtime,
                contract_id=contract_id,
                workspace_path=checkout_workspace,
                sandbox=sandbox,
            )
            codex_result = await agent_runtime.execute_directive(
                DirectiveRequest(
                    sandbox=sandbox,
                    workspace_path=checkout_workspace,
                    resume_session_id=resume_session_id,
                    on_session_started=on_session_started,
                    # The attempt's callback socket and bearer, plus whatever the
                    # `environment` hook exported. The env allow-list gains these names
                    # and nothing else (PRD issue 09's invariant, issue 45's amendment).
                    extra_env=attempt.env,
                    mcp_servers=attempt.mcp_servers,
                    egress_allow_list=attempt.egress_allow_list,
                    prompt=attempt.skills_preamble
                    + _directive_prompt(
                        completion_criteria=runtime_state.completion_criteria,
                        pr_body=request.pr_body,
                        persona_preamble=_persona_prompt_preamble(
                            persona_slug=runtime_state.persona_slug,
                            instructions=runtime_state.persona_instructions,
                            notes=prompt_notes,
                            experience=runtime_state.experience,
                        ),
                    ),
                    base_branch=request.base_ref,
                    work_branch=request.branch_name,
                )
            )
            await attempt.hooks.phase(HookName.POST_RUNTIME)
            await self._fastapi_client.append_evidence(
                request.work_record_id,
                source="ralph.codex_run",
                payload=_codex_evidence_payload(
                    codex_result,
                    persona_slug=runtime_state.persona_slug,
                    persona_instructions=runtime_state.persona_instructions,
                    prompt_notes=prompt_notes,
                ),
            )
            await self._report_harness_usage(
                runtime_state=runtime_state,
                work_record_id=request.work_record_id,
                directive_number=request.directive_number,
                result=codex_result,
            )
            plan = _take_plan(checkout_workspace)
            if codex_result.exit_code != 0:
                if _is_guard_mode_refusal(codex_result):
                    # The runtime refused this Directive (guard-mode). Return a flagged
                    # output so the workflow ends the loop with an attributable Incident
                    # rather than retrying an opaque RuntimeError forever (issue 05). The
                    # ralph.codex_run Evidence above already carries the guard_mode.
                    return BranchPullRequestOutput(
                        repository=request.repository,
                        branch_name=request.branch_name,
                        base_ref=request.base_ref,
                        pr_number=0,
                        pr_url="",
                        branch_created=branch.created,
                        pr_created=False,
                        branch_head_sha="",
                        duration_seconds=self._monotonic() - directive_started,
                        guard_mode_refused=True,
                        usage=await self._directive_usage(
                            work_record_id=request.work_record_id,
                            directive_number=request.directive_number,
                        ),
                        workspace_path=str(workspace_path),
                        grant_snapshot=dict(request.grant_snapshot),
                    )
                raise RuntimeError("Codex CLI edit failed")

            git_evidence = self._git_workspace.collect_git_evidence(
                checkout_workspace,
                output_limit_bytes=_GIT_EVIDENCE_LIMIT_BYTES,
            )
            if not git_evidence.status.strip() and not git_evidence.diff.strip():
                # A no-op push leaves the branch equal to base and PR creation 422s
                # opaquely ("no commits between..."); fail attributably instead.
                raise RuntimeError("Codex run produced no workspace changes")
            commit = self._git_workspace.commit_all(
                CommitAllRequest(
                    repo_full_name=request.repository,
                    workspace_path=checkout_workspace,
                    work_branch=request.branch_name,
                    commit_message=f"feat(task): implement work record {request.work_record_id}",
                )
            )
            changed_paths = _changed_paths_from_git_status(git_evidence.status)
            push_verdict = await self._authorize(
                request.work_record_id,
                runtime_state,
                verb=PUSH_VERB,
                repository=request.repository,
                owner_confirmed_verbs=request.owner_confirmed_verbs,
                target=request.branch_name,
                summary=_owner_confirmation_summary(
                    verb=PUSH_VERB, request=request, changed_paths=changed_paths
                ),
                file_list=changed_paths,
            )
            if not push_verdict.allowed:
                # The work stays in the worker-local workspace: nothing is pushed and no
                # PR is opened, so a refused `push` leaves no trace on the org's repo.
                # An escalation lands here too — the owner is asked before the branch
                # leaves the workspace, not after.
                return BranchPullRequestOutput(
                    repository=request.repository,
                    branch_name=request.branch_name,
                    base_ref=request.base_ref,
                    pr_number=0,
                    pr_url="",
                    branch_created=branch.created,
                    pr_created=False,
                    branch_head_sha="",
                    duration_seconds=self._monotonic() - directive_started,
                    grant_refused=push_verdict.refused,
                    grant_refusal_reason=(
                        push_verdict.refusal_summary if push_verdict.refused else ""
                    ),
                    owner_confirmation=push_verdict.pending,
                    usage=await self._directive_usage(
                        work_record_id=request.work_record_id,
                        directive_number=request.directive_number,
                    ),
                    workspace_path=str(workspace_path),
                    grant_snapshot=dict(request.grant_snapshot),
                )
            # The Directive's artifact is the branch it pushed and the PR that carries it.
            await attempt.hooks.phase(HookName.PRE_ARTIFACT)
            push = self._git_workspace.push_branch(
                PushBranchRequest(
                    repo_full_name=request.repository,
                    workspace_path=checkout_workspace,
                    base_branch=request.base_ref,
                    work_branch=request.branch_name,
                )
            )
            await self._fastapi_client.append_evidence(
                request.work_record_id,
                source="ralph.git_commit_push",
                payload={
                    "repository": request.repository,
                    "branch_name": request.branch_name,
                    "base_ref": request.base_ref,
                    "workspace_path": _workspace_id(checkout_workspace, workspace_root),
                    "status": git_evidence.status,
                    "diff": git_evidence.diff,
                    "stderr": git_evidence.stderr,
                    "status_returncode": git_evidence.status_returncode,
                    "diff_returncode": git_evidence.diff_returncode,
                    "commit_id": commit.commit_id,
                    "push_commit_id": push.commit_id,
                    "refspec": push.refspec,
                    "pushed": True,
                    "branch_created": branch.created,
                },
            )

            role_refusal = await self._refuse_unless_lead(request, verb=PR_OPEN_VERB)
            if role_refusal is not None:
                pr_open_verdict = role_refusal
            else:
                pr_open_verdict = await self._authorize(
                    request.work_record_id,
                    runtime_state,
                    verb=PR_OPEN_VERB,
                    repository=request.repository,
                    owner_confirmed_verbs=request.owner_confirmed_verbs,
                    target=request.branch_name,
                    summary=_owner_confirmation_summary(
                        verb=PR_OPEN_VERB, request=request, changed_paths=changed_paths
                    ),
                    file_list=changed_paths,
                )
            if not pr_open_verdict.allowed:
                return BranchPullRequestOutput(
                    repository=request.repository,
                    branch_name=request.branch_name,
                    base_ref=request.base_ref,
                    pr_number=0,
                    pr_url="",
                    branch_created=branch.created,
                    pr_created=False,
                    branch_head_sha=push.commit_id,
                    duration_seconds=self._monotonic() - directive_started,
                    grant_refused=pr_open_verdict.refused,
                    grant_refusal_reason=(
                        pr_open_verdict.refusal_summary if pr_open_verdict.refused else ""
                    ),
                    owner_confirmation=pr_open_verdict.pending,
                    usage=await self._directive_usage(
                        work_record_id=request.work_record_id,
                        directive_number=request.directive_number,
                    ),
                    workspace_path=str(workspace_path),
                    grant_snapshot=dict(request.grant_snapshot),
                )
            # Opened as a draft (ADR-0011 §3): `pr.open` and `pr.review` are separate
            # verbs, so an Agent granted only the first can publish a change for its
            # owner to see without ever putting it in front of the org's reviewers.
            pr_response = self._github_client.create_or_update_pr(
                PullRequestRequest(
                    repo=request.repository,
                    branch=request.branch_name,
                    base=request.base_ref,
                    title=request.pr_title,
                    body=request.pr_body,
                    draft=True,
                )
            )
            pr_url = _pull_request_url(request.repository, pr_response.pr_number)
            await self._fastapi_client.append_evidence(
                request.work_record_id,
                source="ralph.pr_create_update",
                payload={
                    "repository": request.repository,
                    "branch_name": request.branch_name,
                    "base_ref": request.base_ref,
                    "branch_created": branch.created,
                    "pr_number": pr_response.pr_number,
                    "pr_url": pr_url,
                    "pr_created": pr_response.created,
                    "draft": pr_response.draft,
                },
            )
            await attempt.hooks.phase(HookName.POST_ARTIFACT)
            return BranchPullRequestOutput(
                repository=request.repository,
                branch_name=request.branch_name,
                base_ref=request.base_ref,
                pr_number=pr_response.pr_number,
                pr_url=pr_url,
                branch_created=branch.created,
                pr_created=pr_response.created,
                branch_head_sha=push.commit_id,
                duration_seconds=self._monotonic() - directive_started,
                usage=await self._directive_usage(
                    work_record_id=request.work_record_id,
                    directive_number=request.directive_number,
                ),
                # The loop's state leaves on the output (ADR-0013 §3): the workspace this
                # Directive cloned into, and the snapshot its seams evaluated.
                workspace_path=str(workspace_path),
                grant_snapshot=dict(request.grant_snapshot),
                plan=plan,
            )

    @activity.defn(name="mark_pr_ready_for_review")
    async def mark_pr_ready_for_review(
        self,
        request: PullRequestReadyInput,
    ) -> PullRequestReadyOutput:
        """The ``pr.review`` seam: take the verified draft out of draft (ADR-0011 §3).

        Runs ahead of ``handoff_to_reviewer`` — a refusal here means the reviewers are
        never asked and the change stays a draft its owner can see and nobody else is
        paged for. It is also on the unattended path, because GitHub refuses to merge a
        draft: every route to a merge leaves draft through this verb.
        """

        await self._assert_routed(request)
        if self._github_client is None:
            return PullRequestReadyOutput(
                repository=request.repository,
                pr_number=request.pr_number,
                ready=False,
            )
        verdict = await self._refuse_unless_lead(request, verb=PR_REVIEW_VERB)
        if verdict is None:
            verdict = await self._refuse_on_veto(
                request, verb=PR_REVIEW_VERB, head_sha=request.branch_head_sha
            )
        if verdict is None:
            verdict = await self._authorize(
                request.work_record_id,
                await self._runtime_state(
                    request.work_record_id,
                    grant_snapshot=request.grant_snapshot,
                    agent_id=request.agent_id,
                ),
                verb=PR_REVIEW_VERB,
                repository=request.repository,
                owner_confirmed_verbs=request.owner_confirmed_verbs,
                target=str(request.pr_number),
                summary=(
                    f"Take {request.repository} PR #{request.pr_number} out of draft and put "
                    "it in front of the organisation's reviewers."
                ),
                file_list=self._changed_paths_for_owner(request.repository, request.pr_number),
            )
        if not verdict.allowed:
            return PullRequestReadyOutput(
                repository=request.repository,
                pr_number=request.pr_number,
                ready=False,
                grant_refused=verdict.refused,
                grant_refusal_reason=(verdict.refusal_summary if verdict.refused else ""),
                owner_confirmation=verdict.pending,
            )
        ready = self._github_client.mark_pr_ready_for_review(
            PullRequestReadyRequest(repo=request.repository, pr_number=request.pr_number)
        )
        await self._fastapi_client.append_evidence(
            request.work_record_id,
            source="ralph.pr_ready_for_review",
            payload={
                "repository": request.repository,
                "pr_number": request.pr_number,
                "branch_head_sha": request.branch_head_sha,
                "marked_ready": ready.marked_ready,
            },
        )
        return PullRequestReadyOutput(
            repository=request.repository,
            pr_number=request.pr_number,
            ready=ready.ready,
            marked_ready=ready.marked_ready,
        )

    def _require_workspace_root(self) -> Path:
        if self._workspace_root is None:
            raise RuntimeError("workspace_root is required for runtime code-edit activity")
        return self._workspace_root.resolve()

    async def _record_uid_allocation(self, work_record_id: str, contract_id: str | None) -> None:
        """The Evidence Event for this Contract's uid — ids only (PRD issue 30).

        Written by the caller rather than by ``ContractIsolation``, which is deliberately
        free of every backend seam so M3 can lift it into ``agentic-runner`` unchanged
        (issue 36). Only the call that actually allocates writes one, so a Contract's
        second Work Record adds no noise.
        """

        isolation = self._contract_isolation
        if isolation is None or isolation.has_uid(contract_id):
            return
        uid = isolation.uid_for(contract_id)
        if uid is None:
            return
        await self._fastapi_client.append_evidence(
            work_record_id,
            source=_CONTRACT_UID_SOURCE,
            payload={"contract_id": contract_path_segment(contract_id), "uid": uid},
        )

    def _prepare_workspace(
        self,
        *,
        workspace_root: Path,
        contract_id: str | None,
        work_record_id: str,
    ) -> Path:
        """The Work Record's Workspace: 0700, owned by its Contract's uid (ADR-0015 §2)."""

        if self._contract_isolation is not None:
            return self._contract_isolation.prepare_workspace(contract_id, work_record_id)
        path = _contract_workspace_path(
            workspace_root=workspace_root,
            contract_id=contract_id,
            work_record_id=work_record_id,
        )
        # Created here and not left to the clone: the Workspace is the cwd of every hook
        # in this attempt, and `pre_directive` / `pre_checkout` run before any clone. The
        # isolated branch above has always created it; this one used to return a name.
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _sandboxed_verifier_runner(
        self,
        state: _RuntimeContextState,
        workspace_path: Path,
    ) -> Callable[..., Any]:
        """The verifier runs repository code, so it runs as the Contract's uid (ADR-0015 §1).

        Same floor, same uid and the same hand-over of the checkout as a Directive — the
        verifier is the other thing that executes what the Agent just wrote.
        """

        sandbox = self._directive_sandbox(state, workspace_path)
        if sandbox is None:
            return self._verifier_runner
        base_runner = self._verifier_runner
        spawn_kwargs = sandbox.spawn_kwargs()
        # Added after verifier_command's own env allowlist because it is the Runner's
        # value, not the Product's: the verifier is repository code too, so its temp
        # files belong in the Contract's tree and not in the pod's shared /tmp.
        contract_tmpdir = str(sandbox.tmp_dir)

        def run_sandboxed(
            argv: tuple[str, ...],
            *,
            cwd: Path,
            env: dict[str, str],
            timeout_seconds: float,
        ) -> Any:
            return base_runner(
                argv,
                cwd=cwd,
                env={**env, "TMPDIR": contract_tmpdir},
                timeout_seconds=timeout_seconds,
                **spawn_kwargs,
            )

        return run_sandboxed

    def _directive_sandbox(
        self,
        state: _RuntimeContextState,
        workspace_path: Path,
    ) -> DirectiveSandbox | None:
        """Hand the Workspace to the Contract's uid and describe the floor it spawns under.

        Called immediately before each Directive because everything the Runner did to the
        checkout in between — clone, fetch, commit, push — ran as the Runner's own uid.
        """

        if self._contract_isolation is None:
            return None
        self._contract_isolation.hand_workspace_to_contract(state.contract_id, workspace_path)
        return self._contract_isolation.sandbox(
            state.contract_id,
            runtime_kind=state.cli_kind,
            memory_limit_bytes=state.memory_limit_bytes,
        )

    def _contract_sandbox(self, state: _RuntimeContextState) -> DirectiveSandbox | None:
        """The Contract's floor without handing the checkout over.

        Runner Hooks and the callback socket need the uid and the Contract's tmp
        directory *before* there is a checkout to re-own; `_directive_sandbox` is the
        same thing plus the hand-over, immediately before the Agent Runtime runs.
        """

        if self._contract_isolation is None:
            return None
        return self._contract_isolation.sandbox(
            state.contract_id,
            runtime_kind=state.cli_kind,
            memory_limit_bytes=state.memory_limit_bytes,
        )

    @contextlib.asynccontextmanager
    async def _directive_attempt(
        self,
        *,
        work_record_id: str,
        state: _RuntimeContextState,
        workspace_path: Path,
        directive_number: int,
        sandbox: DirectiveSandbox | None,
        reserve_max_tokens: int | None = None,
        mcp_plan: McpPlan | None = None,
        asked: list[QuestionAsked] | None = None,
    ) -> AsyncIterator[_DirectiveAttempt]:
        """Open one attempt's hook lifecycle and its callback socket (PRD issue 45).

        Both die with the attempt, on every exit path: ``pre_exit`` runs from the hook
        session's own deferred teardown, and the socket — with the bearer that is the
        only thing it accepts — is closed and unlinked here. That expiry is what makes
        the bearer defensible under ADR-0011 §9.
        """

        facts = AttemptFacts(
            work_record_id=work_record_id,
            directive_id=directive_id(
                work_record_id=work_record_id, directive_number=directive_number
            ),
            contract_id=contract_path_segment(state.contract_id),
            agent_id=state.agent_id or "",
            persona=state.persona_slug or "",
            runtime_kind=state.cli_kind,
        )

        async def append_evidence(source: str, payload: Mapping[str, object]) -> None:
            await self._fastapi_client.append_evidence(
                work_record_id, source=source, payload=dict(payload)
            )

        await self._refuse_skill_digest_mismatch(work_record_id, state)
        credentials = await self._resolve_credentials(work_record_id, state)
        if credentials is not None:
            # Names only, at the moment they resolved (22 A1's Evidence): a support read
            # of "which credentials did this Directive have" must not be answerable by
            # reading the value, and a digest of a short API key is a guessing oracle.
            await self._fastapi_client.append_evidence(
                work_record_id,
                source=CREDENTIAL_RESOLVED_SOURCE,
                payload={
                    "event": "credential_reference.resolved",
                    "contract_id": state.contract_id,
                    "references": list(credentials.references()),
                },
            )
        uid = sandbox.uid if sandbox is not None else None
        # Short and derived: the socket path has ~104 bytes to live in (callback.py), and
        # a Workspace path has already spent them on the Contract and Work Record ids.
        key = hashlib.sha256(facts.directive_id.encode()).hexdigest()[:12]
        scratch_root = sandbox.tmp_dir if sandbox is not None else Path(tempfile.gettempdir())
        attempt_dir = prepare_attempt_dir(scratch_root / f"attempt-{key}", uid)
        async with contextlib.AsyncExitStack() as stack:
            stack.callback(shutil.rmtree, attempt_dir, ignore_errors=True)
            callback_env: dict[str, str] = {}
            if self._socket_dir is not None:
                socket_dir = prepare_attempt_dir(self._socket_dir / key, uid)
                stack.callback(shutil.rmtree, socket_dir, ignore_errors=True)
                server = await stack.enter_async_context(
                    AttemptCallbackServer(
                        socket_path=socket_dir / "s.sock",
                        handlers=self._callback_handlers(
                            work_record_id,
                            state,
                            workspace_path,
                            directive_number=directive_number,
                            asked=asked,
                        ),
                        uid=uid,
                    )
                )
                callback_env = server.env()
            if self._llm_proxy is not None and self._meters_through_the_proxy(state):
                # API-key mode only (issue 43): a device-login Contract never has a slot
                # to resolve, and issue 31 meters it from the harness's own output. One
                # bearer serves the callback socket and the proxy alike.
                proxy_attempt = await stack.enter_async_context(
                    self._llm_proxy.attempt(
                        directive_id=facts.directive_id,
                        contract_id=_optional_uuid(state.contract_id),
                        agent_id=_optional_uuid(state.agent_id),
                        work_record_id=_optional_uuid(work_record_id),
                        token=callback_env.get(CALLBACK_TOKEN_ENV),
                        reserve_max_tokens=reserve_max_tokens,
                    )
                )
                callback_env.update(proxy_attempt.env(state.cli_kind))
            mcp_servers, egress_allow_list = await self._start_mcp_and_egress(
                stack,
                work_record_id=work_record_id,
                state=state,
                plan=mcp_plan or McpPlan(),
                credentials=credentials,
                env=callback_env,
            )
            delivered_preamble = await self._deliver_skills(
                stack,
                work_record_id=work_record_id,
                state=state,
                sandbox=sandbox,
                directive_number=directive_number,
            )
            session = await stack.enter_async_context(
                DirectiveHookSession(
                    hooks=self._hooks,
                    facts=facts,
                    workspace_path=workspace_path,
                    sandbox=sandbox,
                    attempt_dir=attempt_dir,
                    append_evidence=append_evidence,
                    # The names the Runner may set on a Directive, as names — never the
                    # values (map ticket 26 §1's v4 reject-gate shape). Derived from the
                    # reserved set rather than restated, so it cannot drift from it; the
                    # `environment` hook has not run yet, so its exports are not here.
                    proposed_env_names=tuple(sorted(RESERVED_DIRECTIVE_ENV)),
                )
            )
            yield _DirectiveAttempt(
                hooks=session,
                extra_env=dict(callback_env),
                credentials=credentials,
                mcp_servers=mcp_servers,
                egress_allow_list=egress_allow_list,
                skills_preamble=delivered_preamble,
            )

    async def _refuse_skill_digest_mismatch(
        self, work_record_id: str, state: _RuntimeContextState
    ) -> None:
        """A Skill whose body is not the version that was published fails the Directive
        non-retryably (console-v2 issue 28): a retry would fetch the same body, and
        running on it would hand the Agent prompt material no person reviewed."""

        mismatch = first_digest_mismatch(state.skills)
        if mismatch is None:
            return
        await _raise_recorded(
            ApplicationError(
                f"Skill {mismatch.slug} version {mismatch.version} does not match its sha256",
                non_retryable=True,
            ),
            self._fastapi_client,
            work_record_id,
            source=SKILLS_EVIDENCE_SOURCE,
            actor=self._actor(),
            payload={
                "event": "skills.digest_mismatch",
                "agent_id": state.agent_id,
                "slug": mismatch.slug,
                "version": mismatch.version,
                "sha256": mismatch.sha256,
            },
        )

    async def _deliver_skills(
        self,
        stack: contextlib.AsyncExitStack,
        *,
        work_record_id: str,
        state: _RuntimeContextState,
        sandbox: DirectiveSandbox | None,
        directive_number: int,
    ) -> str:
        """Put the attached Skills where this runtime reads them; return any preamble.

        Written files are removed on the attempt's exit, success or failure. A Runner
        with no Contract sandbox has no harness root it owns, so it falls back to the
        preamble rather than writing into a shared ``CODEX_HOME``.
        """

        if not state.skills:
            return ""
        delivery = delivery_for(state.cli_kind)
        if delivery is SkillDelivery.DIRECTORY and sandbox is None:
            delivery = SkillDelivery.PROMPT_PREAMBLE
        preamble = ""
        if delivery is SkillDelivery.DIRECTORY:
            assert sandbox is not None
            # Registered before the write, so a write that fails half-way is cleaned too.
            stack.push_async_callback(
                remove_skills, sandbox.harness_config_dir, state.skills, uid=sandbox.uid
            )
            await write_skills(sandbox.harness_config_dir, state.skills, uid=sandbox.uid)
        else:
            preamble = skills_preamble(state.skills)
        await self._fastapi_client.append_evidence(
            work_record_id,
            source=SKILLS_EVIDENCE_SOURCE,
            payload={
                "event": "skills.delivered",
                "directive_number": directive_number,
                "agent_id": state.agent_id,
                "cli_kind": state.cli_kind,
                "delivery": delivery.value,
                "skills": [
                    {"slug": skill.slug, "version": skill.version, "sha256": skill.sha256}
                    for skill in state.skills
                ],
            },
        )
        return preamble

    async def _plan_mcp(
        self,
        work_record_id: str,
        state: _RuntimeContextState,
        *,
        consented: tuple[str, ...] = (),
        declined: tuple[str, ...] = (),
        ask_owner: bool = True,
    ) -> McpPlan | OwnerConfirmationPending:
        """MCP config assembly's decision half, before anything is started (PRD issue 58).

        A ``confirm`` asks the owner here, before the server is written: MCP has no
        per-call seam inside the CLI, so this is the narrowest place the ask can bite. A
        Scoped Allowance answers it on the spot (the raise consults it control-plane
        side); otherwise the Directive returns pending and the loop waits for the
        owner's signal, exactly as a ``confirm`` on ``push`` does. ``ask_owner=False`` is
        a Directive with no owner step to wait on (the Learning Directive): its
        ``confirm`` servers are withheld and the Evidence says why.
        """

        if not state.mcp_servers:
            return McpPlan()
        await self._require_live_link(
            work_record_id,
            state,
            verb="mcp",
            identifier=",".join(spec.slug for spec in state.mcp_servers),
            resource_type="mcp",
        )
        plan = plan_mcp(
            self._current_snapshot(state),
            state.mcp_servers,
            consented=consented,
            declined=declined,
        )
        granted = list(plan.granted)
        withheld = list(plan.withheld)
        for spec, decision in plan.to_confirm:
            if not ask_owner:
                withheld.append((spec, decision, "no_owner_step"))
                continue
            raised = await self._fastapi_client.raise_owner_confirmation(
                {
                    "work_record_id": work_record_id,
                    "agent_id": decision.deciding.agent_id,
                    "verb": decision.verb,
                    "resource": spec.slug,
                    "target": None,
                    "grant_entry": decision.deciding.deciding_entry,
                    "summary": (
                        f"Give the Agent the MCP server {spec.slug!r} for this Directive. "
                        "MCP cannot hide a server's tools, so all of them are exposed: "
                        f"{', '.join(spec.tools) or 'none registered'}."
                    ),
                    "file_list": [],
                }
            )
            if bool(raised.get("granted")):
                granted.append((spec, decision))
                continue
            return OwnerConfirmationPending(
                owner_confirmation_id=str(raised["owner_confirmation_id"]),
                verb=decision.verb,
                window_seconds=float(raised["window_seconds"]),
                reminder_after_seconds=float(raised.get("reminder_after_seconds") or 0.0),
            )
        return McpPlan(granted=tuple(granted), withheld=tuple(withheld))

    async def _start_mcp_and_egress(
        self,
        stack: contextlib.AsyncExitStack,
        *,
        work_record_id: str,
        state: _RuntimeContextState,
        plan: McpPlan,
        credentials: DirectiveCredentials | None,
        env: dict[str, str],
    ) -> tuple[tuple[McpServerEntry, ...] | None, tuple[str, ...]]:
        """MCP config assembly's process half, and the attempt's egress proxy (issue 58).

        Placement is 17 A4's: a row naming a Credential Reference is started here, as
        the Runner's own uid with the value in its env, and handed to the CLI as a
        loopback URL behind the attempt's bearer; every other row is written as the
        command or URL the CLI reaches itself. ``env`` is the attempt's Directive
        environment, and gains the bearer (if no callback socket minted one) and the
        proxy variables.
        """

        needs_token = bool(state.egress_allow_list) or any(
            spec.credential_reference is not None for spec, _ in plan.granted
        )
        if needs_token and CALLBACK_TOKEN_ENV not in env:
            env[CALLBACK_TOKEN_ENV] = secrets.token_urlsafe(32)
        entries: list[McpServerEntry] = []
        for spec, _decision in plan.granted:
            reference = spec.credential_reference
            if reference is None:
                entries.append(entry_for(spec))
                continue
            if credentials is None or reference not in credentials.references():
                # 22 A1's fail-closed half: a server told to run with a credential does
                # not run without it, and the Evidence names the reference.
                error = UnresolvableCredentialReferenceError(
                    reference, contract_id=state.contract_id, reason="not_in_manifest"
                )
                await self._fastapi_client.append_evidence(
                    work_record_id, source=CREDENTIAL_UNRESOLVABLE_SOURCE, payload=error.evidence()
                )
                raise error
            hosted = await stack.enter_async_context(
                RunnerHostedServer(
                    spec,
                    credential_env=credentials.runner_hosted_server_env([reference]),
                    token=env[CALLBACK_TOKEN_ENV],
                    spawner=self._mcp_spawner,
                    health=self._tool_server_health,
                )
            )
            entries.append(entry_for(spec, hosted_url=hosted.url, bearer_env=CALLBACK_TOKEN_ENV))

        allow_list: tuple[str, ...] = ()
        if state.egress_allow_list:
            allow_list = tuple(
                dict.fromkeys(
                    (
                        *state.egress_allow_list,
                        *(
                            host
                            for host in (
                                host_of(_github_remote_url(state.repository)),
                                *(host_of(entry.url) for entry in entries if entry.url),
                                *(
                                    host_of(value)
                                    for name, value in env.items()
                                    if name.endswith("_BASE_URL")
                                ),
                            )
                            if host is not None
                        ),
                    )
                )
            )

            async def refused(host: str, port: int) -> None:
                await self._fastapi_client.append_evidence(
                    work_record_id,
                    source=EGRESS_REFUSED_SOURCE,
                    payload={"event": "egress.refused", "host": host, "port": port},
                )

            proxy = await stack.enter_async_context(
                EgressProxy(
                    allow_list,
                    token=env[CALLBACK_TOKEN_ENV],
                    resolver=self._egress_resolver,
                    on_refused=refused,
                )
            )
            env.update(proxy.env())

        if state.mcp_servers:
            await self._fastapi_client.append_evidence(
                work_record_id,
                source=MCP_ASSEMBLY_SOURCE,
                payload={
                    "agent_id": state.agent_id,
                    "cli_kind": state.cli_kind,
                    **plan.evidence(egress_allow_list=allow_list),
                },
            )
        return (tuple(entries) if state.mcp_servers else None), allow_list

    async def _resolve_credentials(
        self, work_record_id: str, state: _RuntimeContextState
    ) -> DirectiveCredentials | None:
        """Resolve this Contract's Credential References, or fail the Directive closed.

        22 A1: the Runner resolves a reference at Directive time from its host store and
        hands the value only to verb seams and Runner-hosted MCP servers. An unresolvable
        one is not degraded into "run without it" -- the Directive would then do the work
        wrong and the Evidence would not say why -- so it raises, naming the reference,
        after appending the Evidence that names it.
        """

        if not state.credential_references:
            return None
        resolver = self._credentials or CredentialResolver(store=EmptyCredentialStore())
        try:
            return resolver.resolve(
                contract_id=state.contract_id, manifest=state.credential_references
            )
        except UnresolvableCredentialReferenceError as error:
            await self._fastapi_client.append_evidence(
                work_record_id,
                source=CREDENTIAL_UNRESOLVABLE_SOURCE,
                payload=error.evidence(),
            )
            raise

    def _meters_through_the_proxy(self, state: _RuntimeContextState) -> bool:
        """Whether this Directive's runtime is metered at the proxy (issue 43).

        The mirror of :meth:`_report_harness_usage`'s test: an ``api_key`` Directive
        routes through the proxy and is metered there; a device-login one bypasses it
        entirely and is metered from the harness's own output by PRD issue 31. Read off
        the runtime the Work Record's `cli_kind` names, never a process-wide one.
        """

        return self._auth_model(state) == AuthModel.API_KEY

    def _auth_model(self, state: _RuntimeContextState) -> AuthModel:
        # `getattr` with a default, not `.auth_model` outright: a runtime that carries
        # none (a test double built before this port grew the field) is the same case as
        # `api_key` -- nothing to report from the harness -- never a crash on an
        # unrelated Directive. A kind this Runner does not serve reads the same way; the
        # Directive itself has already failed closed in :meth:`_agent_runtime_for`.
        runtime = self._agent_runtimes.get(state.cli_kind)
        model = getattr(runtime, "auth_model", AuthModel.API_KEY)
        return model if isinstance(model, AuthModel) else AuthModel.API_KEY

    async def _agent_runtime_for(
        self, work_record_id: str, state: _RuntimeContextState
    ) -> AgentRuntime:
        """The runtime the Work Record's `cli_kind` names, from this Runner's registry.

        Fails closed (local-agents 01): a kind this process does not serve fails the
        Directive non-retryably, with Evidence naming the kind and the Runner, and never
        falls back to another runtime -- routing should not have sent it here, and a
        retry on this Runner would find the same registry.
        """

        runtime = self._agent_runtimes.get(state.cli_kind)
        if runtime is not None:
            return runtime
        served = sorted(self._agent_runtimes)
        runner_id = self._routing_identity.runner_id if self._routing_identity else None
        await _raise_recorded(
            ApplicationError(
                f"Runner {runner_id or 'unregistered'} serves {served}, not the Work "
                f"Record's Agent Runtime {state.cli_kind!r}",
                non_retryable=True,
            ),
            self._fastapi_client,
            work_record_id,
            source=AGENT_RUNTIME_EVIDENCE_SOURCE,
            actor=self._actor(),
            payload={
                "event": "directive.runtime_unserved",
                "cli_kind": state.cli_kind,
                "runner_id": runner_id,
                "served_cli_kinds": served,
            },
        )

    def _callback_handlers(
        self,
        work_record_id: str,
        state: _RuntimeContextState,
        workspace_path: Path,
        *,
        directive_number: int = 0,
        asked: list[QuestionAsked] | None = None,
    ) -> CallbackHandlers:
        """What ``agentic-runner annotate | artifact upload | verb`` actually do.

        A ``verb`` callback is not a second authority: it runs `_evaluate_verb`, the same
        local Grant evaluation and the same Evidence Event as the activity seams, so a
        Directive that calls back gets exactly the answer the seam would have given and
        the audit shows one evaluation shape (ADR-0011 §10).
        """

        async def annotate(request: AnnotateRequest) -> AnnotateResponse:
            await self._fastapi_client.append_evidence(
                work_record_id,
                source=_ANNOTATION_SOURCE,
                payload={
                    "context": request.context,
                    "style": request.style,
                    # An annotation is Agent-authored text going to the control plane, so
                    # it is scrubbed exactly like a diagnostic (map ticket 06 §4).
                    "body": bound_text(
                        redact_secret_like_text(request.body),
                        _ANNOTATION_LIMIT_BYTES,
                        [],
                        "annotation",
                    ),
                },
            )
            return AnnotateResponse(accepted=True, context=request.context)

        async def artifact(request: ArtifactRequest) -> ArtifactResponse:
            root = workspace_path.resolve(strict=False)
            candidate = (root / request.path).resolve(strict=False)
            if root not in candidate.parents or not candidate.is_file():
                return ArtifactResponse(
                    accepted=False,
                    path=request.path,
                    size_bytes=0,
                    sha256="",
                    reason="an artifact must be an existing file inside this Workspace",
                )
            size_bytes = candidate.stat().st_size
            # Off the event loop: a Workspace artifact is as large as the repository's
            # build makes it, and this runs beside every other activity on the pod.
            digest = await asyncio.to_thread(_file_sha256, candidate)
            await self._fastapi_client.append_evidence(
                work_record_id,
                source=_ARTIFACT_SOURCE,
                payload={
                    "path": candidate.relative_to(root).as_posix(),
                    "label": request.label,
                    "size_bytes": size_bytes,
                    "sha256": digest,
                },
            )
            return ArtifactResponse(
                accepted=True,
                path=candidate.relative_to(root).as_posix(),
                size_bytes=size_bytes,
                sha256=digest,
            )

        async def verb(request: VerbRequest) -> VerbResponse:
            try:
                decision = await self._evaluate_verb(
                    work_record_id, state, verb=request.verb, identifier=request.resource
                )
            except SeamUnavailableError as error:
                # The Directive asked over its own socket, so it is answered rather than
                # failed: a refused verb reads the same as a `deny` except for the reason
                # (PRD issue 44), and the subprocess goes on running. The activity seam is
                # the half that fails the attempt for Temporal to retry.
                return VerbResponse(
                    verb=request.verb,
                    resource=request.resource,
                    decision=Decision.DENY.value,
                    allowed=False,
                    reason=error.reason,
                )
            return VerbResponse(
                verb=request.verb,
                resource=request.resource,
                decision=decision.decision.value,
                allowed=decision.allowed,
                reason=decision.reason,
            )

        async def message_send(request: MessageSendRequest) -> MessageSendResponse:
            decision = await self._evaluate_channel_verb(
                work_record_id, state, verb=CHANNEL_WRITE_VERB, channel_id=request.channel_id
            )
            if not decision.allowed:
                return MessageSendResponse(
                    accepted=False, decision=decision.decision.value, reason=decision.reason
                )
            store, refusal = self._open_store(state)
            if store is None:
                return MessageSendResponse(
                    accepted=False, decision=decision.decision.value, reason=refusal
                )
            envelope = store.append(
                contract_id=state.contract_id or "",
                work_record_id=work_record_id,
                sender_agent_id=state.agent_id or "",
                channel_id=request.channel_id,
                kind=request.kind,
                body=request.body,
                references=request.references,
                recipient_agent_id=request.recipient_agent_id,
                recipient_role=request.recipient_role,
                verdict=request.verdict,
                head_sha=request.head_sha,
            )
            # The Conversation's shape crosses; the body stays (map ticket 10).
            await self._fastapi_client.append_evidence(
                work_record_id,
                source=_MESSAGE_SOURCE,
                payload={"event": "channel.message", **envelope.metadata().model_dump(mode="json")},
            )
            if self._workflow_signaller is not None:
                await self._workflow_signaller.signal(
                    PendingMessagesSignal(
                        work_record_id=work_record_id,
                        agent_id=state.agent_id or "",
                        channel_id=request.channel_id,
                        count=store.depth(
                            contract_id=state.contract_id or "",
                            work_record_id=work_record_id,
                            channel_id=request.channel_id,
                        ),
                        kind=envelope.kind.value,
                        message_id=str(envelope.message_id),
                        recipient_agent_id=(
                            str(envelope.recipient_agent_id) if envelope.recipient_agent_id else ""
                        ),
                        recipient_role=envelope.recipient_role or "",
                        verdict=envelope.verdict or "",
                        head_sha=envelope.head_sha or "",
                    )
                )
            return MessageSendResponse(
                accepted=True,
                decision=decision.decision.value,
                reason=decision.reason,
                message_id=str(envelope.message_id),
            )

        async def message_list(request: MessageListRequest) -> MessageListResponse:
            decision = await self._evaluate_channel_verb(
                work_record_id, state, verb=CHANNEL_READ_VERB, channel_id=request.channel_id
            )
            if not decision.allowed:
                return MessageListResponse(
                    allowed=False, decision=decision.decision.value, reason=decision.reason
                )
            store, refusal = self._open_store(state)
            if store is None:
                return MessageListResponse(
                    allowed=False, decision=decision.decision.value, reason=refusal
                )
            return MessageListResponse(
                allowed=True,
                decision=decision.decision.value,
                reason=decision.reason,
                messages=store.read(
                    contract_id=state.contract_id or "",
                    work_record_id=work_record_id,
                    channel_id=request.channel_id,
                ),
            )

        async def ask(request: AskRequest) -> AskResponse:
            # One Question per Directive, and none from a Directive with no owner step
            # after it (the Learner): the hold happens once this attempt has ended, and
            # the loop holds on one Question at a time.
            if asked is None:
                return AskResponse(
                    accepted=False, decision="deny", reason="this Directive cannot wait on a person"
                )
            if asked:
                return AskResponse(
                    accepted=False,
                    decision="deny",
                    reason="this Directive already asked; its answer wakes the next one",
                )
            if not state.agent_id:
                return AskResponse(
                    accepted=False,
                    decision="deny",
                    reason="a Question names its asking Agent; this Work Record has none",
                )
            answer = await self._fastapi_client.raise_question(
                work_record_id,
                {
                    "agent_id": state.agent_id,
                    # Agent-authored text leaving for the control plane: scrubbed like a
                    # diagnostic or an annotation (map ticket 06 §4).
                    "text": redact_secret_like_text(request.text),
                    "directive_number": directive_number,
                },
            )
            decision = str(answer.get("decision") or "deny")
            reason = str(answer.get("reason") or "")
            question_id = str(answer.get("question_id") or "")
            if decision == Decision.DENY.value or not question_id:
                return AskResponse(accepted=False, decision=decision, reason=reason)
            confirmation = answer.get("owner_confirmation")
            asked.append(
                QuestionAsked(
                    question_id=question_id,
                    owner_confirmation=(
                        None
                        if not isinstance(confirmation, dict)
                        else OwnerConfirmationPending(
                            owner_confirmation_id=str(confirmation["owner_confirmation_id"]),
                            verb=WORK_ASK_VERB,
                            window_seconds=float(confirmation["window_seconds"]),
                            reminder_after_seconds=float(
                                confirmation.get("reminder_after_seconds") or 0.0
                            ),
                        )
                    ),
                )
            )
            return AskResponse(
                accepted=True,
                decision=decision,
                reason=reason,
                question_id=question_id,
                addressed_to=str(answer.get("addressed_to") or ""),
            )

        return CallbackHandlers(
            annotate=annotate,
            artifact=artifact,
            verb=verb,
            message_send=message_send,
            message_list=message_list,
            ask=ask,
        )

    def _open_store(self, state: _RuntimeContextState) -> tuple[MessageStore | None, str]:
        """The store a Directive may write, or why it may not (no store, sealed)."""

        if self._message_store is None:
            return None, "this Runner holds no Message store"
        if not state.contract_id or not state.agent_id:
            return None, "a Message names its sending Agent and Contract; this Work Record has none"
        sealed = self._message_store.sealed_state(state.contract_id)
        if sealed is not None:
            return None, f"the Contract's Message store is sealed ({sealed})"
        return self._message_store, ""

    async def _evaluate_channel_verb(
        self, work_record_id: str, state: _RuntimeContextState, *, verb: str, channel_id: str
    ) -> VerbDecision:
        """The `channel.*` seam (PRD issue 52): the same evaluation as every verb.

        The Agent names the Channel by id; the Grant globs over the registry selector,
        so the snapshot pairs the two. An id the registry does not carry is evaluated as
        itself, which the evaluator refuses as out of scope -- and records so.
        """

        selector = self._current_snapshot(state).channel_selector(channel_id)
        try:
            return await self._evaluate_verb(
                work_record_id,
                state,
                verb=verb,
                identifier=selector or channel_id,
                resource_type=CHANNEL_RESOURCE_TYPE,
            )
        except SeamUnavailableError as error:
            return VerbDecision(
                verb=verb,
                resource_type=CHANNEL_RESOURCE_TYPE,
                identifier=selector or channel_id,
                decision=Decision.DENY,
                reason=error.reason,
                agent_id=state.agent_id,
                contract_state=state.grant_snapshot.contract_state,
                enforced=True,
            )

    async def _verify_hook(
        self,
        name: HookName,
        *,
        work_record_id: str,
        state: _RuntimeContextState,
        workspace_path: Path,
        sandbox: DirectiveSandbox | None,
    ) -> HookRun | None:
        """Run one verify-phase hook. Never fatal, and never touches the verdict.

        ``pre_verify`` / ``post_verify`` sit after ``pre_runtime`` in the catalogue, so a
        non-zero exit is recorded and carried on (map ticket 26 §1). The Verifier is a
        Gate input: a hook may wrap it, never replace it and never overturn it.
        """

        run = await self._hooks.run(
            name,
            cwd=workspace_path,
            # No `directive_id`: the Verifier is its own activity against the approved
            # head, not a Directive attempt, and an empty fact is omitted rather than
            # exported blank -- a hook reading `AGENTIC_RUNNER_DIRECTIVE_ID` here would
            # otherwise be handed a Work Record id under a Directive's name.
            facts=AttemptFacts(
                work_record_id=work_record_id,
                contract_id=contract_path_segment(state.contract_id),
                agent_id=state.agent_id or "",
                persona=state.persona_slug or "",
                runtime_kind=state.cli_kind,
            ),
            sandbox=sandbox,
        )
        if run is not None:
            await self._fastapi_client.append_evidence(
                work_record_id, source=HOOK_EVIDENCE_SOURCE, payload=run.evidence()
            )
        return run

    async def _runtime_state(
        self,
        work_record_id: str,
        *,
        workspace_path: str = "",
        branch_head_sha: str | None = None,
        grant_snapshot: Mapping[str, Any] | None = None,
        agent_id: str = "",
    ) -> _RuntimeContextState:
        """Resolve one Work Record's state for this activity call -- for the Work
        Record's own Agent, or for the Swarm member ``agent_id`` names (PRD issue 53).

        No process cache (ADR-0013 §3, PRD issue 36 retired ``_runtime_contexts``): the
        runtime context is re-read from the control plane every time, and everything an
        earlier activity *learned* — the workspace the clone produced, the head a push
        produced, the Grant snapshot a Directive boundary refreshed — arrives on this
        activity's own input and leaves on its output. A Work Record therefore
        re-assembles on any Runner, and a retry on a fresh pod behaves like a first
        attempt instead of quietly losing state a dictionary was holding.

        The Grant snapshot is the one thing that no longer arrives either way when this
        Runner has a heartbeat stream (PRD issue 44): the control plane pushes it on
        change and the link holds the current one per Agent, so narrowing bites at the
        next *verb* rather than the next Directive. ``grant_snapshot`` carried on the
        input is the link-less host's source and is ignored wherever a link is attached.
        """

        runtime_context = await self._runtime_context_resolver.resolve(
            work_record_id, agent_id=agent_id or None
        )
        resolved_workspace = Path(workspace_path) if workspace_path else None
        if resolved_workspace is None and self._workspace_root is not None:
            resolved_workspace = _contract_workspace_path(
                workspace_root=self._workspace_root.resolve(),
                contract_id=runtime_context.contract_id,
                work_record_id=work_record_id,
            )
        snapshot_payload = (
            self._heartbeat_link.payload_for(runtime_context.agent_id)
            if self._heartbeat_link is not None
            else dict(grant_snapshot or {})
        )
        snapshot = (
            GrantSnapshot.from_payload(snapshot_payload)
            if snapshot_payload
            else UNENFORCED_SNAPSHOT
        )
        return _RuntimeContextState(
            reviewer=runtime_context.reviewer,
            repository=runtime_context.repo,
            verifier_argv=_runtime_verifier_argv(runtime_context),
            work_branch=work_branch_name(
                work_record_id=runtime_context.work_record_id,
                profile_slug=runtime_context.profile_slug,
            ),
            completion_criteria=runtime_context.completion_criteria,
            base_branch=runtime_context.base_branch,
            agent_id=runtime_context.agent_id,
            contract_id=runtime_context.contract_id,
            cli_kind=runtime_context.cli_kind,
            model=runtime_context.model,
            memory_limit_bytes=_profile_memory_limit_bytes(runtime_context.command_policy),
            grant_snapshot=snapshot,
            grant_snapshot_payload=snapshot_payload,
            persona_slug=runtime_context.persona_slug,
            persona_instructions=runtime_context.persona_instructions,
            experience=runtime_context.experience,
            experience_lesson_ids=tuple(runtime_context.experience_lesson_ids),
            experience_truncated=runtime_context.experience_truncated,
            skills=tuple(runtime_context.skills),
            workspace_path=resolved_workspace,
            branch_head_sha=branch_head_sha,
            product_id=runtime_context.product_id,
            data_class=runtime_context.data_class,
            action_tier=runtime_context.action_tier,
            risk_tier=runtime_context.risk_tier,
            requester_id=runtime_context.requester_id,
            tier_within_contract_ceiling=runtime_context.tier_within_contract_ceiling,
            credential_references=tuple(runtime_context.credential_references),
            mcp_servers=tuple(runtime_context.mcp_servers),
            egress_allow_list=_egress_allow_list(runtime_context.network_policy),
            kind=runtime_context.kind,
        )

    async def _refuse_unless_lead(
        self,
        request: BranchPullRequestInput | PullRequestReadyInput | PullRequestMergeInput,
        *,
        verb: str,
    ) -> _SeamVerdict | None:
        """`pr.*` verbs run only for the Lead (ADR-0012 §1, PRD issue 53).

        Decided before the Grant and independent of it: a Builder whose own Grant allows
        `pr.open` is still refused, with Evidence naming its role, because the Lead holds
        the branch and the PR. A `handoff` of Lead moves this with it -- the workflow
        names the current Lead on every seam input. Empty ids are the degenerate Swarm.
        """

        if not request.lead_agent_id or request.agent_id == request.lead_agent_id:
            return None
        # A participant (console-v2 issue 22) is the member whose seam input names no role.
        held = request.role or "no Role"
        await self._fastapi_client.append_evidence(
            request.work_record_id,
            source=_ROLE_REFUSAL_SOURCE,
            payload={
                "event": "swarm.verb_refused",
                "verb": verb,
                "resource": request.repository,
                "agent_id": request.agent_id,
                "role": held,
                "required_role": ROLE_LEAD,
                "lead_agent_id": request.lead_agent_id,
            },
        )
        return _SeamVerdict(
            decision=VerbDecision(
                verb=verb,
                resource_type=REPO_RESOURCE_TYPE,
                identifier=request.repository,
                decision=Decision.DENY,
                reason=(
                    f"`{verb}` runs only for the Lead: Agent {request.agent_id or 'unknown'} "
                    f"holds {held}, not {ROLE_LEAD}"
                ),
                agent_id=request.agent_id or None,
                contract_state="active",
                enforced=True,
            )
        )

    async def _refuse_on_veto(
        self,
        request: PullRequestReadyInput | PullRequestMergeInput,
        *,
        verb: str,
        head_sha: str,
    ) -> _SeamVerdict | None:
        """The Critic's veto, asserted where the verb acts (PRD issue 54, ADR-0012 §3).

        The workflow hands in who holds Critic and the head its standing `clear` names;
        this seam proceeds only when that head is the one it is about to act on. A
        `block`, no verdict yet, or a push since the `clear` all arrive here as a head
        that does not match, and all refuse -- before the Grant, like the Lead rule, and
        with Evidence. No Critic named is no veto: a Channel without one, or no Channel.
        """

        critic = request.veto_critic_agent_id
        if not critic or (head_sha and request.veto_cleared_head_sha == head_sha):
            return None
        reason = (
            f"`{verb}` is vetoed: the Critic {critic} has not cleared head "
            f"{head_sha or 'unknown'}"
            + (
                f" (its standing clear names {request.veto_cleared_head_sha})"
                if request.veto_cleared_head_sha
                else ""
            )
        )
        await self._fastapi_client.append_evidence(
            request.work_record_id,
            source=_CRITIC_VETO_SOURCE,
            payload={
                "event": "critic.veto_refused",
                "enforced_by": "runner",
                "verb": verb,
                "resource": request.repository,
                "critic_agent_id": critic,
                "head_sha": head_sha,
                "cleared_head_sha": request.veto_cleared_head_sha,
            },
        )
        return _SeamVerdict(
            decision=VerbDecision(
                verb=verb,
                resource_type=REPO_RESOURCE_TYPE,
                identifier=request.repository,
                decision=Decision.DENY,
                reason=reason,
                agent_id=request.agent_id or None,
                contract_state="active",
                enforced=True,
            )
        )

    async def _authorize(
        self,
        work_record_id: str,
        state: _RuntimeContextState,
        *,
        verb: str,
        repository: str,
        owner_confirmed_verbs: tuple[str, ...] = (),
        target: str | None = None,
        summary: str | None = None,
        file_list: tuple[str, ...] = (),
    ) -> _SeamVerdict:
        """Evaluate one privileged verb locally and record the evaluation as Evidence.

        Evidence is appended for every evaluation, allowed or refused (ADR-0011 §10):
        an audit that only records refusals cannot show that a merge was authorised.

        A ``confirm`` is neither: the chain escalates the verb to the Agent's owner
        (§14). The Scoped Allowance is consulted by the raise itself — control-plane
        side, where the rows live — so a match proceeds without a record and a
        narrowing that removed the verb never reaches this point, because the local
        evaluation above would already have denied it (§16).
        """

        decision = await self._evaluate_verb(
            work_record_id, state, verb=verb, identifier=repository
        )
        if not decision.needs_owner_confirmation:
            return _SeamVerdict(decision=decision)

        if verb in owner_confirmed_verbs:
            await self._fastapi_client.append_evidence(
                work_record_id,
                source=_OWNER_CONFIRMATION_SOURCE,
                payload={
                    "event": "proceeding_on_owner_confirmation",
                    "agent_id": decision.agent_id,
                    "verb": verb,
                    "resource": repository,
                },
            )
            return _SeamVerdict(decision=decision, owner_consented=True)

        raised = await self._fastapi_client.raise_owner_confirmation(
            {
                "work_record_id": work_record_id,
                "agent_id": decision.agent_id,
                "verb": verb,
                "resource": repository,
                "target": target,
                "grant_entry": decision.deciding_entry,
                "summary": summary,
                "file_list": list(file_list),
            }
        )
        if bool(raised.get("granted")):
            return _SeamVerdict(decision=decision, owner_consented=True)
        return _SeamVerdict(
            decision=decision,
            pending=OwnerConfirmationPending(
                owner_confirmation_id=str(raised["owner_confirmation_id"]),
                verb=verb,
                window_seconds=float(raised["window_seconds"]),
                reminder_after_seconds=float(raised.get("reminder_after_seconds") or 0.0),
            ),
        )

    async def _evaluate_verb(
        self,
        work_record_id: str,
        state: _RuntimeContextState,
        *,
        verb: str,
        identifier: str,
        resource_type: str = REPO_RESOURCE_TYPE,
    ) -> VerbDecision:
        """The one local Grant evaluation, and the Evidence Event that records it.

        Shared on purpose: `_authorize` is the activity seam and `_callback_handlers`'
        ``verb`` route is a Directive calling back over its attempt socket. Both must
        reach the same evaluator and write the same Evidence shape, or the callback
        would be a second, quieter answer to the question the seam already answers
        (ADR-0011 §10).
        """

        await self._require_live_link(
            work_record_id, state, verb=verb, identifier=identifier, resource_type=resource_type
        )
        decision = decide_verb(
            self._current_snapshot(state),
            verb=verb,
            identifier=identifier,
            resource_type=resource_type,
        )
        await self._fastapi_client.append_evidence(
            work_record_id,
            source=_GRANT_EVALUATION_SOURCE,
            payload=decision.evidence(),
        )
        return decision

    def _current_snapshot(self, state: _RuntimeContextState) -> GrantSnapshot:
        """The snapshot to decide *this* verb on, not the one the activity started with.

        ``state.grant_snapshot`` is materialised once per activity, but a push -- including
        the one the wake-forced exchange in ``_require_live_link`` just applied -- replaces
        what the link holds mid-Directive. Reading through the link here is what makes a
        narrowing bite at the next verb rather than the next activity (PRD issue 44); a
        link-less host has only what came in on the input, which is what it falls back to.
        """

        if self._heartbeat_link is None:
            return state.grant_snapshot
        return self._heartbeat_link.snapshot_for(state.agent_id) or state.grant_snapshot

    async def _require_live_link(
        self,
        work_record_id: str,
        state: _RuntimeContextState,
        *,
        verb: str,
        identifier: str,
        resource_type: str = REPO_RESOURCE_TYPE,
    ) -> None:
        """The liveness gate in front of every privileged verb (PRD issue 44).

        A Runner that has not heard the control plane for three minutes cannot know
        whether the Grant it holds is still the Grant, so it refuses the verb rather than
        acting on what it last heard (map ticket 03: staleness is a property of the link,
        not a TTL on a snapshot). The refusal is recorded as the same Evidence a ``deny``
        writes, with its own reason, and the Directive subprocess keeps running -- only
        this attempt fails, and Temporal retries it on this Runner's queue.
        """

        if self._heartbeat_link is None:
            return
        try:
            await self._heartbeat_link.require_ready(state.agent_id, verb=verb)
        except SeamUnavailableError as error:
            await self._fastapi_client.append_evidence(
                work_record_id,
                source=_GRANT_EVALUATION_SOURCE,
                payload={
                    "agent_id": state.agent_id,
                    "verb": verb,
                    "resource_type": resource_type,
                    "resource": identifier,
                    "decision": Decision.DENY.value,
                    "deciding_link": None,
                    "deciding_entry": None,
                    "reason": error.reason,
                    "detail": str(error),
                    "contract_state": state.grant_snapshot.contract_state,
                    "enforced": True,
                },
            )
            raise

    def _changed_paths_for_owner(self, repository: str, pr_number: int) -> tuple[str, ...]:
        """The file list the owner decides on, read off the PR (ADR-0011 s14).

        A best-effort disclosure: a GitHub read that fails must not turn an escalation
        into a hard activity failure, because the escalation exists precisely to keep a
        human in control of what the Agent does next.
        """

        if self._github_client is None or pr_number <= 0:
            return ()
        try:
            listing = self._github_client.list_changed_files(
                PullRequestFilesRequest(repo=repository, pr_number=pr_number)
            )
        except Exception:  # noqa: BLE001 - disclosure is best-effort, never load-bearing
            return ()
        return tuple(listing.changed_paths[:_OWNER_CONFIRMATION_FILE_LIST_LIMIT])

    def _runtime_dependencies_ready(self) -> bool:
        return (
            bool(self._agent_runtimes)
            and self._git_workspace is not None
            and self._github_client is not None
            and self._workspace_root is not None
        )

    async def _advance_work_record_lifecycle(
        self,
        work_record_id: str,
        *,
        to_state: str,
        source: str,
        details: Mapping[str, Any],
        end_reason: str | None = None,
    ) -> None:
        """Advance lifecycle milestones while tolerating idempotent activity retries."""
        payload: dict[str, Any] = {
            "actor": "temporal-worker",
            "source": source,
            "to_state": to_state,
            "payload": {
                "milestone": to_state,
                **dict(details),
            },
        }
        if end_reason is not None:
            # ENDED is the only state that carries one, and it lands in the same write as
            # the status so an ended record always says why (ADR-0011 s15).
            payload["end_reason"] = end_reason
        await self._fastapi_client.transition_work_record(work_record_id, payload)

    @activity.defn(name="wait_for_approval")
    async def wait_for_approval(self, request: ApprovalWaitInput) -> ApprovalWaitOutput:
        """Run one bounded Reviewer Gate poll: approved / denied / still pending.

        Only an explicit changes-requested review on the current head denies; the
        overwhelmingly common "no review yet" is returned as pending (neither flag set)
        so the workflow keeps the gate open. Fixes the bug that ended run 0e4c80b2:
        a single instantaneous poll seconds after PR creation read PENDING as a
        reviewer denial and drove the Work Record terminal INCIDENT.

        A PR merged or closed out from under the gate is equally decisive: the poll
        reports it (externally_merged / pr_closed) so the workflow terminates instead of
        polling an already-settled PR to the ~3-day approval_timeout and recording a
        misleading terminal INCIDENT for a change that may already be merged.
        """
        await self._assert_routed(request)
        if self._github_client is None:
            return ApprovalWaitOutput(
                repository=request.repository,
                pr_number=request.pr_number,
                approved=False,
                approver=None,
                expected_head_sha=None,
                unavailable=True,
            )
        started_at = self._monotonic()
        async with _liveness_heartbeats():
            for poll in range(_APPROVAL_POLLS_PER_ACTIVITY):
                if poll:
                    await asyncio.sleep(_APPROVAL_POLL_INTERVAL_SECONDS)
                if self._monotonic() - started_at >= _APPROVAL_ACTIVITY_TIME_BUDGET_SECONDS:
                    break
                try:
                    # to_thread: the GitHub client is synchronous httpx; run it off the
                    # worker event loop so a slow GitHub response cannot stall the
                    # liveness heartbeats of every activity on this pod (same rule as
                    # run_verifier's subprocess call below).
                    approval = await asyncio.to_thread(
                        self._github_client.wait_for_approval,
                        ApprovalRequest(repo=request.repository, pr_number=request.pr_number),
                    )
                except GitHubAppAuthenticationError:
                    # A failed check is a pending check, never an activity failure:
                    # raising here would put Temporal into unbounded retries that do not
                    # advance the workflow's poll count, freezing the gate forever on a
                    # persistent GitHub outage or credential rotation. Returned-pending
                    # failures burn the poll budget instead, so the gate still ends as
                    # approval_timeout within its ~3-day ceiling.
                    continue
                if approval.pr_merged or approval.pr_closed:
                    # PR lifecycle supersedes the review verdict: a merged or closed PR
                    # can never be approved-and-merged by this loop, so the gate's
                    # question is settled — checked before the approval computation so a
                    # stale current-head approval on a merged PR cannot drive merge_pr.
                    return ApprovalWaitOutput(
                        repository=approval.repo,
                        pr_number=approval.pr_number,
                        approved=False,
                        approver=None,
                        expected_head_sha=None,
                        externally_merged=approval.pr_merged,
                        pr_closed=approval.pr_closed,
                        external_merge_actor=approval.merged_by,
                        merge_commit_sha=approval.merge_commit_sha,
                    )
                # The org's four-eyes rule decides when the gate is satisfied, not the
                # first approval: below N distinct org humans on the current head this
                # is still "keep waiting", never a denial (ADR-0011 s6). The count
                # excludes the Agent's own bot identity by construction.
                approvals_observed = len(approval.approving_reviewers)
                approved = (
                    approval.state == ApprovalState.APPROVED
                    and approval.approved_head_sha is not None
                    and approval.approved_head_sha == approval.current_head_sha
                    and approvals_observed >= request.required_human_approvals
                )
                denied = approval.state == ApprovalState.CHANGES_REQUESTED
                if approved or denied:
                    return ApprovalWaitOutput(
                        repository=approval.repo,
                        pr_number=approval.pr_number,
                        approved=approved,
                        approver=approval.approver,
                        expected_head_sha=approval.current_head_sha if approved else None,
                        approvals_observed=approvals_observed,
                        denied=denied,
                    )
        return ApprovalWaitOutput(
            repository=request.repository,
            pr_number=request.pr_number,
            approved=False,
            approver=None,
            expected_head_sha=None,
        )

    @activity.defn(name="merge_pr")
    async def merge_pr(self, request: PullRequestMergeInput) -> PullRequestMergeOutput:
        """The ``pr.merge`` seam, in front of every merge (ADR-0011 s1, s6, s8).

        Three things must hold before GitHub is asked to merge, and this is the only
        place any of them can be asked: the Agent's Effective Grant allows ``pr.merge``
        on this repository, the Outcome ceiling was cleared by the Autonomy Policy or by
        the org's Approval, and the Product's ``required_human_approvals`` distinct org
        humans have an approving review standing on the head being merged.

        The evaluation is made here rather than handed in, so the deterministic workflow
        cannot fabricate one: it never sees the grant snapshot. The seam holds — it never
        fails — when the approval count is short, because an absent reviewer is not a
        failure of the change (map ticket 08's Product rule).
        """
        await self._assert_routed(request)
        if self._github_client is None:
            return PullRequestMergeOutput(
                repository=request.repository,
                pr_number=request.pr_number,
                merged=False,
                merge_commit_sha="",
            )
        merge_state = await self._runtime_state(
            request.work_record_id,
            grant_snapshot=request.grant_snapshot,
            agent_id=request.agent_id,
        )
        authorization = await self._refuse_unless_lead(request, verb=PR_MERGE_VERB)
        if authorization is None:
            authorization = await self._refuse_on_veto(
                request, verb=PR_MERGE_VERB, head_sha=request.expected_head_sha
            )
        if authorization is None:
            authorization = await self._authorize(
                request.work_record_id,
                merge_state,
                verb=PR_MERGE_VERB,
                repository=request.repository,
                owner_confirmed_verbs=request.owner_confirmed_verbs,
                target=str(request.pr_number),
                summary=(
                    f"Merge {request.repository} PR #{request.pr_number} at "
                    f"{request.expected_head_sha or 'its verified head'}."
                ),
                file_list=self._changed_paths_for_owner(request.repository, request.pr_number),
            )
        if not authorization.allowed:
            return PullRequestMergeOutput(
                repository=request.repository,
                pr_number=request.pr_number,
                merged=False,
                merge_commit_sha="",
                summary=(authorization.refusal_summary if authorization.refused else ""),
                grant_refused=authorization.refused,
                grant_refusal_reason=(
                    authorization.refusal_summary if authorization.refused else ""
                ),
                owner_confirmation=authorization.pending,
            )
        if not request.outcome_clearance:
            # Unreachable from the workflow, which names a clearance on both merge paths.
            # Kept because the ceiling is half of what authorises a merge (ADR-0011 s1):
            # a future caller that forgets it must be refused, never merged by default.
            return PullRequestMergeOutput(
                repository=request.repository,
                pr_number=request.pr_number,
                merged=False,
                merge_commit_sha="",
                summary=_UNCLEARED_OUTCOME_REASON,
                grant_refused=True,
                grant_refusal_reason=_UNCLEARED_OUTCOME_REASON,
            )
        approvals_required = self._required_human_approvals(
            merge_state, repository=request.repository
        )
        approvals_observed = await self._count_human_approvals(request)
        await self._fastapi_client.append_evidence(
            request.work_record_id,
            source=_MERGE_SEAM_SOURCE,
            payload={
                "repository": request.repository,
                "pr_number": request.pr_number,
                "expected_head_sha": request.expected_head_sha,
                "outcome_clearance": request.outcome_clearance,
                "approvals_observed": approvals_observed,
                "approvals_required": approvals_required,
                "merging": approvals_observed >= approvals_required,
            },
        )
        if approvals_observed < approvals_required:
            return PullRequestMergeOutput(
                repository=request.repository,
                pr_number=request.pr_number,
                merged=False,
                merge_commit_sha="",
                summary=_approvals_pending_summary(approvals_observed, approvals_required),
                approvals_pending=True,
                approvals_observed=approvals_observed,
                approvals_required=approvals_required,
            )
        merged = self._merge_authorized_pr(
            request,
            authorization=authorization,
            approvals_observed=approvals_observed,
            approvals_required=approvals_required,
        )
        if merged.retargeted_pr_numbers:
            await self._fastapi_client.append_evidence(
                request.work_record_id,
                source=_STACK_SOURCE,
                payload={
                    "event": "stack.retargeted",
                    "repository": request.repository,
                    "merged_pr_number": request.pr_number,
                    "retargeted_pr_numbers": list(merged.retargeted_pr_numbers),
                    "base": request.retarget_base_ref,
                },
            )
        return merged

    def _merge_authorized_pr(
        self,
        request: PullRequestMergeInput,
        *,
        authorization: _SeamVerdict,
        approvals_observed: int,
        approvals_required: int,
    ) -> PullRequestMergeOutput:
        """The single call site of a GitHub client's ``merge_pr`` (ADR-0011 s8).

        It takes the seam's own outputs as arguments it cannot manufacture — a
        ``_SeamVerdict`` is only ever produced by ``_authorize``, over a ``decide_verb``
        against a grant snapshot — and re-asserts them, so a future caller reaching this
        method without going through the seam raises instead of merging. ``tests/unit/
        test_merge_seam_structure.py`` pins the "single call site" half structurally.
        """
        if not authorization.allowed or approvals_observed < approvals_required:
            raise RuntimeError(
                "merge attempted without a cleared pr.merge seam; this is a programming "
                "error in the Runner, not a governance decision"
            )
        if self._github_client is None:  # pragma: no cover - the seam checks this first
            raise RuntimeError("merge attempted with no GitHub adapter configured")
        try:
            merge = self._github_client.merge_pr(
                MergeRequest(
                    repo=request.repository,
                    pr_number=request.pr_number,
                    commit_title=request.commit_title,
                    expected_head_sha=request.expected_head_sha,
                )
            )
        except (GitHubAppAuthenticationError, PermissionError) as exc:
            return PullRequestMergeOutput(
                repository=request.repository,
                pr_number=request.pr_number,
                merged=False,
                merge_commit_sha="",
                summary=_merge_exception_summary(exc),
            )
        retargeted = self._retarget_dependents(request) if merge.merged else ()
        return PullRequestMergeOutput(
            repository=merge.repo,
            pr_number=merge.pr_number,
            merged=merge.merged,
            merge_commit_sha=merge.merge_commit_sha,
            approvals_observed=approvals_observed,
            approvals_required=approvals_required,
            retargeted_pr_numbers=retargeted,
        )

    def _retarget_dependents(self, request: PullRequestMergeInput) -> tuple[int, ...]:
        """The base of a stack merged: its dependents move to the default branch here,
        at the `pr.merge` seam, because GitHub does not while the branch exists (PRD
        issue 59, 19 A3). One `pr.merge` per PR still: nothing merges but this one."""

        if self._github_client is None or not request.retarget_pr_numbers:
            return ()
        retargeted: list[int] = []
        for pr_number in request.retarget_pr_numbers:
            response = self._github_client.retarget_pr(
                PullRequestRetargetRequest(
                    repo=request.repository, pr_number=pr_number, base=request.retarget_base_ref
                )
            )
            if response.retargeted:
                retargeted.append(pr_number)
        return tuple(retargeted)

    def _required_human_approvals(self, state: _RuntimeContextState, *, repository: str) -> int:
        return self._current_snapshot(state).required_human_approvals(identifier=repository)

    async def _count_human_approvals(self, request: PullRequestMergeInput) -> int:
        """Distinct org humans whose approval stands on the head about to be merged.

        Read at the merge, not carried from the Reviewer Gate: an approval dismissed or
        a review submitted between the gate and the merge changes the count, and it is
        the count at the merge instant that the org's four-eyes rule is about.
        """
        if self._github_client is None:  # pragma: no cover - the seam checks this first
            return 0
        approval = await asyncio.to_thread(
            self._github_client.wait_for_approval,
            ApprovalRequest(repo=request.repository, pr_number=request.pr_number),
        )
        if approval.current_head_sha != request.expected_head_sha:
            # A head that moved under the gate is a different change; its approvals are
            # not approvals of what this merge would land.
            return 0
        # The Critic is never one of the org's four eyes (PRD issue 54), named here on
        # top of the bot rule so it holds whatever identity its reviews arrived as.
        excluded = set(request.excluded_reviewer_logins)
        return len([login for login in approval.approving_reviewers if login not in excluded])

    @activity.defn(name="post_pr_review")
    async def post_pr_review(self, request: PullRequestReviewInput) -> PullRequestReviewOutput:
        """The `pr.comment` seam: the Critic's `verdict` as a PR review (PRD issue 54).

        Evaluated as ``(repo, <repository>, pr.comment)`` against the *Critic's* Effective
        Grant -- ``allow`` posts, ``deny`` refuses and posts nothing, ``confirm`` asks the
        Critic's owner -- with Evidence on every evaluation. The review goes out on the
        App token acting for the named Agent, exactly as ``pr.open`` does, pinned to the
        head the verdict names: `block` is ``REQUEST_CHANGES``, `clear` a ``COMMENT``,
        and there is no path to ``APPROVE``. The findings are the Message body, read
        from this Runner's own store; they reach GitHub and never the control plane.
        """

        await self._assert_routed(request)
        if self._github_client is None:
            return PullRequestReviewOutput(work_record_id=request.work_record_id, posted=False)
        state = await self._runtime_state(
            request.work_record_id,
            grant_snapshot=request.grant_snapshot,
            agent_id=request.agent_id,
        )
        verdict = await self._authorize(
            request.work_record_id,
            state,
            verb=PR_COMMENT_VERB,
            repository=request.repository,
            owner_confirmed_verbs=request.owner_confirmed_verbs,
            target=str(request.pr_number),
            summary=(
                f"Post the Critic's `{request.verdict}` review on {request.repository} PR "
                f"#{request.pr_number} at {request.head_sha}."
            ),
        )
        if not verdict.allowed:
            return PullRequestReviewOutput(
                work_record_id=request.work_record_id,
                posted=False,
                grant_refused=verdict.refused,
                grant_refusal_reason=(verdict.refusal_summary if verdict.refused else ""),
                owner_confirmation=verdict.pending,
            )
        review = self._github_client.submit_review(
            PullRequestReviewRequest(
                repo=request.repository,
                pr_number=request.pr_number,
                commit_id=request.head_sha,
                event=(
                    ReviewEvent.REQUEST_CHANGES
                    if request.verdict == VERDICT_BLOCK
                    else ReviewEvent.COMMENT
                ),
                body=self._verdict_review_body(request, contract_id=state.contract_id),
            )
        )
        return PullRequestReviewOutput(
            work_record_id=request.work_record_id,
            posted=True,
            review_id=review.review_id,
            review_state=review.state,
            reviewer_login=review.reviewer_login,
        )

    def _verdict_review_body(
        self, request: PullRequestReviewInput, *, contract_id: str | None
    ) -> str:
        """The Critic's attribution line, then its findings: the `verdict` Message body."""

        findings = ""
        if self._message_store is not None and contract_id and request.channel_id:
            try:
                messages = self._message_store.read(
                    contract_id=contract_id,
                    work_record_id=request.work_record_id,
                    channel_id=request.channel_id,
                )
            except PermissionError:
                # A sealed store posts the attribution alone: the verdict is the bit the
                # workflow already holds, and the findings are not the Runner's to leak.
                messages = []
            findings = next(
                (m.body for m in messages if str(m.message_id) == request.message_id), ""
            )
        header = (
            f"**Critic** (Agent `{request.agent_id}`): `{request.verdict}` on `{request.head_sha}`"
        )
        return f"{header}\n\n{findings}" if findings else header

    async def _call_github[Response](
        self, call: Callable[[GitHubClient], Response]
    ) -> Response | GitHubCallError:
        """One platform-requested GitHub call (QTS-1253), as a fact or a typed failure.

        Returned rather than raised, and caught broadly: the platform activity that asked
        owns the decision -- the Protected-Path check holds for human, the Incident
        records its PR comment as failed -- and it can only make it from a failure it
        receives. Off the event loop for the same reason as ``wait_for_approval``.
        """

        github = self._github_client
        if github is None:
            return GitHubCallError(reason=GitHubCallFailure.NO_GITHUB_CLIENT)
        try:
            return await asyncio.to_thread(call, github)
        except Exception as exc:  # noqa: BLE001 - reported to the caller, never swallowed
            return GitHubCallError(
                reason=GitHubCallFailure.GITHUB_ERROR,
                detail=redact_secret_like_text(f"{type(exc).__name__}: {exc}")[
                    :_GITHUB_FAILURE_DETAIL_LIMIT
                ],
            )

    @activity.defn(name="request_pr_review")
    async def request_pr_review(self, request: RequestReviewInput) -> RequestReviewOutput:
        """The Reviewer Gate handoff's GitHub nudge: ask the named reviewer on the PR."""

        await self._assert_routed(request)
        review = await self._call_github(
            lambda github: github.request_review(
                ReviewRequest(
                    repo=request.repository,
                    pr_number=request.pr_number,
                    reviewer=request.reviewer,
                )
            )
        )
        if isinstance(review, GitHubCallError):
            return RequestReviewOutput(
                repository=request.repository,
                pr_number=request.pr_number,
                reviewer=request.reviewer,
                failure=review,
            )
        return RequestReviewOutput(
            repository=request.repository,
            pr_number=request.pr_number,
            reviewer=review.reviewer,
            requested=review.requested,
        )

    @activity.defn(name="list_pr_changed_files")
    async def list_pr_changed_files(self, request: ChangedFilesInput) -> ChangedFilesOutput:
        """The full changed-file list the Autonomy Policy's Protected-Path check reads.

        Unbounded, unlike ``_changed_paths_for_owner``: a truncated list could hide the
        one Protected Path that must hold the merge.
        """

        await self._assert_routed(request)
        listing = await self._call_github(
            lambda github: github.list_changed_files(
                PullRequestFilesRequest(repo=request.repository, pr_number=request.pr_number)
            )
        )
        if isinstance(listing, GitHubCallError):
            return ChangedFilesOutput(
                repository=request.repository, pr_number=request.pr_number, failure=listing
            )
        return ChangedFilesOutput(
            repository=request.repository,
            pr_number=request.pr_number,
            changed_paths=tuple(listing.changed_paths),
        )

    @activity.defn(name="post_pr_comment")
    async def post_pr_comment(self, request: PullRequestCommentInput) -> PullRequestCommentOutput:
        """Append the failed-verification Incident's comment to the PR."""

        await self._assert_routed(request)
        comment = await self._call_github(
            lambda github: github.post_comment(
                CommentRequest(
                    repo=request.repository, pr_number=request.pr_number, body=request.body
                )
            )
        )
        if isinstance(comment, GitHubCallError):
            return PullRequestCommentOutput(
                repository=request.repository, pr_number=request.pr_number, failure=comment
            )
        return PullRequestCommentOutput(
            repository=request.repository,
            pr_number=request.pr_number,
            comment_id=comment.comment_number,
        )

    @activity.defn(name="close_pr")
    async def close_pr(self, request: PullRequestCloseInput) -> PullRequestCloseOutput:
        """Close the PR without merging and keep its branch (ADR-0011 s15's ending)."""

        await self._assert_routed(request)
        closed = await self._call_github(
            lambda github: github.close_pr(
                PullRequestCloseRequest(repo=request.repository, pr_number=request.pr_number)
            )
        )
        if isinstance(closed, GitHubCallError):
            return PullRequestCloseOutput(
                repository=request.repository, pr_number=request.pr_number, failure=closed
            )
        return PullRequestCloseOutput(
            repository=request.repository, pr_number=request.pr_number, closed=closed.closed
        )

    @activity.defn(name="probe_repository_access")
    async def probe_repository_access(self, request: RepositoryProbeInput) -> RepositoryProbeOutput:
        """Readiness: prove this Runner's own credential can read ``repository``."""

        await self._assert_routed(request)
        repository = await self._call_github(
            lambda github: github.read_repository(RepositoryReadRequest(repo=request.repository))
        )
        if isinstance(repository, GitHubCallError):
            return RepositoryProbeOutput(repository=request.repository, failure=repository)
        return RepositoryProbeOutput(
            repository=request.repository, default_branch=repository.default_branch
        )

    @activity.defn(name="run_verifier")
    async def run_verifier(self, request: VerifierRunInput) -> VerifierRunOutput:
        """Run product-owned verifier config and record bounded redacted evidence."""
        await self._assert_routed(request)
        async with _liveness_heartbeats(), self._fenced(request.work_record_id):
            if self._runtime_dependencies_ready():
                state = await self._runtime_state(
                    request.work_record_id, workspace_path=request.workspace_path
                )
                if (
                    state.branch_head_sha is not None
                    and state.branch_head_sha != request.approved_head_sha
                ):
                    summary = _approved_head_mismatch_summary(
                        local_head_sha=state.branch_head_sha,
                        approved_head_sha=request.approved_head_sha,
                    )
                    failure_details = _approved_head_mismatch_payload(
                        request=request,
                        local_head_sha=state.branch_head_sha,
                        verifier_summary=summary,
                    )
                    await self._advance_work_record_lifecycle(
                        request.work_record_id,
                        to_state="VERIFYING",
                        source="ralph.approved_head_mismatch",
                        details=failure_details,
                    )
                    await self._fastapi_client.record_verifier_result(
                        request.work_record_id,
                        {
                            "actor": "temporal-worker",
                            "source": "ralph.approved_head_mismatch",
                            "accepted": False,
                            "payload": failure_details,
                        },
                    )
                    return VerifierRunOutput(
                        work_record_id=request.work_record_id,
                        command=request.command,
                        passed=False,
                        summary=summary,
                        terminal=True,
                    )
                await self._advance_work_record_lifecycle(
                    request.work_record_id,
                    to_state="VERIFYING",
                    source="ralph.run_verifier",
                    details={
                        "repository": request.repository,
                        "pr_number": request.pr_number,
                        "merge_commit_sha": request.merge_commit_sha,
                    },
                )
                if state.workspace_path is None:
                    raise RuntimeError("Verifier workspace is unavailable")
                workspace_root = self._require_workspace_root()
                git_workspace = self._git_workspace
                if git_workspace is None:
                    raise RuntimeError("Git workspace is unavailable")
                try:
                    attestation = git_workspace.prepare_verifier_workspace(
                        PrepareVerifierWorkspaceRequest(
                            repo_full_name=state.repository,
                            workspace_root=workspace_root,
                            workspace_path=state.workspace_path,
                            work_branch=state.work_branch,
                            approved_head_sha=request.approved_head_sha,
                        )
                    )
                except (GitWorkspacePolicyError, KeyError) as exc:
                    summary = _verifier_workspace_attestation_failure_summary(exc)
                    await self._fastapi_client.record_verifier_result(
                        request.work_record_id,
                        {
                            "actor": "temporal-worker",
                            "source": "ralph.verifier_workspace_attestation",
                            "accepted": False,
                            "payload": _verifier_workspace_attestation_failure_payload(
                                request=request,
                                state=state,
                                workspace_root=workspace_root,
                                error=exc,
                            ),
                        },
                    )
                    return VerifierRunOutput(
                        work_record_id=request.work_record_id,
                        command=request.command,
                        passed=False,
                        summary=summary,
                        terminal=True,
                    )
                verifier_sandbox = self._contract_sandbox(state)
                await self._verify_hook(
                    HookName.PRE_VERIFY,
                    work_record_id=request.work_record_id,
                    state=state,
                    workspace_path=attestation.workspace_path,
                    sandbox=verifier_sandbox,
                )
                try:
                    # Run in a thread: waiting on the verifier inline would block the worker
                    # event loop for up to the verifier timeout, starving the liveness
                    # heartbeats above (and every other activity on this pod).
                    result = await asyncio.to_thread(
                        verifier_command.run,
                        state.verifier_argv,
                        working_directory=attestation.workspace_path,
                        workspace_root=attestation.workspace_root,
                        runner=self._sandboxed_verifier_runner(state, attestation.workspace_path),
                    )
                except CommandValidationError:
                    await self._fastapi_client.record_verifier_result(
                        request.work_record_id,
                        {
                            "actor": "temporal-worker",
                            "source": "ralph.verifier_command_validation",
                            "accepted": False,
                            "payload": _invalid_verifier_command_payload(request),
                        },
                    )
                    return VerifierRunOutput(
                        work_record_id=request.work_record_id,
                        command=request.command,
                        passed=False,
                        summary="invalid verifier command",
                        terminal=True,
                    )
                finally:
                    # In `finally` so the pair balances on the invalid-command return
                    # above too: `pre_verify` has already run by then. Recorded, never
                    # consulted -- `post_verify` is after `pre_runtime` in the catalogue,
                    # so its exit code changes nothing here. The Verifier is a Gate input
                    # and a hook may wrap it, never overturn it (map ticket 26 §1).
                    await self._verify_hook(
                        HookName.POST_VERIFY,
                        work_record_id=request.work_record_id,
                        state=state,
                        workspace_path=attestation.workspace_path,
                        sandbox=verifier_sandbox,
                    )
                evidence = _verifier_evidence_payload(result)
                if not result.passed:
                    # Non-terminal (ADR-0007): a failing verify feeds the iterate-to-green
                    # loop — a fix Directive reads this and re-verifies. The terminal Incident
                    # is driven only when the workflow exhausts its Directive budget, via
                    # ``create_failed_verification_incident``.
                    await self._fastapi_client.append_evidence(
                        request.work_record_id,
                        source="ralph.verifier",
                        payload={"accepted": False, **evidence},
                    )
                return VerifierRunOutput(
                    work_record_id=request.work_record_id,
                    command=request.command,
                    passed=result.passed,
                    summary=_verifier_summary(result),
                    failure_output="" if result.passed else _verifier_failure_output(result),
                )
            return VerifierRunOutput(
                work_record_id=request.work_record_id,
                command=request.command,
                passed=False,
                summary="verifier unavailable: runtime dependencies missing",
                terminal=True,
            )

    @activity.defn(name="execute_fix_directive")
    async def execute_fix_directive(self, request: FixDirectiveInput) -> FixDirectiveOutput:
        """Execute one fix Directive: re-run the agent on the existing work branch to
        repair a verifier failure, then re-push. Exactly one runtime turn (ADR-0007)."""
        await self._assert_routed(request)
        try:
            async with self._fenced(request.work_record_id):
                return await self._run_fix_directive(request)
        except HookRefusedError as refusal:
            # Same posture as the first Directive: a hook refusal ends the loop
            # attributably instead of raising into an endless retry (PRD issue 45).
            return FixDirectiveOutput(
                work_record_id=request.work_record_id,
                repository=request.repository,
                pr_number=request.pr_number,
                directive_number=request.directive_number,
                branch_head_sha="",
                summary=f"refused by the {refusal.run.name.value} Runner Hook",
                guard_mode_refused=True,
                workspace_path=request.workspace_path,
            )

    async def _run_fix_directive(self, request: FixDirectiveInput) -> FixDirectiveOutput:
        async def prompt(state: _RuntimeContextState, notes: list[str]) -> str:
            return _fix_directive_prompt(
                request.verifier_summary,
                state.completion_criteria,
                verifier_output=request.verifier_output,
                persona_preamble=_persona_prompt_preamble(
                    persona_slug=state.persona_slug,
                    instructions=state.persona_instructions,
                    notes=notes,
                    experience=state.experience,
                ),
            )

        return await self._run_in_place_directive(
            request,
            _InPlaceDirective(
                lifecycle_source="ralph.execute_fix_directive",
                run_source="ralph.fix_directive.codex_run",
                push_source="ralph.fix_directive.git_commit_push",
                commit_message=(
                    f"fix(task): directive {request.directive_number} "
                    f"for work record {request.work_record_id}"
                ),
                push_summary=(
                    f"Push fix Directive {request.directive_number} for "
                    f"{request.verifier_summary or 'a failing verifier'} to "
                    f"{request.repository}."
                ),
                prompt=prompt,
            ),
        )

    @activity.defn(name="execute_member_directive")
    async def execute_member_directive(
        self, request: MemberDirectiveInput
    ) -> MemberDirectiveOutput:
        """One Directive for a woken Swarm member (PRD issue 53, ADR-0012 §1).

        The member's pending Messages are read from the store under its own
        `channel.read`, folded into the prompt beside the task, and marked consumed once
        the turn ran; whatever it changed is pushed under its own `push` to the Lead's
        branch. Every verb inside is attributed to this member, never to the Lead: the
        runtime state below is *its* context, model, Grant and attempt socket.
        """

        await self._assert_routed(request)
        consumed: list[MessageEnvelope] = []
        contract: list[str] = []

        async def prompt(state: _RuntimeContextState, notes: list[str]) -> str:
            contract.append(state.contract_id or "")
            consumed.extend(await self._pending_messages(request, state))
            question = (
                await self._fastapi_client.get_question(request.question_id)
                if request.question_id
                else None
            )
            return _member_directive_prompt(
                role=request.role,
                wake_reason=request.wake_reason,
                pending=consumed,
                completion_criteria=state.completion_criteria,
                persona_preamble=_persona_prompt_preamble(
                    persona_slug=state.persona_slug,
                    instructions=state.persona_instructions,
                    notes=notes,
                    experience=state.experience,
                ),
                board=request.board,
                question=question,
                report=state.kind == KIND_REPORT,
            )

        try:
            async with self._fenced(request.work_record_id):
                ran = await self._run_in_place_directive(
                    request,
                    _InPlaceDirective(
                        lifecycle_source="ralph.execute_member_directive",
                        run_source="ralph.member_directive.codex_run",
                        push_source="ralph.member_directive.git_commit_push",
                        commit_message=(
                            f"feat(task): directive {request.directive_number} by Agent "
                            f"{request.agent_id} for work record {request.work_record_id}"
                        ),
                        push_summary=(
                            f"Push Directive {request.directive_number} ({request.role or 'member'}"
                            f", woken by {request.wake_reason or 'pending messages'}) to "
                            f"{request.repository}."
                        ),
                        prompt=prompt,
                        push_when_clean=False,
                    ),
                )
        except HookRefusedError as refusal:
            return MemberDirectiveOutput(
                work_record_id=request.work_record_id,
                repository=request.repository,
                pr_number=request.pr_number,
                directive_number=request.directive_number,
                branch_head_sha="",
                summary=f"refused by the {refusal.run.name.value} Runner Hook",
                guard_mode_refused=True,
                workspace_path=request.workspace_path,
            )
        if consumed and self._message_store is not None and not ran.guard_mode_refused:
            self._message_store.mark_consumed(
                contract_id=contract[0],
                work_record_id=request.work_record_id,
                agent_id=request.agent_id,
                through=consumed[-1],
            )
        await self._fastapi_client.append_evidence(
            request.work_record_id,
            source=_SWARM_DIRECTIVE_SOURCE,
            payload={
                "event": "swarm.directive",
                "directive_number": request.directive_number,
                "agent_id": request.agent_id,
                "role": request.role,
                "wake_reason": request.wake_reason,
                # PRD issue 60: the Question this wake carried the answer to, by id.
                "question_id": request.question_id or None,
                "messages_consumed": len(consumed),
                "pushed": bool(ran.branch_head_sha),
            },
        )
        return MemberDirectiveOutput(
            **{item.name: getattr(ran, item.name) for item in fields(ran)},
            messages_consumed=len(consumed),
            pushed=bool(ran.branch_head_sha),
        )

    @activity.defn(name="execute_learning_directive")
    async def execute_learning_directive(
        self, request: LearningDirectiveInput
    ) -> LearningDirectiveOutput:
        """The terminal Learning Directive (PRD issue 55, ADR-0012), as the Learner.

        Runs as the Contract's Learner Agent -- its runtime context, its attribution on
        every Usage Record and Evidence Event -- in the Work Record's own Workspace and
        as the Contract's uid (ADR-0015), so it reads that Workspace and the Work Record's
        Conversation transcript from this Runner's store and never another Contract's.
        Nothing is pushed and no verb is evaluated: the Learner proposes, never acts.

        The proxy caps the attempt at the Learning reserve (map 12 B6). A Directive that
        spent the whole reserve -- or a device-login one the proxy never saw, caught here
        from its own usage -- proposes nothing, and says so, never as an Incident.
        """

        await self._assert_routed(request)
        children_bytes = len(learner_children_json(request.children_evidence))
        if children_bytes > LEARNER_CHILDREN_BUDGET_BYTES:
            # The platform fits the children to this budget (issue 73); a section over it
            # is a contract violation, refused rather than cut into half-children.
            raise ApplicationError(
                f"children section is {children_bytes} bytes, over the "
                f"{LEARNER_CHILDREN_BUDGET_BYTES}-byte Learner children budget",
                non_retryable=True,
            )
        if not self._agent_runtimes or self._workspace_root is None:
            return self._learning_output(
                request,
                outcome=LEARNING_FAILED,
                summary="directive unavailable: runtime dependencies missing",
            )
        try:
            async with self._fenced(request.work_record_id):
                return await self._run_learning_directive(request)
        except HookRefusedError as refusal:
            return self._learning_output(
                request,
                outcome=LEARNING_FAILED,
                summary=f"refused by the {refusal.run.name.value} Runner Hook",
            )

    async def _run_learning_directive(
        self, request: LearningDirectiveInput
    ) -> LearningDirectiveOutput:
        async with _liveness_heartbeats(), contextlib.AsyncExitStack() as attempt_stack:
            state = await self._runtime_state(
                request.work_record_id, agent_id=request.learner_agent_id
            )
            agent_runtime = await self._agent_runtime_for(request.work_record_id, state)
            workspace = self._prepare_workspace(
                workspace_root=self._require_workspace_root(),
                contract_id=state.contract_id,
                work_record_id=request.work_record_id,
            )
            output_path = workspace / _LESSONS_FILE
            output_path.unlink(missing_ok=True)
            # The Learning Directive is terminal: there is no owner step for a `confirm`
            # server to wait on, so it runs without one rather than holding the ending.
            learning_plan = await self._plan_mcp(request.work_record_id, state, ask_owner=False)
            assert isinstance(learning_plan, McpPlan)
            attempt = await attempt_stack.enter_async_context(
                self._directive_attempt(
                    work_record_id=request.work_record_id,
                    state=state,
                    workspace_path=workspace,
                    directive_number=request.directive_number,
                    sandbox=self._contract_sandbox(state),
                    reserve_max_tokens=request.reserve_max_tokens or None,
                    mcp_plan=learning_plan,
                )
            )
            await attempt.hooks.phase(HookName.PRE_DIRECTIVE)
            await attempt.run_environment_hook()
            await attempt.hooks.phase(HookName.PRE_RUNTIME)
            result = await agent_runtime.execute_directive(
                DirectiveRequest(
                    sandbox=self._directive_sandbox(state, workspace),
                    workspace_path=workspace,
                    extra_env=attempt.env,
                    mcp_servers=attempt.mcp_servers,
                    egress_allow_list=attempt.egress_allow_list,
                    prompt=attempt.skills_preamble
                    + _learning_prompt(
                        request,
                        base_branch=state.base_branch or request.base_ref,
                        transcript=self._learner_transcript(state, request.work_record_id),
                    ),
                    base_branch=state.base_branch or request.base_ref,
                    work_branch=state.work_branch,
                )
            )
            await attempt.hooks.phase(HookName.POST_RUNTIME)
            lessons = _read_lessons(output_path)
            output_path.unlink(missing_ok=True)
            await self._report_harness_usage(
                runtime_state=state,
                work_record_id=request.work_record_id,
                directive_number=request.directive_number,
                result=result,
            )
            usage = await self._directive_usage(
                work_record_id=request.work_record_id,
                directive_number=request.directive_number,
            )
            # Ids and figures only: the Lessons' text reaches the control plane once, on
            # the outcome activity, and never through an Evidence payload.
            await self._fastapi_client.append_evidence(
                request.work_record_id,
                source=_LEARNING_RUN_SOURCE,
                payload={
                    "directive_number": request.directive_number,
                    "agent_id": state.agent_id,
                    "contract_id": state.contract_id,
                    "exit_code": result.exit_code,
                    "command_hash": result.command_hash,
                    "lessons_drafted": len(lessons),
                    "tokens": usage.tokens,
                    "reserve_max_tokens": request.reserve_max_tokens,
                },
            )
        if 0 < request.reserve_max_tokens <= usage.tokens:
            return self._learning_output(
                request,
                usage=usage,
                outcome=LEARNING_RESERVE_EXHAUSTED,
                summary=(
                    f"the Learning reserve is spent: {usage.tokens} of "
                    f"{request.reserve_max_tokens} tokens"
                ),
            )
        if result.exit_code != 0:
            return self._learning_output(
                request,
                usage=usage,
                outcome=LEARNING_FAILED,
                summary=f"the Learner's runtime exited {result.exit_code}",
            )
        return self._learning_output(
            request,
            lessons=lessons,
            usage=usage,
            summary=f"{len(lessons)} Lesson(s) proposed",
        )

    def _learning_output(
        self,
        request: LearningDirectiveInput,
        *,
        lessons: list[ProposedLesson] | None = None,
        usage: DirectiveUsage | None = None,
        outcome: str = LEARNING_PROPOSED,
        summary: str = "",
    ) -> LearningDirectiveOutput:
        return LearningDirectiveOutput(
            work_record_id=request.work_record_id,
            directive_number=request.directive_number,
            lessons=lessons or [],
            usage=usage or DirectiveUsage(),
            outcome=outcome,
            summary=summary,
        )

    def _learner_transcript(self, state: _RuntimeContextState, work_record_id: str) -> str:
        """The Work Record's Conversation, read here and never shipped (PRD issue 52).

        Read under the Learner's own Contract id -- the one its runtime context resolved
        -- so the store it opens is that Contract's and no other. A sealed store reads as
        no transcript: sealing outranks learning.
        """

        if self._message_store is None or not state.contract_id:
            return ""
        try:
            envelopes = self._message_store.read(
                contract_id=state.contract_id, work_record_id=work_record_id
            )
        except PermissionError:
            return ""
        return "\n\n".join(
            f"[{envelope.kind.value} from Agent {envelope.sender_agent_id}]\n{envelope.body}"
            for envelope in envelopes
        )

    async def _pending_messages(
        self, request: MemberDirectiveInput, state: _RuntimeContextState
    ) -> list[MessageEnvelope]:
        """The woken member's unread Messages, under its own `channel.read` (issue 52)."""

        if not request.channel_id or self._message_store is None:
            return []
        decision = await self._evaluate_channel_verb(
            request.work_record_id, state, verb=CHANNEL_READ_VERB, channel_id=request.channel_id
        )
        store, _ = self._open_store(state)
        if not decision.allowed or store is None:
            return []
        return store.pending(
            contract_id=state.contract_id or "",
            work_record_id=request.work_record_id,
            channel_id=request.channel_id,
            agent_id=request.agent_id,
            roles=request.roles or ((request.role,) if request.role else ()),
        )

    async def _run_in_place_directive(
        self,
        request: FixDirectiveInput | MemberDirectiveInput,
        kind: _InPlaceDirective,
    ) -> FixDirectiveOutput:
        asked: list[QuestionAsked] = []
        return _with_question(await self._run_in_place_turn(request, kind, asked), asked)

    async def _run_in_place_turn(
        self,
        request: FixDirectiveInput | MemberDirectiveInput,
        kind: _InPlaceDirective,
        asked: list[QuestionAsked],
    ) -> FixDirectiveOutput:
        """One runtime turn in the Workspace the first Directive cloned, then push.

        Shared by the fix Directive and the member Directive (PRD issue 53): both iterate
        in place (ADR-0007), both re-read their Agent's snapshot at the boundary
        (ADR-0011 §11) and both push under that Agent's own `push`.
        """

        async with (
            _liveness_heartbeats() as liveness,
            contextlib.AsyncExitStack() as attempt_stack,
        ):
            if (
                not self._agent_runtimes
                or self._git_workspace is None
                or self._workspace_root is None
            ):
                return FixDirectiveOutput(
                    work_record_id=request.work_record_id,
                    repository=request.repository,
                    pr_number=request.pr_number,
                    directive_number=request.directive_number,
                    branch_head_sha="",
                    summary="directive unavailable: runtime dependencies missing",
                )
            directive_started = self._monotonic() - liveness.earlier_attempts_seconds
            # Loop back into the working state for another runtime turn.
            await self._advance_work_record_lifecycle(
                request.work_record_id,
                to_state="IN_PROGRESS",
                source=kind.lifecycle_source,
                details={
                    "repository": request.repository,
                    "branch_name": request.branch_name,
                    "directive_number": request.directive_number,
                },
            )
            # A Directive boundary: re-read the snapshot so a narrowing made between
            # Directives bites at this Directive's first verb (ADR-0011 §11). The
            # snapshot on the input is what a link-less host has (until issue 46 that is
            # every host in production), so it must be passed on or the Directive falls
            # back to UNENFORCED_SNAPSHOT and runs unattenuated.
            state = await self._runtime_state(
                request.work_record_id,
                workspace_path=request.workspace_path,
                grant_snapshot=request.grant_snapshot,
                agent_id=request.agent_id,
            )
            agent_runtime = await self._agent_runtime_for(request.work_record_id, state)
            if state.workspace_path is None or not state.workspace_path.is_dir():
                # Rehydration recovers metadata after a worker restart, but the clone
                # itself must still exist on disk for an in-place Directive.
                raise RuntimeError("fix Directive requires an existing runtime workspace")
            mcp_plan = await self._plan_mcp(
                request.work_record_id,
                state,
                consented=request.owner_confirmed_verbs,
                declined=request.owner_declined_verbs,
            )
            if isinstance(mcp_plan, OwnerConfirmationPending):
                return FixDirectiveOutput(
                    work_record_id=request.work_record_id,
                    repository=request.repository,
                    pr_number=request.pr_number,
                    directive_number=request.directive_number,
                    branch_head_sha="",
                    summary="",
                    owner_confirmation=mcp_plan,
                    workspace_path=str(state.workspace_path),
                    grant_snapshot=state.grant_snapshot_payload,
                )
            attempt = await attempt_stack.enter_async_context(
                self._directive_attempt(
                    work_record_id=request.work_record_id,
                    state=state,
                    workspace_path=state.workspace_path,
                    directive_number=request.directive_number,
                    sandbox=self._contract_sandbox(state),
                    mcp_plan=mcp_plan,
                    asked=asked,
                )
            )
            # No checkout phase here: an in-place Directive runs in the Workspace the
            # first one cloned, which is the whole point of iterating in place (ADR-0007).
            await attempt.hooks.phase(HookName.PRE_DIRECTIVE)
            await attempt.run_environment_hook()
            await self._record_experience_injected(
                request.work_record_id,
                directive_number=request.directive_number,
                state=state,
            )
            prompt_notes: list[str] = []
            await attempt.hooks.phase(HookName.PRE_RUNTIME)
            sandbox = self._directive_sandbox(state, state.workspace_path)
            resume_session_id, on_session_started = await self._harness_session(
                liveness,
                work_record_id=request.work_record_id,
                directive_number=request.directive_number,
                agent_runtime=agent_runtime,
                contract_id=state.contract_id,
                workspace_path=state.workspace_path,
                sandbox=sandbox,
            )
            codex_result = await agent_runtime.execute_directive(
                DirectiveRequest(
                    sandbox=sandbox,
                    workspace_path=state.workspace_path,
                    resume_session_id=resume_session_id,
                    on_session_started=on_session_started,
                    extra_env=attempt.env,
                    mcp_servers=attempt.mcp_servers,
                    egress_allow_list=attempt.egress_allow_list,
                    prompt=attempt.skills_preamble + await kind.prompt(state, prompt_notes),
                    base_branch=request.base_ref,
                    work_branch=state.work_branch,
                )
            )
            await attempt.hooks.phase(HookName.POST_RUNTIME)
            await self._fastapi_client.append_evidence(
                request.work_record_id,
                source=kind.run_source,
                payload={
                    "directive_number": request.directive_number,
                    "agent_id": state.agent_id,
                    **_codex_evidence_payload(
                        codex_result,
                        persona_slug=state.persona_slug,
                        persona_instructions=state.persona_instructions,
                        prompt_notes=prompt_notes,
                    ),
                },
            )
            await self._report_harness_usage(
                runtime_state=state,
                work_record_id=request.work_record_id,
                directive_number=request.directive_number,
                result=codex_result,
            )
            # Taken before the commit below so it never lands on the branch (PRD 59).
            plan = _take_plan(state.workspace_path)
            if codex_result.exit_code != 0:
                if _is_guard_mode_refusal(codex_result):
                    return FixDirectiveOutput(
                        work_record_id=request.work_record_id,
                        repository=request.repository,
                        pr_number=request.pr_number,
                        directive_number=request.directive_number,
                        branch_head_sha="",
                        summary="codex guard-mode refusal",
                        duration_seconds=self._monotonic() - directive_started,
                        guard_mode_refused=True,
                        usage=await self._directive_usage(
                            work_record_id=request.work_record_id,
                            directive_number=request.directive_number,
                        ),
                        workspace_path=str(state.workspace_path),
                        grant_snapshot=state.grant_snapshot_payload,
                    )
                raise RuntimeError("Codex CLI fix directive failed")
            # A report's directory is no checkout (console-v2 issue 23): what the Agent
            # wrote there stays there, and its report left as a Message.
            git_evidence = (
                None
                if state.kind == KIND_REPORT
                else self._git_workspace.collect_git_evidence(
                    state.workspace_path,
                    output_limit_bytes=_GIT_EVIDENCE_LIMIT_BYTES,
                )
            )
            if git_evidence is None or (
                not kind.push_when_clean and not git_evidence.status.strip()
            ):
                # The member only spoke (its Messages already left over the attempt
                # socket): nothing to commit, nothing to push, one Directive spent.
                return FixDirectiveOutput(
                    work_record_id=request.work_record_id,
                    repository=request.repository,
                    pr_number=request.pr_number,
                    directive_number=request.directive_number,
                    branch_head_sha="",
                    summary=f"directive {request.directive_number} changed nothing",
                    duration_seconds=self._monotonic() - directive_started,
                    usage=await self._directive_usage(
                        work_record_id=request.work_record_id,
                        directive_number=request.directive_number,
                    ),
                    workspace_path=str(state.workspace_path),
                    grant_snapshot=state.grant_snapshot_payload,
                    plan=plan,
                )
            commit = self._git_workspace.commit_all(
                CommitAllRequest(
                    repo_full_name=request.repository,
                    workspace_path=state.workspace_path,
                    work_branch=state.work_branch,
                    commit_message=kind.commit_message,
                )
            )
            fix_changed_paths = _changed_paths_from_git_status(git_evidence.status)
            push_verdict = await self._authorize(
                request.work_record_id,
                state,
                verb=PUSH_VERB,
                repository=request.repository,
                owner_confirmed_verbs=request.owner_confirmed_verbs,
                target=state.work_branch,
                summary=kind.push_summary,
                file_list=fix_changed_paths,
            )
            if not push_verdict.allowed:
                return FixDirectiveOutput(
                    work_record_id=request.work_record_id,
                    repository=request.repository,
                    pr_number=request.pr_number,
                    directive_number=request.directive_number,
                    branch_head_sha="",
                    summary=(push_verdict.refusal_summary if push_verdict.refused else ""),
                    duration_seconds=self._monotonic() - directive_started,
                    grant_refused=push_verdict.refused,
                    grant_refusal_reason=(
                        push_verdict.refusal_summary if push_verdict.refused else ""
                    ),
                    owner_confirmation=push_verdict.pending,
                    usage=await self._directive_usage(
                        work_record_id=request.work_record_id,
                        directive_number=request.directive_number,
                    ),
                    workspace_path=str(state.workspace_path),
                    grant_snapshot=state.grant_snapshot_payload,
                    plan=plan,
                )
            await attempt.hooks.phase(HookName.PRE_ARTIFACT)
            push = self._git_workspace.push_branch(
                PushBranchRequest(
                    repo_full_name=request.repository,
                    workspace_path=state.workspace_path,
                    base_branch=request.base_ref,
                    work_branch=state.work_branch,
                )
            )
            await attempt.hooks.phase(HookName.POST_ARTIFACT)
            await self._fastapi_client.append_evidence(
                request.work_record_id,
                source=kind.push_source,
                payload={
                    "directive_number": request.directive_number,
                    "agent_id": state.agent_id,
                    "repository": request.repository,
                    "branch_name": state.work_branch,
                    "status": git_evidence.status,
                    "diff": git_evidence.diff,
                    "stderr": git_evidence.stderr,
                    "commit_id": commit.commit_id,
                    "push_commit_id": push.commit_id,
                    "refspec": push.refspec,
                    "pushed": True,
                },
            )
            return FixDirectiveOutput(
                work_record_id=request.work_record_id,
                repository=request.repository,
                pr_number=request.pr_number,
                directive_number=request.directive_number,
                branch_head_sha=push.commit_id,
                summary=f"directive {request.directive_number} pushed {push.commit_id}",
                duration_seconds=self._monotonic() - directive_started,
                usage=await self._directive_usage(
                    work_record_id=request.work_record_id,
                    directive_number=request.directive_number,
                ),
                # This Directive's boundary refreshed the snapshot (ADR-0011 §11); it and
                # the workspace leave on the output for the next step (ADR-0013 §3).
                workspace_path=str(state.workspace_path),
                grant_snapshot=state.grant_snapshot_payload,
                plan=plan,
            )

    async def _directive_usage(
        self,
        *,
        work_record_id: str,
        directive_number: int,
    ) -> DirectiveUsage:
        """This Directive's metered spend, read once at the end of its turn.

        The ledger read happens **here**, in the activity, so the deterministic loop only
        ever sees it as an activity result recorded in history: a workflow that queried
        the ledger itself would replay differently every time (ADR-0007, map 12 B2).

        Metering must never be the reason a Directive fails, so a control-plane error
        degrades to a zero aggregate — the Budget then under-counts this Directive rather
        than the Work Record ending on a usage lookup.
        """

        try:
            payload = await self._fastapi_client.get_directive_usage(
                work_record_id,
                directive_id(work_record_id=work_record_id, directive_number=directive_number),
            )
        except Exception:  # noqa: BLE001 - see docstring: metering never fails a Directive
            return DirectiveUsage()
        return DirectiveUsage(
            tokens=int(payload.get("tokens", 0) or 0),
            calls=int(payload.get("calls", 0) or 0),
            estimate_usd=float(payload.get("estimate_usd", 0.0) or 0.0),
        )

    async def _record_experience_injected(
        self, work_record_id: str, *, directive_number: int, state: _RuntimeContextState
    ) -> None:
        """``experience_injected`` on the Directive (PRD issue 56): the Lesson ids the
        prompt carries and whether the budget dropped any -- never their text. A
        Directive with no Experience records nothing, as before."""

        if not state.experience:
            return
        await self._fastapi_client.append_evidence(
            work_record_id,
            source=_EXPERIENCE_EVIDENCE_SOURCE,
            payload={
                "event": "experience_injected",
                "directive_number": directive_number,
                "agent_id": state.agent_id,
                "lesson_ids": list(state.experience_lesson_ids),
                "truncated": state.experience_truncated,
            },
        )

    async def _report_harness_usage(
        self,
        *,
        runtime_state: _RuntimeContextState,
        work_record_id: str,
        directive_number: int,
        result: DirectiveResult,
    ) -> None:
        """A device-login/setup-token Directive bypasses the LLM proxy, so this is the
        only metering the Runner can produce for it (PRD issue 31, 17 A9). A no-op for an
        ``api_key`` runtime -- the proxy already metered that call at request time -- and
        never the reason a Directive fails (same posture as :meth:`_directive_usage`).
        """

        if self._auth_model(runtime_state) == AuthModel.API_KEY:
            return
        directive = directive_id(work_record_id=work_record_id, directive_number=directive_number)
        if runtime_state.cli_kind == "claude_code":
            usage_event = extract_claude_result(result.stdout)
            model_name = None
        else:
            usage_event = extract_codex_turn_usage(result.stdout)
            model_name = runtime_state.model or None
        if not usage_event:
            return
        if runtime_state.cli_kind != "claude_code" and model_name is None:
            # The backend 422s a Codex harness report with no model (`report_harness_usage`
            # requires one to price it), and the `suppress` below swallows that -- an Agent
            # with no model configured would otherwise lose this Usage Record with no trace.
            activity.logger.warning(
                "harness usage unreported: Agent %s has no model configured for a Codex "
                "harness Directive (work record %s)",
                runtime_state.agent_id,
                work_record_id,
            )
        with contextlib.suppress(Exception):  # noqa: BLE001 - metering never fails a Directive
            await self._fastapi_client.report_harness_usage(
                {
                    "work_record_id": work_record_id,
                    "agent_id": runtime_state.agent_id,
                    "contract_id": runtime_state.contract_id,
                    "directive_id": directive,
                    "runner_id": None,
                    "runtime_kind": runtime_state.cli_kind,
                    "provider_name": runtime_state.cli_kind,
                    "model_name": model_name,
                    "usage_event": usage_event,
                }
            )

    @activity.defn(name="wipe_contract_residue")
    async def wipe_contract_residue(self, request: ContractResidueInput) -> ContractResidueOutput:
        """Delete everything one Contract left on this Runner and retire its uid (17 A6).

        Its Workspaces, its harness config roots and — once the Message store lands
        (issue 52) — its Message store entries: one directory tree, one `rmtree`, one
        uid returned to the range. Idempotent, because the drain that calls it is
        retryable and a second termination of the same Contract must not fail.

        Registered as an activity of its own, not only reachable through issue 12's
        drain, so an operator (and M3's reassignment-after-wipe-and-ack, ADR-0015 §5) can
        call it directly for a Contract this Runner still holds residue for.
        """

        await self._assert_routed(request)
        isolation = self._contract_isolation
        segment = contract_path_segment(request.contract_id or None)
        held = (
            []
            if isolation is None
            else [
                work_record_id
                for contract_id, work_record_id in isolation.held_work_record_ids()
                if contract_id == segment
            ]
        )
        residue = await self._wipe_contract_residue(
            request.contract_id, contract_state="terminated"
        )
        if self._message_store is not None and request.contract_id:
            # Sealed, not wiped (PRD issue 52, map ticket 10): the org retains the
            # transcripts until retention, the user loses access.
            residue = replace(
                residue,
                message_stores_sealed=self._message_store.seal(
                    request.contract_id, contract_state="terminated"
                ),
            )
        # The drain (issue 12) records the same counts on the ENDED transition it is
        # already writing; called on its own there is no transition, so each Work Record
        # whose Workspace went with the Contract carries the wipe in its own trail.
        for work_record_id in held if residue.workspaces_wiped else []:
            await self._fastapi_client.append_evidence(
                work_record_id,
                source=_CONTRACT_WIPE_SOURCE,
                payload={
                    "contract_id": residue.contract_id,
                    "workspaces_wiped": residue.workspaces_wiped,
                    "harness_roots_wiped": residue.harness_roots_wiped,
                    "uid_retired": residue.uid_retired,
                    "message_stores_sealed": residue.message_stores_sealed,
                },
            )
        return residue

    @activity.defn(name="reassemble_workspace")
    async def reassemble_workspace(
        self,
        request: WorkspaceReassemblyInput,
    ) -> WorkspaceReassemblyOutput:
        """Re-assemble a re-routed Work Record's Workspace from its branch (map 25 §8).

        The Work Record was pinned to a Runner that went stale or was revoked, so the
        next Directive routed elsewhere and the checkout it was carrying is on a machine
        this process cannot see. The **branch** is what survives -- it is on the remote --
        which is why it is this activity's input and not a Runner-local cache (ADR-0013
        §3; PRD issue 36 retired ``_runtime_contexts``).

        ``checkout_work_branch`` prefers ``origin/<work branch>`` when it exists, so the
        re-assembled tree is the work as it was last pushed, not a fresh branch off base.

        A report has no branch (console-v2 issue 23): its Workspace is the empty
        directory alone, on its first Directive and after any re-route alike.
        """

        await self._assert_routed(request)
        if self._git_workspace is None or self._workspace_root is None:
            raise RuntimeError("git workspace and workspace_root are required to re-assemble")
        workspace_root = self._require_workspace_root()
        state = await self._runtime_state(request.work_record_id)
        contract_id = state.contract_id
        await self._record_uid_allocation(request.work_record_id, contract_id)
        workspace_path = self._prepare_workspace(
            workspace_root=workspace_root,
            contract_id=contract_id,
            work_record_id=request.work_record_id,
        )
        if state.kind == KIND_REPORT:
            await self._fastapi_client.append_evidence(
                request.work_record_id,
                source=_ROUTING_EVIDENCE_SOURCE,
                actor=f"runner:{request.routing.runner_id if request.routing else ''}",
                payload={"event": "workspace.prepared", "workspace_path": str(workspace_path)},
            )
            return WorkspaceReassemblyOutput(
                work_record_id=request.work_record_id,
                workspace_path=str(workspace_path),
                branch_name="",
            )
        remote_url = _github_remote_url(request.repository)
        self._git_workspace.clone_repository(
            CloneWorkspaceRequest(
                repo_full_name=request.repository,
                remote_url=remote_url,
                workspace_root=workspace_root,
                workspace_path=workspace_path,
            )
        )
        self._git_workspace.fetch_base_branch(
            FetchBranchRequest(
                repo_full_name=request.repository,
                workspace_path=workspace_path,
                base_branch=request.base_ref,
            )
        )
        checked_out = self._git_workspace.checkout_work_branch(
            CheckoutWorkBranchRequest(
                repo_full_name=request.repository,
                workspace_path=workspace_path,
                base_branch=request.base_ref,
                work_branch=request.branch_name,
            )
        )
        await self._fastapi_client.append_evidence(
            request.work_record_id,
            source=_ROUTING_EVIDENCE_SOURCE,
            actor=f"runner:{request.routing.runner_id if request.routing else ''}",
            payload={
                "event": "workspace.reassembled",
                "repository": request.repository,
                "branch": checked_out.work_branch,
                "workspace_path": str(checked_out.workspace_path),
            },
        )
        return WorkspaceReassemblyOutput(
            work_record_id=request.work_record_id,
            workspace_path=str(checked_out.workspace_path),
            branch_name=checked_out.work_branch,
        )

    @activity.defn(name="delete_expired_workspaces")
    async def delete_expired_workspaces(self) -> WorkspaceRetentionOutput:
        """Delete the Workspaces of terminal Work Records past their retention (ADR-0015 §2).

        One retention period, the Organisation's own (map ticket 15) — there is no second
        one for checkouts. The Runner knows only what is on its disk; which of those Work
        Records are terminal and how old they are is a control-plane fact, so the ids go
        up and the expired subset comes back. A Work Record still running is never in it.
        """

        isolation = self._contract_isolation
        held = [] if isolation is None else isolation.held_work_record_ids()
        # PRD issue 52: Message stores outlive their Workspace (and a wiped Contract's
        # tree) but not the Organisation's one retention period.
        stores = [] if self._message_store is None else self._message_store.held()
        if not held and not stores:
            return WorkspaceRetentionOutput(held=0, deleted=0)
        candidates = sorted({work_record_id for _contract_id, work_record_id in held + stores})
        expired: set[str] = set()
        for start in range(0, len(candidates), _EXPIRED_WORKSPACE_BATCH):
            expired.update(
                await self._fastapi_client.list_expired_workspaces(
                    candidates[start : start + _EXPIRED_WORKSPACE_BATCH]
                )
            )
        deleted = 0
        for contract_id, work_record_id in held:
            if (
                isolation is not None
                and work_record_id in expired
                and isolation.remove_workspace(contract_id, work_record_id)
            ):
                deleted += 1
                await self._fastapi_client.append_evidence(
                    work_record_id,
                    source=_WORKSPACE_RETENTION_SOURCE,
                    payload={"contract_id": contract_id, "workspaces_deleted": 1},
                )
        stores_deleted = 0
        for contract_id, work_record_id in stores:
            if (
                self._message_store is not None
                and work_record_id in expired
                and self._message_store.remove(contract_id, work_record_id)
            ):
                stores_deleted += 1
                await self._fastapi_client.append_evidence(
                    work_record_id,
                    source=_WORKSPACE_RETENTION_SOURCE,
                    payload={"contract_id": contract_id, "message_stores_deleted": 1},
                )
        return WorkspaceRetentionOutput(
            held=len(held), deleted=deleted, message_stores_deleted=stores_deleted
        )

    async def _wipe_contract_residue(
        self,
        contract_id: str,
        *,
        contract_state: str,
    ) -> ContractResidueOutput:
        return wipe_contract_residue(
            self._contract_isolation, contract_id, contract_state=contract_state
        )
