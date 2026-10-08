"""A retry is refused while the prior attempt's process group lives (runner-repo 05)."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from agentic_runner.activities import (
    PRIOR_ATTEMPT_ALIVE_EVIDENCE_SOURCE,
    RunnerRalphActivities,
)
from agentic_runner.attempts import (
    AttemptRecord,
    AttemptRecords,
    PriorAttemptAliveError,
    fence_work_record,
    process_start_time,
)
from agentic_runner.runtime import verifier_command
from agentic_runner.workers._runtime_support import run_subprocess_exec


class _RecordingClient:
    def __init__(self) -> None:
        self.evidence: list[dict[str, Any]] = []

    async def append_evidence(
        self, work_record_id: str, *, source: str, payload: dict[str, Any], **kwargs: Any
    ) -> dict[str, Any]:
        self.evidence.append(
            {"work_record_id": work_record_id, "source": source, "payload": dict(payload)}
        )
        return {}


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    root = tmp_path / "workspaces"
    (root / "wr-a").mkdir(parents=True)
    return root


@pytest.fixture
def records(tmp_path: Path, workspace_root: Path) -> AttemptRecords:
    return AttemptRecords(tmp_path / "state", workspace_root=workspace_root)


async def _spawn(argv: list[str], cwd: Path) -> None:
    await run_subprocess_exec(
        argv=argv, cwd=cwd, env=dict(os.environ), stdin=None, timeout_seconds=30,
        output_limit_bytes=1024,
    )  # fmt: skip


async def _wait_for(path: Path) -> None:
    for _ in range(200):
        if path.exists():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"{path} never appeared")


def _dead_pid() -> int:
    child = subprocess.Popen(["true"])
    child.wait()
    return child.pid


@pytest.mark.asyncio
async def test_a_second_attempt_is_refused_while_the_first_ones_child_sleeps(
    records: AttemptRecords, workspace_root: Path
) -> None:
    workspace = workspace_root / "wr-a"

    async def first_attempt() -> None:
        with fence_work_record(records, "wr-a", attempt=1):
            await _spawn(["sleep", "30"], workspace)

    first = asyncio.create_task(first_attempt())
    await _wait_for(records.path("wr-a"))

    with (
        fence_work_record(records, "wr-a", attempt=2),
        pytest.raises(PriorAttemptAliveError) as refused,
    ):
        await _spawn(["true"], workspace)  # fmt: skip
    assert refused.value.work_record_id == "wr-a"
    assert refused.value.prior.attempt == 1

    # Another Work Record's Workspace is not this one's business.
    with fence_work_record(records, "wr-b", attempt=1):
        await _spawn(["true"], workspace_root)

    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    # The group was killed with the cancel, so its record went with it.
    assert not records.path("wr-a").exists()
    with fence_work_record(records, "wr-a", attempt=3):
        await _spawn(["true"], workspace)


@pytest.mark.asyncio
async def test_the_refusal_is_retryable_and_names_the_running_attempt_in_evidence(
    records: AttemptRecords, workspace_root: Path
) -> None:
    sleeper = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        started = process_start_time(sleeper.pid)
        assert started is not None
        records.path("wr-a").parent.mkdir(parents=True)
        records.path("wr-a").write_text(
            json.dumps({"pgid": sleeper.pid, "start_time": started, "attempt": 4})
        )
        client = _RecordingClient()
        activities = RunnerRalphActivities(client, attempt_records=records)

        # A plain exception, not a non-retryable ApplicationError: Temporal retries it.
        with pytest.raises(PriorAttemptAliveError):
            async with activities._fenced("wr-a"):
                await _spawn(["true"], workspace_root / "wr-a")
    finally:
        sleeper.kill()
        sleeper.wait()

    [evidence] = client.evidence
    assert evidence["work_record_id"] == "wr-a"
    assert evidence["source"] == PRIOR_ATTEMPT_ALIVE_EVIDENCE_SOURCE
    assert evidence["payload"]["prior_attempt"] == 4
    assert evidence["payload"]["prior_pgid"] == sleeper.pid


def test_the_verifier_is_fenced_too(records: AttemptRecords, workspace_root: Path) -> None:
    sleeper = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        started = process_start_time(sleeper.pid)
        assert started is not None
        records.path("wr-a").parent.mkdir(parents=True)
        records.path("wr-a").write_text(
            json.dumps({"pgid": sleeper.pid, "start_time": started, "attempt": 1})
        )
        with fence_work_record(records, "wr-a", attempt=2), pytest.raises(PriorAttemptAliveError):
            verifier_command._subprocess_runner(
                ("true",), cwd=workspace_root, env=dict(os.environ), timeout_seconds=5
            )
    finally:
        sleeper.kill()
        sleeper.wait()

    # And once the prior group is gone, the verifier runs and leaves no record behind.
    with fence_work_record(records, "wr-a", attempt=3):
        completed = verifier_command._subprocess_runner(
            ("true",), cwd=workspace_root, env=dict(os.environ), timeout_seconds=5
        )
    assert completed.returncode == 0
    assert not records.path("wr-a").exists()


def test_a_stale_record_of_a_dead_pid_does_not_block(records: AttemptRecords) -> None:
    path = records.path("wr-a")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"pgid": _dead_pid(), "start_time": "1", "attempt": 1}))

    records.refuse_if_alive("wr-a")

    assert not path.exists()


def test_a_recycled_pid_with_another_start_time_does_not_block(records: AttemptRecords) -> None:
    # This process is alive, but it is not the process the record names: a recycled pid.
    path = records.path("wr-a")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"pgid": os.getpid(), "start_time": "not-ours", "attempt": 1}))

    records.refuse_if_alive("wr-a")

    assert not path.exists()


def test_runner_start_drops_dead_records_and_keeps_live_ones(records: AttemptRecords) -> None:
    live = records.path("wr-live")
    dead = records.path("wr-dead")
    live.parent.mkdir(parents=True)
    live.write_text(
        json.dumps(
            {"pgid": os.getpid(), "start_time": process_start_time(os.getpid()), "attempt": 1}
        )
    )
    dead.write_text(json.dumps({"pgid": _dead_pid(), "start_time": "1", "attempt": 1}))

    records.sweep()

    assert live.exists()
    assert not dead.exists()


@pytest.mark.asyncio
async def test_the_record_is_written_in_the_state_directory_never_the_workspace(
    tmp_path: Path, records: AttemptRecords, workspace_root: Path
) -> None:
    workspace = workspace_root / "wr-a"

    async def attempt() -> None:
        with fence_work_record(records, "wr-a", attempt=1):
            await _spawn(["sleep", "30"], workspace)

    running = asyncio.create_task(attempt())
    try:
        path = records.path("wr-a")
        await _wait_for(path)
        assert path.is_relative_to(tmp_path / "state")
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert json.loads(path.read_text())["attempt"] == 1
        assert list(workspace_root.rglob("*")) == [workspace]
    finally:
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running


def test_a_state_directory_under_the_workspace_root_is_refused(workspace_root: Path) -> None:
    with pytest.raises(ValueError, match="workspace root"):
        AttemptRecords(workspace_root / "wr-a" / ".state", workspace_root=workspace_root)


def test_a_work_record_id_cannot_name_a_path_outside_the_records(
    records: AttemptRecords,
) -> None:
    with pytest.raises(ValueError, match="not a file name"):
        records.path("../escape")


def test_the_start_time_reader_tells_a_live_process_from_a_dead_one() -> None:
    # Runs on the Linux test job (`/proc/<pid>/stat`) and on the macOS workstation job
    # (`ps -o lstart=`), so both readers are exercised.
    started = process_start_time(os.getpid())
    assert started is not None
    assert process_start_time(os.getpid()) == started
    assert process_start_time(_dead_pid()) is None
    child = subprocess.Popen(["sleep", "30"])
    try:
        assert process_start_time(child.pid) not in (None, "")
    finally:
        child.kill()
        child.wait()


@pytest.mark.skipif(sys.platform != "linux", reason="the /proc reader")
def test_the_linux_reader_is_field_22_of_proc_stat() -> None:
    assert process_start_time(os.getpid()) == _proc_field_22(os.getpid())


@pytest.mark.skipif(sys.platform != "darwin", reason="the ps reader")
def test_the_macos_reader_is_ps_lstart() -> None:
    expected = subprocess.run(
        ["ps", "-o", "lstart=", "-p", str(os.getpid())], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert process_start_time(os.getpid()) == expected


def _proc_field_22(pid: int) -> str:
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return fields[19]


def test_an_attempt_record_round_trips(records: AttemptRecords) -> None:
    record = records.record("wr-a", pgid=os.getpid(), attempt=7)
    assert record == AttemptRecord(
        pgid=os.getpid(), start_time=str(process_start_time(os.getpid())), attempt=7
    )
    with pytest.raises(PriorAttemptAliveError):
        records.refuse_if_alive("wr-a")
