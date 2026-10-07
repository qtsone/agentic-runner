"""What a Question's wake tells the Agent (PRD issue 60): the answer, or its absence and
the two moves silence leaves -- proceed on a stated assumption, or park."""

from __future__ import annotations

from agentic_runner.activities import _member_directive_prompt, _with_question
from agentic_runner_contracts.activity_io import FixDirectiveOutput, QuestionAsked


def _prompt(question: dict[str, object]) -> str:
    return _member_directive_prompt(
        role="lead",
        wake_reason="question_answered",
        pending=[],
        completion_criteria="Rotate the key",
        question=question,
    )


def test_an_answered_question_rides_in_with_its_answer() -> None:
    prompt = _prompt(
        {
            "id": "q-1",
            "text": "Which key first?",
            "addressed_to": "requester",
            "outcome": "answered",
            "answer_text": "Staging.",
        }
    )

    assert "Which key first?" in prompt
    assert "The requester answered:\nStaging." in prompt


def test_silence_names_both_moves_and_never_an_ending() -> None:
    prompt = _prompt(
        {"id": "q-1", "text": "Which key?", "addressed_to": "owner", "outcome": "no_answer"}
    )

    assert "No answer came from the owner within the 24-hour window" in prompt
    assert "assumption you state explicitly" in prompt
    assert '{"end_epic": true}' in prompt and ".agentic-os/plan.json" in prompt


def test_a_plain_wake_is_unchanged_by_the_question_seam() -> None:
    plain = _member_directive_prompt(
        role="lead", wake_reason="members_idle", pending=[], completion_criteria="x"
    )

    assert "You asked" not in plain


def test_the_question_rides_out_on_the_output_only_when_one_was_asked() -> None:
    ran = FixDirectiveOutput(
        work_record_id="wr",
        repository="r",
        pr_number=1,
        directive_number=2,
        branch_head_sha="",
        summary="",
    )

    assert _with_question(ran, []) is ran
    assert _with_question(ran, [QuestionAsked(question_id="q-9")]).question == QuestionAsked(
        question_id="q-9"
    )
