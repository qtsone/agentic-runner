"""Decide one privileged verb against a Grant snapshot (ADR-0011 §8-12).

The control plane is the policy *authority* and hands over a **snapshot**; this module
turns that snapshot into a decision for one ``(resource, verb)`` and the Evidence that
records it. The evaluation runs **locally** wherever the snapshot is — no per-verb round
trip — so a seam costs nothing. Since PRD issue 44 the snapshot is **pushed on change**
over the Runner's heartbeat stream (§11) rather than fetched at a Directive boundary, so
a narrowing bites at the next verb; nothing in this module changed for it.

It lives beside the snapshot, in the contracts package, because both sides read it: the
Runner at its verb seams, and the platform's Autonomy Policy, which must know whether
``pr.merge`` would clear before it decides an unattended merge. The *seams themselves* —
the calls, and the Evidence they append — stay in the Runner's activities.

A Protected Path (ADR-0011 §13): a PR touching this module always hits a human gate, so an
Agent can never loosen the seams that bound it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from agentic_runner_contracts.grants.catalogue import mcp_resource_type
from agentic_runner_contracts.grants.evaluator import Resource, evaluate_effective_grant
from agentic_runner_contracts.grants.model import DECISION_RANK, Decision, GrantLink
from agentic_runner_contracts.grants.snapshot import REPO_RESOURCE_TYPE, GrantSnapshot


@dataclass(frozen=True)
class VerbDecision:
    """One evaluated verb, with the provenance an Evidence Event has to name (§10)."""

    verb: str
    resource_type: str
    identifier: str
    decision: Decision
    reason: str
    agent_id: str | None
    contract_state: str
    enforced: bool
    deciding_link: GrantLink | None = None
    deciding_entry: dict[str, object] | None = None

    @property
    def allowed(self) -> bool:
        return self.decision is Decision.ALLOW

    @property
    def needs_owner_confirmation(self) -> bool:
        """The chain escalated this verb to the Agent's owner (ADR-0011 §14).

        Neither allowed nor refused: the seam raises an Owner Confirmation and the
        workflow waits on the owner's signal (``workflows/owner_confirmation.py``).
        """

        return self.decision is Decision.CONFIRM

    @property
    def refusal_summary(self) -> str:
        return f"grant refused {self.verb!r} on {self.identifier}: {self.reason}"

    def evidence(self) -> dict[str, object]:
        return {
            "agent_id": self.agent_id,
            "verb": self.verb,
            "resource_type": self.resource_type,
            "resource": self.identifier,
            "decision": self.decision.value,
            "deciding_link": self.deciding_link.value if self.deciding_link else None,
            "deciding_entry": self.deciding_entry,
            "reason": self.reason,
            "contract_state": self.contract_state,
            "enforced": self.enforced,
        }


def decide_verb(
    snapshot: GrantSnapshot,
    *,
    verb: str,
    identifier: str,
    resource_type: str = REPO_RESOURCE_TYPE,
) -> VerbDecision:
    """Evaluate one privileged verb against the Effective Grant in ``snapshot``."""

    if not snapshot.enforced:
        return _decision(
            snapshot,
            verb=verb,
            resource_type=resource_type,
            identifier=identifier,
            decision=Decision.ALLOW,
            reason="no Agent is bound to this Work Record; nothing to attenuate",
        )
    if not snapshot.dispatchable:
        # §8: the control plane refuses to *dispatch* under an inactive Contract, and the
        # Runner refuses every verb it already holds a snapshot for. The in-flight hold
        # itself is PRD issue 12; this is the seam it will hang off.
        return _decision(
            snapshot,
            verb=verb,
            resource_type=resource_type,
            identifier=identifier,
            decision=Decision.DENY,
            reason=f"the Contract is {snapshot.contract_state}; its Agents are not dispatchable",
        )

    effective = evaluate_effective_grant(
        root=snapshot.root,
        agent=snapshot.agent,
        persona=snapshot.persona,
        resource=Resource(
            resource_type=resource_type,
            identifier=identifier,
            in_contract_scope=snapshot.in_contract_scope(
                resource_type=resource_type, identifier=identifier
            ),
        ),
        verb=verb,
        account_contract=snapshot.account_contract,
    )
    entry = effective.deciding_entry
    return _decision(
        snapshot,
        verb=verb,
        resource_type=resource_type,
        identifier=identifier,
        decision=effective.decision,
        reason=effective.reason,
        deciding_link=effective.deciding_link,
        deciding_entry=(
            None
            if entry is None
            else {
                "resource_type": entry.resource_type,
                "selector": entry.selector,
                "decision": entry.verbs.get(verb, effective.decision).value,
            }
        ),
    )


@dataclass(frozen=True)
class McpServerDecision:
    """Whether one registered MCP server is written into the CLI's config (PRD issue 58).

    MCP cannot list a server while hiding some of its tools, so the seam is per server:
    the server's decision is the most restrictive of its tools' decisions, and
    ``deciding`` is the tool evaluation that produced it -- the link and entry the
    Evidence and the console name. A ``deny`` on one tool therefore withholds the whole
    server; a ``confirm`` on one asks the owner before the server is written at all.
    """

    server: str
    decision: Decision
    deciding: VerbDecision
    tools: tuple[VerbDecision, ...]

    @property
    def verb(self) -> str:
        """The name an Owner Confirmation and a consent carry: the server's resource type."""

        return mcp_resource_type(self.server)

    def evidence(self) -> dict[str, object]:
        return {
            "server": self.server,
            "decision": self.decision.value,
            "deciding_tool": self.deciding.verb,
            "deciding_link": self.deciding.deciding_link.value
            if self.deciding.deciding_link
            else None,
            "deciding_entry": self.deciding.deciding_entry,
            "reason": self.deciding.reason,
        }


