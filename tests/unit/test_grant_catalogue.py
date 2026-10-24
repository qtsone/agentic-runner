"""Catalogue v6: starting a Work Record and naming a Runner are Grant verbs (ADR-0022 §11).
Catalogue v7: so is each harness tool of a Listener firing (QTS-2452).
Catalogue v8: so is the Draft tool, which landed after v7 (QTS-2486)."""

from __future__ import annotations

from agentic_runner_contracts.grants import (
    CATALOGUE_V5,
    CATALOGUE_V6,
    CATALOGUE_V7,
    CATALOGUE_V8,
    CURRENT_CATALOGUE_VERSION,
    LISTENER_DRAFT_VERB,
    LISTENER_REACT_VERB,
    LISTENER_READ_VERB,
    LISTENER_REPLY_VERB,
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


HARNESS_VERBS = {LISTENER_REPLY_VERB, LISTENER_REACT_VERB, LISTENER_READ_VERB}


def test_catalogue_v7_adds_one_verb_per_harness_tool_and_nothing_else() -> None:
    assert catalogue_for("v7") is CATALOGUE_V7
    assert CATALOGUE_V7.verbs == {
        **CATALOGUE_V6.verbs,
        "work": CATALOGUE_V6.verbs_for("work") | HARNESS_VERBS,
    }
    assert CATALOGUE_V7.grantable("work") >= HARNESS_VERBS
    assert CATALOGUE_V7.expands_mcp


def test_a_work_wildcard_saved_under_v6_is_not_widened_to_the_harness_verbs() -> None:
    written_at_v6 = expand_and_validate_grant(
        _work_grant(**{"*": "deny"}), link=GrantLink.AGENT, catalogue=CATALOGUE_V6
    )
    unchanged = expand_and_validate_grant(
        written_at_v6, link=GrantLink.AGENT, catalogue=CATALOGUE_V7
    )
    resaved = expand_and_validate_grant(
        _work_grant(**{"*": "deny"}), link=GrantLink.AGENT, catalogue=CATALOGUE_V7
    )

    assert not HARNESS_VERBS & set(unchanged.entries[0].verbs)
    assert set(resaved.entries[0].verbs) >= HARNESS_VERBS


def test_catalogue_v8_adds_the_draft_verb_and_nothing_else() -> None:
    assert CURRENT_CATALOGUE_VERSION == "v8"
    assert catalogue_for("v8") is CATALOGUE_V8
    assert CATALOGUE_V8.verbs == {
        **CATALOGUE_V7.verbs,
        "work": CATALOGUE_V7.verbs_for("work") | {LISTENER_DRAFT_VERB},
    }
    assert LISTENER_DRAFT_VERB in CATALOGUE_V8.grantable("work")
    assert CATALOGUE_V8.expands_mcp


def test_a_work_wildcard_saved_under_v7_is_not_widened_to_the_draft_verb() -> None:
    written_at_v7 = expand_and_validate_grant(
        _work_grant(**{"*": "deny"}), link=GrantLink.AGENT, catalogue=CATALOGUE_V7
    )
    unchanged = expand_and_validate_grant(
        written_at_v7, link=GrantLink.AGENT, catalogue=CATALOGUE_V8
    )
    resaved = expand_and_validate_grant(
        _work_grant(**{"*": "deny"}), link=GrantLink.AGENT, catalogue=CATALOGUE_V8
    )

    assert LISTENER_DRAFT_VERB not in unchanged.entries[0].verbs
    assert LISTENER_DRAFT_VERB in resaved.entries[0].verbs
