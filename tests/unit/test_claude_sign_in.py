"""Claude Code's in-place sign-in for one Contract (local-agents 05).

A fake ``claude`` (a Python script standing in for ``setup-token``, ``auth login
--claudeai`` and ``auth status --json``) runs through the real seam: the launcher, a real
pseudo-terminal and ``ContractIsolation.sandbox``. Like ``test_contract_device_login.py``
this runs with ``can_separate_uids=False``; the uid line itself is
``tests/integration/test_contract_uid_isolation.py``'s.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import resource
import stat
import sys
import textwrap
from collections.abc import Iterator
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from agentic_runner import service
from agentic_runner.credentials import CredentialResolver, EmptyCredentialStore
from agentic_runner.device_login_activities import ContractDeviceLoginActivities
from agentic_runner.llm_proxy import CeilingStore, LlmProxy, SlotStore, UsageOutbox
from agentic_runner.registration import RunnerState
from agentic_runner.sealed_box import RecipientKeyStore, SealedCredentialStream, seal
from agentic_runner.workers._runtime_support import run_subprocess_exec
from agentic_runner.workers.claude_sign_in import (
    OAUTH_TOKEN_FILE,
    ClaudeSignInError,
    ClaudeSignIns,
    launcher_argv,
)
from agentic_runner.workers.contract_device_login import ContractDeviceLogin
from agentic_runner.workers.contract_isolation import ContractIsolation
from agentic_runner_contracts import __version__ as contracts_version
from agentic_runner_contracts.activity_io import (
    ContractDeviceLoginInput,
    ContractDeviceLoginStatusInput,
)
from agentic_runner_contracts.runner_registration import (
    FloorState,
    HeartbeatAck,
    HeartbeatEnvelope,
)
from agentic_runner_contracts.sealed_credential import (
    SealedSignInCode,
    SignInCodeOutcome,
    SignInCodeRelay,
    sign_in_code_binding,
)

CONTRACT_A = "11111111-2222-4333-8444-555555555555"
CONTRACT_B = "66666666-7777-4888-8999-aaaaaaaaaaaa"
CODE = "browser-code-0123456789#state-abcdef"
TOKEN = "sk-ant-oat01-" + "Tk" * 30

_FAKE_CLAUDE = textwrap.dedent(
    """\
    import json, os, sys, time, tty

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
        argv=args, pid=os.getpid(), pgid=os.getpgid(0), uid=os.getuid(), config_dir=root,
        home=os.environ["HOME"], scrub=os.environ.get("CLAUDE_CODE_SUBPROCESS_ENV_SCRUB"),
    )
    tty.setraw(0)
    pkce = "" if mode == "no-pkce" else "&code_challenge=c&code_challenge_method=S256"
    url = "https://claude.com/cai/oauth/authorize?code=true" + pkce + "&state=s"
    sys.stdout.write("\\x1b]8;id=1;" + url + "\\x07Browser didn't open?\\x1b]8;;\\x07\\r\\n")
    sys.stdout.write("Paste code here if prompted > ")
    sys.stdout.flush()
    if mode == "hang":
        time.sleep(60)
    chunks = []
    while not chunks or "\\r" not in chunks[-1]:
        data = os.read(0, 1024)
        if not data:
            break
        chunks.append(data.decode())
    record(chunks=chunks)
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


def _sign_ins(
    isolation: ContractIsolation, fake_claude: Path, *, mode: str = "ok", **kwargs: Any
) -> ClaudeSignIns:
    fake = (sys.executable, str(fake_claude), f"--mode={mode}")
    return ClaudeSignIns(
        isolation,
        argv_by_method={
            "oauth_token": (*fake, "setup-token"),
            "claude_ai": (*fake, "auth", "login", "--claudeai"),
        },
        status_argv=(sys.executable, str(fake_claude), "auth", "status", "--json"),
        prompt_timeout_seconds=10,
        platform="linux",
        **kwargs,
    )


