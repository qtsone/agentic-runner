from uuid import UUID

from agentic_runner_contracts import public_metadata

ROUTINE_ID = UUID("6f1c2b7e-3a4d-4e5f-8a9b-0c1d2e3f4a5b")


def test_routine_schedule_id_is_built_from_the_routine_id_only() -> None:
    assert public_metadata.routine_schedule_id(routine_id=ROUTINE_ID) == f"routine.{ROUTINE_ID}"


def test_routine_schedule_id_is_stable_per_routine() -> None:
    first = public_metadata.routine_schedule_id(routine_id=ROUTINE_ID)
    assert public_metadata.routine_schedule_id(routine_id=ROUTINE_ID) == first
