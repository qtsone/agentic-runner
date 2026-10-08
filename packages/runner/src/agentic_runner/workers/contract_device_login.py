"""Per-Contract subscription sign-in (PRD issue 31, ADR-0015 §4, research/29).

Today's device-login ritual (``codex login --device-auth``) is rescoped from "the Runner"
to **one Contract's harness root**: the funder starts it from the console, the worker runs
the vendor's CLI as *that Contract's own uid* inside ``{contract_id}/harness/{runtime_kind}/``
(``workers/contract_isolation.py``), the funder approves in their browser, and the CLI
itself -- not this module -- writes the token there. This module never opens that file: it
only spawns the CLI and later ``stat``s the path it would be at, which is why a device
login is delivered in place rather than delivered as a value (``services/llm_credential.py``'s
module doc) -- there is no value here for the Runner to have touched.

The CLI prints its verification URL and one-time code, then blocks polling the vendor for
up to its own device-code TTL (Codex: 15 minutes, research/29 §1.5). ``sign_in`` returns
as soon as that prompt is printed and detaches the CLI to keep polling in the background --
holding the Temporal activity (and the funder's HTTP request behind it) open for the whole
wait would both time out well before the funder could act and never hand back the URL/code
they need to (PRD issue 31 review). ``token_present``/``token_delivered_at`` are the later,
separate read that discovers whether the funder finished.

Everything here lives in the worker tree so M3 (issue 36) moves it into ``agentic-runner``
unchanged: like ``contract_isolation.py`` it imports nothing from the platform's services
or database.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

from agentic_runner.workers._runtime_support import run_subprocess_launch_and_detach
from agentic_runner.workers.contract_isolation import ContractIsolation

__all__ = [
    "DEVICE_LOGIN_ARGV",
    "ContractDeviceLogin",
    "ContractDeviceLoginPromptError",
    "DeviceLoginPrompt",
    "UnknownHarnessError",
    "login_file_counts",
]

# The token file each harness's own device-login flow writes, relative to its config root
# (research/29 §1.1, §2.1). Read only with `Path.stat`/`Path.is_file` below, never opened.
_TOKEN_FILE_BY_RUNTIME: Final[dict[str, str]] = {
    "codex_cli": "auth.json",
    "claude_code": ".credentials.json",
}

# Codex's non-interactive device-code flow (research/29 §1.5, "Headless login is
# first-class"). Claude Code's own login is an interactive `/login` paste-code flow with
# no scriptable device-code equivalent (research/29 §2.1); its M1 path is the
# `claude setup-token` bearer instead, which is a *value* and so goes through
# ``services.llm_credential.replace_api_key`` like any other API key, never through here.
DEVICE_LOGIN_ARGV: Final[dict[str, tuple[str, ...]]] = {
    "codex_cli": ("codex", "login", "--device-auth"),
}

# Codex 0.141.0 prompt shape:
#   1. Open this link ...
#      https://auth.openai.com/codex/device
#   2. Enter this one-time code (expires in 15 minutes)
#      GP7J-D5JAC
_VERIFICATION_URI_PATTERN = re.compile(r"https?://[^\s\"']+")
_USER_CODE_PATTERN = re.compile(r"\b[A-Z0-9]{3,8}-[A-Z0-9]{3,8}\b")
_EXPIRES_IN_MINUTES_PATTERN = re.compile(r"expires in\s+(\d+)\s*minute", re.IGNORECASE)
_DEFAULT_DEVICE_CODE_TTL = timedelta(minutes=15)

# Generous over "the CLI has printed two lines after one HTTP call to mint a device code"
# (typically under a second) while staying well under the Temporal executor's default 60s
# start-and-await ceiling (`integrations/temporal/client.py`) once workflow scheduling and
# activity overhead are added on top (`workflows/contract_device_login.py`'s own timeouts).
_DEFAULT_PROMPT_TIMEOUT_SECONDS = 30.0
_DEFAULT_OUTPUT_LIMIT_BYTES = 65_536


class UnknownHarnessError(ValueError):
    """Raised for a runtime kind this module has no device-login command for."""


class ContractDeviceLoginPromptError(RuntimeError):
    """The CLI never printed a complete verification prompt (exited early, or timed out)."""


@dataclass(frozen=True, slots=True)
class DeviceLoginPrompt:
    """What one sign-in launch produced -- the vendor's verification URL and one-time
    code, never the token itself. The CLI is still running (detached) when this returns;
    ``ContractDeviceLogin.token_present`` is the later, separate check for completion."""

    contract_id: str
    runtime_kind: str
    verification_uri: str
    user_code: str
    expires_at: datetime


class ContractDeviceLogin:
    """Starts one Contract's device-code sign-in inside its own harness root."""

    def __init__(
        self,
        isolation: ContractIsolation,
        *,
        argv_by_runtime: dict[str, tuple[str, ...]] | None = None,
        prompt_timeout_seconds: float = _DEFAULT_PROMPT_TIMEOUT_SECONDS,
        output_limit_bytes: int = _DEFAULT_OUTPUT_LIMIT_BYTES,
    ) -> None:
        self._isolation = isolation
        self._argv_by_runtime = argv_by_runtime or DEVICE_LOGIN_ARGV
        self._prompt_timeout_seconds = prompt_timeout_seconds
        self._output_limit_bytes = output_limit_bytes

    async def sign_in(self, contract_id: str, *, runtime_kind: str) -> DeviceLoginPrompt:
        """Spawn the harness CLI as this Contract's own uid; return once it has printed
        its verification prompt, and leave it running to finish the funder's approval.

        Isolation is the sandbox this Contract already gets for a Directive
        (``ContractIsolation.sandbox``): its own uid, its own ``HOME``/``TMPDIR``, and a
        harness config root no other Contract's uid can read.
        """

        argv = self._argv_by_runtime.get(runtime_kind)
        if argv is None:
            raise UnknownHarnessError(f"no device-login command for runtime {runtime_kind!r}")
        sandbox = self._isolation.sandbox(contract_id, runtime_kind=runtime_kind)
        try:
            prompt_text = await run_subprocess_launch_and_detach(
                argv=list(argv),
                cwd=sandbox.home_dir,
                env=_harness_env(runtime_kind, sandbox.harness_config_dir, sandbox.home_dir),
                is_prompt_complete=_prompt_has_verification,
                prompt_timeout_seconds=self._prompt_timeout_seconds,
                output_limit_bytes=self._output_limit_bytes,
                sandbox=sandbox,
            )
        except (TimeoutError, RuntimeError) as error:
            raise ContractDeviceLoginPromptError(str(error)) from error
        return DeviceLoginPrompt(
            contract_id=contract_id,
            runtime_kind=runtime_kind,
            verification_uri=_extract_verification_uri(prompt_text),
            user_code=_extract_user_code(prompt_text),
            expires_at=_extract_expires_at(prompt_text),
        )

    def token_present(self, contract_id: str, *, runtime_kind: str) -> bool:
        """Whether the harness wrote its token -- a ``stat``, never a read.

        This is the whole of how the platform learns a sign-in landed: it is never opened,
        so its contents (and any refresh token inside it) never reach the Runner process
        beyond the harness's own child process that wrote it.
        """

        path = self._token_path(contract_id, runtime_kind=runtime_kind)
        return path is not None and path.is_file()

    def token_delivered_at(self, contract_id: str, *, runtime_kind: str) -> float | None:
        """The token file's own mtime, again by ``stat`` alone."""

        path = self._token_path(contract_id, runtime_kind=runtime_kind)
        if path is None:
            return None
        try:
            return path.stat().st_mtime
        except FileNotFoundError:
            return None

    def _token_path(self, contract_id: str, *, runtime_kind: str) -> Path | None:
        filename = _TOKEN_FILE_BY_RUNTIME.get(runtime_kind)
        if filename is None:
            return None
        return self._isolation.harness_config_dir(contract_id, runtime_kind) / filename