def _records(isolation: ContractIsolation, contract_id: str) -> list[dict[str, Any]]:
    path = isolation.harness_config_dir(contract_id, "claude_code") / "fake-record.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


async def _until(predicate: Any, *, timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.02)


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
        (None, ["setup-token"]),
        ("oauth_token", ["setup-token"]),
        ("claude_ai", ["auth", "login", "--claudeai"]),
    ],
)
@pytest.mark.asyncio
async def test_each_method_runs_its_cli_in_the_contracts_own_harness_root(
    isolation: ContractIsolation, fake_claude: Path, method: str | None, argv: list[str]
) -> None:
    """``oauth_token`` is the default; either CLI gets this Contract's CLAUDE_CONFIG_DIR."""

    sign_ins = _sign_ins(isolation, fake_claude)

    prompt = await sign_ins.start(CONTRACT_A, method=method)

    assert prompt.verification_uri.startswith("https://claude.com/cai/oauth/authorize?")
    assert "code_challenge_method=S256" in prompt.verification_uri
    assert prompt.sign_in_id.startswith("si_")
    [launched] = _records(isolation, CONTRACT_A)
    assert launched["argv"] == argv
    assert launched["config_dir"] == str(isolation.harness_config_dir(CONTRACT_A, "claude_code"))
    assert launched["home"] == str(isolation.contract_dir(CONTRACT_A))
    assert launched["uid"] == os.getuid()
    assert launched["scrub"] == "1"
    assert _records(isolation, CONTRACT_B) == []
    await sign_ins.close()


@pytest.mark.asyncio
async def test_the_process_group_is_gone_after_the_window(
    isolation: ContractIsolation, fake_claude: Path
) -> None:
    sign_ins = _sign_ins(isolation, fake_claude, mode="hang", window=timedelta(seconds=0.5))

    await sign_ins.start(CONTRACT_A, method="oauth_token")
    [launched] = _records(isolation, CONTRACT_A)
    os.killpg(launched["pgid"], 0)

    await _until(lambda: sign_ins.waiting == 0)
    with pytest.raises(ProcessLookupError):
        os.killpg(launched["pgid"], 0)


@pytest.mark.asyncio
async def test_a_url_without_pkce_is_refused_before_any_code_could_be_relayed(
    isolation: ContractIsolation, fake_claude: Path
) -> None:
    sign_ins = _sign_ins(isolation, fake_claude, mode="no-pkce")

    with pytest.raises(ClaudeSignInError, match="S256"):
        await sign_ins.start(CONTRACT_A, method="oauth_token")
    assert sign_ins.waiting == 0


@pytest.mark.asyncio
async def test_a_cli_that_exits_before_its_url_is_an_error(
    isolation: ContractIsolation, fake_claude: Path
) -> None:
    sign_ins = ClaudeSignIns(
        isolation, argv_by_method={"oauth_token": (sys.executable, "-c", "raise SystemExit(4)")}
    )

    with pytest.raises(ClaudeSignInError, match="code 4"):
        await sign_ins.start(CONTRACT_A, method="oauth_token")


@pytest.mark.asyncio
async def test_the_relayed_code_reaches_the_pty_then_a_separate_enter(
    isolation: ContractIsolation, fake_claude: Path
) -> None:
    sign_ins = _sign_ins(isolation, fake_claude)
    prompt = await sign_ins.start(CONTRACT_A, method="claude_ai")

    outcome = await sign_ins.relay(CONTRACT_A, prompt.sign_in_id, CODE)

    assert outcome is SignInCodeOutcome.WRITTEN
    await _until(lambda: len(_records(isolation, CONTRACT_A)) == 2)
    assert _records(isolation, CONTRACT_A)[1]["chunks"] == [CODE, "\r"]


