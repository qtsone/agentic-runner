"""`scripts/set-version.py`, the release's only writer of versions (`.releaserc`).

It runs against a copy of the real files, so a reformatted `__init__.py` or `Chart.yaml` that
the script no longer matches fails here, not in the release job.
"""

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
VERSIONED_FILES = [
    "packages/runner/src/agentic_runner/__init__.py",
    "packages/contracts/src/agentic_runner_contracts/__init__.py",
    "charts/agentic-runner/Chart.yaml",
]


@pytest.fixture
def repo_copy(tmp_path: Path) -> Path:
    for relative in [*VERSIONED_FILES, "scripts/set-version.py"]:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO_ROOT / relative, target)
    return tmp_path


def run_set_version(repo: Path, version: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(repo / "scripts/set-version.py"), version],
        capture_output=True,
        text=True,
        check=False,
    )


def dunder_version(path: Path) -> str:
    match = re.search(r'^__version__ = "([^"]+)"$', path.read_text(), re.MULTILINE)
    assert match is not None
    return match.group(1)


def test_one_version_reaches_both_packages_and_the_chart(repo_copy: Path) -> None:
    result = run_set_version(repo_copy, "3.1.4")

    assert result.returncode == 0, result.stderr
    runner_init, contracts_init, chart_yaml = (repo_copy / f for f in VERSIONED_FILES)
    assert dunder_version(runner_init) == "3.1.4"
    assert dunder_version(contracts_init) == "3.1.4"
    chart = yaml.safe_load(chart_yaml.read_text())
    assert chart["version"] == "3.1.4"
    assert chart["appVersion"] == "3.1.4"


def test_only_the_version_lines_change(repo_copy: Path) -> None:
    run_set_version(repo_copy, "3.1.4")

    for relative in VERSIONED_FILES:
        before = (REPO_ROOT / relative).read_text().splitlines()
        after = (repo_copy / relative).read_text().splitlines()
        changed = [new for old, new in zip(before, after, strict=True) if old != new]
        assert all("3.1.4" in line for line in changed), relative


def test_a_non_semantic_version_is_refused(repo_copy: Path) -> None:
    result = run_set_version(repo_copy, "v3.1")

    assert result.returncode != 0
    assert "not a semantic version" in result.stderr
    for relative in VERSIONED_FILES:
        assert (repo_copy / relative).read_text() == (REPO_ROOT / relative).read_text()
