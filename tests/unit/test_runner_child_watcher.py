"""A stopped CLI must not wedge the Runner's event loop (QTS-1148).

The CLI stops itself, so the test never races the spawn for its pid; it writes the pid
first so a thread -- not the loop, which may be the thing that is frozen -- can wake it.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
import threading
import time
from asyncio import unix_events
from pathlib import Path

import pytest

from agentic_runner.child_watcher import install_stop_tolerant_child_watcher
from agentic_runner.workers._runtime_support import run_subprocess_exec

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin" or getattr(unix_events, "_ThreadedChildWatcher", None) is None,
    reason="the wedge needs macOS's waitid and Python >= 3.14's threaded child watcher",
)


def _release_later(pid_file: Path, delay: float, sig: signal.Signals) -> threading.Thread:
    def release() -> None:
        deadline = time.monotonic() + delay
        while not pid_file.exists() or not pid_file.read_text().strip():
            time.sleep(0.01)
        time.sleep(max(0.0, deadline - time.monotonic()))
        with contextlib.suppress(ProcessLookupError):
            os.kill(int(pid_file.read_text()), sig)

    thread = threading.Thread(target=release, daemon=True)
    thread.start()
    return thread


async def _run_stopping_cli(
    tmp_path: Path, *, timeout_seconds: int
) -> tuple[float, int | BaseException]:
    pid_file = tmp_path / "cli.pid"
    ticks: list[float] = [time.monotonic()]

    async def tick() -> None:
        while True:
            await asyncio.sleep(0.05)
            ticks.append(time.monotonic())

    ticker = asyncio.create_task(tick())
    try:
        result = await run_subprocess_exec(
            argv=["sh", "-c", f"echo $$ > {pid_file}; kill -STOP $$; exit 7"],
            cwd=tmp_path,
            env={"PATH": os.environ["PATH"]},
            stdin=None,
            timeout_seconds=timeout_seconds,
            output_limit_bytes=1024,
        )
        outcome: int | BaseException = result.exit_code
    except TimeoutError as error:
        outcome = error
    finally:
        ticker.cancel()
    ticks.append(time.monotonic())
    return max(b - a for a, b in zip(ticks, ticks[1:], strict=False)), outcome


@pytest.mark.asyncio
async def test_a_stopped_cli_leaves_the_loop_running_and_times_out(tmp_path: Path) -> None:
    assert install_stop_tolerant_child_watcher(asyncio.get_running_loop())
    # Only a safety net: the Directive timeout is what must end this run.
    _release_later(tmp_path / "cli.pid", 5.0, signal.SIGKILL)

    worst_gap, outcome = await _run_stopping_cli(tmp_path, timeout_seconds=1)

    assert isinstance(outcome, TimeoutError)
    assert worst_gap < 0.5


@pytest.mark.asyncio
async def test_a_continued_cli_still_reports_its_own_exit_code(tmp_path: Path) -> None:
    assert install_stop_tolerant_child_watcher(asyncio.get_running_loop())
    _release_later(tmp_path / "cli.pid", 0.5, signal.SIGCONT)

    worst_gap, outcome = await _run_stopping_cli(tmp_path, timeout_seconds=10)

    assert outcome == 7
    assert worst_gap < 0.5


@pytest.mark.asyncio
async def test_the_stock_watcher_still_wedges(tmp_path: Path) -> None:
    """Pins the CPython defect. When this fails, upstream fixed it: delete child_watcher.py."""

    _release_later(tmp_path / "cli.pid", 1.5, signal.SIGKILL)

    worst_gap, _ = await _run_stopping_cli(tmp_path, timeout_seconds=1)

    assert worst_gap > 1.0
