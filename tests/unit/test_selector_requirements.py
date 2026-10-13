"""Selectors as Kubernetes ``matchExpressions`` (ADR-0022 §22).

A selector is a list of requirements, all of which must hold; each is ``In``, ``NotIn``,
``Exists`` or ``DoesNotExist``. Layers narrow by concatenation, a provably empty
conjunction is refused, and a legacy ``{key: value}`` map converts without changing what
it matched.
"""

from __future__ import annotations

from typing import Any

import pytest
from temporalio.converter import DataConverter

from agentic_runner_contracts.grants.snapshot import GrantSnapshot
from agentic_runner_contracts.routing import (
    PRESENT,
    DirectiveRouting,
    SelectorConflictError,
    SelectorOperator,
    SelectorRequirement,
    narrow_selector,
    selector_from_legacy,
    selector_satisfied,
)

IN = SelectorOperator.IN
NOT_IN = SelectorOperator.NOT_IN
EXISTS = SelectorOperator.EXISTS
DOES_NOT_EXIST = SelectorOperator.DOES_NOT_EXIST

TAGS = {"product": "a", "gpu": "h100"}


def _req(key: str, operator: SelectorOperator, *values: str) -> SelectorRequirement:
    return SelectorRequirement(key=key, operator=operator, values=values)


@pytest.mark.parametrize(
    ("requirement", "satisfied"),
    [
        (_req("product", IN, "a"), True),
        (_req("product", IN, "a", "b"), True),
        (_req("product", IN, "b", "c"), False),
        (_req("zone", IN, "a"), False),
        (_req("product", NOT_IN, "c"), True),
        (_req("product", NOT_IN, "a", "b"), False),
        # Kubernetes semantics: NotIn passes when the key is absent.
        (_req("zone", NOT_IN, "a"), True),
        (_req("gpu", EXISTS), True),
        (_req("zone", EXISTS), False),
        (_req("zone", DOES_NOT_EXIST), True),
        (_req("gpu", DOES_NOT_EXIST), False),
        # A value is a value, never a pattern.
        (_req("product", IN, "*"), False),
    ],
)
def test_each_operator_against_present_and_absent_keys(
    requirement: SelectorRequirement, satisfied: bool
) -> None:
    assert selector_satisfied([requirement], TAGS) is satisfied


def test_every_requirement_must_hold() -> None:
    assert selector_satisfied([_req("product", IN, "a"), _req("gpu", EXISTS)], TAGS)
    assert not selector_satisfied([_req("product", IN, "a"), _req("zone", EXISTS)], TAGS)
    assert selector_satisfied([], TAGS)


@pytest.mark.parametrize(
    ("operator", "values"),
    [(IN, ()), (NOT_IN, ()), (EXISTS, ("a",)), (DOES_NOT_EXIST, ("a",))],
)
def test_a_requirement_takes_values_exactly_when_its_operator_does(
    operator: SelectorOperator, values: tuple[str, ...]
) -> None:
    with pytest.raises(ValueError):
        SelectorRequirement(key="product", operator=operator, values=values)


def test_narrowing_concatenates_the_layers() -> None:
    product = [_req("product", IN, "a", "b")]
    contract = [_req("product", NOT_IN, "b")]
    profile = [_req("gpu", EXISTS)]

    assert narrow_selector(product, contract, profile) == (*product, *contract, *profile)
    assert narrow_selector() == ()


@pytest.mark.parametrize(
    "layers",
    [
        ([_req("region", IN, "eu")], [_req("region", IN, "us")]),
        ([_req("region", IN, "eu", "us")], [_req("region", NOT_IN, "eu", "us")]),
        ([_req("region", EXISTS)], [_req("region", DOES_NOT_EXIST)]),
        ([_req("region", IN, "eu")], [_req("region", DOES_NOT_EXIST)]),
        # One layer can contradict itself just as two can.
        ([_req("region", IN, "eu"), _req("region", IN, "us")],),
    ],
)
def test_a_provably_empty_narrowing_is_refused(
    layers: tuple[list[SelectorRequirement], ...],
) -> None:
    with pytest.raises(SelectorConflictError):
        narrow_selector(*layers)


