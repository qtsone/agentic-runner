"""The Grant schema and its write-time validator (ADR-0011 §1-2, §7).

A Grant is a set of entries ``(resource type, resource selector, verb -> decision)``, deny
by default. Three links chain, each narrowing the last, and each admits its own decision
set: the Contract's root grant is ``allow | deny`` (the org has no standing to make a
user's Agents ask the user), the user's grant to an Agent is ``allow | confirm | deny``,
and the Persona's allow-list is ``allow | deny``.

Everything here is pure: no I/O, no ORM, stdlib + pydantic only, so the Runner imports it
alongside the evaluator.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from enum import StrEnum
from fnmatch import fnmatchcase
from typing import Annotated, Final

from pydantic import BaseModel, ConfigDict, StringConstraints

from agentic_runner_contracts.grants.catalogue import (
    VerbCatalogue,
    is_mcp_resource_type,
    mcp_resource_type,
)

ResourceType = Annotated[str, StringConstraints(min_length=1, max_length=64)]
Selector = Annotated[str, StringConstraints(min_length=1, max_length=512)]
VerbPattern = Annotated[str, StringConstraints(min_length=1, max_length=64)]


class Decision(StrEnum):
    DENY = "deny"
    CONFIRM = "confirm"
    ALLOW = "allow"


class GrantLink(StrEnum):
    """Which link of the chain a Grant is, which fixes the decisions it may carry.

    ``ACCOUNT_CONTRACT`` is the parent root of a Leaf Contract (PRD issue 34, map ticket
    21 A5): the same ``allow | deny`` shape as ``ROOT``, and stored on the Account
    Contract's own ``root_grant`` column -- there is no fourth Grant column anywhere, only
    a fourth term in the intersection.
    """

    ACCOUNT_CONTRACT = "account_contract"
    ROOT = "root"
    AGENT = "agent"
    PERSONA = "persona"


# Ordered by how much they permit. Intersection is the minimum, so ``deny`` anywhere in the
# chain wins and ``confirm`` propagates through an ``allow``.
DECISION_RANK: Final[dict[Decision, int]] = {
    Decision.DENY: 0,
    Decision.CONFIRM: 1,
    Decision.ALLOW: 2,
}

_DECISIONS_BY_LINK: Final[dict[GrantLink, frozenset[Decision]]] = {
    GrantLink.ACCOUNT_CONTRACT: frozenset({Decision.ALLOW, Decision.DENY}),
    GrantLink.ROOT: frozenset({Decision.ALLOW, Decision.DENY}),
    GrantLink.AGENT: frozenset({Decision.ALLOW, Decision.CONFIRM, Decision.DENY}),
    GrantLink.PERSONA: frozenset({Decision.ALLOW, Decision.DENY}),
}


class GrantEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    resource_type: ResourceType
    selector: Selector
    verbs: dict[VerbPattern, Decision]


class Grant(BaseModel):
    """A whole Grant as it is stored in ``contracts.root_grant`` / ``agents.grant``.

    The catalogue version lives in its own column beside the JSON, not in here, because
    the version pins the *write*, not the document.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    entries: tuple[GrantEntry, ...] = ()


EMPTY_GRANT: Final = Grant()


class GrantRefusedError(ValueError):
    """A write-time refusal, naming the entry and the reason (ADR-0011 §7).

    The user sees this in the console instead of a silent clip at run time, so the message
    must stay specific enough to act on.
    """

    def __init__(self, *, entry: GrantEntry, verb: str | None, reason: str) -> None:
        self.entry = entry
        self.verb = verb
        self.reason = reason
        verb_part = f" verb {verb!r}" if verb is not None else ""
        super().__init__(
            f"grant entry ({entry.resource_type}, {entry.selector}){verb_part} refused: {reason}"
        )


def _has_wildcard(pattern: str) -> bool:
    return any(character in pattern for character in "*?[")


def expand_and_validate_grant(
    payload: object,
    *,
    link: GrantLink,
    catalogue: VerbCatalogue,
    root: Grant | None = None,
    mcp_tools: Mapping[str, Collection[str]] | None = None,
) -> Grant:
    """Parse, expand and validate a Grant for one link of the chain.

    Verb wildcards are expanded **here**, at write time, against ``catalogue`` — the
    version the caller pins on the row — so a later catalogue reaches a stored Grant only
    when it is saved again. Reserved verbs are never expanded into and are refused when
    named. When ``root`` is given (the Agent link), an entry that exceeds the root grant is
    refused rather than clipped.

    ``mcp_tools`` is the server registry, slug -> tool names (PRD issue 58). Under a
    catalogue that ``expands_mcp``, an ``mcp:<slug>`` entry for a registered server has
    its wildcards expanded over those tools and an unknown tool name refused; an entry
    for a server the registry does not carry is kept as written — it reaches nothing
    until the server is registered, and the evaluator reads its patterns then.
    """

    grant = payload if isinstance(payload, Grant) else Grant.model_validate(payload)
    allowed_decisions = _DECISIONS_BY_LINK[link]
    expanded: list[GrantEntry] = []
    for entry in grant.entries:
        for verb, decision in entry.verbs.items():
            if decision not in allowed_decisions:
                raise GrantRefusedError(
                    entry=entry,
                    verb=verb,
                    reason=f"a {link.value} grant may not decide {decision.value!r}",
                )
        expanded.append(_expand_entry(entry, catalogue=catalogue, mcp_tools=mcp_tools or {}))

    result = Grant(entries=tuple(expanded))
    if root is not None:
        _refuse_entries_exceeding_root(result, root=root)
    return result