@pytest.mark.asyncio
async def test_a_code_for_a_sign_in_the_runner_did_not_start_is_refused(
    isolation: ContractIsolation, fake_claude: Path
) -> None:
    sign_ins = _sign_ins(isolation, fake_claude)
    prompt = await sign_ins.start(CONTRACT_A, method="oauth_token")

    unknown = await sign_ins.relay(CONTRACT_A, "si_" + "0" * 32, CODE)
    other_contract = await sign_ins.relay(CONTRACT_B, prompt.sign_in_id, CODE)
    keystrokes = await sign_ins.relay(CONTRACT_A, prompt.sign_in_id, "abcdefgh\rexit\r")

    assert unknown is SignInCodeOutcome.UNKNOWN
    assert other_contract is SignInCodeOutcome.UNKNOWN
    assert keystrokes is SignInCodeOutcome.UNOPENABLE
    await sign_ins.close()
    assert [r for r in _records(isolation, CONTRACT_A) if "chunks" in r] == []


@pytest.mark.asyncio
async def test_a_code_after_ten_minutes_is_refused_and_never_written(
    isolation: ContractIsolation, fake_claude: Path
) -> None:
    now = [0.0]
    sign_ins = _sign_ins(isolation, fake_claude, clock=lambda: now[0])
    prompt = await sign_ins.start(CONTRACT_A, method="oauth_token")

    now[0] = 600.0
    outcome = await sign_ins.relay(CONTRACT_A, prompt.sign_in_id, CODE)

    assert outcome is SignInCodeOutcome.EXPIRED
    await asyncio.sleep(0.3)
    await sign_ins.close()
    assert [r for r in _records(isolation, CONTRACT_A) if "chunks" in r] == []


@pytest.mark.asyncio
async def test_a_code_after_the_cli_exited_is_refused(
    isolation: ContractIsolation, fake_claude: Path
) -> None:
    sign_ins = _sign_ins(isolation, fake_claude)
    prompt = await sign_ins.start(CONTRACT_A, method="claude_ai")
    assert await sign_ins.relay(CONTRACT_A, prompt.sign_in_id, CODE) is SignInCodeOutcome.WRITTEN
    await _until(lambda: sign_ins.waiting == 0)

    again = await sign_ins.relay(CONTRACT_A, prompt.sign_in_id, CODE)

    # Forgotten once it ended: a finished sign-in is as unknown as one never started.
    assert again is SignInCodeOutcome.UNKNOWN


@pytest.mark.asyncio
async def test_the_long_lived_token_lands_in_the_harness_root_0600(
    isolation: ContractIsolation, fake_claude: Path
) -> None:
    sign_ins = _sign_ins(isolation, fake_claude)
    prompt = await sign_ins.start(CONTRACT_A, method=None)

    await sign_ins.relay(CONTRACT_A, prompt.sign_in_id, CODE)
    await _until(lambda: sign_ins.waiting == 0)

    token_file = isolation.harness_config_dir(CONTRACT_A, "claude_code") / OAUTH_TOKEN_FILE
    status = token_file.stat()
    assert stat.S_IMODE(status.st_mode) == 0o600
    assert status.st_uid == os.getuid()
    assert token_file.read_text() == TOKEN
    assert not (isolation.harness_config_dir(CONTRACT_B, "claude_code") / OAUTH_TOKEN_FILE).exists()


@pytest.mark.asyncio
async def test_a_token_after_more_output_than_the_cap_still_lands(
    isolation: ContractIsolation, fake_claude: Path
) -> None:
    """The cap keeps the tail: `setup-token` prints the token last."""

    sign_ins = _sign_ins(isolation, fake_claude, mode="long-output")
    prompt = await sign_ins.start(CONTRACT_A, method=None)
    await sign_ins.relay(CONTRACT_A, prompt.sign_in_id, CODE)
    await _until(lambda: sign_ins.waiting == 0)

    token_file = isolation.harness_config_dir(CONTRACT_A, "claude_code") / OAUTH_TOKEN_FILE
    assert token_file.read_text() == TOKEN


