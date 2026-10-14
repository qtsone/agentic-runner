"""The Runner Limit: what a Runner's host denies every Agent on it (ADR-0022 §52-§58).

Three deny-lists -- tools, networks, folders -- set in the Runner's own config and empty
by default. Empty narrows nothing; an entry is a further narrowing on top of the Effective
Grant, the Profile's ``network_policy`` and the command policy's floor, never a widening
of any of them. The control plane only ever reads a limit, from the registration request
and the heartbeat: no route writes one (§57).
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Annotated, Final

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from agentic_runner_contracts.grants.model import Decision, Grant, GrantEntry

RUNNER_LIMIT_ENTRIES_MAX: Final = 64

# The Profile's `network_policy` destination syntax: a host, `host:port` or `*.domain`.
NetworkDestination = Annotated[str, StringConstraints(min_length=1, max_length=255)]
FolderPath = Annotated[str, StringConstraints(min_length=1, max_length=4096)]


class RunnerLimit(BaseModel):
    """The three deny-lists as the host wrote them.

    ``tools`` entries have a Grant entry's shape and ``deny`` as their only decision
    (§53); the Runner checks them against its pinned Verb Catalogue at start, which this
    model cannot do because the catalogue a Runner pins is its own.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tools: tuple[GrantEntry, ...] = Field(default=(), max_length=RUNNER_LIMIT_ENTRIES_MAX)
    networks: tuple[NetworkDestination, ...] = Field(
        default=(), max_length=RUNNER_LIMIT_ENTRIES_MAX
    )
    folders: tuple[FolderPath, ...] = Field(default=(), max_length=RUNNER_LIMIT_ENTRIES_MAX)

    @field_validator("tools")
    @classmethod
    def _deny_only(cls, tools: tuple[GrantEntry, ...]) -> tuple[GrantEntry, ...]:
        for entry in tools:
            for verb, decision in entry.verbs.items():
                if decision is not Decision.DENY:
                    raise ValueError(
                        f"Runner Limit tools entry ({entry.resource_type}, {entry.selector}) "
                        f"verb {verb!r} decides {decision.value!r}; a Runner Limit only denies"
                    )
        return tools

    @field_validator("folders")
    @classmethod
    def _absolute(cls, folders: tuple[str, ...]) -> tuple[str, ...]:
        for folder in folders:
            if not PurePosixPath(folder).is_absolute():
                raise ValueError(f"Runner Limit folder {folder!r} is not an absolute path")
        return folders

    @property
    def tools_grant(self) -> Grant:
        """``tools`` as the evaluator's ``runner_limit`` link takes it."""

        return Grant(entries=self.tools)
