"""The frontier rule, the park closure and the Epic's outcome, as pure functions (PRD
issue 59, map 19 A2-A3, A5): what the deterministic workflow decides from the board."""

from __future__ import annotations

from agentic_runner_contracts.epics import (
    EPIC_WALL_CLOCK_UNBOUNDED_SECONDS,
    ChildCard,
    EpicBoard,
    epic_outcome,
    frontier,
    parked_closure,
)


def _board(*cards: ChildCard) -> EpicBoard:
    return EpicBoard(children=cards)


def test_a_child_with_no_edges_starts_at_once_and_a_blocked_one_waits_for_done() -> None:
    board = _board(
        ChildCard(work_record_id="A", title="a", status="QUEUED"),
        ChildCard(work_record_id="B", title="b", status="QUEUED", blocked_by=("A",)),
        ChildCard(work_record_id="C", title="c", status="QUEUED", based_on="A"),
    )

    assert frontier(board) == (("A", "no blockers"),)

    # A's PR is open: the stacked child starts, the blocked one still waits for DONE.
    board = _board(
        ChildCard(work_record_id="A", title="a", status="IN_PROGRESS", pr_number=7),
        ChildCard(work_record_id="B", title="b", status="QUEUED", blocked_by=("A",)),
        ChildCard(work_record_id="C", title="c", status="QUEUED", based_on="A"),
    )
    assert frontier(board) == (("C", "based_on A has PR #7 open"),)

    board = _board(
        ChildCard(work_record_id="A", title="a", status="DONE", pr_number=7, pr_merged=True),
        ChildCard(work_record_id="B", title="b", status="QUEUED", blocked_by=("A",)),
        ChildCard(work_record_id="C", title="c", status="IN_PROGRESS", pr_number=8, based_on="A"),
    )
    assert frontier(board) == (("B", "blocked_by A is DONE"),)


def test_a_blocker_that_ended_any_other_way_never_releases_its_dependents() -> None:
    board = _board(
        ChildCard(work_record_id="A", title="a", status="ENDED", end_reason="needs_human"),
        ChildCard(work_record_id="B", title="b", status="QUEUED", blocked_by=("A",)),
    )

    assert frontier(board) == ()
    assert epic_outcome(board) == "stalled"


def test_parking_a_child_parks_everything_behind_it_transitively() -> None:
    board = _board(
        ChildCard(work_record_id="A", title="a", status="DONE"),
        ChildCard(work_record_id="B", title="b", status="IN_PROGRESS", blocked_by=("A",)),
        ChildCard(work_record_id="C", title="c", status="QUEUED", based_on="B"),
        ChildCard(work_record_id="D", title="d", status="QUEUED", blocked_by=("C",)),
        ChildCard(work_record_id="E", title="e", status="QUEUED"),
    )

    assert parked_closure(board, "B") == ("B", "C", "D")


def test_the_epic_is_done_only_when_every_child_is_done() -> None:
    assert epic_outcome(EpicBoard()) == "stalled"
    assert (
        epic_outcome(
            _board(
                ChildCard(work_record_id="A", title="a", status="DONE"),
                ChildCard(work_record_id="B", title="b", status="DONE"),
            )
        )
        == "done"
    )
    assert (
        epic_outcome(
            _board(
                ChildCard(work_record_id="A", title="a", status="DONE"),
                ChildCard(work_record_id="B", title="b", status="IN_PROGRESS"),
            )
        )
        == "open"
    )


def test_the_epics_wall_clock_default_is_unbounded_for_any_real_work_record() -> None:
    assert EPIC_WALL_CLOCK_UNBOUNDED_SECONDS >= 100 * 365 * 24 * 3600