@pytest.mark.asyncio
async def test_a_setup_token_that_prints_no_token_is_an_error_not_silence(
    isolation: ContractIsolation, fake_claude: Path, caplog: pytest.LogCaptureFixture
) -> None:
    sign_ins = _sign_ins(isolation, fake_claude, mode="no-token")
    prompt = await sign_ins.start(CONTRACT_A, method=None)
    with caplog.at_level(logging.ERROR):
        await sign_ins.relay(CONTRACT_A, prompt.sign_in_id, CODE)
        await _until(lambda: sign_ins.waiting == 0)

    token_file = isolation.harness_config_dir(CONTRACT_A, "claude_code") / OAUTH_TOKEN_FILE
    assert not token_file.exists()
    assert f"{prompt.sign_in_id} did not complete" in caplog.text
    assert "printed no token" in caplog.text


@pytest.mark.asyncio
async def test_a_harness_launch_sees_the_token_without_the_runner_opening_it(
    isolation: ContractIsolation, fake_claude: Path, opened_paths: list[str]
) -> None:
    sign_ins = _sign_ins(isolation, fake_claude)
    prompt = await sign_ins.start(CONTRACT_A, method="oauth_token")
    await sign_ins.relay(CONTRACT_A, prompt.sign_in_id, CODE)
    await _until(lambda: sign_ins.waiting == 0)
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
    isolation: ContractIsolation, fake_claude: Path, opened_paths: list[str]
) -> None:
    sign_ins = _sign_ins(isolation, fake_claude)
    harness_root = str(isolation.harness_config_dir(CONTRACT_A, "claude_code"))

    before = await sign_ins.status(CONTRACT_A)

    prompt = await sign_ins.start(CONTRACT_A, method="oauth_token")
    await sign_ins.relay(CONTRACT_A, prompt.sign_in_id, CODE)
    await _until(lambda: sign_ins.waiting == 0)
    opened_paths.clear()
    long_lived = await sign_ins.status(CONTRACT_A)
    assert not any(path.startswith(harness_root) for path in opened_paths)

    prompt = await sign_ins.start(CONTRACT_A, method="claude_ai")
    await sign_ins.relay(CONTRACT_A, prompt.sign_in_id, CODE)
    await _until(lambda: sign_ins.waiting == 0)
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


# ------------------------------------------------------------------ the relay, end to end


class _Registration:
    server_date = None

    def __init__(self) -> None:
        self.sent: list[HeartbeatEnvelope] = []
        self.acks: list[HeartbeatAck] = []
        self.relay: list[SealedSignInCode] = []

    async def heartbeat(self, state: Any, envelope: HeartbeatEnvelope) -> HeartbeatAck:
        self.sent.append(envelope)
        ack = HeartbeatAck(
            runner_id=uuid4(),
            floor_state=FloorState.OK,
            contracts_floor=contracts_version,
            tag_set_version=1,
            accepts_new_directives=True,
            sign_in_codes=self.relay,
        )
        self.relay = []
        self.acks.append(ack)
        return ack


def _stream(tmp_path: Path, sign_ins: ClaudeSignIns) -> tuple[Any, _Registration]:
    registration = _Registration()
    runner_id = uuid4()
    stream = service.ControlPlaneStream(
        client=registration,  # type: ignore[arg-type]
        state=RunnerState(
            runner_id=runner_id,
            identity_id="id",
            private_key_pem="unused",
            temporal_namespace="org-x",
            task_queue=f"runner.{runner_id}",
        ),
        isolation=service.IsolationMode.NONE,
        proxy=LlmProxy(slots=SlotStore(), ceilings=CeilingStore(), outbox=UsageOutbox()),
        sealed=SealedCredentialStream(RecipientKeyStore(tmp_path / "keys")),
        credentials=CredentialResolver(store=EmptyCredentialStore()),
        load=service._LoadInterceptor(),
        sign_ins=sign_ins,
    )
    return stream, registration


def _sealed_code(stream: Any, sign_in_id: str, *, bound_to: str | None = None) -> SealedSignInCode:
    key = stream.sealed._keys.current()
    return SealedSignInCode(
        contract_id=UUID(CONTRACT_A),
        sign_in_id=sign_in_id,
        recipient_key_id=key.key_id,
        ciphertext=seal(
            public_key=key.public_key,
            binding=sign_in_code_binding(
                contract_id=CONTRACT_A,
                sign_in_id=bound_to or sign_in_id,
                recipient_key_id=key.key_id,
            ),
            plaintext=CODE,
        ),
    )


