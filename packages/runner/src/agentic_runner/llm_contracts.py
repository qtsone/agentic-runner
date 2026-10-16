"""The Contracts this Runner serves, as the last heartbeat ack named them (QTS-1736).

``agentic-runner sign-in`` is a separate process (``docker exec``, ``kubectl exec``, a
workstation terminal) and takes no ids: it learns the Contract and whether its person may
sign in with their own subscription from this file, which the running Runner rewrites
whenever an ack changes it. A private file in the state directory, beside the identity.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Final

from pydantic import TypeAdapter, ValidationError

from agentic_runner.private_state import private_read, private_write
from agentic_runner_contracts.runner_registration import ContractLlmTerms

__all__ = ["LLM_CONTRACTS_FILENAME", "load_llm_contracts", "save_llm_contracts"]

LLM_CONTRACTS_FILENAME: Final[str] = "llm-contracts.json"

_TERMS: Final = TypeAdapter(list[ContractLlmTerms])


def save_llm_contracts(state_dir: Path, terms: Sequence[ContractLlmTerms]) -> None:
    private_write(state_dir / LLM_CONTRACTS_FILENAME, _TERMS.dump_json(list(terms)))


def load_llm_contracts(state_dir: Path) -> list[ContractLlmTerms] | None:
    """None while no ack has named them: the Runner has not beaten yet, or its control
    plane is below contracts 3.25."""

    raw = private_read(state_dir / LLM_CONTRACTS_FILENAME)
    if raw is None:
        return None
    try:
        return _TERMS.validate_json(raw)
    except ValidationError:
        return None
