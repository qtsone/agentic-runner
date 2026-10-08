"""Keep a *stopped* child from wedging the Runner's event loop (QTS-1148).

Python 3.14's asyncio watches children without pidfd (macOS) with
``_ThreadedChildWatcher``: a thread blocks in ``waitid(WEXITED | WNOWAIT)``, then hands
the reap to the loop thread, which calls a blocking ``waitpid(pid, 0)``. macOS's
``waitid`` also returns for a child that is only stopped (``CLD_STOPPED``), so a
``SIGSTOP``/``SIGTSTP`` on any Runner spawn -- the Agent Runtime CLI, an MCP server, a
sign-in -- freezes the loop until that child exits: no heartbeats, no polling, no
cancellation, and the Directive timeout cannot fire either. CPython has not fixed this
(main still waits on ``WEXITED | WNOWAIT`` alone, as of 2026-10).

The fix sits in front of the watcher thread's wait rather than replacing the watcher: it
consumes stop and continue reports until the report is a real exit, then lets the stock
code run, which now returns at once. It reaches into asyncio's private watcher, so it
installs only when it finds exactly the class it was written against and otherwise
leaves the loop alone; ``tests/unit/test_runner_child_watcher.py`` fails on macOS if
the wedge comes back.
"""

from __future__ import annotations

import asyncio
import os
from asyncio import unix_events
from typing import Any, Final

_EXIT_CODES: Final = frozenset({os.CLD_EXITED, os.CLD_KILLED, os.CLD_DUMPED})
# typeshed leaves ``os.waitid`` out on darwin although CPython has it there, and mypy
# checks against the host platform; a getattr keeps one spelling clean on both.
_waitid: Final[Any] = getattr(os, "waitid", None)


def install_stop_tolerant_child_watcher(loop: asyncio.AbstractEventLoop) -> bool:
    """Make ``loop``'s threaded child watcher wait past stops. Returns whether it did."""

    threaded = getattr(unix_events, "_ThreadedChildWatcher", None)
    watcher: Any = getattr(loop, "_watcher", None)
    if threaded is None or type(watcher) is not threaded or _waitid is None:
        return False

    stock_do_waitpid = watcher._do_waitpid

    def do_waitpid(
        loop: asyncio.AbstractEventLoop, expected_pid: int, callback: Any, args: Any
    ) -> None:
        _wait_until_exited(expected_pid)
        stock_do_waitpid(loop, expected_pid, callback, args)

    watcher._do_waitpid = do_waitpid
    return True


def _wait_until_exited(pid: int) -> None:
    while True:
        try:
            report = _waitid(os.P_PID, pid, os.WEXITED | os.WNOWAIT)
        except ChildProcessError:
            return
        if report is None or report.si_code in _EXIT_CODES:
            return
        # Without consuming it, the same stop report comes straight back under WNOWAIT.
        # WSTOPPED | WCONTINUED never matches an exit, so this cannot reap the child.
        try:
            _waitid(os.P_PID, pid, os.WSTOPPED | os.WCONTINUED | os.WNOHANG)
        except ChildProcessError:
            return
