"""Membership is the Grant: the evaluator over a ``channel`` resource (PRD issue 51).

`.scratch/multi-org-release-1/issues/51-channels-roles-membership-by-grant.md`. An Agent
is a member of a Channel iff its Effective Grant allows ``channel.read`` on it, and may
speak iff ``channel.write``. There is no member list anywhere — these are the tests that
say so, over the same pure evaluator every other resource type goes through.

The Persona-wide fan-out is **not** here: a Persona link is a bound the evaluator takes a
``min`` over, so a Grant document on its own cannot demonstrate it fanning out to two
Agents. That criterion is
``test_channels_service.test_a_persona_wide_selector_fans_out_to_every_agent_of_that_persona``,
where two real Agents of one Persona under one Contract come back members and drop
together when that one Persona's allow-list is narrowed.

The Grants here are built with the model directly rather than through
``expand_and_validate_grant``: ``channel.read`` / ``channel.write`` are **reserved** until
issue 52 registers their seam, so no Grant may carry one yet (see
``test_grant_catalogue.py``). What is under test is the decision the chain makes once they
can be written; the write-time refusal is pinned in its own place.
"""

from __future__ import annotations

from agentic_runner_contracts.grants.evaluator import Resource, evaluate_effective_grant
from agentic_runner_contracts.grants.model import Decision, Grant, GrantEntry, GrantLink
from agentic_runner_contracts.grants.snapshot import (
    CHANNEL_READ_VERB,
    CHANNEL_RESOURCE_TYPE,
    CHANNEL_WRITE_VERB,
)

BACKEND = Resource(
    resource_type=CHANNEL_RESOURCE_TYPE, identifier="#backend", in_contract_scope=True
)
FRONTEND = Resource(
    resource_type=CHANNEL_RESOURCE_TYPE, identifier="#frontend", in_contract_scope=True
)

ALL_CHANNELS: Grant = Grant(
    entries=(
        GrantEntry(
            resource_type=CHANNEL_RESOURCE_TYPE,
            selector="*",
            verbs={CHANNEL_READ_VERB: Decision.ALLOW, CHANNEL_WRITE_VERB: Decision.ALLOW},
        ),
    )
)


def _channel_grant(selector: str, **verbs: Decision) -> Grant:
    return Grant(
        entries=(
            GrantEntry(resource_type=CHANNEL_RESOURCE_TYPE, selector=selector, verbs=dict(verbs)),
        )
    )


def _decide(
    *,
    agent: Grant,
    verb: str,
    resource: Resource = BACKEND,
    root: Grant = ALL_CHANNELS,
    persona: Grant = ALL_CHANNELS,
) -> object:
    return evaluate_effective_grant(
        root=root, agent=agent, persona=persona, resource=resource, verb=verb
    )


def test_read_on_one_channel_makes_a_member_of_that_channel_and_no_other() -> None:
    agent = _channel_grant("#backend", read=Decision.ALLOW)

    assert _decide(agent=agent, verb=CHANNEL_READ_VERB).decision is Decision.ALLOW
    elsewhere = _decide(agent=agent, verb=CHANNEL_READ_VERB, resource=FRONTEND)
    assert elsewhere.decision is Decision.DENY
    # Deny by default: no entry matched, and the chain says which link had nothing to say.
    assert elsewhere.deciding_link is GrantLink.AGENT


def test_read_alone_is_membership_without_the_right_to_speak() -> None:
    agent = _channel_grant("#backend", read=Decision.ALLOW)

    assert _decide(agent=agent, verb=CHANNEL_WRITE_VERB).decision is Decision.DENY


def test_a_root_deny_on_write_beats_an_agent_allow() -> None:
    root = _channel_grant("*", read=Decision.ALLOW, write=Decision.DENY)
    agent = _channel_grant("#backend", read=Decision.ALLOW, write=Decision.ALLOW)

    speaking = _decide(agent=agent, verb=CHANNEL_WRITE_VERB, root=root)

    assert speaking.decision is Decision.DENY
    assert speaking.deciding_link is GrantLink.ROOT
    # Still a member: the Organisation narrowed what it may say, not whether it listens.
    assert _decide(agent=agent, verb=CHANNEL_READ_VERB, root=root).decision is Decision.ALLOW


def test_a_channel_on_a_product_outside_the_contract_is_unreachable() -> None:
    out_of_scope = Resource(
        resource_type=CHANNEL_RESOURCE_TYPE, identifier="#backend", in_contract_scope=False
    )
    agent = _channel_grant("*", read=Decision.ALLOW)

    decision = _decide(agent=agent, verb=CHANNEL_READ_VERB, resource=out_of_scope)

    assert decision.decision is Decision.DENY
    # No link got a say at all — the one refusal the chain does not make (ADR-0011 §4).
    assert decision.deciding_link is None
    assert "not registered to a Product in the Contract's scope" in decision.reason
