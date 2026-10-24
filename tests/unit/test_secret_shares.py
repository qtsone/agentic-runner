"""A Secret: the split, the join and the sealed shares (Spec: Agent Guardrails rev 7, S3-S4).

Asserted against ``secret.fixture.json``, shipped in the contracts wheel, which the
console's tests read too: the browser splits, seals and binds by the same rules, so a
divergence fails here or there rather than as a secret no Runner can open.
"""

from __future__ import annotations

import json
from importlib.resources import files
from typing import Any

import pytest

from agentic_runner.sealed_box import SealedCredentialError, open_sealed
from agentic_runner_contracts.llm_key import platform_share_binding as llm_platform_binding
from agentic_runner_contracts.sealed_credential import key_fingerprint
from agentic_runner_contracts.secret import (
    HolderKind,
    ReleasedSecret,
    SecretRelease,
    SecretReleaseRequest,
    holder_share_binding,
    join_secret,
    platform_share_binding,
    split_secret,
)


def _fixture() -> dict[str, str]:
    raw = files("agentic_runner_contracts").joinpath("fixtures/secret.fixture.json")
    return dict(json.loads(raw.read_text(encoding="utf-8")))


def _release(fixture: dict[str, str]) -> SecretRelease:
    return SecretRelease.model_validate(
        {
            "organisation_id": fixture["organisation_id"],
            "recipient_key_id": fixture["recipient_key_id"],
            "secrets": [_released(fixture)],
        }
    )


def _released(fixture: dict[str, str]) -> dict[str, Any]:
    return {
        "secret_id": fixture["secret_id"],
        "name": fixture["name"],
        "scope": fixture["scope"],
        "holder_kind": fixture["holder_kind"],
        "holder_fingerprint": fixture["holder_fingerprint"],
        "holder_share": fixture["holder_share"],
        "platform_share": fixture["platform_share"],
    }


def test_the_fixtures_two_shares_join_to_its_value() -> None:
    fixture = _fixture()

    joined = join_secret(fixture["holder_share_plaintext"], fixture["platform_share_plaintext"])

    assert joined == fixture["value"]


def test_a_split_joins_back_and_neither_share_is_the_value() -> None:
    value = "ghp_a-token-of-the-usual-length-0123456789"

    holder, platform = split_secret(value)

    assert join_secret(holder, platform) == value
    assert value not in (holder, platform)
    assert split_secret(value)[0] != holder, "the random share is fresh on every split"


@pytest.mark.parametrize(
    ("holder", "platform"),
    [("AAAA", "AAAAAAAA"), ("", ""), ("not base64!", "AAAA"), ("//8=", "AAA=")],
)
def test_shares_that_are_not_one_values_are_refused(holder: str, platform: str) -> None:
    with pytest.raises(ValueError):
        join_secret(holder, platform)


def test_the_bindings_and_the_fingerprint_are_the_fixtures() -> None:
    fixture = _fixture()

    assert key_fingerprint(fixture["organisation_key_public"]) == fixture["holder_fingerprint"]
    assert (
        holder_share_binding(
            organisation_id=fixture["organisation_id"],
            secret_id=fixture["secret_id"],
            holder_kind=HolderKind(fixture["holder_kind"]),
            holder_fingerprint=fixture["holder_fingerprint"],
        )
        == fixture["holder_share_binding"].encode()
    )
    assert (
        platform_share_binding(
            secret_id=fixture["secret_id"], recipient_key_id=fixture["recipient_key_id"]
        )
        == fixture["platform_share_binding"].encode()
    )


def test_the_sealed_shares_open_with_the_organisation_key_and_the_recipient_key() -> None:
    fixture = _fixture()
    release = _release(fixture)
    secret = release.secrets[0]

    holder = open_sealed(
        private_key=fixture["organisation_key_private"],
        binding=release.holder_share_binding(secret),
        ciphertext=secret.holder_share,
    )
    platform = open_sealed(
        private_key=fixture["recipient_private_key"],
        binding=release.platform_share_binding(secret),
        ciphertext=secret.platform_share,
    )

    assert join_secret(holder, platform) == fixture["value"]


def test_a_sealed_share_opens_under_no_other_binding() -> None:
    fixture = _fixture()
    release = _release(fixture)
    secret = release.secrets[0]
    other_id = "0e5f8a31-9b2c-4d7e-a610-3c8b1f4d2e95"
    elsewhere = [
        # Another secret's row, another Organisation, a new Organisation Key, a User Key.
        release.holder_share_binding(secret.model_copy(update={"secret_id": other_id})),
        release.model_copy(update={"organisation_id": other_id}).holder_share_binding(secret),
        release.holder_share_binding(
            secret.model_copy(
                update={"holder_fingerprint": "0000:0000:0000:0000:0000:0000:0000:0000"}
            )
        ),
        release.holder_share_binding(
            secret.model_copy(update={"holder_kind": HolderKind.USER_KEY})
        ),
    ]
    for binding in elsewhere:
        with pytest.raises(SealedCredentialError):
            open_sealed(
                private_key=fixture["organisation_key_private"],
                binding=binding,
                ciphertext=secret.holder_share,
            )
    # Never as an LLM Key's share of the same id: its own domain keeps the two apart.
    with pytest.raises(SealedCredentialError):
        open_sealed(
            private_key=fixture["recipient_private_key"],
            binding=llm_platform_binding(
                llm_key_id=fixture["secret_id"], recipient_key_id=fixture["recipient_key_id"]
            ),
            ciphertext=secret.platform_share,
        )


def test_a_release_carries_no_field_a_value_could_ride_in() -> None:
    fixture = _fixture()
    body = _released(fixture)

    with pytest.raises(ValueError):
        ReleasedSecret.model_validate({**body, "value": fixture["value"]})
    with pytest.raises(ValueError):
        ReleasedSecret.model_validate({**body, "secret_id": "a|b"})
    with pytest.raises(ValueError):
        ReleasedSecret.model_validate({**body, "name": "../SLACK"})


def test_a_request_names_each_secret_once_and_some_holder_key() -> None:
    fingerprint = _fixture()["holder_fingerprint"]

    SecretReleaseRequest(directive_id="d", names=["A", "B"], holder_fingerprints=[fingerprint])
    with pytest.raises(ValueError):
        SecretReleaseRequest(directive_id="d", names=["A", "A"], holder_fingerprints=[fingerprint])
    with pytest.raises(ValueError):
        SecretReleaseRequest(directive_id="d", names=["A"], holder_fingerprints=[])
