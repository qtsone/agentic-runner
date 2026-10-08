"""Epics: the board, the plan and the frontier rule (PRD issue 59, ADR-0012 §5).

An Epic is a Work Record whose Outcome is its children's completion. Its Lead **charts**
the children up front -- a plan the coordination Directive returns -- and the platform
computes the **frontier**: which `QUEUED` child starts, and why, from the board alone.
Both are workflow state, so the shapes live here, sandbox-safe (stdlib only): the
deterministic workflow decides *when* a child starts and never spends a Directive on it
(map ticket 19 A2), the control plane performs the start, and the console draws the same
board.

Two edge kinds (19 A2-A3): ``blocked_by`` -- the child starts when the sibling reached a
successful terminal state -- and ``based_on`` -- the child stacks on the sibling's branch
and starts as soon as the sibling's PR is open, not merged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

__all__ = [
    "EDGE_BASED_ON",
    "EDGE_BLOCKED_BY",
    "END_REASON_CHILDREN_PARKED",
    "END_REASON_NEEDS_HUMAN",
    "EPIC_WALL_CLOCK_UNBOUNDED_SECONDS",
    "KIND_EPIC",
    "KIND_REPORT",
    "KIND_WORK",
    "PLAN_CHILDREN_MAX",
    "PLAN_EDGES_MAX",
    "PLAN_FILE",
    "PLAN_ID_MAX",
    "PLAN_TEXT_MAX",
    "PLAN_TITLE_MAX",
    "STATUS_DONE",
    "STATUS_QUEUED",
    "TERMINAL_STATUSES",
    "WAKE_CHART",
    "WAKE_CHILD_ENDED",
    "WAKE_CHILDREN_DONE",
    "WAKE_REPORT",
    "WORK_END_VERB",
    "WORK_OPEN_VERB",
    "ChildCard",
    "EpicBoard",
    "EpicPlan",
    "PlannedChild",
    "PlannedEnding",
    "epic_outcome",
    "frontier",
    "parked_closure",
]

KIND_WORK: Final = "work"
KIND_EPIC: Final = "epic"
# Console-v2 issue 23: a Work Record whose Outcome is its Lead's closing `result` Message
# on the Conversation -- no branch, no PR, no Verification. It ends through the same
# `work.end` seam an Epic does, which is why the kind lives here.
KIND_REPORT: Final = "report"

EDGE_BLOCKED_BY: Final = "blocked_by"
EDGE_BASED_ON: Final = "based_on"

# `work.open` on the `channel` resource, `work.end` on `work` (ADR-0011, ticket 19 A1/A5).
WORK_OPEN_VERB: Final = "work.open"
WORK_END_VERB: Final = "work.end"

# The two `end_reason` values this slice adds (19 A5): a child parked for a person, and
# an Epic its Lead ended with children parked or ended.
END_REASON_NEEDS_HUMAN: Final = "needs_human"
END_REASON_CHILDREN_PARKED: Final = "children_parked"

STATUS_QUEUED: Final = "QUEUED"
STATUS_DONE: Final = "DONE"
TERMINAL_STATUSES: Final = frozenset({"DONE", "FAILED", "INCIDENT", "ENDED"})

# Wake reasons the Epic's Lead is woken with (19 A2), beside the Swarm's own.
WAKE_CHART: Final = "chart"
WAKE_CHILD_ENDED: Final = "child_ended"
WAKE_CHILDREN_DONE: Final = "children_done"
# A report's Lead is woken first with this one (console-v2 issue 23).
WAKE_REPORT: Final = "report"

# "Wall-clock defaults to unbounded on an Epic" (19 A5): its clock is the org's human
# review time, weeks of it. A finite value so the Budget stays JSON-safe on the wire;
# a hundred years is unbounded for any Work Record that will ever exist.
EPIC_WALL_CLOCK_UNBOUNDED_SECONDS: Final = 100 * 365 * 24 * 60 * 60.0

# Where the Lead's Directive writes its plan in the Workspace, and the Runner reads it.
PLAN_FILE: Final = ".agentic-os/plan.json"

# The bounds a plan is held to. The Runner's parser clamps to these and the control
# plane's `submit_epic_plan` validator rejects beyond them, so they live here where both
# read them: a Lead writing a long title must be trimmed, never turned into a backend
# rejection that ends the Epic in an Incident.
PLAN_CHILDREN_MAX: Final = 256
PLAN_TITLE_MAX: Final = 256
PLAN_TEXT_MAX: Final = 4000
PLAN_ID_MAX: Final = 64
PLAN_EDGES_MAX: Final = 64


@dataclass(frozen=True)
class PlannedChild:
    """One child the Lead charts. ``key`` is plan-local; ``blocked_by`` / ``based_on``
    name sibling keys or the ids of children that already exist (a re-chart)."""

    key: str
    title: str
    description: str
    channel_id: str
    blocked_by: tuple[str, ...] = ()
    based_on: str = ""
    budget_max_directives: int | None = None
    budget_max_wall_clock_seconds: float | None = None
    budget_max_tokens: int | None = None


@dataclass(frozen=True)
class PlannedEnding:
    """`work.end` on one child: ``needs_human`` parks it (19 A5)."""

    work_record_id: str
    end_reason: str = END_REASON_NEEDS_HUMAN


@dataclass(frozen=True)
class EpicPlan:
    """What a coordination Directive returned: children to open, children to end, and
    whether the Lead ends the Epic itself (`work.end` on its own Work Record).

    On a report (console-v2 issue 23) ``end_epic`` ends it on the Lead's closing `result`
    Message, and ``needs_human`` with it parks it for a person instead -- the one ending
    a report's `work.end` has to tell apart, since a plain Work Record's bare `work.end`
    already means `needs_human`."""

    children: tuple[PlannedChild, ...] = ()
    endings: tuple[PlannedEnding, ...] = ()
    end_epic: bool = False
    needs_human: bool = False


@dataclass(frozen=True)
class ChildCard:
    """One child as the board shows it: state, PR, edges (foreman's report shape)."""

    work_record_id: str
    title: str
    status: str
    pr_number: int = 0
    pr_merged: bool = False
    branch_name: str = ""
    lead_agent_id: str = ""
    end_reason: str = ""
    blocked_by: tuple[str, ...] = ()
    based_on: str = ""

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def pr_open(self) -> bool:
        return self.pr_number > 0


@dataclass(frozen=True)
class EpicBoard:
    """The children, their states, PRs and edges, as the Lead's Directive sees them."""

    children: tuple[ChildCard, ...] = ()

    def child(self, work_record_id: str) -> ChildCard | None:
        for card in self.children:
            if card.work_record_id == work_record_id:
                return card
        return None

    @property
    def running(self) -> tuple[ChildCard, ...]:
        return tuple(
            card for card in self.children if not card.terminal and card.status != STATUS_QUEUED
        )

    @property
    def queued(self) -> tuple[ChildCard, ...]:
        return tuple(card for card in self.children if card.status == STATUS_QUEUED)


def frontier(board: EpicBoard) -> tuple[tuple[str, str], ...]:
    """Which `QUEUED` children start now, and the reason each one does (19 A2-A3).

    A child starts when every ``blocked_by`` sibling is ``DONE`` and its ``based_on``
    sibling -- if any -- has an open PR (or is already ``DONE``: the base merged before
    the dependent ever started). A blocker that ended any other way never satisfies the
    edge: the Lead decides what happens to the children behind it (19 A5).
    """

    started: list[tuple[str, str]] = []
    for card in board.queued:
        reasons: list[str] = []
        satisfied = True
        for blocker_id in card.blocked_by:
            blocker = board.child(blocker_id)
            if blocker is None or blocker.status != STATUS_DONE:
                satisfied = False
                break
            reasons.append(f"blocked_by {blocker_id} is DONE")
        if not satisfied:
            continue
        if card.based_on:
            base = board.child(card.based_on)
            if base is None or not (base.pr_open or base.status == STATUS_DONE):
                continue
            reasons.append(
                f"based_on {card.based_on} has PR #{base.pr_number} open"
                if base.pr_open
                else f"based_on {card.based_on} is DONE"
            )
        started.append((card.work_record_id, "; ".join(reasons) or "no blockers"))
    return tuple(started)


def parked_closure(board: EpicBoard, work_record_id: str) -> tuple[str, ...]:
    """Everything `blocked_by` a parked child parks too (19 A5), transitively, in board
    order; the parked child itself first. Only non-terminal children are listed."""

    parked = [work_record_id]
    changed = True
    while changed:
        changed = False
        for card in board.children:
            if card.work_record_id in parked or card.terminal:
                continue
            if any(blocker in parked for blocker in card.blocked_by) or card.based_on in parked:
                parked.append(card.work_record_id)
                changed = True
    return tuple(parked)


def epic_outcome(board: EpicBoard) -> str:
    """``done`` when every child is ``DONE``, ``open`` while any child may still get
    there, ``stalled`` when nothing runs, nothing can start and not every child is
    ``DONE`` -- the Lead decides (19 A5)."""

    if board.children and all(card.status == STATUS_DONE for card in board.children):
        return "done"
    if board.running or frontier(board):
        return "open"
    return "stalled"
