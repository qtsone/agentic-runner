"""Catalogue v6: starting a Work Record and naming a Runner are Grant verbs (ADR-0022 §11)."""

from __future__ import annotations

from agentic_runner_contracts.grants import (
    CATALOGUE_V5,
    CATALOGUE_V6,
    CURRENT_CATALOGUE_VERSION,
    WORK_NAME_RUNNER_VERB,
    WORK_START_VERB,
    Decision,
    GrantLink,
    Resource,
    catalogue_for,
    evaluate_effective_grant,
    expand_and_validate_grant,
)
from agentic_runner_contracts.grants.model import default_persona_allow_list

WORK_RECORD = Resource(resource_type="work", identifier="*", in_contract_scope=True)


def _work_grant(**verbs: str) -> dict[str, object]:
    return {"entries": [{"resource_type": "work", "selector": "*", "verbs": verbs}]}


def test_catalogue_v6_adds_the_two_verbs_on_the_work_record() -> None:
    assert CURRENT_CATALOGUE_VERSION == "v6"
    assert catalogue_for("v6") is CATALOGUE_V6
    assert CATALOGUE_V6.verbs_for("work") == CATALOGUE_V5.verbs_for("work") | {
        WORK_START_VERB,
        WORK_NAME_RUNNER_VERB,
    }
    assert {WORK_START_VERB, WORK_NAME_RUNNER_VERB} <= CATALOGUE_V6.grantable("work")
    assert {
        resource_type: verbs
        for resource_type, verbs in CATALOGUE_V6.verbs.items()
        if resource_type != "work"
    } == {
        resource_type: verbs
        for resource_type, verbs in CATALOGUE_V5.verbs.items()
        if resource_type != "work"
    }
    assert CATALOGUE_V6.expands_mcp


def test_a_work_wildcard_saved_under_v5_is_not_widened_to_the_new_verbs() -> None:
    written_at_v5 = expand_and_validate_grant(
        _work_grant(**{"*": "allow"}), link=GrantLink.ROOT, catalogue=CATALOGUE_V5
    )
    unchanged = expand_and_validate_grant(
        written_at_v5, link=GrantLink.ROOT, catalogue=CATALOGUE_V6
    )
    resaved = expand_and_validate_grant(
        _work_grant(**{"*": "allow"}), link=GrantLink.ROOT, catalogue=CATALOGUE_V6
    )

    assert WORK_START_VERB not in unchanged.entries[0].verbs
    assert {WORK_START_VERB, WORK_NAME_RUNNER_VERB} <= set(resaved.entries[0].verbs)


def test_the_evaluator_answers_each_verb_through_the_whole_chain() -> None:
    root = expand_and_validate_grant(
        _work_grant(**{WORK_START_VERB: "allow", WORK_NAME_RUNNER_VERB: "allow"}),
        link=GrantLink.ROOT,
        catalogue=CATALOGUE_V6,
    )
    agent = expand_and_validate_grant(
        _work_grant(**{WORK_START_VERB: "allow", WORK_NAME_RUNNER_VERB: "deny"}),
        link=GrantLink.AGENT,
        catalogue=CATALOGUE_V6,
    )
    persona = default_persona_allow_list(catalogue=CATALOGUE_V6)

    def decide(verb: str) -> Decision:
        return evaluate_effective_grant(
            root=root, agent=agent, persona=persona, resource=WORK_RECORD, verb=verb
        ).decision

    assert decide(WORK_START_VERB) is Decision.ALLOW
    assert decide(WORK_NAME_RUNNER_VERB) is Decision.DENY
