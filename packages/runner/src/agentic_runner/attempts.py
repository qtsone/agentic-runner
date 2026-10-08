"""One live process group per Work Record, fenced across activity attempts (runner-repo 05).

A lost heartbeat makes Temporal schedule a retry while the first attempt's coroutine,
and the harness it spawned, may still be running in this same process. Both would then
write one Workspace, which ADR-0015 §2 and ADR-0012 assume never happens. So every process
group a Work Record's Directive, verifier run or Runner Hook spawns is recorded as
``{pgid, start_time, attempt}`` under the Runner's state directory, and the next spawn
for that Work Record is refused while the recorded group's leader is still alive.

Liveness is pid *plus* start time, as Paperclip fences its restarts: a pid alone is
recycled, and a record naming a recycled pid must not block a Work Record forever. The
record lives in the state directory, never the Workspace, because the Agent writes the
Workspace and could forge or delete its own fence there.

The binding is a context variable rather than a parameter: the spawn sites sit behind
the Agent Runtime port, the Runner Hook runner and the verifier's command runner, and
none of them knows which Work Record it serves. ``asyncio.to_thread`` copies the
context, so the verifier's thread sees the activity's binding too.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Iterator
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Final

__all__ = [
    "ATTEMPTS_DIR",
    "AttemptRecord",
    "AttemptRecords",
    "PriorAttemptAliveError",
    "fence_work_record",
    "process_start_time",
    "recorded_group",
    "refuse_if_prior_attempt_alive",
]

ATTEMPTS_DIR: Final[str] = "attempts"
_DIR_MODE: Final[int] = 0o700
_FILE_MODE: Final[int] = 0o600
# A Work Record id names a file here, so it must not be able to name one anywhere else.
_SAFE_ID: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]*")


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    pgid: int
    start_time: str
    attempt: int


class PriorAttemptAliveError(Exception):
    """A spawn was refused: an earlier attempt's process group for this Work Record lives."""

    def __init__(self, work_record_id: str, prior: AttemptRecord) -> None:
        super().__init__(
            f"work record {work_record_id}: attempt {prior.attempt} still runs "
            f"(process group {prior.pgid})"
        )
        self.work_record_id = work_record_id
        self.prior = prior


def process_start_time(pid: int) -> str | None:
    """When ``pid`` started, in the platform's own units, or None if no such process.

    Only ever compared for equality with an earlier reading of the same pid on the same
    host, so the units need not agree across platforms: clock ticks since boot on Linux,
    the ``lstart`` text on macOS (one-second resolution, which a pid recycled within the
    same second would defeat -- not a case a harness spawn produces).
    """

    if sys.platform == "linux":
        return _linux_start_time(pid)
    return _ps_start_time(pid)


def _linux_start_time(pid: int) -> str | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except (FileNotFoundError, ProcessLookupError):
        return None
    # Field 2 is the command name in parentheses and may itself hold spaces or ')', so
    # the fields are counted from after its *last* ')': field 3 is index 0, 22 is 19.
    return stat[stat.rindex(")") + 2 :].split()[19]


def _ps_start_time(pid: int) -> str | None:
    completed = subprocess.run(
        ["ps", "-o", "lstart=", "-p", str(pid)],
        capture_output=True,
        text=True,
        check=False,
    )
    started = completed.stdout.strip()
    return started if completed.returncode == 0 and started else None


