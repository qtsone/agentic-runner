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

from agentic_runner.auth_mode import SIGN_IN_NEEDS_CONTRACT_UID_RULE, runs_subscriptions
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
        # where its Directives run as each Contract's own uid (QTS-1450); a person signs
        # Claude Code in on the Runner itself (QTS-1448).
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
        self._refuse_where_no_subscription_runs()
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
        if request.runtime_kind == "claude_code":
            self._refuse_where_no_subscription_runs()
            status = await self._device_login.claude.status(request.contract_id)
            return ContractDeviceLoginStatusResult(
                contract_id=request.contract_id,
                runtime_kind=request.runtime_kind,
                token_present=status.logged_in,
                delivered_at=None,
                auth_method=status.auth_method,
                subscription_type=status.subscription_type,
            )
        self._refuse_where_no_subscription_runs()
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

    def _refuse_where_no_subscription_runs(self) -> None:
        """Non-retryable, and typed by the rule: there is no Work Record to write Evidence
        on here, so the failure the platform's sign-in workflow receives names it.

        A sign-in, or a read of one, only where a Directive could run on it
        (``auth_mode.runs_subscriptions``): the person's own Runner, or a shared one that
        runs each Contract as its own uid. One whose state names no host party (registered
        before issue 42) is refused, as `choose_auth_mode` never runs a subscription on it.
        """

        if not runs_subscriptions(
            self._host_party, separates_contract_uids=self._contract_isolation.can_separate_uids
        ):
            raise ApplicationError(
                f"this Runner is hosted by {self._host_party!r} and does not run each Contract "
                "under its own uid, so no subscription runs on it and it signs nothing in",
                type=SIGN_IN_NEEDS_CONTRACT_UID_RULE,
                non_retryable=True,
            )
