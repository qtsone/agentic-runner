"""``agentic-runner sign-in [claude|codex]``: a person gives their own Runner its LLM access.

One command for Claude Code and Codex, for a subscription or an API key, on a laptop, in
``docker exec -it`` or in ``kubectl exec -it`` (Spec: Runners rev 3). It takes no ids: a
personal Runner serves one Contract, which the last heartbeat ack named
(``llm_contracts``). With no argument it asks about each Agent Runtime the Runner serves.

* **An API key** is read without echo and stored in the Runner's own store under the
  provider's key name (``ANTHROPIC_API_KEY``, ``OPENAI_API_KEY``). The running Runner reads
  it within one heartbeat, probes it and serves it from its LLM proxy; it never reaches
  the Agent Runtime.
* **A subscription** runs the vendor's own sign-in, unmodified, as the Contract's uid in
  its harness root, attached to this terminal: Claude Code's login, or
  ``codex login --device-auth`` with the code shown here. No code or token passes through
  the control plane.

It refuses on a pool Runner, which uses only its pool's keys. It refuses a subscription
when the ack says the Contract lacks the right or the person has not acknowledged, and
says which.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Protocol

from agentic_runner_contracts.llm_access import PROVIDER_KEY_NAMES, provider_for
from agentic_runner_contracts.runner_registration import ContractLlmTerms, SubscriptionRight

__all__ = [
    "AGENT_RUNTIMES",
    "KeyStore",
    "SignIn",
    "SubscriptionSignIn",
]

# The argument a person types, and the Agent Runtime it names.
AGENT_RUNTIMES: Final[Mapping[str, str]] = {"claude": "claude_code", "codex": "codex_cli"}
_LABELS: Final[Mapping[str, str]] = {"claude_code": "Claude Code", "codex_cli": "Codex"}


class KeyStore(Protocol):
    """The Runner's own store, as the host writes it."""

    def put(self, reference: str, value: str) -> None: ...


# Runs the vendor's sign-in for one Contract's Agent Runtime; returns the CLI's exit code.
SubscriptionSignIn = Callable[[str, str], Awaitable[int]]


@dataclass
class SignIn:
    """One ``sign-in`` run. Everything it touches is passed in, so a test drives it whole."""

    host_party: str | None
    # The ack's `llm_contracts`, as the running Runner last saved them; None before any.
    contracts: Sequence[ContractLlmTerms] | None
    # The Agent Runtimes this Runner serves (its CLIs on PATH), by `cli_kind`.
    served: Sequence[str]
    store: KeyStore
    subscription: SubscriptionSignIn
    ask: Callable[[str], str]
    secret: Callable[[str], str]
    say: Callable[[str], None]

    async def run(self, which: str | None) -> int:
        if self.host_party != "user":
            hosted = f"hosted by {self.host_party!r}" if self.host_party else "not registered"
            self.say(
                f"refusing to sign in: this Runner is {hosted}. `sign-in` gives a person's own "
                "Runner their access; a pool Runner uses only its pool's keys"
            )
            return 1
        kinds = [AGENT_RUNTIMES[which]] if which is not None else sorted(self.served)
        missing = [kind for kind in kinds if kind not in self.served]
        if missing:
            self.say(
                f"refusing to sign in: this Runner does not serve {_LABELS[missing[0]]}; "
                "install its CLI on this Runner's PATH and restart it"
            )
            return 1
        if not kinds:
            self.say("this Runner serves no Agent Runtime: install `claude` or `codex` first")
            return 1
        for kind in kinds:
            outcome = await self._one(kind)
            if outcome != 0:
                return outcome
        return 0

    async def _one(self, cli_kind: str) -> int:
        label = _LABELS[cli_kind]
        answer = self.ask(
            f"{label}: sign in with your [s]ubscription, store an API [k]ey, or skip? [s/k/N] "
        )
        choice = answer.strip().lower()[:1]
        if choice == "k":
            return self._key(cli_kind)
        if choice == "s":
            return await self._subscription(cli_kind)
        self.say(f"{label}: skipped")
        return 0

    def _key(self, cli_kind: str) -> int:
        provider = provider_for(cli_kind)
        assert provider is not None  # every entry of AGENT_RUNTIMES has one
        key_name = PROVIDER_KEY_NAMES[provider]
        value = self.secret(f"{key_name} (not shown): ").strip()
        if not value:
            self.say(f"{_LABELS[cli_kind]}: no key typed, nothing stored")
            return 1
        self.store.put(key_name, value)
        self.say(
            f"{_LABELS[cli_kind]}: {key_name} stored in this Runner's own store. The Runner "
            "checks it with the provider and reports it on its next heartbeat"
        )
        return 0

    async def _subscription(self, cli_kind: str) -> int:
        contract = self._contract()
        if contract is None:
            return 1
        if contract.subscription == SubscriptionRight.NO_RIGHT:
            self.say(
                "refusing a subscription: your Contract does not allow your own subscription. "
                "Ask an Organisation Admin to turn it on, or store an API key instead"
            )
            return 1
        if contract.subscription == SubscriptionRight.NOT_ACKNOWLEDGED:
            self.say(
                "refusing a subscription: you have not acknowledged the subscription terms. "
                "Acknowledge them on your Contract in the console, then run this again"
            )
            return 1
        returncode = await self.subscription(str(contract.contract_id), cli_kind)
        if returncode != 0:
            self.say(f"{_LABELS[cli_kind]}: the sign-in exited {returncode}; nothing changed")
        return returncode

    def _contract(self) -> ContractLlmTerms | None:
        if self.contracts is None:
            self.say(
                "refusing a subscription: this Runner has not yet been told its Contract. "
                "Start it, wait for its first heartbeat, and run this again (the control "
                "plane must be on contracts 3.25 or later)"
            )
            return None
        if len(self.contracts) != 1:
            self.say(
                f"refusing a subscription: a personal Runner serves one Contract, and the "
                f"control plane named {len(self.contracts)}"
            )
            return None
        return self.contracts[0]
