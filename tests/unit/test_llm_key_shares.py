"""A pool's LLM Key: the split, the join and the sealed shares (Spec: Runners rev 3).

Asserted against ``llm-key.fixture.json``, shipped in the contracts wheel, which the
console's tests read too: the browser splits, seals and binds by the same rules, so a
divergence fails here or there rather than as a key no Runner can open.
"""

from __future__ import annotations

import json
from importlib.resources import files
from uuid import uuid4

import pytest

from agentic_runner.sealed_box import SealedCredentialError, open_sealed
from agentic_runner_contracts import __version__ as contracts_version
from agentic_runner_contracts.llm_key import (
    LlmKeyRelease,
    join_llm_key,
    platform_share_binding,
    pool_share_binding,
    split_llm_key,
)
from agentic_runner_contracts.runner_registration import FloorState, HeartbeatAck
from agentic_runner_contracts.sealed_credential import delivery_binding, key_fingerprint


def _fixture() -> dict[str, str]:
    raw = files("agentic_runner_contracts").joinpath("fixtures/llm-key.fixture.json")
    return dict(json.loads(raw.read_text(encoding="utf-8")))


def _release(fixture: dict[str, str]) -> LlmKeyRelease:
    return LlmKeyRelease.model_validate(
        {name: fixture[name] for name in LlmKeyRelease.model_fields}
    )


def test_the_fixtures_two_shares_join_to_its_key() -> None:
    fixture = _fixture()

    joined = join_llm_key(fixture["pool_share_plaintext"], fixture["platform_share_plaintext"])

    assert joined == fixture["llm_key"]


def test_a_split_joins_back_and_neither_share_is_the_key() -> None:
    key = "sk-proj-a-key-of-the-usual-length-0123456789"

    pool, platform = split_llm_key(key)

    assert join_llm_key(pool, platform) == key
    assert key not in (pool, platform)
    assert split_llm_key(key)[0] != pool, "the random share is fresh on every split"


@pytest.mark.parametrize(
    ("pool", "platform"),
    [("AAAA", "AAAAAAAA"), ("", ""), ("not base64!", "AAAA"), ("//8=", "AAA=")],
)
def test_shares_that_are_not_one_keys_are_refused(pool: str, platform: str) -> None:
    with pytest.raises(ValueError):
        join_llm_key(pool, platform)


def test_the_bindings_and_the_fingerprint_are_the_fixtures() -> None:
    fixture = _fixture()

    assert key_fingerprint(fixture["pool_key_public"]) == fixture["pool_key_fingerprint"]
    assert (
        pool_share_binding(
            pool_id=fixture["pool_id"],
            llm_key_id=fixture["llm_key_id"],
            pool_key_fingerprint=fixture["pool_key_fingerprint"],
        )
        == fixture["pool_share_binding"].encode()
    )
    assert (
        platform_share_binding(
            llm_key_id=fixture["llm_key_id"], recipient_key_id=fixture["recipient_key_id"]
        )
        == fixture["platform_share_binding"].encode()
    )


def test_the_sealed_shares_open_with_the_pool_key_and_the_recipient_key() -> None:
    fixture = _fixture()
    release = _release(fixture)

    pool = open_sealed(
        private_key=fixture["pool_key_private"],
        binding=release.pool_share_binding(),
        ciphertext=release.pool_share,
    )
    platform = open_sealed(
        private_key=fixture["recipient_private_key"],
        binding=release.platform_share_binding(),
        ciphertext=release.platform_share,
    )

    assert (pool, platform) == (
        fixture["pool_share_plaintext"],
        fixture["platform_share_plaintext"],
    )
    assert join_llm_key(pool, platform) == fixture["llm_key"]


def test_a_sealed_share_opens_under_no_other_binding_or_key() -> None:
    fixture = _fixture()
    release = _release(fixture)
    other_key_id = "0e5f8a31-9b2c-4d7e-a610-3c8b1f4d2e95"
    elsewhere = [
        # Another LLM Key's row, a new Pool Key, and another construction's domain.
        release.model_copy(update={"llm_key_id": other_key_id}).pool_share_binding(),
        release.model_copy(
            update={"pool_key_fingerprint": "0000:0000:0000:0000:0000:0000:0000:0000"}
        ).pool_share_binding(),
        delivery_binding(
            contract_id=fixture["pool_id"], slot="ANTHROPIC_API_KEY", recipient_key_id="rk_x" * 4
        ),
    ]

    for binding in elsewhere:
        with pytest.raises(SealedCredentialError):
            open_sealed(
                private_key=fixture["pool_key_private"],
                binding=binding,
                ciphertext=release.pool_share,
            )
    with pytest.raises(SealedCredentialError):
        open_sealed(
            private_key=fixture["recipient_private_key"],
            binding=release.pool_share_binding(),
            ciphertext=release.pool_share,
        )
    other_key = release.model_copy(update={"llm_key_id": other_key_id})
    with pytest.raises(SealedCredentialError):
        open_sealed(
            private_key=fixture["recipient_private_key"],
            binding=other_key.platform_share_binding(),
            ciphertext=release.platform_share,
        )


def test_a_release_carries_no_field_a_key_could_ride_in() -> None:
    fixture = _fixture()
    body = {name: fixture[name] for name in LlmKeyRelease.model_fields}

    with pytest.raises(ValueError):
        LlmKeyRelease.model_validate({**body, "llm_key": fixture["llm_key"]})
    with pytest.raises(ValueError):
        LlmKeyRelease.model_validate({**body, "llm_key_id": "a|b"})


def test_an_ack_names_the_pool_only_to_a_runner_that_parses_it() -> None:
    """A Runner on 3.25 forbids the key, so an ack with no pool is the body it parses; a
    pool with no Pool Key yet is still sent, and says so."""

    ack = {
        "runner_id": str(uuid4()),
        "floor_state": FloorState.OK.value,
        "contracts_floor": contracts_version,
        "tag_set_version": 1,
        "accepts_new_directives": True,
    }

    assert "pool" not in HeartbeatAck.model_validate(ack).model_dump(mode="json")
    keyless = HeartbeatAck.model_validate({**ack, "pool": {}}).model_dump(mode="json")
    assert keyless["pool"] == {"pool_key_fingerprint": None}
