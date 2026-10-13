"""The ``agentic-runner`` console script: a SIGTERM disposition before the CLI imports.

Importing the CLI takes seconds (Temporal, httpx, pydantic). In a container the Runner is
PID 1, and the kernel drops a signal PID 1 has no handler for, so a SIGTERM in that window
was lost and ``docker stop`` waited out its grace period to SIGKILL (QTS-1536). Nothing
has run yet, so it exits 0 -- what launchd's ``KeepAlive: SuccessfulExit: false`` reads
as a clean stop. ``agentic-runner run`` replaces this with its own handler before its
first await; every other subcommand gets the default disposition back, so a killed
callback such as ``ask`` never reads as one that succeeded.

Imports nothing but the standard library: anything heavier reopens the window.
"""

from __future__ import annotations

import os
import signal
import sys
from types import FrameType


def main() -> int:
    signal.signal(signal.SIGTERM, _exit_before_start)
    from agentic_runner.cli import main as cli_main

    if sys.argv[1:2] != ["run"]:
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
    return cli_main()


def _exit_before_start(signum: int, frame: FrameType | None) -> None:
    # Not SystemExit: raised mid-import, it tears down half-initialised modules and the
    # interpreter prints their errors. Nothing has written state yet to flush or close.
    os._exit(0)


if __name__ == "__main__":  # pragma: no cover - `runner_command`'s fallback without the script
    raise SystemExit(main())
