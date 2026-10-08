"""Workflow-safe activity contracts for the Ralph Engineer PR loop.

These DTOs and the ``RalphActivities`` Protocol form the boundary that the
``@workflow.defn`` module (the platform's ``workflows.ralph``) imports. They deliberately
depend on nothing but the standard library so the workflow import graph never reaches
non-deterministic integration code (e.g. ``httpx`` via ``integrations/github/auth``).
Temporal re-imports the workflow module inside its determinism sandbox; a transitive
import of a restricted module there makes ``RalphWorkflow`` fail validation and crashes
the worker on startup.

They are also the wire between the platform and an installable Runner (ADR-0013 §4),
which is why this package — and not the platform — owns them, and why the distribution
version is the Runner compatibility floor (§7).

The implementations live in two places under the locality rule (ADR-0013 §2): the
Runner's in ``agentic_runner.activities``, the platform's in
``workflows.activities``, classified by its ``workflows.activity_registry``.

**Nothing an activity learns is cached in the Runner's process** (ADR-0013 §3, PRD issue
36 retired ``_runtime_contexts``): the Grant snapshot a Directive boundary refreshed, the
workspace the clone produced and the branch head a push produced all leave on an output
and come back on the next input. That is what lets a Work Record re-assemble on another
Runner, and what makes the loop's state visible in Temporal history rather than in a
dictionary on a pod.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from agentic_runner_contracts.epics import EpicBoard, EpicPlan
from agentic_runner_contracts.routing import DirectiveRouting
from agentic_runner_contracts.swarm import SwarmSnapshot


@dataclass(frozen=True)
class Budget:
    """The ceiling on resources a Work Record may consume across its Ralph Loop.

    Bounds runaway cost/iterations and is a precondition for autonomy (ADR-0007).
    ``max_directives`` and ``max_wall_clock_seconds`` are enforced by the loop.

    ``max_tokens`` is the unit Release 1 meters in (map ticket 12 B1: tokens, not money —
    one number, prompt + completion summed, any model, cached input at full weight). It is
    decremented from each Directive's own reported aggregate, and **0 means no token
    ceiling** — the shape every Work Record has until intake or an owner sets one.

    Its exhaustion is the one dimension that does not fault: ``max_directives`` and
    ``max_wall_clock_seconds`` end the loop with an Incident, tokens **hold** and ask the
    owner to raise or end (map ticket 12 B5, amending ADR-0007 for this dimension alone).

    ``max_cost`` is gone (PRD issue 13): it was never enforced — there was no per-turn cost
    source — and money was replaced by tokens as the unit. Temporal's JSON converter builds
    a dataclass from the fields it declares, so a Budget pickled with ``max_cost`` in an
    in-flight history still deserialises; the extra key is ignored.
    """

    max_directives: int
    max_wall_clock_seconds: float
    max_tokens: int = 0


# Default per-Work-Record Budget applied when the control plane does not assign one.
# ``max_directives`` keeps the historical single-shot-plus-fixes ceiling. This is a
# pure constant so the ``@workflow.defn`` module stays import-clean for Temporal's
# determinism sandbox (issue 02); env-configurable defaults are resolved control-plane
# side (see ``config.AppSettings`` / ``services/engineer_flow``) and passed in.
DEFAULT_BUDGET = Budget(
    max_directives=5,
    max_wall_clock_seconds=1800.0,
    max_tokens=0,
)


@dataclass(frozen=True)
class DirectiveUsage:
    """One Directive's metered LLM spend, as the Runner reports it (map ticket 12 B2).

    Carried on the Directive's own output so the deterministic loop can decrement the
    token Budget from what the Directive reported and never from the ledger: a workflow
    that read the ledger would replay differently every time it was replayed.

    ``estimate_usd`` is display-only; ``tokens`` is what the Budget is in.
    """

    tokens: int = 0
    calls: int = 0
    estimate_usd: float = 0.0


@dataclass(frozen=True)
class ContextAssemblyInput:
    """Input for assembling deterministic Engineer PR context."""

    work_record_id: str
    repository: str
    base_ref: str
    reviewer: str | None


@dataclass(frozen=True)
class ContextAssemblyOutput:
    """Assembled branch, PR, and verifier context for the Ralph loop."""

    work_record_id: str
    repository: str
    base_ref: str
    branch_name: str
    pr_title: str
    pr_body: str
    verifier_command: str
    # The Work Record's Budget, resolved when context is assembled (ADR-0007: the Budget
    # is recorded on the Work Record). Defaults to the configurable ``DEFAULT_BUDGET``.
    budget: Budget = field(default_factory=lambda: DEFAULT_BUDGET)
    # The repository's Product rule (ADR-0011 s6), resolved from the same grant snapshot
    # the Runner evaluates verbs against. Carried through workflow state so the Reviewer
    # Gate knows how long to hold without a control-plane call of its own; the merge seam
    # re-reads it from its own snapshot and is the authority.
    required_human_approvals: int = 1
    # The Agent's Grant snapshot as the control plane served it (ADR-0011 §11). Carried
    # so the first Directive's seams evaluate the snapshot this assembly read, instead of
    # a Runner-local cache: it travels in activity I/O, and the raw payload is what
    # travels so the Runner parses exactly what the control plane wrote.
    grant_snapshot: dict[str, Any] = field(default_factory=dict)
    # PRD issue 53: the Swarm as its role-assignment Evidence folds (ADR-0012 §1) and
    # each member's own Grant snapshot, keyed by Agent id, so a Builder's Directive runs
    # under the Builder's Grant and never the Lead's. The empty snapshot is the degenerate
    # Swarm -- no Channel -- and every fixture that predates Channels reads as one.
    swarm: SwarmSnapshot = field(default_factory=SwarmSnapshot)
    member_grant_snapshots: dict[str, dict[str, Any]] = field(default_factory=dict)
    # PRD issue 59: ``epic`` runs the coordination loop instead of the PR loop;
    # ``parent_work_record_id`` names the Epic a child reports its PR and ending to;
    # ``stack_base_work_record_id`` the sibling a `based_on` child stacks on (its branch
    # is then ``base_ref`` above); ``coordination_budget`` the Epic's own Budget (12 B3),
    # wall-clock unbounded by default. Defaulted so every pre-Epic fixture reads as a
    # plain Work Record.
    kind: str = "work"
    parent_work_record_id: str = ""
    stack_base_work_record_id: str = ""
    coordination_budget: Budget | None = None


@dataclass(frozen=True)
class OwnerConfirmationPending:
    """A seam stopped to ask the Agent's owner, and the loop must wait (ADR-0011 s14).

    Neither an allow nor a refusal: the control plane holds the request, the workflow
    waits ``window_seconds`` for the owner's signal, and what a refusal or a timeout then
    does depends on ``verb`` (s15). ``window_seconds`` is what is *left* of the 24 h, so
    a request resumed after a Contract hold does not restart the owner's clock.
    """

    owner_confirmation_id: str
    verb: str
    window_seconds: float
    # When the owner's reminder is due, counted from now (PRD issue 21). The control
    # plane computes it from the owner's Preference because the workflow may not read a
    # database; 0 means there is no room left in the window for one.
    reminder_after_seconds: float = 0.0


@dataclass(frozen=True)
class QuestionAsked:
    """The Directive's Agent asked a human through ``agentic-runner ask`` (PRD issue 60).

    `work.ask` already allowed it at the callback -- a `deny` never produces one -- so the
    loop holds on it once the Directive has run. ``owner_confirmation`` is set when the
    verb evaluated `confirm`: the owner is asked first (issue 11's shape), and only a
    confirmation opens the Question.
    """

    question_id: str
    owner_confirmation: OwnerConfirmationPending | None = None


class HarnessHoldKind(StrEnum):
    """Why a subscription-mode harness stopped on its person's account (local-agents 08)."""

    # The plan's usage window is spent; the harness may say when it resets.
    USAGE_LIMIT = "usage_limit"
    # The sign-in expired, was revoked, or is absent: only the person can sign in again.
    SIGN_IN_REQUIRED = "sign_in_required"


@dataclass(frozen=True)
class HarnessHold:
    """A subscription Directive stopped for a reason a retry cannot fix (local-agents 08).

    Returned instead of raised, with nothing pushed, so the loop can hold rather than
    spend a fix Directive and Budget on it (issue 09). ``retry_not_before`` is ISO 8601
    UTC text -- the same reason ``ContractDeviceLoginResult.expires_at`` is -- and None
    when the harness named no reset time.
    """

    kind: HarnessHoldKind
    retry_not_before: str | None = None


@dataclass(frozen=True)
class QuestionOpenInput:
    """Route an allowed Question and start its 24 h window (PRD issue 60)."""

    work_record_id: str
    question_id: str


@dataclass(frozen=True)
class QuestionOpenOutput:
    """``opened`` false: the Question was voided before it could be asked (a termination).

    ``window_seconds`` is what is left of the window -- the full 24 h on a first open.
    """

    question_id: str
    opened: bool
    window_seconds: float = 0.0
    addressed_to: str = ""


@dataclass(frozen=True)
class QuestionTimeoutInput:
    """The loop's timer ran out on an open Question (PRD issue 60)."""

    work_record_id: str
    question_id: str


@dataclass(frozen=True)
class QuestionTimeoutOutput:
    """What the record says once the loop's timer fired.

    ``outcome`` is ``no_answer`` when this call recorded the timeout, ``answered`` when an
    answer landed in the same second, ``voided`` when a termination voided it, and empty
    when a Contract suspension paused its clock -- ``remaining_window_seconds`` is then
    what the loop waits out instead (the carried remainder, PRD issue 12).
    """

    question_id: str
    outcome: str
    remaining_window_seconds: float = 0.0


@dataclass(frozen=True)
class QuestionVoidInput:
    """Void a Question the owner refused (or let time out) at `work.ask: confirm`."""

    work_record_id: str
    question_id: str
    reason: str


@dataclass(frozen=True)
class QuestionVoidOutput:
    question_id: str
    voided: bool


@dataclass(frozen=True)
class OwnerConfirmationReminderInput:
    """Nudge the owner of a request still waiting at *deadline - lead* (PRD issue 21)."""

    work_record_id: str
    owner_confirmation_id: str


@dataclass(frozen=True)
class OwnerConfirmationReminderOutput:
    """Whether a reminder actually went out. ``False`` once the request is settled."""

    owner_confirmation_id: str
    reminded: bool


@dataclass(frozen=True)
class BranchPullRequestInput:
    """Input for branch creation and PR create/update activity."""

    work_record_id: str
    repository: str
    base_ref: str
    branch_name: str
    pr_title: str
    pr_body: str
    # Verbs the Agent's owner has already confirmed for this step (ADR-0011 s14). The
    # seam treats a `confirm` on one of these as allowed instead of raising again; the
    # workflow only ever sets it after the owner's signal arrived.
    owner_confirmed_verbs: tuple[str, ...] = ()
    # PRD issue 58: `mcp:<server>` asks the owner refused or let time out. The
    # server is withheld and the Directive runs without it, rather than asking again.
    owner_declined_verbs: tuple[str, ...] = ()
    # Which Directive of this Work Record this attempt is — the producing Directive is 1,
    # and a re-run to carry an owner's confirmation (ADR-0011 s15) is the next. It keys
    # the Directive's Usage Records, so the Runner can read back what this turn spent.
    directive_number: int = 1
    # Runner state carried in, never cached in the Runner's process (ADR-0013 §3). Empty
    # on the first attempt: the activity then refreshes the snapshot and derives the
    # workspace itself, and returns both so the next step gets them.
    grant_snapshot: dict[str, Any] = field(default_factory=dict)
    workspace_path: str = ""
    # The routing decision this activity was dispatched by (PRD issue 42): the selector
    # it was matched on and the Contract's host party, so the Runner fails closed before
    # any verb when either disagrees with its own registration. Optional only so a
    # history written before issue 42 still decodes; a registered Runner refuses None.
    routing: DirectiveRouting | None = None
    # PRD issue 53: the Swarm member this step runs as, and who leads. Empty is the
    # degenerate Swarm -- the Work Record's own Agent, exactly as before Channels. The
    # Runner resolves the named member's own runtime context, model and Grant, and its
    # `pr.*` seams refuse any member but the Lead whatever its Grant says (ADR-0012 §1).
    agent_id: str = ""
    role: str = ""
    lead_agent_id: str = ""


@dataclass(frozen=True)
class BranchPullRequestOutput:
    """Output for branch creation and PR create/update activity."""

    repository: str
    branch_name: str
    base_ref: str
    pr_number: int
    pr_url: str
    branch_created: bool
    pr_created: bool
    branch_head_sha: str = ""
    # Runtime duration of this Directive (one runtime turn), accrued against the
    # Work Record's wall-clock Budget (ADR-0007). 0.0 until a runtime reports it.
    duration_seconds: float = 0.0
    # The runtime refused to execute this Directive for guard-mode reasons (e.g. a
    # missing sandbox/policy config or expired Codex auth — the silent exit-126 stall).
    # Distinct from an ordinary failure: it ends the loop with an attributable Incident
    # rather than retrying opaquely (issue 05).
    guard_mode_refused: bool = False
    # The Agent's Effective Grant refused a privileged verb at its seam (ADR-0011 §8-10).
    # A deterministic refusal: retrying replays the same snapshot, so it is returned as a
    # flag the workflow turns into a terminal Incident rather than raised.
    grant_refused: bool = False
    grant_refusal_reason: str = ""
    # The chain escalated a verb to the Agent's owner; nothing was pushed or opened.
    owner_confirmation: OwnerConfirmationPending | None = None
    # This Directive's own LLM spend, decremented from the token Budget (PRD issue 13).
    usage: DirectiveUsage = field(default_factory=DirectiveUsage)
    # The workspace this Directive cloned into, and the Grant snapshot its boundary
    # refreshed: the loop's state, returned rather than kept (ADR-0013 §3).
    workspace_path: str = ""
    grant_snapshot: dict[str, Any] = field(default_factory=dict)
    # PRD issue 59 (19 A8): the Lead charted children under its own Work Record -- the
    # plan file it wrote, parsed by the Runner and evaluated by the control plane.
    plan: EpicPlan | None = None
    # PRD issue 60: the Question this Directive's Agent asked over its callback socket.
    question: QuestionAsked | None = None
    # Local-agents 08: the subscription harness hit its usage limit or lost its sign-in.
    harness_hold: HarnessHold | None = None


@dataclass(frozen=True)
class PullRequestReadyInput:
    """Input for the ``pr.review`` seam: take the draft PR out of draft (ADR-0011 §3)."""

    work_record_id: str
    repository: str
    pr_number: int
    branch_head_sha: str
    owner_confirmed_verbs: tuple[str, ...] = ()
    # The snapshot the producing Directive refreshed; the seam evaluates ``pr.review``
    # against it rather than re-reading a Runner-local cache (ADR-0013 §3).
    grant_snapshot: dict[str, Any] = field(default_factory=dict)
    # The routing decision this activity was dispatched by (PRD issue 42): the selector
    # it was matched on and the Contract's host party, so the Runner fails closed before
    # any verb when either disagrees with its own registration. Optional only so a
    # history written before issue 42 still decodes; a registered Runner refuses None.
    routing: DirectiveRouting | None = None
    # PRD issue 53: the Swarm member this step runs as, and who leads. Empty is the
    # degenerate Swarm -- the Work Record's own Agent, exactly as before Channels. The
    # Runner resolves the named member's own runtime context, model and Grant, and its
    # `pr.*` seams refuse any member but the Lead whatever its Grant says (ADR-0012 §1).
    agent_id: str = ""
    role: str = ""
    lead_agent_id: str = ""
    # PRD issue 54, the Critic's veto as a workflow-state test the seam cannot fabricate:
    # the Critic holding the role (empty: no Critic, no veto) and the head its standing
    # `clear` names. The seam asserts that head is the one it is about to act on, so a
    # `block`, no verdict at all, or a push since the `clear` all refuse.
    veto_critic_agent_id: str = ""
    veto_cleared_head_sha: str = ""


@dataclass(frozen=True)
class PullRequestReadyOutput:
    """Result of the ``pr.review`` seam.

    ``ready`` is True once the PR is out of draft (including when it already was, so an
    activity retry is a no-op). A grant refusal leaves the PR a draft and ends the loop.
    """

    repository: str
    pr_number: int
    ready: bool
    marked_ready: bool = False
    grant_refused: bool = False
    grant_refusal_reason: str = ""
    owner_confirmation: OwnerConfirmationPending | None = None


@dataclass(frozen=True)
class ReviewerHandoffInput:
    """Input for handing an Engineer PR to a reviewer."""

    work_record_id: str
    repository: str
    pr_number: int
    pr_url: str
    reviewer: str | None


@dataclass(frozen=True)
class ReviewerHandoffOutput:
    """Output for reviewer handoff."""

    repository: str
    pr_number: int
    reviewer: str | None
    handoff_id: str


@dataclass(frozen=True)
class ApprovalWaitInput:
    """Input for waiting on reviewer approval.

    ``required_human_approvals`` is how many distinct org humans must have an approving
    review standing on the current head before the gate reports approved (ADR-0011 s6).
    It is a *hold* cadence, never an authorisation: the ``pr.merge`` seam re-counts from
    its own grant snapshot before anything merges.
    """

    work_record_id: str
    repository: str
    pr_number: int
    reviewer: str | None
    handoff_id: str
    required_human_approvals: int = 1
    # The routing decision this activity was dispatched by (PRD issue 42): the selector
    # it was matched on and the Contract's host party, so the Runner fails closed before
    # any verb when either disagrees with its own registration. Optional only so a
    # history written before issue 42 still decodes; a registered Runner refuses None.
    routing: DirectiveRouting | None = None


@dataclass(frozen=True)
class ApprovalWaitOutput:
    """Output of one bounded Reviewer Gate poll.

    Tri-state: ``approved`` (current-head approval), ``denied`` (an explicit
    changes-requested review — the only reviewer rejection), or neither, which means
    still pending and the workflow must keep polling. ``unavailable`` marks a headless
    worker (no GitHub adapter) so the workflow can fail the gate closed immediately
    instead of polling to the deadline.

    ``externally_merged``/``pr_closed`` are decisive PR lifecycle outcomes observed by
    the poll: a human merged the PR out from under the gate (attributed via
    ``external_merge_actor``/``merge_commit_sha`` when GitHub provides them), or closed
    it without merging. The workflow must stop polling on either — close as an
    externally-merged success, or deny terminally as pr_closed — instead of polling an
    already-settled PR to the ~3-day approval_timeout.
    """

    repository: str
    pr_number: int
    approved: bool
    approver: str | None
    expected_head_sha: str | None = None
    approvals_observed: int = 0
    denied: bool = False
    unavailable: bool = False
    externally_merged: bool = False
    pr_closed: bool = False
    external_merge_actor: str | None = None
    merge_commit_sha: str | None = None


@dataclass(frozen=True)
class ApprovalDenialInput:
    """Input for recording terminal approval denial evidence."""

    work_record_id: str
    repository: str
    pr_number: int
    reason: str
    verifier_summary: str
    denied_by: str | None = None


@dataclass(frozen=True)
class ApprovalDenialOutput:
    """Output for terminal approval denial recording."""

    work_record_id: str
    terminal_state: str
    recorded: bool


@dataclass(frozen=True)
class AutonomyEvaluationInput:
    """Input for evaluating whether a verified-green Outcome may merge unattended.

    Carries the verified head so the decision is bound to the head that was verified
    (ADR-0002): an auto-merge merges exactly that head, no human approval in between."""

    work_record_id: str
    repository: str
    pr_number: int
    base_ref: str
    branch_head_sha: str


@dataclass(frozen=True)
class AutonomyEvaluationOutput:
    """Result of the Autonomy Policy decision for a verified Outcome (issue 08).

    ``auto_merge`` is the only field the deterministic workflow branches on; ``reason``
    is carried for the terminal result. The decision is recorded as Evidence inside the
    activity, not by the workflow. Safe by construction: anything the activity cannot
    positively clear as an allow-listed low-risk class with no reviewer gate stays
    ``False`` and falls through to the human gate (ADR-0005 §4)."""

    auto_merge: bool
    reason: str


@dataclass(frozen=True)
class PullRequestMergeInput:
    """Input for merging an approved pull request.

    ``outcome_clearance`` is the seam's second input (ADR-0011 s1): a merge needs the
    Agent's ``pr.merge`` **and** a cleared Outcome ceiling. It names which cleared it —
    the Autonomy Policy, or the org's Approval. It defaults to the empty string, so a
    call that does not state a clearance is refused rather than merged.
    """

    work_record_id: str
    repository: str
    pr_number: int
    approver: str
    commit_title: str
    expected_head_sha: str
    outcome_clearance: str = ""
    owner_confirmed_verbs: tuple[str, ...] = ()
    # The snapshot the loop is carrying; the ``pr.merge`` seam and the four-eyes count
    # read it from here, not from a Runner-local cache (ADR-0013 §3).
    grant_snapshot: dict[str, Any] = field(default_factory=dict)
    # The routing decision this activity was dispatched by (PRD issue 42): the selector
    # it was matched on and the Contract's host party, so the Runner fails closed before
    # any verb when either disagrees with its own registration. Optional only so a
    # history written before issue 42 still decodes; a registered Runner refuses None.
    routing: DirectiveRouting | None = None
    # PRD issue 53: the Swarm member this step runs as, and who leads. Empty is the
    # degenerate Swarm -- the Work Record's own Agent, exactly as before Channels. The
    # Runner resolves the named member's own runtime context, model and Grant, and its
    # `pr.*` seams refuse any member but the Lead whatever its Grant says (ADR-0012 §1).
    agent_id: str = ""
    role: str = ""
    lead_agent_id: str = ""
    # PRD issue 54, the Critic's veto as a workflow-state test the seam cannot fabricate:
    # the Critic holding the role (empty: no Critic, no veto) and the head its standing
    # `clear` names. The seam asserts that head is the one it is about to act on, so a
    # `block`, no verdict at all, or a push since the `clear` all refuse.
    veto_critic_agent_id: str = ""
    veto_cleared_head_sha: str = ""
    # The GitHub logins the Critic's reviews were posted under. Excluded from the
    # four-eyes count by name, whatever Persona holds the role -- on top of the bot rule
    # that already excludes every Agent identity (ADR-0011 s6).
    excluded_reviewer_logins: tuple[str, ...] = ()
    # PRD issue 59 (19 A3): the PRs stacked on this one, retargeted to
    # ``retarget_base_ref`` by this seam once the merge landed -- GitHub does not while
    # the branch exists. Empty for every unstacked Work Record.
    retarget_pr_numbers: tuple[int, ...] = ()
    retarget_base_ref: str = ""


@dataclass(frozen=True)
class PullRequestMergeOutput:
    """Output for pull request merge.

    Three outcomes the workflow branches on, beyond the merge itself:
    ``grant_refused`` is a governance refusal — the Effective Grant denied ``pr.merge``,
    escalated it to an Owner Confirmation, or the Outcome ceiling was not cleared — and
    ends the loop with an attributable Incident. ``approvals_pending`` is a *hold*, not a
    failure: fewer distinct org humans have approved the verified head than the Product's
    ``required_human_approvals``, so the loop keeps waiting for reviewers (ADR-0011 s6).
    """

    repository: str
    pr_number: int
    merged: bool
    merge_commit_sha: str
    summary: str = ""
    grant_refused: bool = False
    grant_refusal_reason: str = ""
    approvals_pending: bool = False
    approvals_observed: int = 0
    approvals_required: int = 0
    owner_confirmation: OwnerConfirmationPending | None = None
    # PRD issue 59: the dependent PRs this merge retargeted to the default branch.
    retargeted_pr_numbers: tuple[int, ...] = ()


@dataclass(frozen=True)
class FixDirectiveInput:
    """Input for one fix Directive — a single runtime turn that reads a verifier
    failure, repairs the change, and re-pushes the work branch (ADR-0007)."""

    work_record_id: str
    repository: str
    pr_number: int
    base_ref: str
    branch_name: str
    directive_number: int
    verifier_summary: str
    # Bounded, redacted tail of the failing verifier's stdout/stderr
    # (``VerifierRunOutput.failure_output``); "" when no output was captured.
    verifier_output: str = ""
    owner_confirmed_verbs: tuple[str, ...] = ()
    # PRD issue 58: `mcp:<server>` asks the owner refused or let time out. The
    # server is withheld and the Directive runs without it, rather than asking again.
    owner_declined_verbs: tuple[str, ...] = ()
    # Runner state carried in rather than cached (ADR-0013 §3): the workspace the first
    # Directive produced, and the snapshot to refresh from at this Directive's boundary.
    workspace_path: str = ""
    grant_snapshot: dict[str, Any] = field(default_factory=dict)
    # The routing decision this activity was dispatched by (PRD issue 42): the selector
    # it was matched on and the Contract's host party, so the Runner fails closed before
    # any verb when either disagrees with its own registration. Optional only so a
    # history written before issue 42 still decodes; a registered Runner refuses None.
    routing: DirectiveRouting | None = None
    # PRD issue 53: the Swarm member this step runs as, and who leads. Empty is the
    # degenerate Swarm -- the Work Record's own Agent, exactly as before Channels. The
    # Runner resolves the named member's own runtime context, model and Grant, and its
    # `pr.*` seams refuse any member but the Lead whatever its Grant says (ADR-0012 §1).
    agent_id: str = ""
    role: str = ""
    lead_agent_id: str = ""


@dataclass(frozen=True)
class FixDirectiveOutput:
    """Output of one fix Directive turn, carrying the repaired work-branch head."""

    work_record_id: str
    repository: str
    pr_number: int
    directive_number: int
    branch_head_sha: str
    summary: str
    # Runtime duration of this fix Directive, accrued against the wall-clock Budget
    # (ADR-0007). 0.0 until a runtime reports it.
    duration_seconds: float = 0.0
    # This Directive's own LLM spend, decremented from the token Budget (PRD issue 13).
    usage: DirectiveUsage = field(default_factory=DirectiveUsage)
    # The runtime refused to execute this fix Directive for guard-mode reasons; ends the
    # loop with an attributable Incident rather than retrying opaquely (issue 05).
    guard_mode_refused: bool = False
    # The Agent's Effective Grant refused this Directive's push (ADR-0011 §8-10).
    grant_refused: bool = False
    grant_refusal_reason: str = ""
    owner_confirmation: OwnerConfirmationPending | None = None
    # ...and carried back out for the next Directive (ADR-0013 §3).
    workspace_path: str = ""
    grant_snapshot: dict[str, Any] = field(default_factory=dict)
    # PRD issue 59: the plan the Lead's Directive wrote (a coordination Directive on an
    # Epic, or a child's Lead charting a gap under its own Work Record, 19 A8). None when
    # the Directive wrote none; only the Lead's plan is ever acted on.
    plan: EpicPlan | None = None
    # PRD issue 60: the Question this Directive's Agent asked over its callback socket.
    question: QuestionAsked | None = None
    # Local-agents 08: the subscription harness hit its usage limit or lost its sign-in.
    harness_hold: HarnessHold | None = None


@dataclass(frozen=True)
class MemberDirectiveInput:
    """One Directive for a woken Swarm member (PRD issue 53, ADR-0012 §1).

    Not a fix: the member was woken by its pending Messages (a `request`, a `result`, a
    `handoff`) or, for the Lead, because every other member is idle. The Runner folds
    every pending Message addressed to ``agent_id`` into this one Directive's context
    under that Agent's own `channel.read`, marks them consumed, and pushes whatever the
    Directive changed under that Agent's own `push` -- to the Lead's branch, since one
    Workspace serves every member. A Directive that only emits Messages still costs one.
    """

    work_record_id: str
    repository: str
    pr_number: int
    base_ref: str
    branch_name: str
    directive_number: int
    agent_id: str
    role: str
    lead_agent_id: str
    channel_id: str
    # Why the scheduler woke this member: ``request`` | ``result`` | ``handoff`` |
    # ``verdict`` | ``members_idle``. Prompt material and Evidence, never a branch.
    wake_reason: str = ""
    # Every role this member holds, so a role-addressed Message to any of them is
    # pending for it; ``role`` above is the one it was woken as.
    roles: tuple[str, ...] = ()
    owner_confirmed_verbs: tuple[str, ...] = ()
    # PRD issue 58: `mcp:<server>` asks the owner refused or let time out. The
    # server is withheld and the Directive runs without it, rather than asking again.
    owner_declined_verbs: tuple[str, ...] = ()
    workspace_path: str = ""
    grant_snapshot: dict[str, Any] = field(default_factory=dict)
    routing: DirectiveRouting | None = None
    # PRD issue 59: the Epic's board (children, states, PRs, edges), structured input to
    # a coordination Directive the way Messages are (19 A2). None on a plain member
    # Directive, whose prompt is then byte-identical to the pre-Epic form.
    board: EpicBoard | None = None
    # PRD issue 60: the Question this wake answers (``wake_reason`` says whether it was
    # answered or ran out). The Runner reads the text and the answer from the record,
    # never from the payload, so an answer is never in Temporal history.
    question_id: str = ""


@dataclass(frozen=True)
class MemberDirectiveOutput(FixDirectiveOutput):
    """A fix Directive's output plus what the wake consumed.

    ``pushed`` is false when the Directive changed nothing in the Workspace -- it only
    spoke -- so the loop keeps the head it had rather than a blank one.
    """

    messages_consumed: int = 0
    pushed: bool = False


@dataclass(frozen=True)
class SwarmHandoffInput:
    """Record a `handoff` Message's role transfer as Evidence (ADR-0012 §1): the same
    ``swarm.role_assigned`` Event the Channel's defaults were instantiated with."""

    work_record_id: str
    role: str
    from_agent_id: str
    to_agent_id: str
    message_id: str
    directive_number: int
    # `handoff` is a Lead power (ADR-0012 §1: who is in charge is fixed at dispatch and
    # changed only by the Lead's `handoff`). One posted by any other member transfers
    # nothing: the workflow sets ``refused`` and the Event is a ``swarm.role_refusal``
    # naming the sender, not a role assignment.
    sender_agent_id: str = ""
    lead_agent_id: str = ""
    refused: bool = False


@dataclass(frozen=True)
class SwarmHandoffOutput:
    work_record_id: str
    recorded: bool


@dataclass(frozen=True)
class PullRequestReviewInput:
    """The `pr.comment` seam (PRD issue 54): post the Critic's `verdict` as a PR review.

    Evaluated as ``(repo, <repository>, pr.comment)`` against the Critic's own Effective
    Grant -- ``agent_id`` and ``grant_snapshot`` are the Critic's, never the Lead's. The
    findings are the `verdict` Message's body, which the Runner reads from its own store
    by ``message_id``; they never cross the control plane.
    """

    work_record_id: str
    repository: str
    pr_number: int
    head_sha: str
    verdict: str
    message_id: str
    channel_id: str
    agent_id: str
    owner_confirmed_verbs: tuple[str, ...] = ()
    grant_snapshot: dict[str, Any] = field(default_factory=dict)
    routing: DirectiveRouting | None = None


@dataclass(frozen=True)
class PullRequestReviewOutput:
    """Whether the review was posted, and as what. ``review_state`` is GitHub's
    (``CHANGES_REQUESTED`` for `block`, ``COMMENTED`` for `clear`; never an approval)."""

    work_record_id: str
    posted: bool
    review_id: int = 0
    review_state: str = ""
    reviewer_login: str = ""
    grant_refused: bool = False
    grant_refusal_reason: str = ""
    owner_confirmation: OwnerConfirmationPending | None = None


class GitHubCallFailure(StrEnum):
    """Why a Runner's platform-requested GitHub call did not happen (QTS-1253)."""

    # The Runner was composed without a GitHub client, so no call was attempted.
    NO_GITHUB_CLIENT = "no_github_client"
    # The call was made and failed: no credential, refused, not found, transport, or a
    # response the client could not parse.
    GITHUB_ERROR = "github_error"


@dataclass(frozen=True)
class GitHubCallError:
    reason: GitHubCallFailure
    detail: str = ""


# The four PR calls and the readiness probe the platform used to make with its own GitHub
# App (QTS-1253): the Runner makes them with the credential it already holds for the verb
# seams. GitHub I/O only -- the decision and the Evidence stay with the platform activity
# that asks. Every output is a success exactly when ``failure`` is None; a failed call is
# never reported as an empty success.


@dataclass(frozen=True)
class RequestReviewInput:
    work_record_id: str
    repository: str
    pr_number: int
    # A bare GitHub login; the caller strips the chat-facing "@".
    reviewer: str
    routing: DirectiveRouting | None = None


@dataclass(frozen=True)
class RequestReviewOutput:
    repository: str
    pr_number: int
    reviewer: str
    requested: bool = False
    failure: GitHubCallError | None = None


@dataclass(frozen=True)
class ChangedFilesInput:
    work_record_id: str
    repository: str
    pr_number: int
    routing: DirectiveRouting | None = None


@dataclass(frozen=True)
class ChangedFilesOutput:
    """The PR's full changed-file list, or a failure -- never both, never neither.

    The Autonomy Policy's Protected-Path check reads this: an empty list on error would
    read as "nothing changed" and could auto-merge, so a failure carries no list at all.
    """

    repository: str
    pr_number: int
    changed_paths: tuple[str, ...] | None = None
    failure: GitHubCallError | None = None

    def __post_init__(self) -> None:
        if (self.changed_paths is None) == (self.failure is None):
            raise ValueError("ChangedFilesOutput carries exactly one of changed_paths, failure")


@dataclass(frozen=True)
class PullRequestCommentInput:
    work_record_id: str
    repository: str
    pr_number: int
    body: str
    routing: DirectiveRouting | None = None


@dataclass(frozen=True)
class PullRequestCommentOutput:
    repository: str
    pr_number: int
    comment_id: int = 0
    failure: GitHubCallError | None = None


@dataclass(frozen=True)
class PullRequestCloseInput:
    work_record_id: str
    repository: str
    pr_number: int
    routing: DirectiveRouting | None = None


@dataclass(frozen=True)
class PullRequestCloseOutput:
    """``closed`` is False on success when the PR was already merged or closed, so a
    retry of the terminal step is a no-op rather than an error."""

    repository: str
    pr_number: int
    closed: bool = False
    failure: GitHubCallError | None = None


@dataclass(frozen=True)
class RepositoryProbeInput:
    """Readiness: can this Runner's credential read ``repository``? No Work Record."""

    repository: str
    routing: DirectiveRouting | None = None


@dataclass(frozen=True)
class RepositoryProbeOutput:
    repository: str
    default_branch: str = ""
    failure: GitHubCallError | None = None


@dataclass(frozen=True)
class CriticVetoEventInput:
    """One Evidence Event about the Critic's veto (PRD issue 54).

    ``event`` is one of ``critic.verdict`` (a Critic's verdict took effect), ``critic.
    verdict_ignored`` (a `verdict` from a non-Critic, or one naming no head or bit, kept
    as a `note`), ``critic.veto_invalidated`` (a push moved the head under a standing
    verdict), ``critic.veto_refused`` (`pr.review` / `pr.merge` held on it) and
    ``critic.no_critic`` (a Channel with no Critic: the seam runs with no veto check).
    """

    work_record_id: str
    event: str
    critic_agent_id: str = ""
    sender_agent_id: str = ""
    head_sha: str = ""
    previous_head_sha: str = ""
    verdict: str = ""
    verb: str = ""
    review_id: int = 0
    review_posted: bool = False
    message_id: str = ""
    reason: str = ""


@dataclass(frozen=True)
class CriticVetoEventOutput:
    work_record_id: str
    recorded: bool


# Why a Learning Directive proposed nothing (PRD issue 55), on its `_ended` Evidence.
LEARNING_PROPOSED = "proposed"
LEARNING_RESERVE_EXHAUSTED = "reserve_exhausted"
LEARNING_FAILED = "failed"

# Why no Learning Directive ran (PRD issue 68), on its `_skipped` Evidence. The loop names
# an ending it never learns from by its own status value, or a Work Record no Runner was
# ever routed for; the assembly alone finds a Contract without its Learner Agent.
LEARNING_SKIPPED_NO_RUNNER = "no_runner"
LEARNING_SKIPPED_NO_LEARNER_AGENT = "no_learner_agent"

# An Epic's children reach the Learner through this one byte budget (issue 73), read by
# both sides so they cannot drift: the platform fits every child it keeps inside it, and
# the Runner prints the section uncut -- a section over it is a contract violation.
LEARNER_CHILDREN_BUDGET_BYTES = 60_000


def learner_children_json(value: object) -> str:
    """The children section exactly as the Runner prints it: the budget bounds this
    string's length. ASCII-escaped, so its length in characters is its length in bytes."""

    return json.dumps(value, separators=(",", ":"), default=str)


@dataclass(frozen=True)
class ProposedLesson:
    """One Lesson as the Learner drafts it (map ticket 09): a general-form ``body`` and
    the org-specific detail beside it in ``org_note``, which the user strips at review."""

    kind: str
    title: str
    body: str
    org_note: str = ""


@dataclass(frozen=True)
class LearningInputsInput:
    """``skip_reason`` is set when the loop has already decided no Learning Directive
    runs (PRD issue 68): the control plane then assembles nothing and records
    ``learning_directive_skipped`` with that reason instead of ``_started``."""

    work_record_id: str
    directive_number: int
    skip_reason: str = ""


@dataclass(frozen=True)
class LearningInputsOutput:
    """What the terminal Learning Directive reads, assembled on the control plane.

    ``learner_agent_id`` is empty when the Work Record has no Learner to run it (no
    Contract, or a Contract missing its Learner Agent, recorded as
    ``learning_directive_skipped``) or the loop gave a ``skip_reason``: the loop
    schedules nothing.
    ``children_evidence`` is the metadata-only projection of an Epic's children (map 19
    A7), one entry per child in chart order -- id, kind, state, ``ended`` (with
    ``ending_kind`` only when true), end reason, PR and its most recent Evidence metadata
    -- never a child's transcript or diff, which may belong to another Contract. Its
    :func:`learner_children_json` fits ``LEARNER_CHILDREN_BUDGET_BYTES``:
    ``children_omitted`` counts the children dropped from the end, and
    ``children_evidence_omitted`` the older Evidence events cut from those kept (each
    entry's ``evidence_omitted`` says whose).
    Recording ``learning_directive_started`` is part of the assembly, so its id is here.
    """

    work_record_id: str
    learner_agent_id: str = ""
    ending_kind: str = ""
    end_reason: str = ""
    reserve_max_tokens: int = 0
    evidence: list[dict[str, Any]] = field(default_factory=list)
    children_evidence: list[dict[str, Any]] = field(default_factory=list)
    children_omitted: int = 0
    children_evidence_omitted: int = 0
    existing_lessons: list[dict[str, str]] = field(default_factory=list)
    started_evidence_event_id: str = ""


@dataclass(frozen=True)
class LearningDirectiveInput:
    """The Learning Directive (PRD issue 55, ADR-0012): run on the Runner as the
    Contract's Learner Agent, in the Work Record's own Workspace, as the Contract's uid
    (ADR-0015). The Conversation transcript is read inside the Runner and never crosses."""

    work_record_id: str
    repository: str
    base_ref: str
    directive_number: int
    learner_agent_id: str
    ending_kind: str
    end_reason: str = ""
    # Platform-defined, on top of the Work Record Budget (map 12 B6): the proxy refuses
    # this attempt's calls once it is spent, whatever the Budget says.
    reserve_max_tokens: int = 0
    evidence: list[dict[str, Any]] = field(default_factory=list)
    children_evidence: list[dict[str, Any]] = field(default_factory=list)
    children_omitted: int = 0
    children_evidence_omitted: int = 0
    existing_lessons: list[dict[str, str]] = field(default_factory=list)
    routing: DirectiveRouting | None = None


@dataclass(frozen=True)
class LearningDirectiveOutput:
    work_record_id: str
    directive_number: int
    lessons: list[ProposedLesson] = field(default_factory=list)
    usage: DirectiveUsage = field(default_factory=DirectiveUsage)
    outcome: str = LEARNING_PROPOSED
    summary: str = ""
    harness_hold: HarnessHold | None = None


@dataclass(frozen=True)
class LearningOutcomeInput:
    """Write the proposals as `proposed` Lessons and the `_ended` Event naming them."""

    work_record_id: str
    learner_agent_id: str
    directive_number: int
    ending_kind: str
    lessons: list[ProposedLesson] = field(default_factory=list)
    usage: DirectiveUsage = field(default_factory=DirectiveUsage)
    outcome: str = LEARNING_PROPOSED
    summary: str = ""


@dataclass(frozen=True)
class LearningOutcomeOutput:
    work_record_id: str
    lesson_ids: list[str] = field(default_factory=list)
    evidence_event_id: str = ""


@dataclass(frozen=True)
class BudgetDecrementInput:
    """Input for recording one Directive's Budget decrement as an Evidence Event.

    ``directives_used``, ``wall_clock_used_seconds`` and ``tokens_used`` are the cumulative
    consumption after the Directive identified by ``directive_number``; ``budget`` carries
    the ceilings so the Evidence captures both consumed and remaining (ADR-0007).

    ``usage`` is *this* Directive's aggregate — the one Evidence Event per Directive map
    ticket 12 B2 asks for, beside the running totals."""

    work_record_id: str
    repository: str
    pr_number: int
    directive_number: int
    directives_used: int
    wall_clock_used_seconds: float
    budget: Budget
    tokens_used: int = 0
    usage: DirectiveUsage = field(default_factory=DirectiveUsage)


@dataclass(frozen=True)
class BudgetDecrementOutput:
    """Output for a per-Directive Budget decrement Evidence record."""

    work_record_id: str
    recorded: bool


@dataclass(frozen=True)
class VerifierRunInput:
    """Input for running the verifier against an approved PR head."""

    work_record_id: str
    repository: str
    pr_number: int
    merge_commit_sha: str
    command: str
    approved_head_sha: str
    # The workspace the producing Directive cloned into; empty falls back to deriving it
    # from the Runner's workspace root, which is what a retry on a fresh Runner does.
    workspace_path: str = ""
    # The routing decision this activity was dispatched by (PRD issue 42): the selector
    # it was matched on and the Contract's host party, so the Runner fails closed before
    # any verb when either disagrees with its own registration. Optional only so a
    # history written before issue 42 still decodes; a registered Runner refuses None.
    routing: DirectiveRouting | None = None


@dataclass(frozen=True)
class VerifierRunOutput:
    """Output for a verifier run.

    ``terminal`` distinguishes an unfixable failure (workspace attestation, invalid
    command, head integrity) — which ends the loop immediately — from a retriable
    failure (the verifier ran and tests are red), which a fix Directive can repair.

    ``failure_output`` is a bounded, redacted tail of the verifier's captured
    stdout/stderr on a retriable failure, so the fix Directive prompt can name the
    failing tests instead of re-running the whole suite to rediscover them. Empty
    on pass and on terminal failures. It rides through workflow state into
    ``FixDirectiveInput``, so it is bounded far below Temporal's 2MB payload limit.
    """

    work_record_id: str
    command: str
    passed: bool
    summary: str
    terminal: bool = False
    failure_output: str = ""


@dataclass(frozen=True)
class SuccessCloseInput:
    """Input for closing a successfully verified work record.

    ``external_merge_actor`` attributes a merge performed by a human outside the loop's
    own merge activity (the Reviewer Gate observed the PR already merged); None on the
    normal loop-merged path.
    """

    work_record_id: str
    repository: str
    pr_number: int
    merge_commit_sha: str
    verifier_summary: str
    external_merge_actor: str | None = None


@dataclass(frozen=True)
class SuccessCloseOutput:
    """Output for successful work closure."""

    work_record_id: str
    closed: bool
    summary: str


@dataclass(frozen=True)
class TerminalWorkflowFailureInput:
    """Input for terminally recording a workflow failure after verification."""

    work_record_id: str
    repository: str
    pr_number: int
    merge_commit_sha: str
    reason: str
    verifier_summary: str


@dataclass(frozen=True)
class TerminalWorkflowFailureOutput:
    """Output for terminal workflow failure recording."""

    work_record_id: str
    terminal_state: str
    recorded: bool


@dataclass(frozen=True)
class FailedVerificationIncidentInput:
    """Input for creating an incident after failed verification."""

    work_record_id: str
    repository: str
    pr_number: int
    merge_commit_sha: str
    verifier_summary: str
    terminal_failure_payload: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class FailedVerificationIncidentOutput:
    """Output for failed verification incident creation."""

    work_record_id: str
    incident_id: str
    summary: str


@dataclass(frozen=True)
class ContractDeviceLoginInput:
    """Input for one Contract's device-code sign-in (PRD issue 31).

    Runs on whichever Runner pod polls this Organisation's task queue, since only that
    pod's local disk holds the Contract's harness root (ADR-0015 §4).
    """

    contract_id: str
    runtime_kind: str
    # The Runner whose disk holds that harness root, and so whose queue the activity is
    # dispatched on (PRD issue 37). Optional only for a start that predates the split.
    runner_id: str | None = None


@dataclass(frozen=True)
class ContractDeviceLoginResult:
    """The vendor's verification prompt for one sign-in attempt -- never the token.

    The CLI is still running (detached) when this returns (PRD issue 31 review): the
    activity waits only for the prompt to print, not for the funder to act on it, so a
    single ``execute_workflow`` round-trip stays well under the Temporal executor's
    default timeout. ``expires_at`` travels as ISO 8601 text, not a ``datetime`` -- this
    dataclass crosses the workflow determinism sandbox, and Temporal's JSON converter
    round-trips plain strings without a custom payload codec.
    """

    contract_id: str
    runtime_kind: str
    verification_uri: str
    user_code: str
    expires_at: str


@dataclass(frozen=True)
class ContractDeviceLoginStatusInput:
    """Input for the later, separate read of one sign-in attempt (PRD issue 31 review)."""

    contract_id: str
    runtime_kind: str
    runner_id: str | None = None


@dataclass(frozen=True)
class ContractDeviceLoginStatusResult:
    """Whether the funder's sign-in has landed yet -- a ``stat``, never a read.

    ``delivered_at`` is the token file's own mtime as ISO 8601 text, or None before it
    exists.
    """

    contract_id: str
    runtime_kind: str
    token_present: bool
    delivered_at: str | None


class ContractDeviceLoginActivities(Protocol):
    """Start-side boundary for a Contract's device-code sign-in (PRD issue 31)."""

    async def sign_in_contract_device_login(
        self, request: ContractDeviceLoginInput
    ) -> ContractDeviceLoginResult:
        """Launch the harness's device-login command as this Contract's own uid and
        return once it has printed its verification prompt, leaving it running."""


class ContractDeviceLoginStatusActivities(Protocol):
    """Status-side boundary for a Contract's device-code sign-in (PRD issue 31): the
    later, separate check of whether an already-started sign-in finished."""

    async def check_contract_device_login_status(
        self, request: ContractDeviceLoginStatusInput
    ) -> ContractDeviceLoginStatusResult:
        """Stat this Contract's harness token file; never open it."""


@dataclass(frozen=True)
class TriageDirectiveInput:
    """One admitted message, ready for the Triage Directive (PRD issue 29).

    Everything deterministic admission (issue 28) already resolved: which Product, which
    Intake Lead and Contract fund the Directive, and the Slack coordinates a reply -- if
    any -- goes back to.
    """

    product_id: str
    product_slug: str
    source_id: str
    requester_profile_id: str
    intake_lead_agent_id: str
    intake_lead_contract_id: str
    message_text: str
    team_id: str
    channel_id: str
    user_id: str
    message_ts: str
    thread_ts: str


@dataclass(frozen=True)
class TriageDirectiveOutput:
    """The Directive's outcome, as the control plane's dispatch endpoint settled it."""

    outcome: str
    classification_source: str
    reason: str
    work_record_id: str | None
    replied: bool


@dataclass(frozen=True)
class TriagePreparation:
    """The Directive's prompt and where its one turn runs (issue 81).

    ``runner_id`` is empty when no live Runner serves the Intake Lead's Contract: the
    turn is then skipped and dispatch falls back to the keyword classifier.
    """

    prompt: str
    directive_id: str
    runner_id: str
    reserve_max_tokens: int


@dataclass(frozen=True)
class TriageTurnInput:
    """The Triage Directive's one turn, run through the Runner's LLM proxy (issue 81)."""

    prompt: str
    directive_id: str
    intake_lead_agent_id: str
    intake_lead_contract_id: str
    # The Triage Reserve (PRD issue 29): the control plane sets it, the proxy enforces it.
    reserve_max_tokens: int


@dataclass(frozen=True)
class TriageTurnOutput:
    """The completion's text, or ``None`` on a hang, a refusal or a malformed answer."""

    raw_text: str | None


@dataclass(frozen=True)
class TriageDispatchInput:
    request: TriageDirectiveInput
    raw_text: str | None


class TriageActivities(Protocol):
    """Temporal-ready activity boundary for the Triage Directive (PRD issue 29).

    Three activities since issue 81: the platform prepares the prompt and routes, the
    Runner runs the turn through its own LLM proxy (the only holder of the funder's
    key), and the platform dispatches the outcome.
    """

    async def prepare_triage_directive(self, request: TriageDirectiveInput) -> TriagePreparation:
        """Build the prompt and pick the Runner serving the Intake Lead's Contract."""

    async def run_triage_turn(self, runner_id: str, request: TriageTurnInput) -> TriageTurnOutput:
        """Run the one turn on ``runner.{runner_id}``; a model failure is ``None``, not raised."""

    async def dispatch_triage_directive(
        self, request: TriageDispatchInput
    ) -> TriageDirectiveOutput:
        """Hand the raw answer to the control plane, which decides and acts on it."""

    async def run_triage_directive(self, request: TriageDirectiveInput) -> TriageDirectiveOutput:
        """Pre-issue-81 histories only: dispatch with no model answer (keyword fallback)."""


@dataclass(frozen=True)
class OwnerConfirmationTimeoutInput:
    """The 24 h window ran out before the owner answered (ADR-0011 s15).

    An Evidence Event, never an Incident: an absent human is not an org-side failure.
    """

    work_record_id: str
    owner_confirmation_id: str
    verb: str


@dataclass(frozen=True)
class OwnerConfirmationTimeoutOutput:
    """``recorded`` false means the request was *not* timed out after all.

    Three things produce that: the owner answered in the same second; a Contract
    suspension paused the request's clock (PRD issue 12) — the control plane pushed the
    deadline out by the held span, so there is window left, and ``remaining_window_seconds``
    is what the loop waits out instead of ending the work on a clock its own timer ran
    ahead of; and a Contract *termination* voided the ask outright. ``voided`` is that
    third case, and it is not a timeout: the owner never had a window to miss, so the loop
    drains on the Contract rather than ending the work on them.
    """

    owner_confirmation_id: str
    recorded: bool
    remaining_window_seconds: float = 0.0
    voided: bool = False


@dataclass(frozen=True)
class BudgetRaiseInput:
    """The token Budget is spent; ask the Agent's owner to raise it or end the work.

    The verb-less Owner Confirmation kind (PRD issue 13): no seam refused anything, so
    there is nothing to confirm *for* — the two outcomes are *raise to N* and *end*.
    """

    work_record_id: str
    directive_number: int
    tokens_used: int
    max_tokens: int


@dataclass(frozen=True)
class BudgetRaiseOutput:
    """The raised request, or why there was nobody to ask.

    ``raised`` is False when the Work Record has no Agent — a pre-platform record, or one
    a flow bound none to. There is then no owner with standing to answer (ADR-0011 §14),
    and the loop falls back to ending on the Budget as it did before issue 13.
    """

    raised: bool
    owner_confirmation_id: str = ""
    window_seconds: float = 0.0
    # As on OwnerConfirmationPending: when the owner's reminder is due (PRD issue 21).
    reminder_after_seconds: float = 0.0


@dataclass(frozen=True)
class OwnerEndingInput:
    """Close the PR, keep the branch, end the Work Record (ADR-0011 s15).

    What a refusal or a timeout on `pr.open`, `pr.review` or `pr.merge` produces. The
    branch is deliberately left standing: the work survives for whoever picks it up, the
    org is just never asked to review it.
    """

    work_record_id: str
    repository: str
    pr_number: int
    verb: str
    end_reason: str
    owner_confirmation_id: str


@dataclass(frozen=True)
class OwnerEndingOutput:
    repository: str
    pr_number: int
    pr_closed: bool
    end_reason: str
    summary: str


@dataclass(frozen=True)
class ContractGateInput:
    """Read the Contract's state at a Directive boundary (PRD issue 12, ADR-0012 §4).

    ``held_seconds`` is what the loop has already spent holding on this boundary, so the
    Evidence of a resumed hold names the span that was *not* charged to the wall-clock
    Budget. 0 on the first read of a boundary.
    """

    work_record_id: str
    repository: str
    directive_number: int
    held_seconds: float = 0.0


@dataclass(frozen=True)
class ContractGateOutput:
    """What the Contract says about dispatching the next Directive.

    One field to branch on rather than a pair of booleans, because the three answers are
    mutually exclusive: ``proceed`` runs the Directive, ``hold`` waits for a resume with
    the Budget paused, and ``drain`` ends the Work Record without dispatching anything.
    """

    verdict: str
    contract_state: str
    reason: str
    contract_id: str = ""


CONTRACT_GATE_PROCEED = "proceed"
CONTRACT_GATE_HOLD = "hold"
CONTRACT_GATE_DRAIN = "drain"


@dataclass(frozen=True)
class FairUseGateInput:
    """Read the Organisation's daily Directive fair-use ceiling (PRD issue 26 AC4).

    ``held_seconds`` mirrors :class:`ContractGateInput`'s own field: what the loop has
    already spent holding on this boundary, so a held span the ceiling accounts for is
    never charged to the wall-clock Budget either.
    """

    work_record_id: str
    directive_number: int
    held_seconds: float = 0.0


@dataclass(frozen=True)
class FairUseGateOutput:
    """``hold`` when the Organisation-day's Directive count is already at its ceiling;
    the boundary resets at UTC midnight, so the same read proceeds once the day rolls."""

    verdict: str
    used: int
    allowed: int


FAIR_USE_GATE_PROCEED = "proceed"
FAIR_USE_GATE_HOLD = "hold"


@dataclass(frozen=True)
class ContractEndInput:
    """Drain the loop: end the Work Record, leave the PR standing (PRD issue 12).

    The open PR is deliberately *not* closed — map ticket 02: "orphaned open PRs are the
    org's". The contractor's Contract ended; the change they already pushed did not.
    """

    work_record_id: str
    repository: str
    pr_number: int
    contract_id: str
    contract_state: str
    reason: str
    # What the Runner's own `wipe_contract_residue` removed, as facts the workflow hands
    # over to be recorded (ADR-0013 §2: the Runner does the thing, the platform records
    # it). Defaulted so a worker on this version tolerates a history written before the
    # queues split (PRD issue 37).
    workspaces_wiped: int = 0
    harness_roots_wiped: int = 0
    uid_retired: bool = False


@dataclass(frozen=True)
class ContractEndOutput:
    """``workspaces_wiped`` is the termination wipe's count, as a fact (map 17 A6).

    ``harness_roots_wiped`` and ``uid_retired`` are the rest of that fact once the
    Contract owns a directory tree and a uid (ADR-0015 §2, PRD issue 30). Both default
    so a worker on this version tolerates a workflow history written before it.
    """

    work_record_id: str
    end_reason: str
    pr_left_open: bool
    workspaces_wiped: int
    summary: str
    harness_roots_wiped: int = 0
    uid_retired: bool = False


@dataclass(frozen=True)
class ContractResidueInput:
    """Wipe everything one Contract left on this Runner (map ticket 17 A6)."""

    contract_id: str
    # The routing decision this activity was dispatched by (PRD issue 42): the selector
    # it was matched on and the Contract's host party, so the Runner fails closed before
    # any verb when either disagrees with its own registration. Optional only so a
    # history written before issue 42 still decodes; a registered Runner refuses None.
    routing: DirectiveRouting | None = None


@dataclass(frozen=True)
class ContractResidueOutput:
    """Ids and counts only — what a termination wipe removed (PRD issue 30 Evidence)."""

    contract_id: str
    workspaces_wiped: int
    harness_roots_wiped: int
    uid_retired: bool
    # PRD issue 52: the Contract's Message stores are *sealed*, not wiped -- the org
    # retains them until retention, the user loses access (map ticket 10).
    message_stores_sealed: int = 0


@dataclass(frozen=True)
class WorkspaceRetentionOutput:
    """The periodic retention delete's count (ADR-0015 §2, one retention period only).

    ``held`` is every Workspace this Runner had on disk when the sweep ran; ``deleted``
    is the subset whose Work Record is terminal and older than the Organisation's
    ``retention_days``. A Contract that ended does not wait for this — its tree went with
    :class:`ContractResidueInput`.
    """

    held: int
    deleted: int
    # PRD issue 52: Message stores dropped by the same sweep, on the same period.
    message_stores_deleted: int = 0


@dataclass(frozen=True)
class WorkAcceptGateInput:
    """Read before the first Directive dispatches (PRD issue 29): whether this Work
    Record's Agent must accept it, and if so, what it says."""

    work_record_id: str


@dataclass(frozen=True)
class WorkAcceptGateOutput:
    """One field to branch on, mirroring :class:`ContractGateOutput`: ``allow`` and
    ``not_applicable`` both proceed, ``deny`` and ``confirm`` do not."""

    verdict: str
    owner_confirmation_id: str = ""
    window_seconds: float = 0.0
    reminder_after_seconds: float = 0.0


WORK_ACCEPT_ALLOW = "allow"
WORK_ACCEPT_CONFIRM = "confirm"
WORK_ACCEPT_DENY = "deny"
WORK_ACCEPT_NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True)
class WorkAcceptReturnInput:
    """`deny`, a refusal or a timeout: hand the Work Record back to its creator."""

    work_record_id: str
    reason: str


