"""Claude Code's sign-in for one Contract, run by the person on the Runner (QTS-1448).

A fake ``claude`` (a Python script standing in for ``setup-token``, ``auth login
--claudeai`` and ``auth status --json``) runs through the real seam: the launcher, a real
pseudo-terminal relayed to the caller's descriptors, and ``ContractIsolation.sandbox``.
The caller's terminal is a pipe the test types into and a file it reads back. Like
``test_contract_device_login.py`` this runs with ``can_separate_uids=False``; the uid line
itself is ``tests/integration/test_contract_uid_isolation.py``'s.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import resource
import stat
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from agentic_runner import cli, service
from agentic_runner.workers._runtime_support import run_subprocess_exec
from agentic_runner.workers.claude_sign_in import (
    OAUTH_TOKEN_FILE,
    ClaudeSignInError,
    ClaudeSignIns,
    launcher_argv,
)
from agentic_runner.workers.contract_device_login import ContractDeviceLogin, UnknownHarnessError
from agentic_runner.workers.contract_isolation import ContractIsolation
from agentic_runner_contracts import __version__ as contracts_version
from agentic_runner_contracts.runner_registration import FloorState, HeartbeatAck

CONTRACT_A = "11111111-2222-4333-8444-555555555555"
CONTRACT_B = "66666666-7777-4888-8999-aaaaaaaaaaaa"
CODE = "browser-code-0123456789#state-abcdef"
TOKEN = "sk-ant-oat01-" + "Tk" * 30

_FAKE_CLAUDE = textwrap.dedent(
    """\
    import json, os, sys, tty

    args = sys.argv[1:]
    mode = args.pop(0)[len("--mode="):] if args and args[0].startswith("--mode=") else "ok"
    root = os.environ["CLAUDE_CONFIG_DIR"]

    def record(**fields):
        with open(os.path.join(root, "fake-record.jsonl"), "a") as handle:
            handle.write(json.dumps(fields) + "\\n")

    if args[:2] == ["auth", "status"]:
        if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
            method = "oauth_token"
        elif os.path.exists(os.path.join(root, ".credentials.json")):
            method = "claude.ai"
        else:
            method = "none"
        report = {"loggedIn": method != "none", "authMethod": method}
        if method != "none":
            report["subscriptionType"] = "max"
        print(json.dumps(report))
        sys.exit(0 if method != "none" else 1)

    record(
        argv=args, uid=os.getuid(), config_dir=root, home=os.environ["HOME"],
        scrub=os.environ.get("CLAUDE_CODE_SUBPROCESS_ENV_SCRUB"), tty=os.isatty(0),
        columns=os.get_terminal_size(0).columns,
    )
    tty.setraw(0)
    sys.stdout.write("Paste code here if prompted > ")
    sys.stdout.flush()
    chunks = []
    while not chunks or "\\r" not in chunks[-1]:
        data = os.read(0, 1024)
        if not data:
            break
        chunks.append(data.decode())
    record(typed="".join(chunks))
    if mode == "fail":
        sys.exit(3)
    if args == ["setup-token"]:
        if mode == "long-output":
            sys.stdout.write("x" * 70000 + "\\r\\n")
        sys.stdout.write("\\r\\nYour OAuth token (valid for 1 year):\\r\\n\\r\\n")
        if mode != "no-token":
            sys.stdout.write(TOKEN + "\\r\\n")
    else:
        with open(os.path.join(root, ".credentials.json"), "w") as handle:
            handle.write("{}")
    sys.stdout.flush()
    sys.exit(0)
    """
).replace("write(TOKEN +", f"write({TOKEN!r} +")


@pytest.fixture(autouse=True)
def _no_real_rlimits(monkeypatch: pytest.MonkeyPatch) -> None:
    # As in test_contract_device_login.py: macOS misreports RLIMIT_DATA's hard cap.
    monkeypatch.setattr(resource, "setrlimit", lambda *_args, **_kwargs: None)


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
def fake_claude(tmp_path: Path) -> Path:
    script = tmp_path / "fake_claude.py"
    script.write_text(_FAKE_CLAUDE)
    return script


class _Terminal:
    """The caller's side: what the person types, and what their screen shows."""

    def __init__(self, tmp_path: Path) -> None:
        self.stdin_fd, self._keyboard = os.pipe()
        screen = tmp_path / f"screen-{uuid4().hex}"
        self.stdout_fd = os.open(screen, os.O_WRONLY | os.O_CREAT, 0o600)
        self._screen = os.open(screen, os.O_RDONLY)
        self._shown = bytearray()

    def screen(self) -> str:
        while chunk := os.read(self._screen, 65_536):
            self._shown.extend(chunk)
        return self._shown.decode(errors="replace")

    async def type_at_prompt(self, typed: str) -> None:
        # As a person would: after the prompt, once the CLI has put its terminal in raw
        # mode -- `tty.setraw` flushes whatever was typed before it.
        while "Paste code here" not in self.screen():
            await asyncio.sleep(0.02)
        os.write(self._keyboard, typed.encode())
        os.close(self._keyboard)

    def close(self) -> None:
        for fd in (self.stdin_fd, self.stdout_fd, self._screen):
            os.close(fd)
        with contextlib.suppress(OSError):
            os.close(self._keyboard)


