"""The Learner prompt names the occasion: a Work Record's ending, or an Assignee's hand-off.

agentic-os sends ``ending_kind=HANDOFF`` for the leaving Assignee's Learning Directive
(QTS-1566). The Work Record goes on, so its prompt must not say it ended (QTS-1574).
"""

from __future__ import annotations

from agentic_runner.activities import _learning_prompt
from agentic_runner_contracts.activity_io import LearningDirectiveInput


def _prompt(ending_kind: str, end_reason: str = "") -> str:
    request = LearningDirectiveInput(
        work_record_id="wr-1",
        repository="acme/app",
        base_ref="main",
        directive_number=3,
        learner_agent_id="learner-1",
        ending_kind=ending_kind,
        end_reason=end_reason,
    )
    return _learning_prompt(request, base_branch="main", transcript="")


def test_handoff_prompt_does_not_say_the_work_record_ended() -> None:
    prompt = _prompt("HANDOFF")

    assert "has ended" not in prompt
    assert "no longer its Assignee" in prompt
    assert "Propose Lessons from its own part of the work" in prompt


def test_terminal_prompt_says_the_work_record_ended_with_its_reason() -> None:
    prompt = _prompt("FAILED", "budget exhausted")

    assert prompt.startswith(
        "You are the Learner. This Work Record has ended (FAILED / budget exhausted). "
        "Propose Lessons a future Directive of the same Persona should know -- from a "
        "failure as much as from a success."
    )
    assert "Assignee" not in prompt
