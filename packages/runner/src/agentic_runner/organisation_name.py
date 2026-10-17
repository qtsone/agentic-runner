"""The Organisation this Runner serves, by the name the last heartbeat ack gave (QTS-1909).

``agentic-runner list`` is a separate process and reads it from this file, which the
running Runner rewrites whenever an ack changes it. Absent while the control plane names
no Organisation -- one below contracts 3.29 -- so ``list`` shows the id instead.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from agentic_runner.private_state import private_read, private_write

__all__ = ["ORGANISATION_NAME_FILENAME", "load_organisation_name", "save_organisation_name"]

ORGANISATION_NAME_FILENAME: Final[str] = "organisation-name"


def save_organisation_name(state_dir: Path, name: str | None) -> None:
    path = state_dir / ORGANISATION_NAME_FILENAME
    if name is None:
        path.unlink(missing_ok=True)
    else:
        private_write(path, name.encode("utf-8"))


def load_organisation_name(state_dir: Path) -> str | None:
    raw = private_read(state_dir / ORGANISATION_NAME_FILENAME)
    return None if raw is None else raw.decode("utf-8", errors="replace")
