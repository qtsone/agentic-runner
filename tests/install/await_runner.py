"""Wait until the fake control plane has seen Runners register and heartbeat.

The install tests (``docker.sh``, ``workstation.sh``) start a Runner the way a guide in
``docs/`` tells a person to, against the conformance kit's fake control plane
(``python -m agentic_runner.testing``), then call this: it polls ``/stats`` until every
expected Runner has registered with the expected isolation mode and each one has
heartbeat, and fails with the last stats seen.

Usage: await_runner.py <stats url> --runners N --isolation MODE [--timeout SECONDS]
Standard library only: it runs on a bare CI host, outside any virtualenv.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request


def fetch(url: str) -> dict[str, object]:
    with urllib.request.urlopen(url, timeout=5) as response:
        stats: dict[str, object] = json.loads(response.read())
    return stats


def satisfied(stats: dict[str, object], runners: int, isolation: str) -> bool:
    heartbeat_ids = stats.get("heartbeat_runner_ids")
    return (
        stats.get("bootstraps") == runners
        and stats.get("isolation_modes") == [isolation]
        and isinstance(heartbeat_ids, list)
        and len(heartbeat_ids) == runners
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("--runners", type=int, default=1)
    parser.add_argument("--isolation", required=True, choices=("contract_uid", "none"))
    parser.add_argument("--timeout", type=float, default=180)
    arguments = parser.parse_args()

    deadline = time.monotonic() + arguments.timeout
    stats: dict[str, object] = {}
    while time.monotonic() < deadline:
        try:
            stats = fetch(arguments.url)
        except (urllib.error.URLError, OSError):
            stats = {}
        if satisfied(stats, arguments.runners, arguments.isolation):
            print(f"registered and heartbeating: {json.dumps(stats)}")
            return 0
        time.sleep(2)
    print(
        f"timed out after {arguments.timeout:.0f}s waiting for {arguments.runners} "
        f"{arguments.isolation} Runner(s) to register and heartbeat; last stats: "
        f"{json.dumps(stats)}",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
