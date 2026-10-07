"""The Effective Grant: root grant ∩ Agent grant ∩ Persona allow-list (ADR-0011 §7).

Under a Leaf Contract the intersection gains one term by reference (ADR-0011, PRD issue
34): **Account Contract root ∩ Leaf root ∩ Agent ∩ Persona**. Nothing else changes —
a Leaf stored wider than its Account Contract is legal and simply decides less.

Pure, deterministic, never stored — there is no effective grant to invalidate, so a
narrowing anywhere in the chain bites at the next verb with no drain and no revocation
job. The Runner imports this module and evaluates every privileged verb against it; the
control plane uses the same function to show a user what their Agent can actually do.
"""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase

from agentic_runner_contracts.grants.catalogue import is_mcp_resource_type
from agentic_runner_contracts.grants.model import (
    DECISION_RANK,
    Decision,
    Grant,
    GrantEntry,
    GrantLink,
)


@dataclass(frozen=True)
class Resource:
    """One concrete resource a verb is about, already resolved against Product scope.

    ``in_contract_scope`` is false both for a resource registered to a Product outside the
    Contract's Product set and for one registered to no Product at all: either way it is
    unreachable whatever the selector says (ADR-0011 §4), so the caller collapses both into
    this one fact.
    """

    resource_type: str
    identifier: str
    in_contract_scope: bool


@dataclass(frozen=True)
class LinkDecision:
    link: GrantLink
    decision: Decision
    entry: GrantEntry | None


@dataclass(frozen=True)
class EffectiveDecision:
    """The answer plus its provenance: which link decided, on which entry, and why.

    ``deciding_link`` is ``None`` only when no link got a say because the resource is
    outside the Contract's Product scope — the one refusal the chain does not make.
    """

    decision: Decision
    reason: str
    links: tuple[LinkDecision, ...]
    deciding_link: GrantLink | None
    deciding_entry: GrantEntry | None


def evaluate_effective_grant(
    *,
    root: Grant,
    agent: Grant,
    persona: Grant,
    resource: Resource,
    verb: str,
    account_contract: Grant | None = None,
) -> EffectiveDecision:
    """Intersect the links for one (resource, verb) and say which link decided.

    ``account_contract`` is the parent root of a Leaf Contract (PRD issue 34); ``None`` --
    every direct Contract -- leaves the three-link chain exactly as it was. It is
    evaluated first so that a refusal the agency inherited from the client is named as
    the ``account_contract`` link rather than attributed to the Leaf's own root.
    """

    links = (
        *(
            ()
            if account_contract is None
            else (
                decide_link(
                    GrantLink.ACCOUNT_CONTRACT, account_contract, resource=resource, verb=verb
                ),
            )
        ),
        decide_link(GrantLink.ROOT, root, resource=resource, verb=verb),
        decide_link(GrantLink.AGENT, agent, resource=resource, verb=verb),
        decide_link(GrantLink.PERSONA, persona, resource=resource, verb=verb),
    )
    if not resource.in_contract_scope:
        return EffectiveDecision(
            decision=Decision.DENY,
            reason=(
                f"{resource.identifier} is not registered to a Product in the Contract's "
                "scope, so it is unreachable whatever the selector says"
            ),
            links=links,
            deciding_link=None,
            deciding_entry=None,
        )

    decision = min((link.decision for link in links), key=lambda value: DECISION_RANK[value])
    deciding = next(link for link in links if link.decision is decision)
    if deciding.entry is None:
        reason = (
            f"no {deciding.link.value} grant entry matches {resource.identifier} for {verb!r}; "
            "grants are deny by default"
        )
    else:
        reason = (
            f"{deciding.link.value} grant entry ({deciding.entry.resource_type}, "
            f"{deciding.entry.selector}) decides {decision.value!r} for {verb!r}"
        )
    return EffectiveDecision(
        decision=decision,
        reason=reason,
        links=links,
        deciding_link=deciding.link,
        deciding_entry=deciding.entry,
    )


def decide_link(
    link: GrantLink,
    grant: Grant,
    *,
    resource: Resource,
    verb: str,
) -> LinkDecision:
    """The most restrictive matching entry in one Grant, or deny when none matches.

    Public because one link on its own is a real question: the Organisation Console's
    root-grant editor asks what the *root* says about a resource, with no Agent and no
    Persona to intersect with yet (PRD issue 20).

    Several entries may match one resource; the minimum wins so that a narrow explicit
    ``deny`` beats a broad ``allow`` written beside it. Specificity ordering is
    deliberately not modelled — attenuation is the point, and "most restrictive wins" is
    the reading that can only ever refuse too much, never too little.
    """

    decided: LinkDecision | None = None
    for entry in grant.entries:
        if entry.resource_type != resource.resource_type:
            continue
        if not fnmatchcase(resource.identifier, entry.selector):
            continue
        for decision in _decisions_for(entry, verb):
            if decided is None or DECISION_RANK[decision] < DECISION_RANK[decided.decision]:
                decided = LinkDecision(link=link, decision=decision, entry=entry)
    return decided or LinkDecision(link=link, decision=Decision.DENY, entry=None)


def _decisions_for(entry: GrantEntry, verb: str) -> tuple[Decision, ...]:
    """What one entry says about ``verb``.

    A platform verb is looked up exactly: its wildcards were expanded at write time. An
    ``mcp:`` entry may still carry a pattern -- the Persona link is derived with ``*``
    at read time, and an entry saved before its server was registered was never
    expanded -- so its patterns are matched here, most restrictive wins (PRD issue 58).
    """

    if not is_mcp_resource_type(entry.resource_type):
        decision = entry.verbs.get(verb)
        return () if decision is None else (decision,)
    exact = entry.verbs.get(verb)
    if exact is not None:
        return (exact,)
    return tuple(
        decision for pattern, decision in entry.verbs.items() if fnmatchcase(verb, pattern)
    )
