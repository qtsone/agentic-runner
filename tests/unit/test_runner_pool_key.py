"""The Pool Key on a Runner, and the LLM Keys it opens (Spec: Runners rev 3).

The fixture is the one the console's tests read (``llm-key.fixture.json``), so the Runner
here opens exactly what the admin's browser stores.
"""

from __future__ import annotations

import json
import stat
from importlib.resources import files
from pathlib import Path

import pytest

from agentic_runner.llm_proxy import CredentialSlot, llm_slot_references
from agentic_runner.pool_key import (
    POOL_KEY_ENV,
    POOL_KEY_FILENAME,
    LlmKeyOpener,
    LlmKeyUnopenableError,
    PoolKeyError,
    load_pool_key,
    parse_pool_key,
    save_pool_key,
)
from agentic_runner.sealed_box import RecipientKeyPair, RecipientKeyStore, generate_recipient_key
from agentic_runner_contracts.llm_key import LlmKeyRelease


def _fixture() -> dict[str, str]:
    raw = files("agentic_runner_contracts").joinpath("fixtures/llm-key.fixture.json")
    return dict(json.loads(raw.read_text(encoding="utf-8")))


def _release(fixture: dict[str, str]) -> LlmKeyRelease:
    return LlmKeyRelease.model_validate(
        {name: fixture[name] for name in LlmKeyRelease.model_fields}
    )


def _recipient_keys(tmp_path: Path, fixture: dict[str, str]) -> RecipientKeyStore:
    """A Runner whose Recipient Key is the one QTS sealed the fixture's share to."""

    keys = RecipientKeyStore(tmp_path)
    pair = RecipientKeyPair(
        key_id=fixture["recipient_key_id"],
        public_key=fixture["recipient_public_key"],
        private_key=fixture["recipient_private_key"],
    )
    keys.install(pair, managed_by="test", now=keys.rotated_at())
    return keys


def test_the_pool_key_is_the_consoles_private_half_and_its_fingerprint() -> None:
    fixture = _fixture()

    pool_key = parse_pool_key(f"  {fixture['pool_key_private']}\n")

    assert pool_key.public_key == fixture["pool_key_public"]
    assert pool_key.fingerprint == fixture["pool_key_fingerprint"]
    assert fixture["pool_key_private"] not in repr(pool_key)


# Fixed ids: xdist requires every worker to collect the same names, and a key id is random.
@pytest.mark.parametrize(
    "value",
    ["not base64!", "AAAA", generate_recipient_key().key_id],
    ids=["not-base64", "too-short", "recipient-key-id"],
)
def test_a_value_that_is_not_a_pool_key_is_refused(value: str) -> None:
    with pytest.raises(PoolKeyError):
        parse_pool_key(value)


def test_the_pool_key_is_read_from_the_environment_and_taken_out_of_it(tmp_path: Path) -> None:
    fixture = _fixture()
    environ = {POOL_KEY_ENV: fixture["pool_key_private"], "PATH": "/usr/bin"}

    pool_key = load_pool_key(tmp_path, environ)

    assert pool_key is not None and pool_key.fingerprint == fixture["pool_key_fingerprint"]
    assert environ == {"PATH": "/usr/bin"}, "no child inherits it"
    assert load_pool_key(tmp_path, {}) is None, "a Runner given no Pool Key holds none"


def test_a_workstation_keeps_its_pool_key_private_for_every_later_start(tmp_path: Path) -> None:
    fixture = _fixture()

    save_pool_key(tmp_path, fixture["pool_key_private"])
    pool_key = load_pool_key(tmp_path, {})

    assert pool_key is not None and pool_key.fingerprint == fixture["pool_key_fingerprint"]
    assert stat.S_IMODE((tmp_path / POOL_KEY_FILENAME).stat().st_mode) == 0o600
    with pytest.raises(PoolKeyError):
        save_pool_key(tmp_path, "AAAA")


def test_a_released_llm_key_opens_into_a_slot_for_its_provider(tmp_path: Path) -> None:
    fixture = _fixture()
    opener = LlmKeyOpener(
        pool_key=parse_pool_key(fixture["pool_key_private"]),
        recipient_keys=_recipient_keys(tmp_path, fixture),
        endpoints=llm_slot_references({"anthropic": "https://gateway.example/v1"}),
    )

    slot = opener.open(_release(fixture))

    assert isinstance(slot, CredentialSlot)
    assert (slot.value, slot.provider_name, slot.reference) == (
        fixture["llm_key"],
        "anthropic",
        "ANTHROPIC_API_KEY",
    )
    assert (slot.key_id, slot.base_url) == (fixture["llm_key_id"], "https://gateway.example/v1")


def test_a_runner_with_another_pool_key_or_none_opens_nothing(tmp_path: Path) -> None:
    fixture = _fixture()
    keys = _recipient_keys(tmp_path, fixture)
    release = _release(fixture)
    old = parse_pool_key(generate_recipient_key().private_key)

    for pool_key in (old, None):
        with pytest.raises(LlmKeyUnopenableError):
            LlmKeyOpener(pool_key=pool_key, recipient_keys=keys).open(release)
    # Claiming the Runner's own fingerprint does not help: the share is sealed to the other.
    with pytest.raises(LlmKeyUnopenableError):
        LlmKeyOpener(pool_key=old, recipient_keys=keys).open(
            release.model_copy(update={"pool_key_fingerprint": old.fingerprint})
        )


def test_a_platform_share_sealed_to_another_runner_opens_nothing(tmp_path: Path) -> None:
    fixture = _fixture()
    opener = LlmKeyOpener(
        pool_key=parse_pool_key(fixture["pool_key_private"]),
        recipient_keys=RecipientKeyStore(tmp_path),
    )

    with pytest.raises(LlmKeyUnopenableError):
        opener.open(_release(fixture))
