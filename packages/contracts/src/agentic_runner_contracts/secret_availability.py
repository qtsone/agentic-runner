"""An Admin's choice of "Also members' Runners" for one Secret (Spec: Agent Guardrails
rev 12, S5, S7, S8, A1).

An Organisation or Product Secret an Admin marks "Also members' Runners" has its holder
share resealed to approved members' User Keys (:mod:`.secret`, :mod:`.user_key_approval`).
Which Secrets are marked was the control plane's word alone, so a control plane that is
taken over, holding one approved member key, could have any Organisation Secret resealed
to it. So every reseal also checks an **availability tag** on the Secret: HMAC-SHA256
over the Organisation and the Secret (:func:`availability_message`), keyed by a key
derived from the Organisation Key's private half under :data:`AVAILABILITY_DOMAIN`. The
control plane never holds that private half, so it can neither make nor change a tag; it
stores the one an Admin's browser wrote when the Admin chose "Also members' Runners"
with their passkey, deletes it when the Admin switches back, and hands it out beside the
share.

A tag for another Secret or Organisation fails, as does one keyed by anything but this
Organisation Key, and an approval tag (its own domain, so its own key and message).

**Format** (the browser's too; the fixture holds both sides to it): the key is
``HKDF-SHA256(organisation_key_private, salt="", info=AVAILABILITY_DOMAIN)``, 32 bytes;
the tag is ``HMAC-SHA256(key, availability_message(...))``, base64. Both ids are
identifiers with no ``|``, so the joined message reads one way only.

A tag does not expire: a control plane that is taken over could restore one an Admin
deleted. Only a new Organisation Key revokes every tag at once.

``fixtures/secret-availability.fixture.json`` is one tag computed by these rules, with
the tags that must not verify. The contracts tests check it, and the console's tests read
the same file.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
from typing import Annotated, Final

from pydantic import StringConstraints

from agentic_runner_contracts.user_key_approval import organisation_key_hmac_key

__all__ = [
    "AVAILABILITY_DOMAIN",
    "AvailabilityTag",
    "availability_key",
    "availability_message",
    "availability_tag",
    "availability_tag_valid",
]

AVAILABILITY_DOMAIN: Final[str] = "agentic-os/secret-availability/v1"

# 32 bytes of HMAC-SHA256, base64.
AvailabilityTag = Annotated[str, StringConstraints(min_length=44, max_length=44)]

# The `Identifier` pattern of :mod:`.llm_key`, checked here too because these functions
# take plain strings.
_IDENTIFIER: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


def availability_key(organisation_key_private: str) -> bytes:
    return organisation_key_hmac_key(organisation_key_private, AVAILABILITY_DOMAIN)


def availability_message(*, organisation_id: str, secret_id: str) -> bytes:
    for name, value in (("organisation_id", organisation_id), ("secret_id", secret_id)):
        if not _IDENTIFIER.fullmatch(value):
            raise ValueError(f"{name} must be an identifier")
    return "|".join((AVAILABILITY_DOMAIN, organisation_id, secret_id)).encode("utf-8")


def availability_tag(*, organisation_key_private: str, organisation_id: str, secret_id: str) -> str:
    message = availability_message(organisation_id=organisation_id, secret_id=secret_id)
    digest = hmac.new(availability_key(organisation_key_private), message, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def availability_tag_valid(
    tag: str | None,
    *,
    organisation_key_private: str,
    organisation_id: str,
    secret_id: str,
) -> bool:
    """Whether ``tag`` marks this Secret of this Organisation "Also members' Runners". No
    tag is not a mark."""

    if not tag:
        return False
    expected = availability_tag(
        organisation_key_private=organisation_key_private,
        organisation_id=organisation_id,
        secret_id=secret_id,
    )
    return hmac.compare_digest(expected.encode("ascii"), tag.encode("ascii", errors="replace"))