@dataclass(frozen=True)
class WorkAcceptReturnOutput:
    work_record_id: str
    agent_id: str


@dataclass(frozen=True)
class DirectiveRoutingInput:
    """Ask the control plane which Runner may run the next Directive (PRD issue 42).

    Asked before *every* Directive, not once per run: a Runner goes stale when a laptop
    sleeps (23 item 4) and a Work Record outlives that, so the pin is re-validated rather
    than trusted. ``wiped_runner_id`` is set on the second ask of a wipe-and-reassign
    (17 A2): the loop has collected the ack, and the router may now hand that ``none``
    Runner to this Contract.
    """

    work_record_id: str
    directive_number: int
    wiped_runner_id: str = ""


@dataclass(frozen=True)
class DirectiveRoutingOutput:
    """The router's verdict, mirroring :class:`ContractGateOutput`'s one-field shape.

    ``routed`` dispatches on ``routing``; ``waiting`` holds the Work Record ("waiting for
    a Runner", 25 §9) until its wall-clock Budget runs out; ``wipe_required`` names the
    ``none`` Runner that must be wiped of the Contract it currently serves before it can
    take this one, with ``routing`` describing that wipe dispatch.
    """

    verdict: str
    routing: DirectiveRouting | None = None
    # The Work Record was pinned to a Runner that is gone (25 §8), so its Workspace has
    # to be re-assembled from the branch on the newly routed one before the Directive.
    workspace_reassembly_required: bool = False
    reason: str = ""