def decide_mcp_server(
    snapshot: GrantSnapshot, *, server: str, tools: Sequence[str]
) -> McpServerDecision:
    """Evaluate every tool of one server and keep the most restrictive answer.

    A server that registered no tools is evaluated on ``*`` alone, which only a pattern
    entry (the Persona's derived ``*``, a root ``*``) can match -- so a toolless server
    is granted exactly when every link grants the whole server.

    Unlike a platform verb, an unenforced snapshot (no Agent bound) grants no server:
    the verbs it allows are the loop's own, which predate the Agent entity, while a
    server is new capability -- often a credentialed one -- that only an attributable
    Agent's Grant can hand out.
    """

    resource_type = mcp_resource_type(server)
    if not snapshot.enforced:
        refused = _decision(
            snapshot,
            verb="*",
            resource_type=resource_type,
            identifier=server,
            decision=Decision.DENY,
            reason="no Agent is bound to this Work Record; MCP servers are granted per Agent",
        )
        return McpServerDecision(
            server=server, decision=Decision.DENY, deciding=refused, tools=(refused,)
        )
    decisions = tuple(
        decide_verb(snapshot, verb=tool, identifier=server, resource_type=resource_type)
        for tool in (tuple(tools) or ("*",))
    )
    deciding = min(decisions, key=lambda decision: DECISION_RANK[decision.decision])
    return McpServerDecision(
        server=server, decision=deciding.decision, deciding=deciding, tools=decisions
    )


def _decision(
    snapshot: GrantSnapshot,
    *,
    verb: str,
    resource_type: str,
    identifier: str,
    decision: Decision,
    reason: str,
    deciding_link: GrantLink | None = None,
    deciding_entry: dict[str, object] | None = None,
) -> VerbDecision:
    return VerbDecision(
        verb=verb,
        resource_type=resource_type,
        identifier=identifier,
        decision=decision,
        reason=reason,
        agent_id=snapshot.agent_id,
        contract_state=snapshot.contract_state,
        enforced=snapshot.enforced,
        deciding_link=deciding_link,
        deciding_entry=deciding_entry,
    )
