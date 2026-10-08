"""Runner Hooks: operator executables in platform-named slots (ADR-0013 §10, ticket 26 §1).

The Buildkite shape, narrowed. An operator drops an executable named exactly as a
:class:`HookName` into the hooks path and the Runner runs it at that point of a Directive
attempt; nobody adds a slot, and there are no repository or plugin hooks — a repository
hook is code the Agent itself can author, which the Runner would then execute *outside*
the Agent Runtime Profile's command policy (ADR-0011 §12), and MCP is the extension point
instead of plugins (map ticket 05).

Two invariants this module exists to hold:

* **A hook sees no credential value** (map ticket 17 A10). Values reach verb seams and
  Runner-hosted MCP servers only. The environment here is built from an allow-list of
  platform ids and enums plus the attempt's own callback socket — never the parent
  process environment, which on a Runner pod carries the GitHub App key and the internal
  service token. A hook that needs a credential is map fog; there is deliberately no path
  for it.
* **A hook runs as the Contract's uid, in the Workspace** (ADR-0015 §1). It is repository-
  adjacent code like the Directive and the Verifier, so it runs under the same floor:
  same uid, same rlimits, same ``TMPDIR`` inside the Contract's 0700 tree.

The ``environment`` hook is **not** the secrets hook (ADR-0011 §9). Everything it exports
reaches the Agent Runtime subprocess, which is the one process on the box that must hold
no org credential — which inverts the purpose Buildkite documents for the same slot.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Final, Self

from agentic_runner.workers._runtime_support import (
    RESERVED_DIRECTIVE_ENV,
    run_subprocess_exec,
)
from agentic_runner.workers.contract_isolation import DirectiveSandbox
from agentic_runner_contracts.public_metadata import (
    DIRECTIVE_HOOK_ORDER,
    FATAL_HOOKS,
    HookName,
    hook_name,
)
from agentic_runner_contracts.redaction import redact_secret_like_text

__all__ = [
    "DIRECTIVE_HOOK_ORDER",
    "DIRECTIVE_REJECTED_EVIDENCE_SOURCE",
    "HOOK_EVIDENCE_SOURCE",
    "RESERVED_DIRECTIVE_ENV",
    "AttemptFacts",
    "DirectiveHookSession",
    "DirectiveRejectedError",
    "HookName",
    "HookRefusedError",
    "HookRun",
    "HookRunner",
    "load_hooks",
    "prepare_attempt_dir",
]

HOOK_EVIDENCE_SOURCE: Final[str] = "runner.hook"
DIRECTIVE_REJECTED_EVIDENCE_SOURCE: Final[str] = "runner.directive_rejected"

# What the Runner tells a hook about the attempt: platform ids and enums, never Directive
# text (map ticket 07 — a hook name and a hook's environment are as displayed as a queue
# name). The prompt, the repository and the branch are deliberately absent.
_FACT_ENV_PREFIX: Final[str] = "AGENTIC_RUNNER_"
# Where the `environment` hook writes its exports (`NAME=value`, one per line), and where
# `pre_directive` reads the proposed Agent Runtime environment as *names only* — the v4
# Buildkite shape, so the reject gate can refuse on what will be set without being handed
# the values.
ENV_FILE_ENV: Final[str] = f"{_FACT_ENV_PREFIX}ENV_FILE"
PROPOSED_ENV_FILE_ENV: Final[str] = f"{_FACT_ENV_PREFIX}PROPOSED_ENV_FILE"

_ENV_NAME_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_HOOK_TIMEOUT_SECONDS: Final[int] = 300
_HOOK_OUTPUT_LIMIT_BYTES: Final[int] = 16_384
# A hook that cannot be executed (a ConfigMap mounted without `defaultMode: 0755` is the
# way this happens in practice) is a failed hook, not a crashed Runner: 126 is the shell's
# own "found but not executable" so the Evidence reads the way an operator expects.
_NOT_EXECUTABLE_EXIT_CODE: Final[int] = 126
_TIMEOUT_EXIT_CODE: Final[int] = 124
_DIR_MODE: Final[int] = 0o700

AppendEvidence = Callable[[str, Mapping[str, object]], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class AttemptFacts:
    """What one Directive attempt is, in platform vocabulary a hook may be told."""

    # All defaulted: `runner_startup` and `runner_shutdown` run outside any Directive,
    # so there is no Work Record, Contract or Agent to name — and an empty value is
    # omitted from the environment rather than exported blank.
    work_record_id: str = ""
    directive_id: str = ""
    contract_id: str = ""
    agent_id: str = ""
    persona: str = ""
    runtime_kind: str = ""

    def env(self) -> dict[str, str]:
        return {
            f"{_FACT_ENV_PREFIX}{key.upper()}": value
            for key, value in (
                ("work_record_id", self.work_record_id),
                ("directive_id", self.directive_id),
                ("contract_id", self.contract_id),
                ("agent_id", self.agent_id),
                ("persona", self.persona),
                ("runtime_kind", self.runtime_kind),
            )
            if value
        }


@dataclass(frozen=True, slots=True)
class HookRun:
    """One hook execution, in the shape the Evidence Event carries (ticket 26 §1)."""

    name: HookName
    exit_code: int
    duration_ms: int
    stdout: str

    @property
    def failed(self) -> bool:
        return self.exit_code != 0

    def evidence(self) -> dict[str, object]:
        return {
            "hook": self.name.value,
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "stdout": self.stdout,
        }


class HookRefusedError(RuntimeError):
    """A hook at or before ``pre_runtime`` exited non-zero, so the attempt stops."""

    def __init__(self, run: HookRun) -> None:
        super().__init__(f"{run.name.value} hook exited {run.exit_code}")
        self.run = run


class DirectiveRejectedError(HookRefusedError):
    """``pre_directive`` refused this Directive before anything ran (the reject gate)."""


def load_hooks(hooks_path: Path | None) -> dict[HookName, Path]:
    """Every installed hook, keyed by its slot.

    Refuses a file whose name is not in the catalogue rather than ignoring it: an
    operator who writes ``pre-directive`` or ``post_command`` has installed a reject gate
    that would never run, and a security control that silently does nothing is worse than
    a Runner that will not start. Dotfiles are skipped (editor swap files, `..data` from
    a Kubernetes ConfigMap projection) and so are subdirectories.
    """

    if hooks_path is None or not hooks_path.is_dir():
        return {}
    installed: dict[HookName, Path] = {}
    for entry in sorted(hooks_path.iterdir()):
        if entry.name.startswith(".") or not entry.is_file():
            continue
        installed[hook_name(entry.name)] = entry
    return installed


def prepare_attempt_dir(path: Path, uid: int | None) -> Path:
    """A 0700 directory the Contract's uid owns — the attempt's own scratch."""

    path.mkdir(parents=True, exist_ok=True)
    path.chmod(_DIR_MODE)
    if uid is not None:
        os.chown(path, uid, uid)
    return path


class HookRunner:
    """Loads the hooks path once and runs one slot at a time under the Contract's floor."""

    def __init__(
        self,
        *,
        hooks_path: Path | None = None,
        hooks: Mapping[HookName, Path] | None = None,
        spawn: Callable[..., Awaitable[Any]] = run_subprocess_exec,
        timeout_seconds: int = _HOOK_TIMEOUT_SECONDS,
        output_limit_bytes: int = _HOOK_OUTPUT_LIMIT_BYTES,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._hooks = dict(hooks) if hooks is not None else load_hooks(hooks_path)
        self._spawn = spawn
        self._timeout_seconds = timeout_seconds
        self._output_limit_bytes = output_limit_bytes
        self._monotonic = monotonic

    @property
    def installed(self) -> frozenset[HookName]:
        return frozenset(self._hooks)

    def has(self, name: HookName) -> bool:
        return name in self._hooks

    async def run(
        self,
        name: HookName,
        *,
        cwd: Path,
        facts: AttemptFacts,
        sandbox: DirectiveSandbox | None = None,
        extra_env: Mapping[str, str] | None = None,
    ) -> HookRun | None:
        """Run one hook, or return None when that slot is empty."""

        hook_path = self._hooks.get(name)
        if hook_path is None:
            return None
        env = build_hook_env(
            facts=facts,
            hook=name,
            hook_path=hook_path,
            workspace_path=cwd,
            sandbox=sandbox,
            extra_env=extra_env,
        )
        started = self._monotonic()
        try:
            result = await self._spawn(
                argv=[str(hook_path)],
                cwd=cwd,
                env=env,
                stdin=None,
                timeout_seconds=self._timeout_seconds,
                output_limit_bytes=self._output_limit_bytes,
                sandbox=sandbox,
            )
            exit_code = int(result.exit_code)
            output = f"{result.stdout}{result.stderr}"
        except TimeoutError:
            exit_code, output = _TIMEOUT_EXIT_CODE, f"{name.value} hook timed out"
        except OSError as error:
            exit_code = _NOT_EXECUTABLE_EXIT_CODE
            output = f"{name.value} hook could not be executed: {error.__class__.__name__}"
        return HookRun(
            name=name,
            exit_code=exit_code,
            # Scrubbed exactly like verifier diagnostics (map ticket 06 §4): a hook prints
            # whatever it likes and this text lands in the control plane's Evidence.
            stdout=redact_secret_like_text(output)[: self._output_limit_bytes],
            duration_ms=int((self._monotonic() - started) * 1000),
        )


def build_hook_env(
    *,
    facts: AttemptFacts,
    hook: HookName,
    hook_path: Path,
    workspace_path: Path,
    sandbox: DirectiveSandbox | None,
    extra_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """The allow-list environment a hook runs with — no credential value, ever.

    Built from nothing but this attempt's platform ids, the paths the Runner owns and a
    ``PATH``. The parent process environment is never inherited: on a Runner pod it holds
    the GitHub App private key, the internal service token and the database URL, and
    inheritance is how every one of those would reach an operator's shell script.
    """

    env = {
        "PATH": os.environ.get("PATH") or os.defpath,
        "HOME": str(sandbox.home_dir) if sandbox else os.environ.get("HOME", str(workspace_path)),
        f"{_FACT_ENV_PREFIX}HOOK": hook.value,
        f"{_FACT_ENV_PREFIX}HOOK_PATH": str(hook_path),
        f"{_FACT_ENV_PREFIX}WORKSPACE": str(workspace_path),
        **facts.env(),
    }
    if sandbox is not None:
        env["TMPDIR"] = str(sandbox.tmp_dir)
    env.update(extra_env or {})
    return env


class DirectiveHookSession:
    """The hook lifecycle of one Directive attempt, in catalogue order.

    An async context manager because ``pre_exit`` is a *deferred teardown*: it runs on
    every exit path — success, a refusal by an earlier hook, an exception, and
    cancellation (a Temporal activity cancel, a worker drain) — which is the whole reason
    the slot exists. Everything else is ``phase()`` calls the activity makes in order.
    """

    def __init__(
        self,
        *,
        hooks: HookRunner,
        facts: AttemptFacts,
        workspace_path: Path,
        append_evidence: AppendEvidence,
        sandbox: DirectiveSandbox | None = None,
        attempt_dir: Path | None = None,
        proposed_env_names: tuple[str, ...] = (),
        teardown_grace_seconds: float = 30.0,
    ) -> None:
        self._hooks = hooks
        self._facts = facts
        self._workspace_path = workspace_path
        self._append_evidence = append_evidence
        self._sandbox = sandbox
        self._attempt_dir = attempt_dir or workspace_path
        self._proposed_env_names = proposed_env_names
        self._teardown_grace_seconds = teardown_grace_seconds
        # What `pre_exit` is told about the attempt it is cleaning up after: non-zero if
        # anything in it failed, whether or not that failure was fatal. Reported, never
        # consulted — nothing downstream reads it, least of all the Verifier's verdict.
        self._exit_code = 0

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if exc is not None and self._exit_code == 0:
            self._exit_code = 1
        teardown = asyncio.ensure_future(self._run_pre_exit())
        try:
            await asyncio.shield(teardown)
        except asyncio.CancelledError:
            # The attempt was cancelled while the teardown ran. `pre_exit` is exactly the
            # hook an operator installs to clean up after a cancel, so give it a bounded
            # second chance before letting the cancellation through.
            with contextlib.suppress(Exception, asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(
                    asyncio.shield(teardown), timeout=self._teardown_grace_seconds
                )
            raise

    def installed(self, name: HookName) -> bool:
        """Whether this slot has a hook — asked before the one override, ``checkout``."""

        return self._hooks.has(name)

    async def phase(self, name: HookName) -> HookRun | None:
        """Run one slot, enforcing the catalogue's fatality rule.

        Fatal through ``pre_runtime`` (:data:`FATAL_HOOKS`); ``pre_directive`` refuses the
        Directive outright. Later slots — ``post_runtime``, ``post_verify``,
        ``post_artifact`` — are recorded and carried on, so a failing ``post_verify``
        never changes the Verifier's verdict.
        """

        run = await self._run(name)
        if run is None:
            return None
        if run.failed:
            self._exit_code = run.exit_code
            if name is HookName.PRE_DIRECTIVE:
                await self._append_evidence(
                    DIRECTIVE_REJECTED_EVIDENCE_SOURCE,
                    {
                        "work_record_id": self._facts.work_record_id,
                        "directive_id": self._facts.directive_id,
                        "hook": name.value,
                        "exit_code": run.exit_code,
                        "reason": "the pre_directive reject gate refused this Directive",
                    },
                )
                raise DirectiveRejectedError(run)
            if name in FATAL_HOOKS:
                raise HookRefusedError(run)
        return run

    async def environment(self) -> dict[str, str]:
        """Run the ``environment`` hook and return what it exported.

        Buildkite sources the hook and diffs the shell environment; a polyglot hook there
        has to call the Job API instead. One mechanism is enough for both: the hook writes
        ``NAME=value`` lines to :data:`ENV_FILE_ENV`, so a compiled hook and a shell hook
        export the same way and nothing has to be sourced into the Runner's own process.

        Reserved names are dropped, not honoured: see :data:`RESERVED_DIRECTIVE_ENV`.
        """

        if not self._hooks.has(HookName.ENVIRONMENT):
            return {}
        env_file = self._attempt_dir / "environment.env"
        await self.phase(HookName.ENVIRONMENT)
        return _read_env_file(env_file)

    async def _run(self, name: HookName) -> HookRun | None:
        """Run one slot and record it. Every hook run is an Evidence Event (ticket 26 §1)."""

        run = await self._hooks.run(
            name,
            cwd=self._workspace_path,
            facts=self._facts,
            sandbox=self._sandbox,
            extra_env=self._slot_env(name),
        )
        if run is not None:
            await self._append_evidence(HOOK_EVIDENCE_SOURCE, run.evidence())
        return run

    def _slot_env(self, name: HookName) -> dict[str, str]:
        if name is HookName.ENVIRONMENT:
            return {ENV_FILE_ENV: str(self._attempt_dir / "environment.env")}
        if name is HookName.PRE_DIRECTIVE:
            return {PROPOSED_ENV_FILE_ENV: str(self._write_proposed_env_file())}
        if name is HookName.PRE_EXIT:
            return {f"{_FACT_ENV_PREFIX}EXIT_CODE": str(self._exit_code)}
        return {}

    def _write_proposed_env_file(self) -> Path:
        """The names — never the values — the Agent Runtime subprocess is about to get."""

        path = self._attempt_dir / "proposed-env.names"
        path.write_text("\n".join(self._proposed_env_names) + "\n", encoding="utf-8")
        path.chmod(0o600)
        if self._sandbox is not None and self._sandbox.uid is not None:
            os.chown(path, self._sandbox.uid, self._sandbox.uid)
        return path

    async def _run_pre_exit(self) -> None:
        await self._run(HookName.PRE_EXIT)


def _read_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    exports: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        name, separator, value = line.partition("=")
        name = name.strip()
        if not separator or not _ENV_NAME_RE.fullmatch(name):
            continue
        if name in RESERVED_DIRECTIVE_ENV:
            continue
        exports[name] = value
    return exports