def _sign_ins(
    isolation: ContractIsolation, fake_claude: Path, *, mode: str = "ok", platform: str = "linux"
) -> ClaudeSignIns:
    fake = (sys.executable, str(fake_claude), f"--mode={mode}")
    return ClaudeSignIns(
        isolation,
        argv_by_method={
            "oauth_token": (*fake, "setup-token"),
            "claude_ai": (*fake, "auth", "login", "--claudeai"),
        },
        status_argv=(sys.executable, str(fake_claude), "auth", "status", "--json"),
        platform=platform,
    )


async def _sign_in(
    sign_ins: ClaudeSignIns, tmp_path: Path, contract_id: str = CONTRACT_A, **kwargs: Any
) -> tuple[int, str]:
    terminal = _Terminal(tmp_path)
    typing = asyncio.ensure_future(terminal.type_at_prompt(CODE + "\r"))
    try:
        async with asyncio.timeout(20):
            code = await sign_ins.sign_in(
                contract_id, stdin_fd=terminal.stdin_fd, stdout_fd=terminal.stdout_fd, **kwargs
            )
        return code, terminal.screen()
    finally:
        typing.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await typing
        terminal.close()


def _records(isolation: ContractIsolation, contract_id: str) -> list[dict[str, Any]]:
    path = isolation.harness_config_dir(contract_id, "claude_code") / "fake-record.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.fixture
