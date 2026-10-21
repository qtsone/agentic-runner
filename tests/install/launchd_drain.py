"""On a real launchd: `restart` and `stop` let a drain run past launchd's 60 s (QTS-2178).

A LaunchAgent with the Runner's own plist, label and launchctl verbs, whose program takes
DRAIN seconds to exit on SIGTERM and logs when it does. launchd SIGKILLs a stop of its own
after at most 60 s, so the clean-exit line shows the drain was let run out.

Usage (macOS, with agentic-runner importable): python tests/install/launchd_drain.py <work dir>
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from agentic_runner import workstation
from agentic_runner_contracts.runner_registration import InstallChannel

DRAIN = 90


def main(work: Path) -> int:
    paths = workstation.RunnerPaths(root=work / "drain-root", name="drain-probe")
    paths.create()
    settings = workstation.WorkstationSettings(
        control_plane_url="http://127.0.0.1:1",
        path=os.environ["PATH"],
        install_channel=InstallChannel.MANUAL,
    )
    settings.save(paths)
    exits = work / "drain-exits"
    program = work / "drain-probe.sh"
    program.write_text(
        "#!/bin/sh\n"
        f"trap 'sleep {DRAIN}; echo \"$$\" >> {exits}; exit 0' TERM\n"
        f"echo probe > {paths.started_version}\n"
        f"echo $$ > {paths.pidfile}\n"
        "while :; do sleep 1; done\n"
    )
    program.chmod(0o755)
    definition = workstation.service_definition(paths, settings, command=[str(program)])
    definition.path.parent.mkdir(parents=True, exist_ok=True)
    definition.path.write_bytes(definition.content)
    workstation.drive(definition.start)
    try:
        first = _await_pid(paths)
        began = time.monotonic()
        second, _ = workstation.restart(paths)
        _check("restart", first, began, exits)
        began = time.monotonic()
        workstation.stop(paths)
        _check("stop", second, began, exits)
    finally:
        subprocess.run(definition.stop[0], check=False, capture_output=True)
    print(f"launchd let both drains run {DRAIN}s")
    return 0


def _await_pid(paths: workstation.RunnerPaths) -> int:
    for _ in range(30):
        if (pid := workstation.process_id(paths)) is not None:
            return pid
        time.sleep(1)
    raise SystemExit("the probe never wrote its pidfile")


def _check(verb: str, pid: int, began: float, exits: Path) -> None:
    took = time.monotonic() - began
    logged = exits.read_text().split() if exits.is_file() else []
    print(f"{verb}: returned after {took:.0f}s, clean exits {logged}")
    if str(pid) not in logged or took < DRAIN:
        raise SystemExit(f"{verb} cut the {DRAIN}s drain of pid {pid} short")


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
