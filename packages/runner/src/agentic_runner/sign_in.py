"""``agentic-runner sign-in claude|codex``: a person gives their own Runner its LLM access.

One command for Claude Code and Codex, for a subscription or an API key, on a laptop, in
``docker exec -it`` or in ``kubectl exec -it`` (Spec: Runners rev 3). It takes no ids: a
personal Runner serves one Contract, which the last heartbeat ack named
(``llm_contracts``). It names the one Agent Runtime it signs in: a bare ``sign-in`` read as
a Claude Code sign-in, so the console script refuses it (QTS-2159).

* **An API key** is read without echo and stored in the Runner's own store under the
  provider's key name (``ANTHROPIC_API_KEY``, ``OPENAI_API_KEY``). The running Runner reads
  it within one heartbeat, probes it and serves it from its LLM proxy; it never reaches
  the Agent Runtime.
* **A subscription** runs the vendor's own sign-in, unmodified, as the Contract's uid in
  its harness root, attached to this terminal: Claude Code's login, or
  ``codex login --device-auth`` with the code shown here. No code or token passes through
  the control plane.

It refuses on a pool Runner, which uses only its pool's keys. It offers a subscription
only when the ack says the Contract allows it; otherwise it says which of the right or
the person's acknowledgment is missing, names the console page that turns it on, and
offers the key (QTS-1776).

A person's Runner connected to several Organisations serves one Contract per connection
(Spec: Runners rev 14). A subscription then asks which connection it signs in, by the
Organisation's name, and each connection keeps its own sign-in in its own harness root: an
employer's team seat in one, the person's own plan in another. A key serves them all.
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
    # Rev 14: a Runner `login` made, which has an operator and no host party.
    operated: bool = False

    async def run(self, which: str) -> int:
        if not (self.operated or self.host_party == "user"):
            hosted = f"hosted by {self.host_party!r}" if self.host_party else "not registered"
            self.say(
                f"refusing to sign in: this Runner is {hosted}. `sign-in` gives a person's own "
                "Runner their access; a pool Runner uses only its pool's keys"
            )
            return 1
        kind = AGENT_RUNTIMES[which]
        if kind not in self.served:
            self.say(
                f"refusing to sign in: this Runner does not serve {_LABELS[kind]}; "
                "install its CLI on this Runner's PATH and restart it"
            )
            return 1
        return await self._one(kind)

    async def _one(self, cli_kind: str) -> int:
        label = _LABELS[cli_kind]
        offer = self._subscription_offer()
        if isinstance(offer, str):
            # Offering `[s]` and then refusing it is what sent an Organisation Admin looking
            # for a switch nothing named (QTS-1776).
            self.say(f"{label}: {offer}")
            answer = self.ask(f"{label}: store an API [k]ey, or skip? [k/N] ")
        else:
            answer = self.ask(
                f"{label}: sign in with your [s]ubscription, store an API [k]ey, or skip? [s/k/N] "
            )
        choice = answer.strip().lower()[:1]
        if choice == "k":
            return self._key(cli_kind)
        if choice == "s" and not isinstance(offer, str):
            contract = self._which(offer, label)
            if contract is None:
                self.say(f"{label}: no such connection, nothing changed")
                return 1
            return await self._subscription(contract, cli_kind)
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

    async def _subscription(self, contract: ContractLlmTerms, cli_kind: str) -> int:
        returncode = await self.subscription(str(contract.contract_id), cli_kind)
        if returncode != 0:
            self.say(f"{_LABELS[cli_kind]}: the sign-in exited {returncode}; nothing changed")
        return returncode

    def _which(self, allowed: Sequence[ContractLlmTerms], label: str) -> ContractLlmTerms | None:
        """The connection a subscription signs in: the one there is, or the one asked for."""

        if len(allowed) == 1:
            return allowed[0]
        for number, contract in enumerate(allowed, start=1):
            self.say(f"  [{number}] {_connection_label(contract)}")
        answer = self.ask(f"{label}: sign in for which Organisation? [number] ").strip()
        if answer.isdigit() and 0 < int(answer) <= len(allowed):
            return allowed[int(answer) - 1]
        return None

    def _subscription_offer(self) -> list[ContractLlmTerms] | str:
        """The Contracts a subscription may sign in, one per connection, or why none."""

        if self.contracts is None:
            return (
                "no subscription yet: this Runner has not yet been told its Contract. Start "
                "it, wait for its first heartbeat, and run this again (the control plane must "
                "be on contracts 3.25 or later)"
            )
        if not self.contracts:
            return (
                "no subscription: this Runner is connected to no Organisation yet; connect it "
                "with `agentic-runner connect`, wait for its next heartbeat and run this again"
            )
        allowed = [c for c in self.contracts if c.subscription == SubscriptionRight.ALLOWED]
        if allowed:
            return allowed
        if len(self.contracts) > 1:
            return "your own subscription is off or not acknowledged in " + ", ".join(
                f"{_connection_label(c)} ({_REFUSED[c.subscription]})" for c in self.contracts
            )
        contract = self.contracts[0]
        if contract.subscription == SubscriptionRight.NO_RIGHT:
            # TODO(QTS-1744): name the Contract editor once the right moves onto the Contract.
            return (
                "your own subscription is off for this Runner. An Organisation Admin turns it "
                "on in the console with Allow subscription sign-in, on this Runner under "
                "Personal Runners on /orgs/<organisation>/runners"
            )
        if contract.subscription == SubscriptionRight.NOT_ACKNOWLEDGED:
            return (
                "your own subscription is on for this Runner, and you have not acknowledged "
                "the subscription terms. Acknowledge them in the console on My Contracts "
                "(/me/contracts), then run this again"
            )
        raise AssertionError(contract.subscription)  # ALLOWED returned above


_REFUSED: Final[Mapping[SubscriptionRight, str]] = {
    SubscriptionRight.NO_RIGHT: "an Organisation Admin has not turned it on",
    SubscriptionRight.NOT_ACKNOWLEDGED: "acknowledge the terms on /me/contracts",
    SubscriptionRight.ALLOWED: "allowed",
}


def _connection_label(contract: ContractLlmTerms) -> str:
    """The Organisation by its name, else its id's first characters; never an escape
    sequence someone typed into a name in the console."""

    name = contract.organisation_name or contract.contract_name
    if name:
        return "".join(character for character in name if character.isprintable())
    return str(contract.organisation_id or contract.contract_id)[:8]
