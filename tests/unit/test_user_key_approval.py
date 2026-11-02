"""An Admin's approval tag on a member's User Key (QTS-2699, Spec: Agent Guardrails rev 10, S5).

Asserted against ``user-key-approval.fixture.json``, shipped in the contracts wheel, which
the console's tests read too: the browser that writes a tag and the Runner that checks it
derive and compute it by the same rules, so a divergence fails here or there rather than
as a member who never gets a Secret.
"""

from __future__ import annotations

import base64
import json
from importlib.resources import files
from typing import Any

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from agentic_runner_contracts.user_key_approval import (
    APPROVAL_DOMAIN,
    approval_key,
    approval_message,
    approval_tag,
    approval_tag_valid,
)


def _fixture(name: str) -> dict[str, Any]:
    raw = files("agentic_runner_contracts").joinpath(f"fixtures/{name}.fixture.json")
    return dict(json.loads(raw.read_text(encoding="utf-8")))


FIXTURE = _fixture("user-key-approval")


def _inputs() -> dict[str, str]:
    return {
        "organisation_key_private": FIXTURE["organisation_key_private"],
        "organisation_id": FIXTURE["organisation_id"],
        "user_id": FIXTURE["user_id"],
        "user_key_fingerprint": FIXTURE["user_key_fingerprint"],
    }


def test_the_fixture_is_the_secret_and_user_key_fixtures_member_and_organisation() -> None:
    secret = _fixture("secret")
    user_key = _fixture("user-key")

    assert FIXTURE["approval_domain"] == APPROVAL_DOMAIN
    assert FIXTURE["organisation_key_private"] == secret["organisation_key_private"]
    assert FIXTURE["organisation_id"] == secret["organisation_id"]
    assert FIXTURE["user_id"] == user_key["user_profile_id"]
    assert FIXTURE["user_key_fingerprint"] == user_key["user_key_fingerprint"]


def test_the_key_is_rfc_5869_hkdf_of_the_organisation_keys_private_half() -> None:
    reference = HKDF(
        algorithm=hashes.SHA256(), length=32, salt=None, info=APPROVAL_DOMAIN.encode()
    ).derive(base64.b64decode(FIXTURE["organisation_key_private"]))

    assert approval_key(FIXTURE["organisation_key_private"]) == reference
    assert base64.b64encode(reference).decode() == FIXTURE["approval_key"]


def test_the_tag_matches_the_fixture_and_verifies() -> None:
    inputs = _inputs()
    message = approval_message(
        organisation_id=inputs["organisation_id"],
        user_id=inputs["user_id"],
        user_key_fingerprint=inputs["user_key_fingerprint"],
    )

    assert message.decode() == FIXTURE["approval_message"]
    assert approval_tag(**inputs) == FIXTURE["approval_tag"]
    assert approval_tag_valid(FIXTURE["approval_tag"], **inputs)


@pytest.mark.parametrize(
    "refused", FIXTURE["refused"], ids=[item["why"] for item in FIXTURE["refused"]]
)
def test_a_tag_for_anything_else_does_not_verify(refused: dict[str, str]) -> None:
    assert not approval_tag_valid(refused["approval_tag"], **_inputs())


@pytest.mark.parametrize("tag", [None, "", "not base64 at all", "A" * 44])
def test_no_tag_or_a_malformed_one_does_not_verify(tag: str | None) -> None:
    assert not approval_tag_valid(tag, **_inputs())


@pytest.mark.parametrize("private", ["not base64", base64.b64encode(b"short").decode()])
def test_a_malformed_organisation_key_is_refused(private: str) -> None:
    with pytest.raises(ValueError, match="Organisation Key"):
        approval_key(private)