ROUTE_DIRECTIVE_ROUTED = "routed"
ROUTE_DIRECTIVE_WAITING = "waiting"
ROUTE_DIRECTIVE_WIPE_REQUIRED = "wipe_required"


@dataclass(frozen=True)
class WorkspaceReassemblyInput:
    """Re-assemble a re-routed Work Record's Workspace from its branch (25 §8).

    The branch is the input, not a process cache (ADR-0013 §3): a Work Record whose
    Runner went stale carries nothing forward but what is on the remote, because issue
    36 retired ``_runtime_contexts``.
    """

    work_record_id: str
    repository: str
    base_ref: str
    branch_name: str
    routing: DirectiveRouting | None = None


@dataclass(frozen=True)
class WorkspaceReassemblyOutput:
    """Where the re-assembled Workspace landed on the new Runner, and at which head."""

    work_record_id: str
    workspace_path: str
    branch_name: str
    head_sha: str = ""


@dataclass(frozen=True)
class RunnerUnavailableInput:
    """No Runner matched for the whole wall-clock window (25 §9, 23).

    A sleeping laptop is this case: governance, not failure -- no Incident, and the PR
    (if the loop got that far) is left standing like every other ``ENDED`` path.
    """

    work_record_id: str
    repository: str
    pr_number: int
    waited_seconds: float
    reason: str


