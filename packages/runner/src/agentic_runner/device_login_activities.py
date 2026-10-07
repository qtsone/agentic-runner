"""The Runner's activities behind the Contract device-login workflows (PRD issue 31).

Dispatched to ``runner.{runner_id}`` (the locality rule, ADR-0013 §2): the only place a
Contract's harness root lives is the Runner's own disk (ADR-0015 §4), so they ship with
the Runner distribution and the platform's workflow module only names them.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from temporalio import activity

from agentic_runner.workers.contract_device_login import ContractDeviceLogin
from agentic_runner.workers.contract_isolation import ContractIsolation
from agentic_runner_contracts.activity_io import (
    ContractDeviceLoginInput,
    ContractDeviceLoginResult,
    ContractDeviceLoginStatusInput,
    ContractDeviceLoginStatusResult,
)


class ContractDeviceLoginActivities:
    """Temporal activities for one Contract's device-code sign-in: start it, and
    separately check whether the funder has finished it."""

    def __init__(
        self,
        *,
        contract_isolation: ContractIsolation,
        device_login: ContractDeviceLogin | None = None,
    ) -> None:
        self._contract_isolation = contract_isolation
        # Injectable for tests; None falls through to the real `codex` subprocess.
        self._device_login = device_login or ContractDeviceLogin(contract_isolation)

    def activity_callables(self) -> list[Callable[..., object]]:
        return [
            self.sign_in_contract_device_login,
            self.check_contract_device_login_status,
        ]

    @activity.defn(name="sign_in_contract_device_login")
    async def sign_in_contract_device_login(
        self, request: ContractDeviceLoginInput
    ) -> ContractDeviceLoginResult:
        prompt = await self._device_login.sign_in(
            request.contract_id, runtime_kind=request.runtime_kind
        )
        return ContractDeviceLoginResult(
            contract_id=prompt.contract_id,
            runtime_kind=prompt.runtime_kind,
            verification_uri=prompt.verification_uri,
            user_code=prompt.user_code,
            expires_at=prompt.expires_at.isoformat(),
        )

    @activity.defn(name="check_contract_device_login_status")
    async def check_contract_device_login_status(
        self, request: ContractDeviceLoginStatusInput
    ) -> ContractDeviceLoginStatusResult:
        present = self._device_login.token_present(
            request.contract_id, runtime_kind=request.runtime_kind
        )
        delivered_at = None
        if present:
            mtime = self._device_login.token_delivered_at(
                request.contract_id, runtime_kind=request.runtime_kind
            )
            if mtime is not None:
                delivered_at = datetime.fromtimestamp(mtime, tz=UTC).isoformat()
        return ContractDeviceLoginStatusResult(
            contract_id=request.contract_id,
            runtime_kind=request.runtime_kind,
            token_present=present,
            delivered_at=delivered_at,
        )
