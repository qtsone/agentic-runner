"""An Admin's availability tag on an "Also members' Runners" Secret (QTS-2701, Spec: Agent
Guardrails rev 12, S5).

Asserted against ``secret-availability.fixture.json``, shipped in the contracts wheel,
which the console's tests read too: the browser that writes a tag and the Runner that
checks it derive and compute it by the same rules.
"""

from __future__ import annotations

import base64
import json
from importlib.resources import files
from typing import Any

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from agentic_runner_contracts.secret_availability import (
    AVAILABILITY_DOMAIN,
    availability_key,
    availability_message,
    availability_tag,
    availability_tag_valid,
)
from agentic_runner_contracts.user_key_approval import APPROVAL_DOMAIN


def _fixture(name: str) -> dict[str, Any]:
    raw = files("agentic_runner_contracts").joinpath(f"fixtures/{name}.fixture.json")
    return dict(json.loads(raw.read_text(encoding="utf-8")))


FIXTURE = _fixture("secret-availability")


def _inputs() -> dict[str, str]:
    return {
        "organisation_key_private": FIXTURE["organisation_key_private"],
        "organisation_id": FIXTURE["organisation_id"],
        "secret_id": FIXTURE["secret_id"],
    }


def test_the_fixture_is_the_secret_fixtures_secret_and_organisation() -> None:
    secret = _fixture("secret")

    assert FIXTURE["availability_domain"] == AVAILABILITY_DOMAIN
    assert AVAILABILITY_DOMAIN != APPROVAL_DOMAIN
    assert FIXTURE["organisation_key_private"] == secret["organisation_key_private"]
    assert FIXTURE["organisation_id"] == secret["organisation_id"]
    assert FIXTURE["secret_id"] == secret["secret_id"]


def test_the_key_is_rfc_5869_hkdf_of_the_organisation_keys_private_half() -> None:
    reference = HKDF(
        algorithm=hashes.SHA256(), length=32, salt=None, info=AVAILABILITY_DOMAIN.encode()
    ).derive(base64.b64decode(FIXTURE["organisation_key_private"]))

    assert availability_key(FIXTURE["organisation_key_private"]) == reference
    assert base64.b64encode(reference).decode() == FIXTURE["availability_key"]


def test_the_tag_matches_the_fixture_and_verifies() -> None:
    inputs = _inputs()
    message = availability_message(
        organisation_id=inputs["organisation_id"], secret_id=inputs["secret_id"]
    )

    assert message.decode() == FIXTURE["availability_message"]
    assert availability_tag(**inputs) == FIXTURE["availability_tag"]
    assert availability_tag_valid(FIXTURE["availability_tag"], **inputs)


@pytest.mark.parametrize(
    "refused", FIXTURE["refused"], ids=[item["why"] for item in FIXTURE["refused"]]
)
def test_a_tag_for_anything_else_does_not_verify(refused: dict[str, str]) -> None:
    assert not availability_tag_valid(refused["availability_tag"], **_inputs())


@pytest.mark.parametrize("tag", [None, "", "not base64 at all", "A" * 44])
def test_no_tag_or_a_malformed_one_does_not_verify(tag: str | None) -> None:
    assert not availability_tag_valid(tag, **_inputs())


@pytest.mark.parametrize(
    "ids",
    [
        {"organisation_id": "a|b", "secret_id": "c"},
        {"organisation_id": "a", "secret_id": "b|c"},
        {"organisation_id": "", "secret_id": "c"},
    ],
)
def test_an_id_that_could_read_two_ways_is_refused(ids: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="identifier"):
        availability_message(**ids)
