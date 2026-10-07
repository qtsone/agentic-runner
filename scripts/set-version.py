#!/usr/bin/env python3
"""Write one release version into both packages and the chart.

Usage: python scripts/set-version.py <version>

semantic-release's prepare step runs this (`.releaserc`). Both packages release together at
one version, and the chart's `appVersion` is the image tag it installs by default, so all four
fields move as one. Standard library only: the release job installs nothing for it.
"""

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

RUNNER_INIT = REPO_ROOT / "packages/runner/src/agentic_runner/__init__.py"
CONTRACTS_INIT = REPO_ROOT / "packages/contracts/src/agentic_runner_contracts/__init__.py"
CHART = REPO_ROOT / "charts/agentic-runner/Chart.yaml"
DUNDER_VERSION = r'^__version__ = "[^"]*"$'

TARGETS = [
    (RUNNER_INIT, DUNDER_VERSION, '__version__ = "{v}"'),
    (CONTRACTS_INIT, DUNDER_VERSION, '__version__ = "{v}"'),
    (CHART, r"^version: .*$", "version: {v}"),
    (CHART, r"^appVersion: .*$", 'appVersion: "{v}"'),
]

SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")


def set_version(version: str, targets: list[tuple[Path, str, str]] = TARGETS) -> None:
    if not SEMVER.match(version):
        raise SystemExit(f"not a semantic version: {version!r}")
    for path, pattern, replacement in targets:
        text = path.read_text(encoding="utf-8")
        new_text, count = re.subn(
            pattern, replacement.format(v=version), text, count=1, flags=re.MULTILINE
        )
        # A silent no-op would publish packages whose metadata disagrees with the tag.
        if count != 1:
            raise SystemExit(f"{path}: no line matches {pattern}")
        path.write_text(new_text, encoding="utf-8")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    set_version(sys.argv[1])
