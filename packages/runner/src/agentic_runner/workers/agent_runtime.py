"""The AgentRuntime port: the single-turn contract every runtime implements.

Temporal owns the Ralph Loop (ADR-0007), so a runtime never runs its own loop — it
executes exactly one Directive and returns the result. This module defines the
runtime-neutral port and the data that crosses it, so a second runtime (Claude Code,
issue 11) can be added behind the same seam as Codex (ADR-0006).

Concrete runtimes are siblings of this module in ``agentic_runner.workers`` and are
injected into the activity adapter. They must never enter the workflow import graph (the
determinism sandbox); the deterministic workflow only ever invokes them by activity name.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol, runtime_checkable

from agentic_runner.workers.contract_isolation import DirectiveSandbox
from agentic_runner.workers.mcp_config import McpServerEntry


class AuthMode(StrEnum):
    """How one Directive authenticates, chosen by the Runner before spawn (local-agents 04).

    ``api_key`` runs through the Runner's LLM proxy on the attempt's bearer and is metered
    there; ``subscription`` runs on the login the Contract's person created in its harness
    root, and is metered from the harness's own output (PRD issue 31). A runtime declares
    which of these it *can* run in; which one a Directive *does* run in is never the
    runtime's to decide (``agentic_runner.auth_mode``).
    """

    API_KEY = "api_key"
    SUBSCRIPTION = "subscription"


@dataclass(frozen=True, slots=True)
class DirectiveRequest:
    """The input to one Directive: a single runtime turn in a prepared workspace."""

    workspace_path: Path
    prompt: str
    base_branch: str
    work_branch: str
    # Which Contract's uid this turn runs as, where its harness config root is, and the
    # floor it may not exceed (ADR-0015 §1). None only where the caller built no
    # isolation at all (unit fakes); the composition root always supplies one.
    sandbox: DirectiveSandbox | None = None
    # The attempt's callback socket and bearer, plus whatever the `environment` Runner
    # Hook exported (PRD issue 45). Pairs rather than a mapping so the request stays
    # frozen; every runtime merges them through `_runtime_support.apply_extra_env`, which
    # drops the names the Runner reserves for itself.
    extra_env: tuple[tuple[str, str], ...] = ()
    # The MCP servers the Agent's Effective Grant lets this Directive have (PRD issue
    # 58). ``None`` -- no server is bound to the Work Record's Product -- leaves the CLI's
    # own config untouched, so a Product without MCP runs byte-identically to before;
    # ``()`` is "servers are bound, none granted", which still has to be said to the CLI
    # so nothing ungranted is loaded from anywhere else.
    mcp_servers: tuple[McpServerEntry, ...] | None = None
    # The destinations this Directive may reach (the Profile's list plus what the Runner
    # added), enforced by the attempt's egress proxy. Empty: no Profile list, so the
    # runtime keeps its own network posture.
    egress_allow_list: tuple[str, ...] = ()
    # A retried attempt continues the harness session an earlier attempt of the same
    # Directive started (ADR-0007, amendment 2026-10-03). The caller has already matched
    # the Contract, Workspace and head, and asked ``has_session``.
    resume_session_id: str | None = None
    # Called with the id of a session the runtime *starts*, as soon as it knows it --
    # before the turn ends, so an attempt lost mid-turn still leaves it in the activity's
    # heartbeat details. Not called on a resume (the caller already holds that id), nor by
    # a runtime that cannot name its session.
    on_session_started: Callable[[str], None] | None = None
    auth_mode: AuthMode = AuthMode.API_KEY


@dataclass(frozen=True, slots=True)
class DirectiveEvidence:
    """Bounded, redacted record of one Directive, safe for control-plane storage."""

    workspace_id: str
    base_branch: str
    work_branch: str
    command_hash: str
    guard_mode: str
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class DirectiveResult:
    """The outcome of one Directive. ``exit_code`` 0 is success; a guard refusal is
    signalled by ``exit_code`` 126 and an ``evidence.guard_mode`` starting with
    ``"refused"`` — the discriminator the Ralph Loop branches on."""

    exit_code: int
    stdout: str
    stderr: str
    error: str
    command_hash: str
    evidence: DirectiveEvidence


# The permission mode of a runtime whose guard configuration is incomplete: it refuses
# every Directive, so there is no mode it passes.
REFUSED_PERMISSION_MODE = "refused"


@dataclass(frozen=True, slots=True)
class RuntimeCapabilities:
    """What a runtime declares about itself on the heartbeat (local-agents 17).

    ``permission_mode`` is the fixed permission and approval mode the runtime passes its
    harness, as a short patterned string. It is reported, never the gate: the Runner's
    own floor is (ADR-0011 §12).
    """

    auth_modes: frozenset[AuthMode]
    permission_mode: str


@runtime_checkable
class AgentRuntime(Protocol):
    """The pluggable engine an Agent uses to execute one Directive per step."""

    # The modes this runtime can run a Directive in -- a capability, not a choice.
    auth_modes: frozenset[AuthMode]
    # Whether the host operator configured a provider key for this runtime itself, which
    # counts as "an API key is present" when the Runner chooses the mode.
    host_api_key: bool

    def capabilities(self) -> RuntimeCapabilities: ...

    async def execute_directive(self, request: DirectiveRequest) -> DirectiveResult: ...


@runtime_checkable
class ResumableAgentRuntime(AgentRuntime, Protocol):
    """A runtime whose harness can continue a session in a new process (local-agents 16)."""

    def has_session(self, session_id: str, sandbox: DirectiveSandbox | None) -> bool:
        """Whether the harness still holds ``session_id`` where this Directive would run.

        A resume of a session the harness no longer has fails the whole turn, so the
        caller starts fresh instead.
        """
        ...