@pytest.mark.asyncio
async def test_a_code_sealed_for_another_sign_in_does_not_open(
    tmp_path: Path, isolation: ContractIsolation, fake_claude: Path
) -> None:
    sign_ins = _sign_ins(isolation, fake_claude)
    stream, registration = _stream(tmp_path, sign_ins)
    prompt = await sign_ins.start(CONTRACT_A, method="oauth_token")

    registration.relay = [_sealed_code(stream, prompt.sign_in_id, bound_to="si_" + "1" * 32)]
    await stream.exchange([])
    await stream.exchange([])

    assert registration.sent[-1].sign_in_codes == [
        SignInCodeRelay(sign_in_id=prompt.sign_in_id, outcome=SignInCodeOutcome.UNOPENABLE)
    ]
    await sign_ins.close()


@pytest.mark.asyncio
async def test_neither_the_code_nor_the_token_reaches_a_log_or_a_payload(
    tmp_path: Path,
    isolation: ContractIsolation,
    fake_claude: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Every log record, both activities' inputs and outputs, and every envelope and ack
    of one whole sign-in -- the code and the token appear in none of them."""

    caplog.set_level(logging.DEBUG)
    sign_ins = _sign_ins(isolation, fake_claude)
    stream, registration = _stream(tmp_path, sign_ins)
    activities = ContractDeviceLoginActivities(
        contract_isolation=isolation,
        device_login=ContractDeviceLogin(isolation, claude=sign_ins),
        host_party="user",
    )
    start = ContractDeviceLoginInput(contract_id=CONTRACT_A, runtime_kind="claude_code")
    started = await activities.sign_in_contract_device_login(start)

    registration.relay = [_sealed_code(stream, started.sign_in_id)]
    await stream.exchange([])
    await _until(lambda: sign_ins.waiting == 0)
    await stream.exchange([])
    check = ContractDeviceLoginStatusInput(contract_id=CONTRACT_A, runtime_kind="claude_code")
    status = await activities.check_contract_device_login_status(check)

    assert status.token_present is True
    assert status.auth_method == "oauth_token"
    assert registration.sent[-1].sign_in_codes == [
        SignInCodeRelay(sign_in_id=started.sign_in_id, outcome=SignInCodeOutcome.WRITTEN)
    ]
    observed = [
        caplog.text,
        *(json.dumps(asdict(io)) for io in (start, started, check, status)),
        *(envelope.model_dump_json() for envelope in registration.sent),
        *(ack.model_dump_json() for ack in registration.acks),
    ]
    for text in observed:
        assert CODE not in text
        assert TOKEN not in text
        assert TOKEN[len("sk-ant-oat01-") :] not in text


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
    sign_ins = _sign_ins(isolation, fake_claude)
    sign_ins._platform = "darwin"
    root = isolation.harness_config_dir(CONTRACT_A, "claude_code")
    home = str(isolation.contract_dir(CONTRACT_A))

    await sign_ins.start(CONTRACT_A, method="oauth_token")
    assert not log.exists(), "the long-lived token needs no keychain"
    await sign_ins.start(CONTRACT_A, method="claude_ai")
    await sign_ins.start(CONTRACT_A, method="claude_ai")
    await sign_ins.close()

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


@pytest.mark.asyncio
async def test_a_beat_with_no_relay_outcome_omits_the_key(
    tmp_path: Path, isolation: ContractIsolation, fake_claude: Path
) -> None:
    """A control plane on contracts 3.5 forbids extra keys, so an ordinary beat must still
    be the body it parses."""

    stream, registration = _stream(tmp_path, _sign_ins(isolation, fake_claude))

    await stream.exchange([])

    assert "sign_in_codes" not in json.loads(registration.sent[0].model_dump_json())