@dataclass(frozen=True)
class RunnerUnavailableOutput:
    work_record_id: str
    end_reason: str
    summary: str


# ------------------------------------------------------------------ Epics (PRD issue 59)

EPIC_PLAN_APPLIED = "applied"
EPIC_PLAN_CONFIRM = "confirm"
EPIC_PLAN_REFUSED = "refused"
EPIC_PLAN_REQUESTED_PARENT = "requested_parent"
EPIC_PLAN_PARKED = "parked"
EPIC_PLAN_REPORTED = "reported"


@dataclass(frozen=True)
class EpicPlanInput:
    """Submit the Lead's plan: the `work.open` / `work.end` seam (19 A1, A5, A8).

    ``consented_keys`` are the planned children whose `work.open` the owner confirmed on
    an earlier attempt, ``declined_keys`` the ones refused or timed out (opened by nobody,
    the Lead carries on). The activity is re-run with them until every child is decided;
    only then are the children created, so a re-run never opens one twice.
    """

    work_record_id: str
    directive_number: int
    lead_agent_id: str
    plan: EpicPlan
    consented_keys: tuple[str, ...] = ()
    declined_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class EpicPlanOutput:
    """``applied`` created the allowed children and applied the endings; ``confirm``
    stopped on one child's Owner Confirmation (``pending_key`` names it); ``refused``
    means the Lead holds no `work.open` and had nothing else to apply -- on a child
    with a parent Epic a `request` was posted to the parent's Lead
    (``requested_parent``), with no parent the child was parked (``parked``). A report's
    `work.end` on its closing `result` Message ended it ``DONE`` (``reported``)."""

    work_record_id: str
    verdict: str
    board: EpicBoard = field(default_factory=EpicBoard)
    opened: tuple[str, ...] = ()
    refused: tuple[str, ...] = ()
    ended: tuple[str, ...] = ()
    pending_key: str = ""
    owner_confirmation: OwnerConfirmationPending | None = None
    # 19 A8: a plain Work Record whose Lead charted children flipped to `epic`.
    converted: bool = False
    end_epic: bool = False


