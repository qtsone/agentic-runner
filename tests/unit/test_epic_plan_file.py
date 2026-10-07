"""The Runner's half of charting (PRD issue 59): the plan file a Lead's Directive writes
is parsed, bounded and removed before anything is committed, and the board rides into a
coordination Directive's prompt as structured input the way Messages do."""

from __future__ import annotations

import json
from pathlib import Path

from agentic_runner.activities import (
    _PLAN_FILE,
    _member_directive_prompt,
    _read_plan,
    _take_plan,
)
from agentic_runner_contracts.epics import (
    PLAN_EDGES_MAX,
    PLAN_ID_MAX,
    PLAN_TITLE_MAX,
    ChildCard,
    EpicBoard,
)


def test_a_plan_file_is_parsed_and_taken_off_the_workspace(tmp_path: Path) -> None:
    path = tmp_path / _PLAN_FILE
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "children": [
                    {"key": "a", "title": "auth: token refresh", "channel_id": "chan-1"},
                    {
                        "key": "b",
                        "title": "auth: rotate",
                        "description": "after a",
                        "channel_id": "chan-1",
                        "blocked_by": ["a"],
                        "based_on": "a",
                        "budget_max_tokens": 5000,
                    },
                    {"key": "c", "title": "no channel: the Epic's default fills it"},
                    {"key": "d", "channel_id": "chan-1"},
                ],
                "endings": [{"work_record_id": "wr-old", "end_reason": "needs_human"}],
                "end_epic": False,
            }
        ),
        encoding="utf-8",
    )

    plan = _take_plan(tmp_path)

    assert plan is not None
    assert [child.key for child in plan.children] == ["a", "b", "c"]
    assert plan.children[2].channel_id == ""
    assert plan.children[1].blocked_by == ("a",)
    assert plan.children[1].based_on == "a"
    assert plan.children[1].budget_max_tokens == 5000
    assert plan.endings[0].work_record_id == "wr-old"
    assert plan.end_epic is False
    # Never committed with the change: the file is gone once it has been read.
    assert not path.exists()


def test_a_missing_or_empty_plan_decides_nothing(tmp_path: Path) -> None:
    assert _read_plan(tmp_path / _PLAN_FILE) is None
    path = tmp_path / _PLAN_FILE
    path.parent.mkdir(parents=True)
    path.write_text("{}", encoding="utf-8")
    assert _read_plan(path) is None
    path.write_text("not json", encoding="utf-8")
    assert _read_plan(path) is None


def test_a_plan_is_clamped_to_the_control_planes_bounds_rather_than_rejected(
    tmp_path: Path,
) -> None:
    # `submit_epic_plan` refuses beyond these with a 422 that ends the Epic in an
    # Incident, so the parser trims what the validator would reject.
    path = tmp_path / _PLAN_FILE
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "children": [
                    {
                        "key": "k" * 100,
                        "title": "t" * 300,
                        "channel_id": "c" * 100,
                        "blocked_by": ["b" * 100] * 70,
                        "based_on": "a" * 100,
                        "budget_max_directives": 0,
                        "budget_max_wall_clock_seconds": -1,
                        "budget_max_tokens": -5,
                    }
                ],
                "endings": [{"work_record_id": "w" * 100, "end_reason": "r" * 100}],
            }
        ),
        encoding="utf-8",
    )

    plan = _read_plan(path)

    assert plan is not None
    (child,) = plan.children
    assert len(child.key) == len(child.channel_id) == len(child.based_on) == PLAN_ID_MAX
    assert len(child.title) == PLAN_TITLE_MAX
    assert len(child.blocked_by) == PLAN_EDGES_MAX
    assert all(len(item) == PLAN_ID_MAX for item in child.blocked_by)
    assert child.budget_max_directives is None
    assert child.budget_max_wall_clock_seconds is None
    assert child.budget_max_tokens is None
    (ending,) = plan.endings
    assert len(ending.work_record_id) == PLAN_ID_MAX
    assert ending.end_reason == "needs_human"


def test_the_board_rides_into_a_coordination_prompt_and_a_plain_one_is_unchanged() -> None:
    plain = _member_directive_prompt(
        role="lead", wake_reason="members_idle", pending=[], completion_criteria="ship it"
    )
    coordinating = _member_directive_prompt(
        role="lead",
        wake_reason="child_ended",
        pending=[],
        completion_criteria="ship it",
        board=EpicBoard(
            children=(ChildCard(work_record_id="wr-a", title="auth", status="DONE", pr_number=3),)
        ),
    )

    assert "Board" not in plain and str(_PLAN_FILE) not in plain
    assert '"work_record_id": "wr-a"' in coordinating
    assert str(_PLAN_FILE) in coordinating
    # The pipeline rules a plain member is told are told to a coordinating Lead too.
    assert coordinating.endswith(plain[plain.index("Act on what is asked") :])


def test_a_reports_work_end_tells_its_report_from_parking_it_for_a_person(
    tmp_path: Path,
) -> None:
    """Console-v2 issue 23: a bare `work.end` ends a report on its closing `result`, and
    ``needs_human`` beside it parks the report instead."""

    path = tmp_path / _PLAN_FILE
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"end_epic": True}), encoding="utf-8")
    ended = _read_plan(path)
    path.write_text(json.dumps({"end_epic": True, "needs_human": True}), encoding="utf-8")
    parked = _read_plan(path)

    assert ended is not None and (ended.end_epic, ended.needs_human) == (True, False)
    assert parked is not None and (parked.end_epic, parked.needs_human) == (True, True)


def test_a_report_members_prompt_says_how_a_report_ends_and_asks_for_no_commit() -> None:
    prompt = _member_directive_prompt(
        role="lead",
        wake_reason="report",
        pending=[],
        completion_criteria="A weekly summary of the Product's open work",
        report=True,
    )

    assert "ends in a written report, not a pull request" in prompt
    assert '{"end_epic": true, "needs_human": true}' in prompt
    assert "the pipeline commits, pushes" not in prompt