class AttemptRecords:
    """The ``attempts/`` directory of one Runner's state directory."""

    def __init__(self, state_dir: Path, *, workspace_root: Path) -> None:
        self._dir = state_dir / ATTEMPTS_DIR
        if self._dir.resolve().is_relative_to(workspace_root.resolve()):
            raise ValueError(
                f"attempt records at {self._dir} would sit under the workspace root "
                f"{workspace_root}, where an Agent could forge them"
            )

    def path(self, work_record_id: str) -> Path:
        if not _SAFE_ID.fullmatch(work_record_id):
            raise ValueError(f"work record id is not a file name: {work_record_id!r}")
        return self._dir / f"{work_record_id}.json"

    def refuse_if_alive(self, work_record_id: str) -> None:
        """Raise if a recorded group for this Work Record lives; clear a stale record."""

        path = self.path(work_record_id)
        record = _read(path)
        if record is None:
            return
        if _alive(record):
            raise PriorAttemptAliveError(work_record_id, record)
        _unlink_if(path, record)

    def record(self, work_record_id: str, *, pgid: int, attempt: int) -> AttemptRecord | None:
        """Claim the Work Record for a just-spawned group; None if it already exited.

        Exclusive create, so two attempts that both passed the pre-spawn check in the
        same instant cannot both hold the claim: the second finds the first's record
        and is refused.
        """

        start_time = process_start_time(pgid)
        if start_time is None:
            return None
        record = AttemptRecord(pgid=pgid, start_time=start_time, attempt=attempt)
        path = self.path(work_record_id)
        self._dir.mkdir(mode=_DIR_MODE, parents=True, exist_ok=True)
        while True:
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, _FILE_MODE)
            except FileExistsError:
                self.refuse_if_alive(work_record_id)
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(asdict(record), handle)
            return record

    def release(self, work_record_id: str, record: AttemptRecord) -> None:
        """Remove our own record once its group leader has exited.

        Kept while the leader lives -- a cancel whose kill did not land -- so the next
        attempt is still refused rather than let in beside it.
        """

        if not _alive(record):
            _unlink_if(self.path(work_record_id), record)

    def sweep(self) -> None:
        """At Runner start: drop records whose process is gone, keep the live ones.

        A workstation Runner restarted without its children leaves them running in the
        Workspace; their records stay so the check still refuses until they exit.
        """

        if not self._dir.is_dir():
            return
        for path in self._dir.glob("*.json"):
            record = _read(path)
            if record is None or not _alive(record):
                path.unlink(missing_ok=True)


def _read(path: Path) -> AttemptRecord | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return AttemptRecord(
            pgid=int(payload["pgid"]),
            start_time=str(payload["start_time"]),
            attempt=int(payload["attempt"]),
        )
    except FileNotFoundError:
        return None
    except (ValueError, KeyError, TypeError):
        # Half-written by a Runner that died mid-write: it fences nothing it can name.
        return AttemptRecord(pgid=0, start_time="", attempt=0)


def _alive(record: AttemptRecord) -> bool:
    return record.pgid > 0 and process_start_time(record.pgid) == record.start_time


def _unlink_if(path: Path, record: AttemptRecord) -> None:
    # Another attempt may have replaced a stale record between our read and this unlink;
    # only the record we judged is ours to remove.
    if _read(path) == record:
        path.unlink(missing_ok=True)


@dataclass(frozen=True, slots=True)
class _Fence:
    records: AttemptRecords
    work_record_id: str
    attempt: int


_FENCE: ContextVar[_Fence | None] = ContextVar("agentic_runner_attempt_fence", default=None)


@contextlib.contextmanager
def fence_work_record(
    records: AttemptRecords, work_record_id: str, *, attempt: int
) -> Iterator[None]:
    """Fence every process group spawned inside this block to ``work_record_id``."""

    token = _FENCE.set(_Fence(records=records, work_record_id=work_record_id, attempt=attempt))
    try:
        yield
    finally:
        _FENCE.reset(token)


def refuse_if_prior_attempt_alive() -> None:
    """Called before a spawn; a no-op outside a fenced block."""

    fence = _FENCE.get()
    if fence is not None:
        fence.records.refuse_if_alive(fence.work_record_id)


@contextlib.contextmanager
def recorded_group(pgid: int) -> Iterator[None]:
    """Hold the Work Record's record for a spawned group while the block waits on it.

    Raises :class:`PriorAttemptAliveError` on entry if another attempt claimed the Work
    Record since the pre-spawn check; the caller kills the group it just spawned.
    """

    fence = _FENCE.get()
    record = (
        None
        if fence is None
        else fence.records.record(fence.work_record_id, pgid=pgid, attempt=fence.attempt)
    )
    try:
        yield
    finally:
        if fence is not None and record is not None:
            fence.records.release(fence.work_record_id, record)
