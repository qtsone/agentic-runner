"""The Runner's activities behind the Contract device-login workflows (PRD issue 31).

Dispatched to ``runner.{runner_id}`` (the locality rule, ADR-0013 §2): the only place a
Contract's harness root lives is the Runner's own disk (ADR-0015 §4), so they ship with
the Runner distribution and the platform's workflow module only names them.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from temporalio import activity
from temporalio.exceptions import ApplicationError

from agentic_runner.auth_mode import SHARED_RUNNER_SIGN_IN_RULE, is_shared_runner
from agentic_runner.workers.claude_sign_in import OAUTH_TOKEN
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
        host_party: str | None = None,
    ) -> None:
        self._contract_isolation = contract_isolation
        # The party this process was registered as hosted by. A shared Runner signs in only
        # a Claude Code long-lived token (local-agents 04, amended by 21).
        self._host_party = host_party
        # Injectable for tests; None falls through to the real vendor CLIs.
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
        long_lived = request.runtime_kind == "claude_code" and (request.method or OAUTH_TOKEN) == (
            OAUTH_TOKEN
        )
        self._refuse_on_shared_runner(allow=long_lived)
        prompt = await self._device_login.sign_in(
            request.contract_id, runtime_kind=request.runtime_kind, method=request.method
        )
        return ContractDeviceLoginResult(
            contract_id=prompt.contract_id,
            runtime_kind=prompt.runtime_kind,
            verification_uri=prompt.verification_uri,
            user_code=prompt.user_code,
            expires_at=prompt.expires_at.isoformat(),
            sign_in_id=prompt.sign_in_id,
        )

    @activity.defn(name="check_contract_device_login_status")
    async def check_contract_device_login_status(
        self, request: ContractDeviceLoginStatusInput
    ) -> ContractDeviceLoginStatusResult:
        if request.runtime_kind == "claude_code":
            self._refuse_on_shared_runner(allow=True)
            status = await self._device_login.claude.status(request.contract_id)
            return ContractDeviceLoginStatusResult(
                contract_id=request.contract_id,
                runtime_kind=request.runtime_kind,
                token_present=status.logged_in,
                delivered_at=None,
                auth_method=status.auth_method,
                subscription_type=status.subscription_type,
            )
        self._refuse_on_shared_runner()
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

    def _refuse_on_shared_runner(self, *, allow: bool = False) -> None:
        """Non-retryable, and typed by the rule: there is no Work Record to write Evidence
        on here, so the failure the platform's sign-in workflow receives names it.

        Only a Runner known to be the person's own signs in, except that a shared Runner
        may make (``allow``) a Claude Code long-lived token (local-agents 21). One whose
        state names no host party (registered before issue 42) is refused either way, as
        `choose_auth_mode` never runs a subscription on it."""

        if self._host_party != "user" and not (allow and is_shared_runner(self._host_party)):
            raise ApplicationError(
                f"this Runner is hosted by {self._host_party!r}; only a person's own Runner "
                "signs in, a shared Runner runs API keys and Claude Code long-lived tokens "
                "only",
                type=SHARED_RUNNER_SIGN_IN_RULE,
                non_retryable=True,
            )