@pytest.mark.parametrize(
    "layers",
    [
        ([_req("region", IN, "eu", "us")], [_req("region", IN, "us", "ap")]),
        ([_req("region", IN, "eu", "us")], [_req("region", NOT_IN, "eu")]),
        # Absent satisfies both, so this narrowing still has a Runner it can match.
        ([_req("region", NOT_IN, "eu")], [_req("region", DOES_NOT_EXIST)]),
        ([_req("region", EXISTS)], [_req("region", NOT_IN, "eu")]),
    ],
)
def test_a_narrowing_some_runner_can_satisfy_is_kept(
    layers: tuple[list[SelectorRequirement], ...],
) -> None:
    narrow_selector(*layers)


@pytest.mark.parametrize(
    ("legacy", "converted"),
    [
        ({}, ()),
        ({"region": "eu"}, (_req("region", IN, "eu"),)),
        # The legacy presence marker meant "the key, whatever its value".
        ({"gpu": PRESENT}, (_req("gpu", EXISTS),)),
    ],
)
def test_a_legacy_map_converts_to_requirements(
    legacy: dict[str, str], converted: tuple[SelectorRequirement, ...]
) -> None:
    assert selector_from_legacy(legacy) == converted


@pytest.mark.parametrize(
    "legacy",
    [{"product": "a"}, {"product": "b"}, {"gpu": PRESENT}, {"zone": PRESENT}, {"zone": "x"}],
)
def test_a_converted_legacy_map_matches_exactly_what_it_matched(legacy: dict[str, str]) -> None:
    before = all(
        key in TAGS and (value == PRESENT or TAGS[key] == value) for key, value in legacy.items()
    )
    assert selector_satisfied(selector_from_legacy(legacy), TAGS) is before


def _decode(payload: dict[str, Any]) -> DirectiveRouting:
    converter = DataConverter.default.payload_converter
    [encoded] = converter.to_payloads([payload])
    [decoded] = converter.from_payloads([encoded], [DirectiveRouting])
    assert isinstance(decoded, DirectiveRouting)
    return decoded


def test_routing_carries_requirements_over_the_wire() -> None:
    routing = DirectiveRouting(
        runner_id="r-1",
        host_party="organisation",
        requirements=(_req("product", IN, "a", "b"), _req("zone", DOES_NOT_EXIST)),
    )
    converter = DataConverter.default.payload_converter
    [encoded] = converter.to_payloads([routing])
    [decoded] = converter.from_payloads([encoded], [DirectiveRouting])

    assert decoded == routing


def test_a_routing_payload_from_before_requirements_still_decodes() -> None:
    decoded = _decode(
        {"runner_id": "r-1", "host_party": "organisation", "selector": {"region": "eu"}}
    )

    assert decoded.requirements == ()
    assert decoded.matched_on() == (_req("region", IN, "eu"),)


def test_routing_is_matched_on_its_legacy_map_and_its_requirements_together() -> None:
    routing = DirectiveRouting(
        runner_id="r-1",
        host_party="organisation",
        selector={"gpu": PRESENT},
        requirements=(_req("product", NOT_IN, "c"),),
    )

    assert routing.matched_on() == (_req("gpu", EXISTS), _req("product", NOT_IN, "c"))


def test_a_product_reach_reads_its_legacy_map_and_its_requirements() -> None:
    snapshot = GrantSnapshot.from_payload(
        {
            "agent_id": "agent-1",
            "contract_id": "contract-1",
            "contract_state": "active",
            "dispatchable": True,
            "root_grant": {"entries": []},
            "agent_grant": {"entries": []},
            "persona_allow_list": {"entries": []},
            "reach": [
                {
                    "product_id": "product-1",
                    "runner_selector": {"pool": "hetzner"},
                    "runner_requirements": [
                        {"key": "product", "operator": "NotIn", "values": ["c"]}
                    ],
                },
                {"product_id": "product-2", "runner_selector": {}},
            ],
        }
    )

    assert snapshot.runner_selector_of("product-1") == (
        _req("pool", IN, "hetzner"),
        _req("product", NOT_IN, "c"),
    )
    assert snapshot.runner_selector_of("product-2") == ()
    assert snapshot.runner_selector_of("product-3") is None