def _expand_entry(
    entry: GrantEntry,
    *,
    catalogue: VerbCatalogue,
    mcp_tools: Mapping[str, Collection[str]],
) -> GrantEntry:
    if is_mcp_resource_type(entry.resource_type):
        tools = mcp_tools.get(entry.resource_type.removeprefix(mcp_resource_type("")))
        if not catalogue.expands_mcp or tools is None:
            return entry
        return _expand_verbs(entry, known=frozenset(tools), version=catalogue.version)
    if not catalogue.knows(entry.resource_type):
        raise GrantRefusedError(
            entry=entry,
            verb=None,
            reason=(
                f"resource type {entry.resource_type!r} is not in verb catalogue "
                f"{catalogue.version}"
            ),
        )

    for pattern in entry.verbs:
        if catalogue.is_reserved(entry.resource_type, pattern):
            raise GrantRefusedError(
                entry=entry,
                verb=pattern,
                reason=(
                    f"verb {pattern!r} is reserved in catalogue {catalogue.version} and has "
                    "no Runner seam yet"
                ),
            )
    # A wildcard never reaches a reserved verb: `known` is the grantable set, so `pr.*`
    # means "every pr verb that has a seam".
    return _expand_verbs(
        entry, known=catalogue.grantable(entry.resource_type), version=catalogue.version
    )


def _expand_verbs(entry: GrantEntry, *, known: frozenset[str], version: str) -> GrantEntry:
    # Concrete names first, wildcards over them: `{"*": allow, "create_issue": confirm}`
    # must keep the confirm whichever order the editor serialised the two in.
    verbs: dict[str, Decision] = {}
    explicit: dict[str, Decision] = {}
    for pattern, decision in entry.verbs.items():
        if not _has_wildcard(pattern):
            if pattern not in known:
                raise GrantRefusedError(
                    entry=entry,
                    verb=pattern,
                    reason=f"verb is not in catalogue {version} for {entry.resource_type!r}",
                )
            explicit[pattern] = decision
            continue
        matched = sorted(verb for verb in known if fnmatchcase(verb, pattern))
        if not matched:
            raise GrantRefusedError(
                entry=entry,
                verb=pattern,
                reason=f"verb wildcard matches no verb in catalogue {version}",
            )
        for verb in matched:
            verbs[verb] = decision
    verbs.update(explicit)
    return entry.model_copy(update={"verbs": verbs})


def _refuse_entries_exceeding_root(grant: Grant, *, root: Grant) -> None:
    for entry in grant.entries:
        if is_mcp_resource_type(entry.resource_type):
            # The root check below reads verbs against the platform catalogue; a server's
            # tools are the registry's. Evaluation stays the truth for them: a root that
            # denies the server withholds it at config assembly, naming the root.
            continue
        for verb, decision in entry.verbs.items():
            if decision is Decision.DENY:
                continue
            if not _root_covers(
                root, resource_type=entry.resource_type, selector=entry.selector, verb=verb
            ):
                raise GrantRefusedError(
                    entry=entry,
                    verb=verb,
                    reason="exceeds the Contract's root grant, which does not allow this verb "
                    "on every resource the selector reaches",
                )


def _root_covers(root: Grant, *, resource_type: str, selector: str, verb: str) -> bool:
    """Whether some root entry allows ``verb`` over every resource ``selector`` reaches.

    Containment between two glob patterns is undecidable in general, so this is the
    conservative approximation: the agent's selector must itself be matched by the root's
    selector (``qtsone/agentic-os`` and ``qtsone/*`` are both inside root ``qtsone/*``;
    ``*`` is not). A narrower pattern the approximation cannot see through is refused with
    a reason rather than quietly accepted, and evaluation stays the source of truth: a root
    entry that denies a subset still bites at the seam.
    """

    return any(
        entry.resource_type == resource_type
        and fnmatchcase(selector, entry.selector)
        and entry.verbs.get(verb) is Decision.ALLOW
        for entry in root.entries
    )


def default_persona_allow_list(*, catalogue: VerbCatalogue) -> Grant:
    """Every catalogued resource type, every grantable verb, ``allow`` — a Persona's bound.

    What migration 0030 wrote for the Personas that already existed, and what a new
    platform-defined Persona gets: the Persona link bounds *capability*, and the narrowing
    a user can see and set lives on the Contract's root grant and the Agent's Grant.
    """

    return Grant(
        entries=tuple(
            GrantEntry(
                resource_type=resource_type,
                selector="*",
                verbs=dict.fromkeys(sorted(grantable), Decision.ALLOW),
            )
            for resource_type, grantable in (
                (resource_type, catalogue.grantable(resource_type))
                for resource_type in sorted(catalogue.verbs)
            )
            if grantable
        )
    )


def persona_allow_list(
    *,
    allow_list: object | None,
    mcp_grants: Sequence[str] = (),
) -> Grant:
    """Assemble a Persona's Grant from its stored allow-list plus its ``mcp_grants``.

    The MCP slugs are folded in here rather than copied into the column by migration 0030
    so that ``mcp_grants`` — still the field operators edit — cannot drift from the Grant
    that quotes it. Live since PRD issue 58: each becomes ``mcp:<server>`` with every
    tool allowed, the Persona's bound on which servers its Agents may be given at all.
    """

    stored = Grant.model_validate({"entries": allow_list or ()})
    servers = tuple(
        GrantEntry(
            resource_type=mcp_resource_type(slug),
            selector="*",
            verbs={"*": Decision.ALLOW},
        )
        for slug in mcp_grants
    )
    return Grant(entries=stored.entries + servers)
