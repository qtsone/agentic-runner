"""A harness's ``--version``, run as each Contract would run it (local-agents 17).

What the heartbeat's ``cli`` attestation says is that the Runner's own uid can start a
CLI. Whether a Contract can is a different fact: its uid, its harness root and its
sandbox floor all stand between the two. So the self-test spawns exactly what a
Directive spawns, minus the prompt, and keeps only the verdict -- never the output, which
on a harness root can carry an account name.

At most once per interval per Contract and never while that Contract has a Directive
running: the harness root is the Directive's while it runs, and a concurrent spawn
under the same rlimit floor would count against the Directive's process ceiling.
"""

from __future__ import annotations

import os
import subprocess
from collections import Counter
from collections.abc import Awaitable, Callable, Collection, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Final
from uuid import UUID

from agentic_runner.cli_floor import CLI_NAMES, meets_floor, parse_cli_version
from agentic_runner.workers._runtime_support import SubprocessResult, run_subprocess_exec
from agentic_runner.workers.contract_isolation import ContractIsolationError, DirectiveSandbox
from agentic_runner_contracts.runner_registration import HarnessSelfTest, SelfTestFailure

__all__ = [
    "SELF_TEST_INTERVAL_SECONDS",
    "DirectivesInFlight",
    "HarnessSelfTests",
]

SELF_TEST_INTERVAL_SECONDS: Final = 15 * 60
_TIMEOUT_SECONDS: Final = 10
_OUTPUT_LIMIT_BYTES: Final = 4096

# Where each harness reads its config root from; the same variable its runtime sets.
_HARNESS_ROOT_ENV: Final = {"codex_cli": "CODEX_HOME", "claude_code": "CLAUDE_CONFIG_DIR"}

SandboxFor = Callable[[str, str], DirectiveSandbox | None]


class DirectivesInFlight:
    """Which Contracts have a Directive running in this process right now."""

    def __init__(self) -> None:
        self._running: Counter[str] = Counter()

    @contextmanager
    def running(self, contract_id: str | None) -> Iterator[None]:
        key = contract_id or ""
        self._running[key] += 1
        try:
            yield
        finally:
            self._running[key] -= 1
            if self._running[key] <= 0:
                del self._running[key]

    def busy(self, contract_id: str) -> bool:
        return self._running[contract_id] > 0


class HarnessSelfTests:
    """The latest self-test per ``(contract_id, cli_kind)``, and when the next one is due."""

    def __init__(
        self,
        *,
        sandbox_for: SandboxFor,
        in_flight: DirectivesInFlight,
        served: Collection[str],
        run: Callable[..., Awaitable[SubprocessResult]] = run_subprocess_exec,
        monotonic: Callable[[], float],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        interval_seconds: float = SELF_TEST_INTERVAL_SECONDS,
    ) -> None:
        self._sandbox_for = sandbox_for
        self._in_flight = in_flight
        self._served = frozenset(served)
        self._run = run
        self._monotonic = monotonic
        self._clock = clock
        self._interval = interval_seconds
        self._results: dict[tuple[str, str], HarnessSelfTest] = {}
        self._last_run: dict[tuple[str, str], float] = {}

    def result(self, contract_id: UUID | str, cli_kind: str) -> HarnessSelfTest | None:
        return self._results.get((str(contract_id), cli_kind))

    async def run_due(self, targets: Sequence[tuple[str, str]]) -> None:
        for gone in (self._results.keys() | self._last_run.keys()) - set(targets):
            self._forget(gone)
        for contract_id, cli_kind in targets:
            key = (contract_id, cli_kind)
            last = self._last_run.get(key)
            if last is not None and self._monotonic() - last < self._interval:
                continue
            # Checked before the spawn only: a Directive that starts during the (at most
            # _TIMEOUT_SECONDS) spawn overlaps it. Accepted -- `--version` neither reads
            # nor writes the harness root's state and holds one process slot briefly.
            if self._in_flight.busy(contract_id):
                # Not stamped: it runs on the first sweep after the Directive ends.
                continue
            result = await self._self_test(contract_id, cli_kind)
            if result is None:
                self._forget(key)
                continue
            self._results[key] = result
            self._last_run[key] = self._monotonic()

    def _forget(self, key: tuple[str, str]) -> None:
        self._results.pop(key, None)
        self._last_run.pop(key, None)

    async def _self_test(self, contract_id: str, cli_kind: str) -> HarnessSelfTest | None:
        """``None`` when the harness root is gone, wiped since the sweep listed it."""

        name = CLI_NAMES.get(cli_kind)
        if name is None or cli_kind not in self._served:
            return self._failed(SelfTestFailure.NOT_FOUND)
        try:
            sandbox = self._sandbox_for(contract_id, cli_kind)
        except (ContractIsolationError, OSError):
            return self._failed(SelfTestFailure.SPAWN_FAILED)
        if sandbox is None:
            return None
        env = {
            "PATH": os.environ.get("PATH") or os.defpath,
            "HOME": str(sandbox.home_dir),
            "TMPDIR": str(sandbox.tmp_dir),
            _HARNESS_ROOT_ENV[cli_kind]: str(sandbox.harness_config_dir),
        }
        try:
            answer = await self._run(
                argv=[name, "--version"],
                cwd=sandbox.home_dir,
                env=env,
                stdin=None,
                timeout_seconds=_TIMEOUT_SECONDS,
                output_limit_bytes=_OUTPUT_LIMIT_BYTES,
                sandbox=sandbox,
            )
        except TimeoutError:
            return self._failed(SelfTestFailure.TIMEOUT)
        except FileNotFoundError:
            return self._failed(SelfTestFailure.NOT_FOUND)
        except (OSError, subprocess.SubprocessError):
            # SubprocessError: the sandbox floor's own preexec hook refused the spawn.
            return self._failed(SelfTestFailure.SPAWN_FAILED)
        version = parse_cli_version(answer.stdout or answer.stderr)
        if answer.exit_code != 0 or version is None:
            return self._failed(SelfTestFailure.SPAWN_FAILED)
        if not meets_floor(name, version):
            return self._failed(SelfTestFailure.BELOW_FLOOR)
        return HarnessSelfTest(ok=True, at=self._clock())

    def _failed(self, reason: SelfTestFailure) -> HarnessSelfTest:
        return HarnessSelfTest(ok=False, at=self._clock(), reason=reason)