def login_file_counts(isolation: ContractIsolation, contract_id: str | None) -> dict[str, int]:
    """How many login files each of this Contract's harness roots holds, by ``stat`` alone.

    A count per harness and never a path: the residue report on a shared Runner says that
    a login is there, not where or what it is (local-agents 04).
    """

    counts: dict[str, int] = {}
    for runtime_kind, filename in _TOKEN_FILE_BY_RUNTIME.items():
        if (isolation.harness_config_dir(contract_id, runtime_kind) / filename).is_file():
            counts[runtime_kind] = 1
    return counts


def _prompt_has_verification(raw: str) -> bool:
    return bool(_VERIFICATION_URI_PATTERN.search(raw) and _USER_CODE_PATTERN.search(raw))


def _extract_verification_uri(text: str) -> str:
    match = _VERIFICATION_URI_PATTERN.search(text)
    if match is None:
        raise ContractDeviceLoginPromptError("device login prompt carried no verification_uri")
    return match.group(0)


def _extract_user_code(text: str) -> str:
    match = _USER_CODE_PATTERN.search(text)
    if match is None:
        raise ContractDeviceLoginPromptError("device login prompt carried no user_code")
    return match.group(0)


def _extract_expires_at(text: str) -> datetime:
    match = _EXPIRES_IN_MINUTES_PATTERN.search(text)
    ttl = timedelta(minutes=int(match.group(1))) if match else _DEFAULT_DEVICE_CODE_TTL
    return datetime.now(UTC) + ttl


def _harness_env(runtime_kind: str, harness_config_dir: Path, home_dir: Path) -> dict[str, str]:
    env = {
        "HOME": str(home_dir),
        "PATH": os.environ.get("PATH") or os.defpath,
    }
    if runtime_kind == "codex_cli":
        env["CODEX_HOME"] = str(harness_config_dir)
    elif runtime_kind == "claude_code":
        env["CLAUDE_CONFIG_DIR"] = str(harness_config_dir)
    return env
