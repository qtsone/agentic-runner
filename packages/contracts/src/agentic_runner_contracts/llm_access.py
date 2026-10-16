"""Whose LLM access a Directive uses, and under which key name (Spec: Runners rev 3).

A Runner uses only its host's LLM access: a personal Runner its person's subscription or
key, a pool Runner its pool's keys. The control plane chooses one when it routes and
names it in the Directive's routing as :class:`LlmAccess`; the Runner uses exactly that
and fails the Directive closed when it no longer holds it, never falling back to other
access.

The key-name table is here, in the contracts, because both halves read it: the Runner
fills its LLM proxy from its own store by these names, and the control plane refuses
them as Credential References. Stdlib only, like :mod:`agentic_runner_contracts.routing`,
which carries :class:`LlmAccess` on every Runner activity input.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final

__all__ = [
    "PROVIDER_KEY_NAMES",
    "RUNTIME_PROVIDERS",
    "LlmAccess",
    "LlmAccessKind",
    "LlmProvider",
    "key_name_for",
    "provider_for",
]


class LlmProvider(StrEnum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"


# Named after the variable each vendor's own CLI reads, so a host stores the name the
# vendor's docs already taught them.
PROVIDER_KEY_NAMES: Final[Mapping[LlmProvider, str]] = MappingProxyType(
    {
        LlmProvider.ANTHROPIC: "ANTHROPIC_API_KEY",
        LlmProvider.OPENAI: "OPENAI_API_KEY",
    }
)

# The provider each Agent Runtime's key comes from. A `cli_kind` absent here has no key
# the Runner can hold for it: an ACP harness the Profile names brings its own access.
RUNTIME_PROVIDERS: Final[Mapping[str, LlmProvider]] = MappingProxyType(
    {
        "claude_code": LlmProvider.ANTHROPIC,
        "codex_cli": LlmProvider.OPENAI,
    }
)


def provider_for(cli_kind: str) -> LlmProvider | None:
    return RUNTIME_PROVIDERS.get(cli_kind)


def key_name_for(cli_kind: str) -> str | None:
    provider = provider_for(cli_kind)
    return None if provider is None else PROVIDER_KEY_NAMES[provider]


class LlmAccessKind(StrEnum):
    # The Subscription Principal's own sign-in, in the Contract's harness root on their
    # own Runner. Never on a pool Runner.
    SUBSCRIPTION = "subscription"
    # The provider's key in the Runner's own store: the person's on a personal Runner,
    # the pool's on a pool Runner.
    HOST_KEY = "host_key"
    # One of the pool's stored LLM Keys, by id, opened with the Pool Key (T7).
    LLM_KEY = "llm_key"


@dataclass(frozen=True)
class LlmAccess:
    """The access one Directive was routed with.

    ``kind`` is an :class:`LlmAccessKind` value, held as a string because this rides a
    Temporal payload and a Runner one minor behind must still decode it.
    ``llm_key_id`` is set for ``llm_key`` only.
    """

    kind: str
    llm_key_id: str = ""