def opened_paths(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Every path this process opens. The CLI's own child process is not affected."""

    paths: list[str] = []
    real_open = open
    real_path_open = Path.open

    def audited_open(file, *args, **kwargs):  # type: ignore[no-untyped-def]
        paths.append(str(file))
        return real_open(file, *args, **kwargs)

    def audited_path_open(self: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
        paths.append(str(self))
        return real_path_open(self, *args, **kwargs)

    monkeypatch.setattr("builtins.open", audited_open)
    monkeypatch.setattr(Path, "open", audited_path_open)
    yield paths


@pytest.mark.parametrize(
    ("method", "argv"),
    [
        ({}, ["setup-token"]),
        ({"method": "oauth_token"}, ["setup-token"]),
        ({"method": "claude_ai"}, ["auth", "login", "--claudeai"]),
    ],
)
@pytest.mark.asyncio
async def test_each_method_runs_its_cli_on_a_terminal_in_the_contracts_own_harness_root(
    tmp_path: Path,
    isolation: ContractIsolation,
    fake_claude: Path,
    method: dict[str, str],
    argv: list[str],
) -> None:
    """``oauth_token`` is the default; the person's keys reach the CLI and its prompt
    reaches the person, and the CLI runs with this Contract's CLAUDE_CONFIG_DIR."""

    code, screen = await _sign_in(_sign_ins(isolation, fake_claude), tmp_path, **method)

    assert code == 0
    assert "Paste code here if prompted > " in screen
    launched, typed = _records(isolation, CONTRACT_A)
    assert launched["argv"] == argv
    assert launched["config_dir"] == str(isolation.harness_config_dir(CONTRACT_A, "claude_code"))
    assert launched["home"] == str(isolation.contract_dir(CONTRACT_A))
    assert launched["uid"] == os.getuid()
    assert launched["scrub"] == "1"
    assert launched["tty"] is True
    assert launched["columns"] == 1000
    assert typed == {"typed": CODE + "\r"}
    assert _records(isolation, CONTRACT_B) == []


@pytest.mark.asyncio
async def test_the_long_lived_token_lands_in_the_harness_root_0600(
    tmp_path: Path, isolation: ContractIsolation, fake_claude: Path
) -> None:
    await _sign_in(_sign_ins(isolation, fake_claude), tmp_path)

    token_file = isolation.harness_config_dir(CONTRACT_A, "claude_code") / OAUTH_TOKEN_FILE
    status = token_file.stat()
    assert stat.S_IMODE(status.st_mode) == 0o600
    assert status.st_uid == os.getuid()
    assert token_file.read_text() == TOKEN
    assert not (isolation.harness_config_dir(CONTRACT_B, "claude_code") / OAUTH_TOKEN_FILE).exists()


@pytest.mark.asyncio
async def test_a_token_after_more_output_than_the_cap_still_lands(
    tmp_path: Path, isolation: ContractIsolation, fake_claude: Path
) -> None:
    """The cap keeps the tail: `setup-token` prints the token last."""

    await _sign_in(_sign_ins(isolation, fake_claude, mode="long-output"), tmp_path)

    token_file = isolation.harness_config_dir(CONTRACT_A, "claude_code") / OAUTH_TOKEN_FILE
    assert token_file.read_text() == TOKEN


@pytest.mark.asyncio
async def test_a_setup_token_that_prints_no_token_is_an_error_not_silence(
    tmp_path: Path, isolation: ContractIsolation, fake_claude: Path
) -> None:
    with pytest.raises(ClaudeSignInError, match="printed no token"):
        await _sign_in(_sign_ins(isolation, fake_claude, mode="no-token"), tmp_path)

    token_file = isolation.harness_config_dir(CONTRACT_A, "claude_code") / OAUTH_TOKEN_FILE
    assert not token_file.exists()


@pytest.mark.parametrize("method", ["oauth_token", "claude_ai"])
@pytest.mark.asyncio
async def test_a_failed_sign_in_returns_its_exit_code_and_keeps_the_previous_login(
    tmp_path: Path, isolation: ContractIsolation, fake_claude: Path, method: str
) -> None:
    await _sign_in(_sign_ins(isolation, fake_claude), tmp_path)

    code, _ = await _sign_in(
        _sign_ins(isolation, fake_claude, mode="fail"), tmp_path, method=method
    )

    assert code == 3
    token_file = isolation.harness_config_dir(CONTRACT_A, "claude_code") / OAUTH_TOKEN_FILE
    assert token_file.read_text() == TOKEN


@pytest.mark.asyncio
async def test_the_token_reaches_the_persons_terminal_and_no_log(
    tmp_path: Path,
    isolation: ContractIsolation,
    fake_claude: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The person sees what `claude setup-token` shows them; nothing else does."""

    caplog.set_level(logging.DEBUG)

    _, screen = await _sign_in(_sign_ins(isolation, fake_claude), tmp_path)

    assert TOKEN in screen
    assert TOKEN not in caplog.text
    assert TOKEN[len("sk-ant-oat01-") :] not in caplog.text


@pytest.mark.asyncio
async def test_a_harness_launch_sees_the_token_without_the_runner_opening_it(
    tmp_path: Path, isolation: ContractIsolation, fake_claude: Path, opened_paths: list[str]
) -> None:
    await _sign_in(_sign_ins(isolation, fake_claude), tmp_path)
    sandbox = isolation.sandbox(CONTRACT_A, runtime_kind="claude_code")
    opened_paths.clear()

    harness = await run_subprocess_exec(
        argv=launcher_argv(
            sandbox.harness_config_dir,
            [
                sys.executable,
                "-c",
                "import os; e = os.environ; "
                f"print(e.get('CLAUDE_CODE_OAUTH_TOKEN') == {TOKEN!r}, "
                "e.get('CLAUDE_CODE_SUBPROCESS_ENV_SCRUB'))",
            ],
        ),
        cwd=sandbox.home_dir,
        env={"PATH": os.environ["PATH"], "HOME": str(sandbox.home_dir)},
        stdin=None,
        timeout_seconds=10,
        output_limit_bytes=4096,
        sandbox=sandbox,
    )

    assert harness.stdout.split() == ["True", "1"]
    assert not any(path.endswith(OAUTH_TOKEN_FILE) for path in opened_paths)


@pytest.mark.asyncio
async def test_presence_comes_from_auth_status_for_both_methods(
    tmp_path: Path, isolation: ContractIsolation, fake_claude: Path, opened_paths: list[str]
) -> None:
    sign_ins = _sign_ins(isolation, fake_claude)
    harness_root = str(isolation.harness_config_dir(CONTRACT_A, "claude_code"))

    before = await sign_ins.status(CONTRACT_A)

    await _sign_in(sign_ins, tmp_path, method="oauth_token")
    opened_paths.clear()
    long_lived = await sign_ins.status(CONTRACT_A)
    assert not any(path.startswith(harness_root) for path in opened_paths)

    await _sign_in(sign_ins, tmp_path, method="claude_ai")
    opened_paths.clear()
    short_lived = await sign_ins.status(CONTRACT_A)
    assert not any(path.startswith(harness_root) for path in opened_paths)

    assert (before.logged_in, before.auth_method) == (False, None)
    assert (long_lived.logged_in, long_lived.auth_method, long_lived.subscription_type) == (
        True,
        "oauth_token",
        "max",
    )
    # A finished `claude_ai` sign-in replaced the token the launcher would have preferred.
    assert (short_lived.logged_in, short_lived.auth_method) == (True, "claude.ai")
    assert not (Path(harness_root) / OAUTH_TOKEN_FILE).exists()


_FAKE_SECURITY = textwrap.dedent(
    """\
    #!/bin/sh
    stdin=""
    if [ "$1" = unlock-keychain ]; then read -r stdin; fi
    if [ "$1" = create-keychain ]; then read -r new; read -r again; stdin="$new,$again"; fi
    printf '%s|%s|%s\\n' "$HOME" "$stdin" "$*" >>"$SECURITY_LOG"
    if [ "$1" = create-keychain ]; then : >"$2"; fi
    """
)


@pytest.mark.asyncio
async def test_macos_short_lived_sign_in_gets_a_per_contract_keychain(
    tmp_path: Path,
    isolation: ContractIsolation,
    fake_claude: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 2026-10-08 spike: Claude saves a `claude_ai` login into the default keychain
    of the sandbox HOME, which has none -- so the Runner makes one there, and the launcher
    unlocks it (from stdin, never argv) before the CLI runs."""

    log = tmp_path / "security.log"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # Baked in: the sandbox env carries PATH but nothing else of this process's.
    (bin_dir / "security").write_text(_FAKE_SECURITY.replace("$SECURITY_LOG", str(log)))
    (bin_dir / "security").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    sign_ins = _sign_ins(isolation, fake_claude, platform="darwin")
    root = isolation.harness_config_dir(CONTRACT_A, "claude_code")
    home = str(isolation.contract_dir(CONTRACT_A))

    await _sign_in(sign_ins, tmp_path, method="oauth_token")
    assert not log.exists(), "the long-lived token needs no keychain"
    await _sign_in(sign_ins, tmp_path, method="claude_ai")
    await _sign_in(sign_ins, tmp_path, method="claude_ai")

    password = (root / "keychain-password").read_text()
    assert stat.S_IMODE((root / "keychain-password").stat().st_mode) == 0o600
    keychain = str(root / "claude.keychain-db")
    calls = [line.split("|", 2) for line in log.read_text().splitlines()]
    assert all(called_home == home for called_home, _, _ in calls)
    made = [(stdin, argv) for _, stdin, argv in calls if not argv.startswith("unlock-keychain")]
    assert made == [
        (f"{password},{password}", f"create-keychain {keychain}"),
        ("", f"set-keychain-settings {keychain}"),
        ("", f"list-keychains -d user -s {keychain}"),
        ("", f"default-keychain -d user -s {keychain}"),
    ], "made once, on the first short-lived sign-in"
    unlocks = [(stdin, argv) for _, stdin, argv in calls if argv.startswith("unlock-keychain")]
    assert unlocks == [(password, f"unlock-keychain {keychain}")] * 2


# ------------------------------------------------------------------ nothing is relayed


@pytest.mark.asyncio
async def test_no_console_starts_a_claude_sign_in(isolation: ContractIsolation) -> None:
    """The device-login activity has no Claude Code command left to start (QTS-1448)."""

    with pytest.raises(UnknownHarnessError):
        await ContractDeviceLogin(isolation).sign_in(CONTRACT_A, runtime_kind="claude_code")


def test_the_ack_carries_no_sign_in_code() -> None:
    """Contracts 3.14: the field is gone. A 3.13 control plane's empty one still parses, so
    either side may upgrade first; an ack that carries a code is refused."""

    ack = {
        "runner_id": str(uuid4()),
        "floor_state": FloorState.OK,
        "contracts_floor": contracts_version,
        "tag_set_version": 1,
        "accepts_new_directives": True,
    }
    code = {
        "contract_id": CONTRACT_A,
        "sign_in_id": "si_" + "0" * 32,
        "recipient_key_id": "rk_" + "0" * 32,
        "ciphertext": "c2VhbGVk",
    }

    assert HeartbeatAck.model_validate({**ack, "sign_in_codes": []}) == (
        HeartbeatAck.model_validate(ack)
    )
    assert "sign_in_codes" not in HeartbeatAck.model_validate(ack).model_dump()
    with pytest.raises(ValidationError):
        HeartbeatAck.model_validate({**ack, "sign_in_codes": [code]})


# ------------------------------------------------------------------ the command


def test_the_command_refuses_a_contract_id_that_is_not_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(service, "sign_in_claude", _unexpected)

    assert cli.main(["sign-in", "--contract", "../other"]) == 2
    assert "expects a Contract id" in capsys.readouterr().err


def test_the_command_needs_a_terminal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(service, "sign_in_claude", _unexpected)
    monkeypatch.setattr(os, "isatty", lambda _fd: False)

    assert cli.main(["sign-in", "--contract", CONTRACT_A]) == 2
    assert "kubectl exec -it" in capsys.readouterr().err


def test_the_command_signs_in_the_named_contract_with_the_named_method(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []

    async def sign_in_claude(contract_id: str, *, method: str) -> int:
        calls.append((contract_id, method))
        return 0

    monkeypatch.setattr(service, "sign_in_claude", sign_in_claude)
    monkeypatch.setattr(os, "isatty", lambda _fd: True)

    assert cli.main(["sign-in", "--contract", CONTRACT_A.upper()]) == 0
    assert cli.main(["sign-in", "--contract", CONTRACT_A, "--method", "claude_ai"]) == 0
    assert calls == [(CONTRACT_A, "oauth_token"), (CONTRACT_A, "claude_ai")]


@pytest.mark.asyncio
async def test_without_the_runners_environment_the_command_refuses(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The uid map and the harness roots are the Runner's: guessing them would sign in
    a directory no Directive reads."""

    monkeypatch.setenv(service.ISOLATION_ENV, "none")
    for name in ("TEMPORAL_ADDRESS", "INTERNAL_FASTAPI_BASE_URL", service.CONTROL_PLANE_ENV):
        monkeypatch.delenv(name, raising=False)

    assert await service.sign_in_claude(CONTRACT_A, method="oauth_token") == 2
    assert "--org" in capsys.readouterr().err


async def _unexpected(contract_id: str, *, method: str) -> int:
    raise AssertionError("refused before any sign-in")