@dataclass(frozen=True)
class EpicBoardInput:
    work_record_id: str


@dataclass(frozen=True)
class EpicBoardOutput:
    work_record_id: str
    board: EpicBoard


@dataclass(frozen=True)
class ChildStartInput:
    """Start one `QUEUED` child whose blockers are satisfied (19 A2): no Directive."""

    work_record_id: str
    child_work_record_id: str
    reason: str


@dataclass(frozen=True)
class ChildStartOutput:
    child_work_record_id: str
    started: bool


@dataclass(frozen=True)
class EpicEndInput:
    """End the Epic: ``DONE`` when every child is, ``ENDED / children_parked`` when the
    Lead ended it short, listing the children that are not ``DONE`` (19 A5)."""

    work_record_id: str
    outcome: str
    directives_executed: int
    children_not_done: tuple[str, ...] = ()


@dataclass(frozen=True)
class EpicEndOutput:
    work_record_id: str
    status: str
    summary: str


@dataclass(frozen=True)
class ChildPrOpenedInput:
    """A child's PR is open: the control plane tells the Epic so a `based_on` sibling
    may start (19 A3). Posted as a `note` in the Epic's Conversation."""

    work_record_id: str
    parent_work_record_id: str
    pr_number: int
    pr_url: str
    branch_name: str


