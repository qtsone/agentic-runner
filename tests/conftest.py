"""Suite-wide fixtures."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _per_test_tempdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A Runner attempt lives at gettempdir()/attempt-<sha256(directive_id)[:12]> and is
    # rmtree'd on exit; tests share Work Record ids, so under xdist one worker's cleanup
    # deletes another's attempt mid-write unless each test gets its own temp root.
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
