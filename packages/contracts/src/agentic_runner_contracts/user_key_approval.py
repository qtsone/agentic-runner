"""An Admin's approval of a member's User Key (Spec: Agent Guardrails rev 10, S5, S7).

An Organisation or Product secret an Admin marks "Also members' Runners" has its holder
share resealed to each eligible member's User Key, by an Organisation Runner or by an
Admin's browser (:mod:`.secret`). Which User Key is a member's is the control plane's
word, so a control plane that is taken over, or a database that is written to, could
name a key of its own and have every such secret resealed to it. So every reseal first
checks an **approval tag** on the User Key: HMAC-SHA256 over the Organisation, the
member and the User Key fingerprint (:func:`approval_message`), keyed by a key derived
from the Organisation Key's private half (:func:`approval_key`). The control plane never
holds that private half, so it can neither make nor change a tag; it only stores the one
an Admin's browser wrote when the Admin approved the key with their passkey, and hands
it out beside the key.

A tag for another member, Organisation or fingerprint fails, as does one keyed by
anything but this Organisation Key: a new Organisation Key or a new User Key needs a new
approval.

**Format** (the browser's too; the fixture holds both sides to it): the key is
``HKDF-SHA256(organisation_key_private, salt="", info=APPROVAL_DOMAIN)``, 32 bytes; the
tag is ``HMAC-SHA256(key, approval_message(...))``, base64.

``fixtures/user-key-approval.fixture.json`` is one tag computed by these rules, with the
inputs that must not verify. The contracts tests check it, and the console's tests read
the same file.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
from typing import Annotated, Final

from pydantic import StringConstraints

__all__ = [
    "APPROVAL_DOMAIN",
    "ApprovalTag",
    "approval_key",
    "approval_message",
    "approval_tag",
    "approval_tag_valid",
]

APPROVAL_DOMAIN: Final[str] = "agentic-os/user-key-approval/v1"

_KEY_BYTES: Final[int] = 32

# 32 bytes of HMAC-SHA256, base64.
ApprovalTag = Annotated[str, StringConstraints(min_length=44, max_length=44)]


def _private_half(organisation_key_private: str) -> bytes:
    try:
        raw = base64.b64decode(organisation_key_private, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ValueError("the Organisation Key's private half must be base64") from error
    if len(raw) != _KEY_BYTES:
        raise ValueError("the Organisation Key's private half must be 32 bytes")
    return raw


def approval_key(organisation_key_private: str) -> bytes:
    """HKDF-SHA256 (RFC 5869) of the Organisation Key's private half, one 32-byte block.

    An empty salt is HMAC's all-zero key, which is what WebCrypto's HKDF uses too.
    """

    prk = hmac.new(b"", _private_half(organisation_key_private), hashlib.sha256).digest()
    return hmac.new(prk, APPROVAL_DOMAIN.encode("utf-8") + b"\x01", hashlib.sha256).digest()


def approval_message(*, organisation_id: str, user_id: str, user_key_fingerprint: str) -> bytes:
    return "|".join((APPROVAL_DOMAIN, organisation_id, user_id, user_key_fingerprint)).encode(
        "utf-8"
    )


def approval_tag(
    *,
    organisation_key_private: str,
    organisation_id: str,
    user_id: str,
    user_key_fingerprint: str,
) -> str:
    message = approval_message(
        organisation_id=organisation_id, user_id=user_id, user_key_fingerprint=user_key_fingerprint
    )
    digest = hmac.new(approval_key(organisation_key_private), message, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def approval_tag_valid(
    tag: str | None,
    *,
    organisation_key_private: str,
    organisation_id: str,
    user_id: str,
    user_key_fingerprint: str,
) -> bool:
    """Whether ``tag`` approves this member's User Key in this Organisation. No tag is not
    an approval."""

    if not tag:
        return False
    expected = approval_tag(
        organisation_key_private=organisation_key_private,
        organisation_id=organisation_id,
        user_id=user_id,
        user_key_fingerprint=user_key_fingerprint,
    )
    return hmac.compare_digest(expected.encode("ascii"), tag.encode("ascii", errors="replace"))
