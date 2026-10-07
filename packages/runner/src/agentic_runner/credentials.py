"""Credential References on the Runner (PRD issue 48; map 22 A1-A2, 25 §11, 17 A4/A10).

The **norm**, and the thing this module is mostly about: the Contract's credential
manifest names each credential -- name, type, scope -- and the control plane holds those
names and nothing else. The value is installed by whoever hosts the Runner, in their own
store: a Kubernetes Secret mounted ``0400`` (issue 46) or the workstation store (issue
47), under the reference's name. Endpoints are plain Profile config and are not secrets,
so they are nowhere near here.

Release 1's default for a **cross-party** value is hand-over (22 A2): the funder gives it
to the host operator out of band, who installs it as any other reference. That is a
legitimate answer, not a gap -- :mod:`agentic_runner.sealed_box` is the exception path for
a funder who does not host, and its opened values land in the same resolver below, under
the same reference name, precisely so nothing downstream can tell the two apart.

Two rules, and the second is the reason this is a module rather than a dictionary:

* **The manifest bounds what a Contract may resolve.** 17 asks how one Contract is kept
  off another's credentials on a shared Runner; the structural answer is that resolution
  is a lookup in the Contract's own manifest first and the host store second, so a
  reference the Contract never declared is unresolvable however the host store is laid
  out. An unknown reference fails the Directive **closed**, with Evidence naming it.
* **Values reach verb seams and Runner-hosted MCP servers only** (17 A10). Never the
  Agent Runtime's environment -- that process is the one on the box that must hold no
  org credential (ADR-0011 §9) -- and never a hook, which is repository-adjacent code the
  Agent itself can influence. :class:`DirectiveCredentials` has two accessors and both
  are named after the one place they may be spent; there is deliberately no ``env()``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol
from uuid import UUID

__all__ = [
    "CREDENTIAL_RESOLVED_SOURCE",
    "CREDENTIAL_UNRESOLVABLE_SOURCE",
    "CredentialResolver",
    "DirectiveCredentials",
    "DirectoryCredentialStore",
    "EmptyCredentialStore",
    "FakeCredentialStore",
    "HostCredentialStore",
    "UnresolvableCredentialReferenceError",
]

# The Evidence a Directive that failed closed on a reference appends (ids and the
# reference *name*, never a value and never a digest of one).
CREDENTIAL_UNRESOLVABLE_SOURCE: Final[str] = "runner.credential_unresolvable"
# The Evidence a Directive appends when its references resolved. Names only.
CREDENTIAL_RESOLVED_SOURCE: Final[str] = "runner.credential_resolved"


class HostCredentialStore(Protocol):
    """Where the host operator installed the values. The Runner reads; it never writes."""

    def get(self, reference: str) -> str | None: ...


class DirectoryCredentialStore:
    """A mounted Secret or a workstation store: one file per Credential Reference.

    The Kubernetes shape (issue 46) and the workstation shape (issue 47) are the same
    shape -- a directory of ``0400`` files named after the reference -- so one reader
    serves both and neither installer has to teach the Runner a format.

    A reference is a single path segment (:meth:`_path` refuses anything else) because a
    manifest is operator-authored data and a Contract must not be able to name
    ``../another-contract/key``.
    """

    def __init__(self, root: Path) -> None:
        self._root = root

    def get(self, reference: str) -> str | None:
        path = self._path(reference)
        if path is None or not path.is_file():
            return None
        # A Secret written by `kubectl create secret --from-literal` and one written by
        # an editor differ by exactly one trailing newline; stripping it is the
        # difference between a working key and a 401 nobody can explain.
        return path.read_text(encoding="utf-8").strip()

    def _path(self, reference: str) -> Path | None:
        if not reference or "/" in reference or reference in {".", ".."}:
            return None
        return self._root / reference


class FakeCredentialStore:
    """The host store a test wires (``integrations/*/fake_*`` keep the same shape)."""

    def __init__(self, values: Mapping[str, str] | None = None) -> None:
        self._values = dict(values or {})

    def get(self, reference: str) -> str | None:
        return self._values.get(reference)

    def put(self, reference: str, value: str) -> None:
        self._values[reference] = value


class EmptyCredentialStore:
    """The store a Runner with none configured behaves as: everything is uninstalled.

    Not a null object for convenience -- it is the fail-closed reading. A Contract that
    declares a manifest and lands on a Runner with no host store gets a refused Directive
    naming the reference, instead of one that ran without the credential it was told to use.
    """

    def get(self, reference: str) -> str | None:
        return None


class UnresolvableCredentialReferenceError(RuntimeError):
    """A Directive named a Credential Reference it cannot have (22 A1's fail-closed half).

    ``reason`` is ``not_in_manifest`` (the Contract never declared it) or
    ``not_installed`` (declared, but the host has not put a value there yet, and no
    funder has sealed one). Both fail the Directive; they differ in who fixes it, which
    is why the Evidence carries the distinction.
    """

    def __init__(self, reference: str, *, contract_id: str | None, reason: str) -> None:
        super().__init__(
            f"Credential Reference {reference!r} is unresolvable for Contract "
            f"{contract_id or 'unknown'}: {reason}"
        )
        self.reference = reference
        self.contract_id = contract_id
        self.reason = reason

    def evidence(self) -> dict[str, object]:
        """The Evidence payload naming the reference -- the whole point of failing here."""

        return {
            "event": "credential_reference.unresolvable",
            "reference": self.reference,
            "contract_id": self.contract_id,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class DirectiveCredentials:
    """The values one Directive may spend, resolved once at its start (22 A1).

    Held for the life of the attempt and no longer. Both accessors are named after the
    only two consumers 17 A10 admits; there is no accessor that yields an environment
    mapping, because the two processes that take one -- the Agent Runtime subprocess and
    a Runner Hook -- are exactly the two that must never see a value.
    """

    contract_id: str | None
    _values: Mapping[str, str]

    def for_verb_seam(self, reference: str) -> str:
        """The value a privileged verb seam spends (push, PR, review, merge)."""

        return self._require(reference)

    def for_runner_hosted_server(self, reference: str) -> str:
        """The value a Runner-hosted MCP server is started with (issue 58)."""

        return self._require(reference)

    def references(self) -> tuple[str, ...]:
        """The reference *names* resolved, for Evidence and for a server's config."""

        return tuple(sorted(self._values))

    def runner_hosted_server_env(self, references: Iterable[str]) -> dict[str, str]:
        """The environment one Runner-hosted MCP server is started with (issue 58).

        A *subset*, named by the server's own registry entry: a server declaring one
        reference is handed that one, not the Contract's whole manifest. The Agent talks
        to the server over the Runner's socket and never holds what the server holds,
        which is the arrangement 17 A10 admits.
        """

        return {reference: self.for_runner_hosted_server(reference) for reference in references}

    def _require(self, reference: str) -> str:
        value = self._values.get(reference)
        if value is None:
            raise UnresolvableCredentialReferenceError(
                reference, contract_id=self.contract_id, reason="not_in_manifest"
            )
        return value


class CredentialResolver:
    """Resolves a Contract's manifest into values, at Directive time (22 A1, 25 §11).

    Per Directive rather than per process, for the same reason the LLM slot is read per
    request (22 A8): a value the host replaced or a funder re-delivered takes effect at
    the next Directive rather than at the next Runner restart.

    ``delivered`` is the sealed half's output -- ``{(contract_id, slot): plaintext}``,
    opened by :class:`agentic_runner.sealed_box.RecipientKeyStore`'s private key and held
    in memory only. It is consulted before the host store because a funder who delivered a
    value is stating it is theirs to state; the host's own install is the fallback, which
    is exactly the hand-over default.
    """

    def __init__(
        self,
        *,
        store: HostCredentialStore,
        delivered: Mapping[tuple[str, str], str] | None = None,
    ) -> None:
        self._store = store
        self._delivered = dict(delivered or {})

    @property
    def host_store(self) -> HostCredentialStore:
        """The host's own store, for the one reader that is not a Directive: a
        user-connected Source's connector (PRD issue 50), whose Source row -- not a
        Contract manifest -- names the reference, and whose value is its owner's own."""

        return self._store

    def deliver(self, contract_id: UUID | str, slot: str, value: str) -> None:
        self._delivered[(str(contract_id), slot)] = value

    def drop(self, contract_id: UUID | str, slot: str) -> None:
        """Termination and revocation (22 A9): the value goes, it is not listed."""

        self._delivered.pop((str(contract_id), slot), None)

    def resolve(self, *, contract_id: str | None, manifest: Iterable[str]) -> DirectiveCredentials:
        """Every reference the Contract declares, or the first failure, named.

        Fails on the *first* unresolvable reference rather than collecting them: a
        Directive that cannot have one of its credentials is not going to run, and a
        partial resolution left lying around is a value held for no reason.
        """

        values: dict[str, str] = {}
        for reference in manifest:
            value = self._delivered.get((str(contract_id), reference))
            if value is None:
                value = self._store.get(reference)
            if value is None:
                raise UnresolvableCredentialReferenceError(
                    reference, contract_id=contract_id, reason="not_installed"
                )
            values[reference] = value
        return DirectiveCredentials(contract_id=contract_id, _values=values)
