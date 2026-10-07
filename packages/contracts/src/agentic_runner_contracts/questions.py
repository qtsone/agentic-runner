"""The Question: an Agent asks a human, the Work Record waits one window (PRD issue 60).

The fifth Gate kind (ADR-0012), open to every Work Record. The Lead (or Liaison) calls
``agentic-runner ask`` from inside a Directive; `work.ask` on the `work` resource decides
it (`allow | confirm | deny`, default `allow`, ticket 19 A9); an allowed Question is
routed to the Requester through the Source or to the Agent's owner, and after the
Directive the loop holds on the ``question_answered`` signal with a 24 h timer (ticket 08's
window). An answer or the window running out wakes the Lead, which decides. **Nothing
ends on silence** (19): no ending is attached to a timeout.

A Question is a Work Record record, never a Message -- Messages are Agent-to-Agent (19).
Stdlib-only and sandbox-safe: the workflow imports these names.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from agentic_runner_contracts.public_metadata import signal_name

__all__ = [
    "ADDRESSED_OWNER",
    "ADDRESSED_REQUESTER",
    "OUTCOME_ANSWERED",
    "OUTCOME_NO_ANSWER",
    "OUTCOME_VOIDED",
    "QUESTION_ANSWERED_SIGNAL",
    "QUESTION_HOLD_RECHECK_SECONDS",
    "QUESTION_TEXT_MAX",
    "QUESTION_WINDOW_SECONDS",
    "WAKE_QUESTION_ANSWERED",
    "WAKE_QUESTION_NO_ANSWER",
    "WORK_ASK_VERB",
    "QuestionAnswered",
]

WORK_ASK_VERB: Final = "work.ask"

ADDRESSED_REQUESTER: Final = "requester"
ADDRESSED_OWNER: Final = "owner"

# `voided` is the Contract's answer, not the addressee's: a termination voids an open
# Question the way it voids an unanswered Owner Confirmation (PRD issue 12).
OUTCOME_ANSWERED: Final = "answered"
OUTCOME_NO_ANSWER: Final = "no_answer"
OUTCOME_VOIDED: Final = "voided"

# Ticket 08's one window, unchanged.
QUESTION_WINDOW_SECONDS: Final = 24 * 60 * 60
# A Question whose clock a Contract suspension paused cannot run out; the loop re-checks
# it at the Contract hold's own cadence rather than waiting out a deadline that moved.
QUESTION_HOLD_RECHECK_SECONDS: Final = 5 * 60
QUESTION_TEXT_MAX: Final = 4000

QUESTION_ANSWERED_SIGNAL: Final = signal_name("question_answered")

# Why the Lead is woken after the hold: prompt material and Evidence, never a branch.
WAKE_QUESTION_ANSWERED: Final = "question_answered"
WAKE_QUESTION_NO_ANSWER: Final = "question_no_answer"


@dataclass(frozen=True)
class QuestionAnswered:
    """The signal payload: which Question. Never the answer -- a signal is unauthenticated
    input, so the woken Directive reads the answer from the record."""

    question_id: str
