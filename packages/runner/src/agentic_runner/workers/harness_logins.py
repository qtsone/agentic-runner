"""Subscription sign-ins in a Contract's harness root, read by ``stat`` alone (QTS-1736).

The vendor's own CLI writes each login, as the Contract's uid, into the Contract's harness
root; the Runner never opens one. What it reads is whether the file is there and how old
it is, which is enough for the heartbeat's subscription state (Spec: Runners rev 3) and
for the shared Runner's backstop (``auth_mode.choose_auth_mode``).

Codex signs in only in the terminal: ``agentic-runner sign-in codex`` runs
``codex login --device-auth`` attached to the person's terminal, and the code it prints
is read there and nowhere else. The console's "Connect" flow, which relayed it, is gone.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

from agentic_runner.workers.claude_sign_in import (
    KEYCHAIN_FILE,
    OAUTH_TOKEN_FILE,
    ClaudeSignInError,
    run_attached,
)
from agentic_runner.workers.contract_isolation import ContractIsolation, DirectiveSandbox
from agentic_runner_contracts.runner_registration import (
    RuntimeSubscriptionReport,
    SubscriptionState,
    UsageWindow,
)

__all__ = [
    "CODEX_SIGN_IN_ARGV",
    "CodexSignInError",
    "CodexSignIns",
    "holds_login",
    "login_file_counts",
    "subscription_report",
]

# The login files each harness's own sign-in writes, relative to its config root
# (research/29 §1.1, §2.1). Claude's `claude_ai` login on macOS is a Keychain item, not a
# file, so a shared Runner on macOS runs a Claude subscription on the long-lived token
# only; the subscription state also counts the per-Contract keychain `sign-in` makes.
_TOKEN_FILES_BY_RUNTIME: Final[dict[str, tuple[str, ...]]] = {
    "codex_cli": ("auth.json",),
    "claude_code": (".credentials.json", OAUTH_TOKEN_FILE),
}

# `claude setup-token` mints a token valid for one year; past that the Runner can say
# `expired` from the file's own age without asking Anthropic.
_CLAUDE_OAUTH_TOKEN_LIFETIME: Final[timedelta] = timedelta(days=365)

CODEX_SIGN_IN_ARGV: Final[tuple[str, ...]] = ("codex", "login", "--device-auth")


def holds_login(isolation: ContractIsolation, contract_id: str | None, runtime_kind: str) -> bool:
    """Whether this Contract's harness root for ``runtime_kind`` holds a login file, by
    ``stat`` alone. A kind with no known login file holds none: the shared Runner's
    subscription check (``auth_mode.choose_auth_mode``) fails closed on it."""

    filenames = _TOKEN_FILES_BY_RUNTIME.get(runtime_kind, ())
    root = isolation.harness_config_dir(contract_id, runtime_kind)
    return any((root / filename).is_file() for filename in filenames)


def login_file_counts(isolation: ContractIsolation, contract_id: str | None) -> dict[str, int]:
    """How many login files each of this Contract's harness roots holds, by ``stat`` alone.

    A count per harness and never a path: the residue report on a shared Runner says that
    a login is there, not where or what it is (local-agents 04).
    """

    return {
        runtime_kind: 1
        for runtime_kind in _TOKEN_FILES_BY_RUNTIME
        if holds_login(isolation, contract_id, runtime_kind)
    }


def subscription_report(
    isolation: ContractIsolation,
    contract_id: str,
    cli_kind: str,
    *,
    windows: Sequence[UsageWindow] | None,
    now: datetime,
) -> RuntimeSubscriptionReport:
    """One Agent Runtime's subscription in one Contract's harness root.

    ``windows`` is what the harness itself last said of its usage (local-agents 10). A
    window at 100 % holds the subscription until the latest such window resets.
    """

    root = isolation.harness_config_dir(contract_id, cli_kind)
    signed_in = holds_login(isolation, contract_id, cli_kind) or (
        cli_kind == "claude_code" and (root / KEYCHAIN_FILE).is_file()
    )
    if not signed_in:
        return RuntimeSubscriptionReport(cli_kind=cli_kind, state=SubscriptionState.NONE)
    if cli_kind == "claude_code" and _expired(root / OAUTH_TOKEN_FILE, now):
        return RuntimeSubscriptionReport(cli_kind=cli_kind, state=SubscriptionState.EXPIRED)
    used_up = [window for window in windows or () if window.used_percent >= 100.0]
    if used_up:
        resets = [window.resets_at for window in used_up if window.resets_at is not None]
        return RuntimeSubscriptionReport(
            cli_kind=cli_kind,
            state=SubscriptionState.AT_LIMIT,
            resets_at=max(resets) if resets else None,
        )
    return RuntimeSubscriptionReport(cli_kind=cli_kind, state=SubscriptionState.SIGNED_IN)


def _expired(token_file: Path, now: datetime) -> bool:
    try:
        written = datetime.fromtimestamp(token_file.stat().st_mtime, UTC)
    except FileNotFoundError:
        return False
    return now - written > _CLAUDE_OAUTH_TOKEN_LIFETIME


class CodexSignInError(RuntimeError):
    """Codex's sign-in did not start."""


class CodexSignIns:
    """``codex login --device-auth`` for one Contract, attached to the person's terminal."""

    def __init__(
        self, isolation: ContractIsolation, *, argv: Sequence[str] = CODEX_SIGN_IN_ARGV
    ) -> None:
        self._isolation = isolation
        self._argv = list(argv)

    def has_harness_root(self, contract_id: str) -> bool:
        return self._isolation.existing_sandbox(contract_id, runtime_kind="codex_cli") is not None

    async def sign_in(self, contract_id: str, *, stdin_fd: int = 0, stdout_fd: int = 1) -> int:
        """Run Codex's own device sign-in as the Contract's uid in its harness root; Codex
        writes its login there itself. Returns the CLI's exit code."""

        sandbox = self._isolation.sandbox(contract_id, runtime_kind="codex_cli")
        output = bytearray()
        try:
            return await run_attached(
                self._argv,
                sandbox,
                env=_codex_env(sandbox),
                output=output,
                stdin_fd=stdin_fd,
                stdout_fd=stdout_fd,
            )
        except ClaudeSignInError as error:
            raise CodexSignInError(str(error)) from None
        finally:
            output.clear()


def _codex_env(sandbox: DirectiveSandbox) -> dict[str, str]:
    return {
        "HOME": str(sandbox.home_dir),
        "TMPDIR": str(sandbox.tmp_dir),
        "PATH": os.environ.get("PATH") or os.defpath,
        "CODEX_HOME": str(sandbox.harness_config_dir),
    }
