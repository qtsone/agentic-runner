"""Per-Contract subscription sign-in (PRD issue 31, ADR-0015 §4).

Runs a fake vendor CLI (a small Python script standing in for ``codex login
--device-auth``) through the real spawn path -- ``ContractIsolation.sandbox`` and
``run_subprocess_launch_and_detach`` -- so what is under test is the wiring: ``sign_in``
returns the prompt without waiting for the CLI to finish (PRD issue 31 review), the token
lands under the *signing-in* Contract's own harness root once it does, a second Contract's
root is untouched, and this process's own Python code never opens the token file itself.

uid separation itself (``CAP_SETUID``) is ``tests/integration/test_contract_uid_isolation.py``'s;
this file runs with ``can_separate_uids=False``, the same honest-degradation posture that
suite documents, so it proves the per-Contract *path* isolation on any laptop.
"""

from __future__ import annotations

import asyncio
import resource
import stat
import sys
import textwrap
from pathlib import Path

import pytest

from agentic_runner.workers.contract_device_login import ContractDeviceLogin
from agentic_runner.workers.contract_isolation import ContractIsolation


@pytest.fixture(autouse=True)
def _no_real_rlimits(monkeypatch: pytest.MonkeyPatch) -> None:
    """The spawn floor's own mechanics are ``test_contract_isolation.py``'s; here it
    would just fail unprivileged on a platform whose ``RLIMIT_DATA`` hard cap the
    ``resource`` module misreports (macOS). ``preexec_fn`` runs in the forked child
    before exec, which inherits this process's monkeypatch, so the fake CLI still runs.
    """

    monkeypatch.setattr(resource, "setrlimit", lambda *_args, **_kwargs: None)


CONTRACT_A = "11111111-2222-4333-8444-555555555555"
CONTRACT_B = "66666666-7777-4888-8999-aaaaaaaaaaaa"

_FAKE_CODEX = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    # A fake vendor device-code flow: print the prompt (flushed, like the real CLI),
    # sleep a beat standing in for "polling the vendor" (research/29), then write the
    # token in place -- so a test can observe the prompt arriving before completion.
    import os
    import sys
    import time

    print("Open this link: https://auth.example.com/device", flush=True)
    print("Enter this one-time code (expires in 15 minutes): ABCD-1234", flush=True)
    time.sleep(0.3)
    codex_home = os.environ["CODEX_HOME"]
    with open(os.path.join(codex_home, "auth.json"), "w") as handle:
        handle.write('{"tokens": {"refresh_token": "not-a-real-token"}}')
    sys.exit(0)
    """
)


@pytest.fixture
def isolation(tmp_path: Path) -> ContractIsolation:
    return ContractIsolation(
        workspace_root=tmp_path / "workspaces",
        state_dir=tmp_path / "state",
        uid_min=60_000,
        uid_max=60_009,
        max_processes=512,
        memory_limit_bytes=4 * 1024**3,
        can_separate_uids=False,
    )


@pytest.fixture
def fake_codex(tmp_path: Path) -> Path:
    script = tmp_path / "fake_codex.py"
    script.write_text(_FAKE_CODEX)
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


@pytest.fixture
def device_login(isolation: ContractIsolation, fake_codex: Path) -> ContractDeviceLogin:
    return ContractDeviceLogin(
        isolation,
        argv_by_runtime={"codex_cli": (sys.executable, str(fake_codex))},
        prompt_timeout_seconds=10,
    )


@pytest.mark.asyncio
async def test_sign_in_returns_the_prompt_before_the_cli_finishes(
    device_login: ContractDeviceLogin,
) -> None:
    """The blocking bug (PRD issue 31 review): a funder cannot open the vendor page,
    sign in and approve a device code inside a single request/response. ``sign_in`` must
    hand back the URL/code as soon as they are printed, detaching the CLI to finish."""

    prompt = await device_login.sign_in(CONTRACT_A, runtime_kind="codex_cli")

    assert prompt.verification_uri == "https://auth.example.com/device"
    assert prompt.user_code == "ABCD-1234"
    # The fake CLI is still sleeping before it writes the token -- proof `sign_in` did
    # not wait for it.
    assert device_login.token_present(CONTRACT_A, runtime_kind="codex_cli") is False


@pytest.mark.asyncio
async def test_the_token_lands_under_the_signing_in_contracts_own_harness_root(
    device_login: ContractDeviceLogin,
) -> None:
    await device_login.sign_in(CONTRACT_A, runtime_kind="codex_cli")

    await asyncio.sleep(0.6)
    assert device_login.token_present(CONTRACT_A, runtime_kind="codex_cli") is True


@pytest.mark.asyncio
async def test_a_second_contracts_root_is_untouched(
    device_login: ContractDeviceLogin,
) -> None:
    await device_login.sign_in(CONTRACT_A, runtime_kind="codex_cli")

    assert device_login.token_present(CONTRACT_B, runtime_kind="codex_cli") is False


@pytest.mark.asyncio
async def test_the_two_contracts_get_two_separate_harness_directories(
    isolation: ContractIsolation, device_login: ContractDeviceLogin
) -> None:
    await device_login.sign_in(CONTRACT_A, runtime_kind="codex_cli")
    await asyncio.sleep(0.6)

    root_a = isolation.harness_config_dir(CONTRACT_A, "codex_cli")
    root_b = isolation.harness_config_dir(CONTRACT_B, "codex_cli")
    assert (root_a / "auth.json").is_file()
    assert root_a != root_b
    assert not (root_b / "auth.json").exists()


@pytest.mark.asyncio
async def test_the_worker_process_never_opens_the_token_file(
    device_login: ContractDeviceLogin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point of a device login (research/29): the Runner starts the CLI and
    later checks for the file it wrote, but never reads its contents. The fake CLI above
    opens it from its own, separate child process -- unaffected by this patch, exactly as
    a real Codex subprocess would be -- so a call reaching this process's own ``open``
    would mean this module read the token itself, which is what this asserts against.
    """

    opened_paths: list[str] = []
    real_open = open
    real_path_open = Path.open

    def audited_open(file, *args, **kwargs):  # type: ignore[no-untyped-def]
        opened_paths.append(str(file))
        return real_open(file, *args, **kwargs)

    def audited_path_open(self: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
        opened_paths.append(str(self))
        return real_path_open(self, *args, **kwargs)

    monkeypatch.setattr("builtins.open", audited_open)
    # `Path.open` routes through `io.open`, a separate attribute `builtins.open` alone
    # does not reach -- patched too so this guard would actually catch a `Path.open` read
    # of the token, not just a bare `open()` one.
    monkeypatch.setattr(Path, "open", audited_path_open)

    await device_login.sign_in(CONTRACT_A, runtime_kind="codex_cli")
    await asyncio.sleep(0.6)

    assert device_login.token_present(CONTRACT_A, runtime_kind="codex_cli") is True
    assert not any(path.endswith("auth.json") for path in opened_paths)