@dataclass(frozen=True)
class ChildPrOpenedOutput:
    work_record_id: str
    notified: bool


@dataclass(frozen=True)
class StackDependent:
    work_record_id: str
    pr_number: int


@dataclass(frozen=True)
class StackReadInput:
    """What a stacked child needs before `pr.merge` (19 A3): whether its base merged,
    which PRs depend on it, and the branch they are retargeted to."""

    work_record_id: str
    # Record the refusal as Evidence when the base is still unmerged.
    at_merge: bool = False


@dataclass(frozen=True)
class StackReadOutput:
    work_record_id: str
    base_work_record_id: str = ""
    base_merged: bool = True
    base_status: str = ""
    default_branch: str = ""
    dependents: tuple[StackDependent, ...] = ()


class RalphActivities(Protocol):
    """Temporal-ready activity boundary for the deterministic Ralph loop."""

    async def submit_epic_plan(self, request: EpicPlanInput) -> EpicPlanOutput:
        """Evaluate `work.open` / `work.end` on the Lead's plan and apply it (PRD 59)."""

    async def read_epic_board(self, request: EpicBoardInput) -> EpicBoardOutput:
        """The Epic's children, states, PRs and edges (PRD issue 59)."""

    async def start_epic_child(self, request: ChildStartInput) -> ChildStartOutput:
        """Start one queued child the frontier released (PRD issue 59)."""

    async def end_epic(self, request: EpicEndInput) -> EpicEndOutput:
        """End the Epic ``DONE`` or ``ENDED / children_parked`` (PRD issue 59)."""

    async def record_child_pr_opened(self, request: ChildPrOpenedInput) -> ChildPrOpenedOutput:
        """Tell the parent Epic a child's PR is open (PRD issue 59)."""

    async def read_stack(self, request: StackReadInput) -> StackReadOutput:
        """Whether a stacked child's base merged, and its dependents (PRD issue 59)."""

    async def assemble_context(self, request: ContextAssemblyInput) -> ContextAssemblyOutput:
        """Assemble workflow context from future durable services."""

    async def create_or_update_branch_pr(
        self,
        request: BranchPullRequestInput,
    ) -> BranchPullRequestOutput:
        """Create/update branch and PR through a future GitHub activity."""

    async def mark_pr_ready_for_review(
        self,
        request: PullRequestReadyInput,
    ) -> PullRequestReadyOutput:
        """Evaluate ``pr.review`` and take the draft PR out of draft (ADR-0011 §8)."""

    async def handoff_to_reviewer(self, request: ReviewerHandoffInput) -> ReviewerHandoffOutput:
        """Request reviewer handoff through a future chat/review activity."""

    async def evaluate_autonomy(
        self,
        request: AutonomyEvaluationInput,
    ) -> AutonomyEvaluationOutput:
        """Decide whether the verified Outcome may merge unattended (Autonomy Policy,
        issue 08). Records the decision as Evidence; safe-default is a human gate."""

    async def wait_for_approval(self, request: ApprovalWaitInput) -> ApprovalWaitOutput:
        """Run one bounded Reviewer Gate poll; pending outputs mean poll again."""

    async def record_approval_denial(self, request: ApprovalDenialInput) -> ApprovalDenialOutput:
        """Record terminal approval denial evidence through control-plane APIs."""

    async def merge_pr(self, request: PullRequestMergeInput) -> PullRequestMergeOutput:
        """Merge an approved PR through a future GitHub activity."""

    async def execute_fix_directive(self, request: FixDirectiveInput) -> FixDirectiveOutput:
        """Execute one fix Directive — read the verifier failure, repair the change,
        and re-push the work branch. Exactly one runtime turn (ADR-0007)."""

    async def execute_member_directive(
        self, request: MemberDirectiveInput
    ) -> MemberDirectiveOutput:
        """One Directive for a woken Swarm member, its pending Messages folded in
        (PRD issue 53). Exactly one runtime turn, one Budget decrement."""

    async def record_swarm_handoff(self, request: SwarmHandoffInput) -> SwarmHandoffOutput:
        """Record a `handoff` role transfer as role-assignment Evidence, or a non-Lead's
        attempt as ``swarm.role_refusal`` Evidence (ADR-0012 §1)."""

    async def post_pr_review(self, request: PullRequestReviewInput) -> PullRequestReviewOutput:
        """The `pr.comment` seam: the Critic's `verdict` as a PR review (PRD issue 54)."""

    async def record_critic_veto(self, request: CriticVetoEventInput) -> CriticVetoEventOutput:
        """Record one Critic veto Evidence Event (PRD issue 54)."""

    async def assemble_learning_inputs(self, request: LearningInputsInput) -> LearningInputsOutput:
        """Assemble the Learning Directive's inputs and record it started (PRD issue 55)."""

    async def execute_learning_directive(
        self, request: LearningDirectiveInput
    ) -> LearningDirectiveOutput:
        """Run the terminal Learning Directive as the Contract's Learner (PRD issue 55)."""

    async def record_learning_outcome(self, request: LearningOutcomeInput) -> LearningOutcomeOutput:
        """Write the proposed Lessons and the Directive's `_ended` Event (PRD issue 55)."""

    async def record_budget_decrement(
        self,
        request: BudgetDecrementInput,
    ) -> BudgetDecrementOutput:
        """Append a per-Directive Budget decrement Evidence Event (ADR-0007)."""

    async def run_verifier(self, request: VerifierRunInput) -> VerifierRunOutput:
        """Run the verifier through a future worker activity, before the human gate."""

    async def close_success(self, request: SuccessCloseInput) -> SuccessCloseOutput:
        """Close the work record after successful verification."""

    async def record_terminal_workflow_failure(
        self,
        request: TerminalWorkflowFailureInput,
    ) -> TerminalWorkflowFailureOutput:
        """Record terminal workflow failure evidence through control-plane APIs."""

    async def create_failed_verification_incident(
        self,
        request: FailedVerificationIncidentInput,
    ) -> FailedVerificationIncidentOutput:
        """Create an incident after failed verification."""

    async def raise_budget_confirmation(self, request: BudgetRaiseInput) -> BudgetRaiseOutput:
        """Ask the Agent's owner to raise the spent token Budget or end the work."""

    async def record_owner_confirmation_timeout(
        self,
        request: OwnerConfirmationTimeoutInput,
    ) -> OwnerConfirmationTimeoutOutput:
        """Record that an Owner Confirmation's 24 h window ran out (ADR-0011 s15)."""

    async def send_owner_confirmation_reminder(
        self,
        request: OwnerConfirmationReminderInput,
    ) -> OwnerConfirmationReminderOutput:
        """Remind the owner at *deadline - lead* that a request is still waiting."""

    async def end_work_record_on_owner_decision(
        self,
        request: OwnerEndingInput,
    ) -> OwnerEndingOutput:
        """Close the PR, keep the branch and end the Work Record (ADR-0011 s15)."""

    async def read_contract_gate(self, request: ContractGateInput) -> ContractGateOutput:
        """Read the Contract's state from the grant snapshot before the next Directive."""

    async def read_fair_use_gate(self, request: FairUseGateInput) -> FairUseGateOutput:
        """Read the Organisation's daily Directive fair-use ceiling (PRD issue 26 AC4)."""

    async def wipe_contract_residue(self, request: ContractResidueInput) -> ContractResidueOutput:
        """Delete everything one Contract left on the Runner, and retire its uid."""

    async def end_work_record_on_contract_end(
        self,
        request: ContractEndInput,
    ) -> ContractEndOutput:
        """End the Work Record ``ENDED / contract_terminated``, leaving the PR open."""

    async def evaluate_work_accept(self, request: WorkAcceptGateInput) -> WorkAcceptGateOutput:
        """Evaluate `work.accept` before the first Directive dispatches (PRD issue 29)."""

    async def route_directive(self, request: DirectiveRoutingInput) -> DirectiveRoutingOutput:
        """Pick the Runner for the next Directive (PRD issue 42, control-plane-side)."""

    async def reassemble_workspace(
        self,
        request: WorkspaceReassemblyInput,
    ) -> WorkspaceReassemblyOutput:
        """Re-clone a re-routed Work Record's branch onto the Runner now serving it."""

    async def end_work_record_on_runner_unavailable(
        self,
        request: RunnerUnavailableInput,
    ) -> RunnerUnavailableOutput:
        """End the Work Record ``ENDED / runner_unavailable`` -- no Incident (25 §9)."""

    async def return_work_record_to_intake_lead(
        self,
        request: WorkAcceptReturnInput,
    ) -> WorkAcceptReturnOutput:
        """`deny`, a refusal or a timeout: hand the Work Record back to its creator."""

    async def open_question(self, request: QuestionOpenInput) -> QuestionOpenOutput:
        """Route an allowed Question and start its 24 h window (PRD issue 60)."""

    async def record_question_timeout(self, request: QuestionTimeoutInput) -> QuestionTimeoutOutput:
        """The window ran out: `no_answer`, unless held, answered or voided meanwhile."""

    async def void_question(self, request: QuestionVoidInput) -> QuestionVoidOutput:
        """The owner refused `work.ask: confirm`: the Question is never asked."""


