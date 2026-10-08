"""Install, start, stop and wake, kept until a heartbeat carries them (PRD issue 47).

The Evidence trail is the control plane's, so a lifecycle fact reaches it on the stream
or not at all. Two of the four happen when no heartbeat is in flight -- ``install``
before the process ever ran, ``stop`` after its last beat -- so the facts wait in the
state directory and the next acknowledged heartbeat takes them. Ids and timestamps only.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from pydantic import TypeAdapter

from agentic_runner.private_state import private_read, private_write
from agentic_runner_contracts.runner_registration import LifecycleEvent, LifecycleKind

__all__ = ["LIFECYCLE_FILENAME", "LifecycleOutbox"]

LIFECYCLE_FILENAME = "lifecycle-pending.json"
# The envelope's own bound: a Runner that restarted in a crash loop while the control
# plane was unreachable keeps the newest facts rather than an envelope it cannot send.
_KEEP = 32
_EVENTS = TypeAdapter(list[LifecycleEvent])


class LifecycleOutbox:
    def __init__(self, state_dir: Path) -> None:
        self._path = state_dir / LIFECYCLE_FILENAME

    def add(self, kind: LifecycleKind, at: datetime, *, slept_at: datetime | None = None) -> None:
        events = [*self.pending(), LifecycleEvent(kind=kind, at=at, slept_at=slept_at)]
        self._write(events[-_KEEP:])

    def pending(self) -> list[LifecycleEvent]:
        raw = private_read(self._path)
        return [] if raw is None else _EVENTS.validate_json(raw)

    def acknowledge(self, sent: list[LifecycleEvent]) -> None:
        """Drop what an acknowledged heartbeat carried; keep anything added meanwhile."""

        remaining = [event for event in self.pending() if event not in sent]
        self._write(remaining)

    def _write(self, events: list[LifecycleEvent]) -> None:
        private_write(self._path, _EVENTS.dump_json(events))