@dataclass(frozen=True)
class SeatSyncInput:
    """Input for prorating a payer Account's licensed seat quantity (PRD issue 25).

    ``contract_id`` is carried for correlation only (Evidence, logs); the activity acts
    on ``subscription_id`` alone. ``delta`` is +1 on Contract activation, -1 on
    termination -- never anything else, so the activity has nothing to validate.
    """

    contract_id: str
    subscription_id: str
    delta: int


@dataclass(frozen=True)
class SeatSyncOutput:
    adjusted: bool


class SeatSyncActivities(Protocol):
    """Single-activity boundary for the seat-sync workflow (PRD issue 25)."""

    async def adjust_seat_quantity(self, request: SeatSyncInput) -> SeatSyncOutput:
        """Call back into the backend, which prorates the quantity on Stripe."""


@dataclass(frozen=True)
class PlanReconcileOutput:
    """The daily reconcile's own summary (PRD issue 25), for its own Evidence-adjacent log."""

    accounts_checked: int
    accounts_corrected: int


class PlanReconcileActivities(Protocol):
    """Single-activity boundary for the daily Plan reconcile (PRD issue 25)."""

    async def reconcile_account_plans(self) -> PlanReconcileOutput:
        """Call back into the backend, which corrects every Account's mirror from Stripe."""


@dataclass(frozen=True)
class NamespaceReconcileOutput:
    """The namespace reconcile's own summary (PRD issue 38): pending rows it retried."""

    checked: int
    flipped_to_ready: int


class NamespaceReconcileActivities(Protocol):
    """Single-activity boundary for the periodic namespace reconcile (PRD issue 38)."""

    async def reconcile_pending_namespaces(self) -> NamespaceReconcileOutput:
        """Call back into the backend, which retries `ensure_namespace` for every pending
        Organisation ("a system-worker retry reconciles pending rows")."""


class WorkspaceRetentionActivities(Protocol):
    """Single-activity boundary for the periodic Workspace retention delete (issue 30)."""

    async def delete_expired_workspaces(self) -> WorkspaceRetentionOutput:
        """Delete every Workspace on this Runner whose Work Record is past retention."""
