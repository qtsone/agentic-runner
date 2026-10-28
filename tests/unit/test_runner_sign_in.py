"""``agentic-runner sign-in claude|codex`` on a person's own Runner (QTS-1736, rev 3).

What this pins, per acceptance criterion:

* ``sign-in claude`` and ``sign-in codex`` take no ids, for a subscription and for an API
  key; with no argument the console script refuses and lists both forms (QTS-2159);
* a key is read without echo, lands in the Runner's own store under the provider's key
  name, reaches the LLM proxy, and the next heartbeat reports it with its probe;
* the command refuses on a pool Runner, and offers a subscription only when the ack says
  the Contract allows it; otherwise it names which is missing and the console page that
  turns it on, and offers the key (QTS-1776);
* the vendor's sign-in runs for the one Contract the ack named.

The vendor CLIs themselves are ``test_claude_sign_in.py``'s; here they are fakes.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from agentic_runner import cli, service
from agentic_runner.config import RunnerConfig
from agentic_runner.credentials import FakeCredentialStore
from agentic_runner.llm_contracts import save_llm_contracts
from agentic_runner.llm_proxy import CeilingStore, CredentialSlot, LlmProxy, SlotStore, UsageOutbox
from agentic_runner.private_state import ensure_private_dir
from agentic_runner.registration import RunnerState, save_state
from agentic_runner.sealed_box import RecipientKeyStore, SealedCredentialStream
from agentic_runner.sign_in import SignIn
from agentic_runner.workers.claude_sign_in import ClaudeSignIns
from agentic_runner.workers.harness_logins import CodexSignIns
from agentic_runner_contracts import __version__ as contracts_version
from agentic_runner_contracts.runner_registration import (
    ContractLlmAccess,
    ContractLlmTerms,
    FloorState,
    HeartbeatAck,
    HeartbeatEnvelope,
    InstallTarget,
    IsolationMode,
    ProviderKeyReport,
    RunnerInstall,
    SlotProbe,
    SubscriptionRight,
)

CONTRACT = UUID("11111111-1111-4111-8111-111111111111")
BOTH = ("claude_code", "codex_cli")


class _Person:
    """The terminal: what the person answers, what they type unseen, what they read."""

    def __init__(self, answers: list[str], secrets: list[str] | None = None) -> None:
        self._answers = iter(answers)
        self._secrets = iter(secrets or [])
        self.prompts: list[str] = []
        self.secret_prompts: list[str] = []
        self.read: list[str] = []

    def ask(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return next(self._answers)

    def secret(self, prompt: str) -> str:
        self.secret_prompts.append(prompt)
        return next(self._secrets)

    def say(self, line: str) -> None:
        self.read.append(line)


class _Vendor:
    def __init__(self, returncode: int = 0) -> None:
        self.calls: list[tuple[str, str]] = []
        self.returncode = returncode

    async def __call__(self, contract_id: str, cli_kind: str) -> int:
        self.calls.append((contract_id, cli_kind))
        return self.returncode


def _sign_in(
    person: _Person,
    *,
    operated: bool | None = True,
    right: SubscriptionRight | None = SubscriptionRight.ALLOWED,
    served: tuple[str, ...] = BOTH,
    store: FakeCredentialStore | None = None,
    vendor: _Vendor | None = None,
) -> tuple[SignIn, FakeCredentialStore, _Vendor]:
    store = store if store is not None else FakeCredentialStore()
    vendor = vendor or _Vendor()
    command = SignIn(
        operated=operated,
        contracts=(
            None if right is None else [ContractLlmTerms(contract_id=CONTRACT, subscription=right)]
        ),
        served=served,
        store=store,
        subscription=vendor,
        ask=person.ask,
        secret=person.secret,
        say=person.say,
    )
    return command, store, vendor


# ------------------------------------------------------------------ an API key


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("which", "key_name"), [("claude", "ANTHROPIC_API_KEY"), ("codex", "OPENAI_API_KEY")]
)
async def test_a_key_is_read_without_echo_into_the_store_under_the_providers_name(
    which: str, key_name: str
) -> None:
    person = _Person(answers=["k"], secrets=["sk-typed-unseen\n"])
    command, store, vendor = _sign_in(person)

    assert await command.run(which) == 0

    assert store.get(key_name) == "sk-typed-unseen"
    # Read through the no-echo prompt, and never printed back.
    assert person.secret_prompts == [f"{key_name} (not shown): "]
    assert not any("sk-typed-unseen" in line for line in person.prompts + person.read)
    assert vendor.calls == []


@pytest.mark.asyncio
async def test_an_empty_key_stores_nothing() -> None:
    person = _Person(answers=["k"], secrets=["  "])
    command, store, _ = _sign_in(person)

    assert await command.run("codex") == 1
    assert store.get("OPENAI_API_KEY") is None


@pytest.mark.asyncio
async def test_a_key_needs_no_contract_terms_from_the_ack() -> None:
    """The store is the host's, not a Contract's: a key can go in before the first beat."""

    person = _Person(answers=["k"], secrets=["sk-early"])
    command, store, _ = _sign_in(person, right=None)

    assert await command.run("codex") == 0
    assert store.get("OPENAI_API_KEY") == "sk-early"


# ------------------------------------------------------------------ a subscription


@pytest.mark.asyncio
@pytest.mark.parametrize(("which", "cli_kind"), [("claude", "claude_code"), ("codex", "codex_cli")])
async def test_a_subscription_runs_the_vendors_sign_in_for_the_one_contract(
    which: str, cli_kind: str
) -> None:
    person = _Person(answers=["s"])
    command, store, vendor = _sign_in(person)

    assert await command.run(which) == 0

    assert vendor.calls == [(str(CONTRACT), cli_kind)]
    assert store._values == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("right", "named"),
    [
        (SubscriptionRight.NO_RIGHT, "/orgs/<organisation>/runners"),
        (SubscriptionRight.NOT_ACKNOWLEDGED, "/me/contracts"),
        (None, "not yet been told its Contract"),
    ],
)
async def test_without_the_right_no_subscription_is_offered_and_the_page_is_named(
    right: SubscriptionRight | None, named: str
) -> None:
    """QTS-1776: the prompt offered `[s]`, then refused it without naming the page."""

    person = _Person(answers=["s"])
    command, store, vendor = _sign_in(person, right=right)

    assert await command.run("claude") == 0

    assert person.prompts == ["Claude Code: store an API [k]ey, or skip? [k/N] "]
    assert any(named in line for line in person.read)
    assert (store._values, vendor.calls) == ({}, [])


@pytest.mark.asyncio
async def test_without_the_right_the_key_is_still_offered() -> None:
    person = _Person(answers=["k"], secrets=["sk-ant-api03-typed"])
    command, store, vendor = _sign_in(person, right=SubscriptionRight.NO_RIGHT)

    assert await command.run("claude") == 0
    assert store.get("ANTHROPIC_API_KEY") == "sk-ant-api03-typed"
    assert vendor.calls == []


@pytest.mark.asyncio
async def test_a_failed_vendor_sign_in_is_its_exit_code() -> None:
    person = _Person(answers=["s"])
    command, _, _ = _sign_in(person, vendor=_Vendor(returncode=3))

    assert await command.run("claude") == 3


# ------------------------------------------------------------------ what it asks and refuses


@pytest.mark.asyncio
async def test_a_skipped_runtime_changes_nothing() -> None:
    person = _Person(answers=["n"])
    command, store, vendor = _sign_in(person)

    assert await command.run("codex") == 0
    assert (store._values, vendor.calls) == ({}, [])


@pytest.mark.asyncio
@pytest.mark.parametrize("operated", [False, None])
async def test_a_pool_runner_refuses_before_asking_anything(operated: bool | None) -> None:
    person = _Person(answers=[])
    command, store, vendor = _sign_in(person, operated=operated)

    assert await command.run("claude") == 1

    assert (person.prompts, person.secret_prompts, store._values, vendor.calls) == ([], [], {}, [])
    assert any("pool Runner uses only its pool's keys" in line for line in person.read)


# ------------------------------------------------------------------ several connections (rev 14)

EMPLOYER = ContractLlmTerms(
    contract_id=uuid4(),
    subscription=SubscriptionRight.ALLOWED,
    organisation_id=uuid4(),
    organisation_name="Employer",
)
OWN = ContractLlmTerms(
    contract_id=uuid4(),
    subscription=SubscriptionRight.ALLOWED,
    organisation_id=uuid4(),
    organisation_name="Own\x1b[31m Dev",
)


def _logged_in(person: _Person, contracts: list[ContractLlmTerms]) -> tuple[SignIn, _Vendor]:
    vendor = _Vendor()
    command = SignIn(
        operated=True,
        contracts=contracts,
        served=BOTH,
        store=FakeCredentialStore(),
        subscription=vendor,
        ask=person.ask,
        secret=person.secret,
        say=person.say,
    )
    return command, vendor


@pytest.mark.asyncio
@pytest.mark.parametrize(("answer", "chosen"), [("1", EMPLOYER), ("2", OWN)])
async def test_with_two_connections_a_subscription_asks_which_and_signs_in_that_one(
    answer: str, chosen: ContractLlmTerms
) -> None:
    """Each connection keeps its own sign-in: the vendor's runs in the chosen one's Contract."""

    person = _Person(answers=["s", answer])
    command, vendor = _logged_in(person, [EMPLOYER, OWN])

    assert await command.run("claude") == 0

    assert vendor.calls == [(str(chosen.contract_id), "claude_code")]
    assert person.prompts[-1] == "Claude Code: sign in for which Organisation? [number] "
    # Listed by the Organisation's name, with nothing a console user typed reaching the
    # terminal as an escape sequence.
    assert "  [1] Employer" in person.read
    assert "  [2] Own[31m Dev" in person.read


@pytest.mark.asyncio
async def test_with_two_connections_only_the_allowed_one_is_offered_without_asking() -> None:
    refused = OWN.model_copy(update={"subscription": SubscriptionRight.NO_RIGHT})
    person = _Person(answers=["s"])
    command, vendor = _logged_in(person, [EMPLOYER, refused])

    assert await command.run("codex") == 0

    assert vendor.calls == [(str(EMPLOYER.contract_id), "codex_cli")]


@pytest.mark.asyncio
async def test_with_no_connection_allowing_it_each_organisation_says_why() -> None:
    person = _Person(answers=["n"])
    command, vendor = _logged_in(
        person,
        [
            EMPLOYER.model_copy(update={"subscription": SubscriptionRight.NO_RIGHT}),
            OWN.model_copy(update={"subscription": SubscriptionRight.NOT_ACKNOWLEDGED}),
        ],
    )

    assert await command.run("claude") == 0

    assert person.prompts == ["Claude Code: store an API [k]ey, or skip? [k/N] "]
    [why] = [line for line in person.read if "Employer" in line]
    assert "has not turned it on" in why and "/me/contracts" in why
    assert vendor.calls == []


@pytest.mark.asyncio
async def test_a_logged_in_runner_connected_nowhere_offers_only_the_key() -> None:
    person = _Person(answers=["n"])
    command, _ = _logged_in(person, [])

    assert await command.run("claude") == 0

    assert any("agentic-runner connect" in line for line in person.read)


@pytest.mark.asyncio
async def test_a_wrong_connection_number_changes_nothing() -> None:
    person = _Person(answers=["s", "9"])
    command, vendor = _logged_in(person, [EMPLOYER, OWN])

    assert await command.run("claude") == 1
    assert vendor.calls == []


@pytest.mark.asyncio
async def test_a_runtime_this_runner_does_not_serve_is_refused() -> None:
    person = _Person(answers=[])
    command, _, _ = _sign_in(person, served=("claude_code",))

    assert await command.run("codex") == 1
    assert any("does not serve Codex" in line for line in person.read)


# ------------------------------------------------------------------ the composition root


@pytest.fixture
def runner_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The Runner's environment, as `docker exec` gives it; returns its state directory."""

    state_dir = tmp_path / "runner-state"
    ensure_private_dir(state_dir)
    for name, value in {
        service.ISOLATION_ENV: "none",
        "AGENTIC_RUNNER_CONFIG": str(tmp_path / "absent.toml"),
        "AGENTIC_RUNNER_STATE_DIR": str(state_dir),
        "TEMPORAL_ADDRESS": "127.0.0.1:7233",
        "INTERNAL_FASTAPI_BASE_URL": "http://agentic-api.internal:8000",
        "WORKSPACE_ROOT": str(tmp_path / "workspaces"),
        "WORKER_STATE_DIR": str(tmp_path / "worker-state"),
        "CODEX_HOME": str(tmp_path / "codex-home"),
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(service, "can_separate_uids", lambda: False)
    monkeypatch.setattr(service.shutil, "which", lambda name: f"/usr/bin/{name}")
    return state_dir


def _registered(state_dir: Path, *, operated: bool) -> None:
    save_state(
        state_dir,
        RunnerState(
            runner_id=uuid4(),
            identity_id="id",
            private_key_pem="unused",
            temporal_namespace="org-x",
            task_queue="runner.x",
            operator_user_profile_id=uuid4() if operated else None,
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("which", "cli_kind"), [("claude", "claude_code"), ("codex", "codex_cli")])
async def test_the_command_signs_in_the_contract_the_last_ack_named(
    runner_env: Path, monkeypatch: pytest.MonkeyPatch, which: str, cli_kind: str
) -> None:
    _registered(runner_env, operated=True)
    save_llm_contracts(
        runner_env,
        [ContractLlmTerms(contract_id=CONTRACT, subscription=SubscriptionRight.ALLOWED)],
    )
    signed: list[tuple[str, str]] = []

    async def claude(self: ClaudeSignIns, contract_id: str, *, method: str) -> int:
        signed.append((contract_id, "claude_code"))
        return 0

    async def codex(self: CodexSignIns, contract_id: str) -> int:
        signed.append((contract_id, "codex_cli"))
        return 0

    monkeypatch.setattr(ClaudeSignIns, "sign_in", claude)
    monkeypatch.setattr(CodexSignIns, "sign_in", codex)
    monkeypatch.setattr("builtins.input", lambda _prompt: "s")

    assert await service.sign_in(which, store=FakeCredentialStore()) == 0
    assert signed == [(str(CONTRACT), cli_kind)]


@pytest.mark.asyncio
async def test_the_command_refuses_on_a_registered_pool_runner(
    runner_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _registered(runner_env, operated=False)
    monkeypatch.setattr("builtins.input", _no_prompt)

    assert await service.sign_in("claude", store=FakeCredentialStore()) == 1
    assert "the Organisation's own" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_an_environment_without_a_callback_url_is_refused_naming_the_field(
    runner_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _registered(runner_env, operated=True)
    save_llm_contracts(
        runner_env,
        [ContractLlmTerms(contract_id=CONTRACT, subscription=SubscriptionRight.ALLOWED)],
    )
    monkeypatch.delenv("INTERNAL_FASTAPI_BASE_URL")
    monkeypatch.delenv(service.CONTROL_PLANE_ENV, raising=False)
    monkeypatch.setattr(ClaudeSignIns, "sign_in", _unexpected)
    monkeypatch.setattr("builtins.input", lambda _prompt: "s")

    assert await service.sign_in("claude", store=FakeCredentialStore()) == 2
    assert "refusing to sign in: INTERNAL_FASTAPI_BASE_URL (Field required)" in (
        capsys.readouterr().err
    )


@pytest.mark.asyncio
async def test_a_key_typed_into_sign_in_reaches_the_proxy_and_the_next_heartbeat(
    runner_env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The whole path on a container Runner: `sign-in` writes the state directory's own
    store, the running Runner reads it by the provider's key name, probes it, serves it
    from its proxy, and reports it -- held, with its probe, never its value."""

    _registered(runner_env, operated=True)
    config = RunnerConfig(state_dir=runner_env)
    monkeypatch.setattr("builtins.input", lambda _prompt: "k")
    monkeypatch.setattr(service.getpass, "getpass", lambda _prompt: "sk-ant-api03-typed")

    assert await service.sign_in("claude", store=service.own_store(config)) == 0
    assert oct((runner_env / "credentials" / "ANTHROPIC_API_KEY").stat().st_mode & 0o777) == (
        "0o600"
    )

    sent: list[HeartbeatEnvelope] = []

    class _Registration:
        server_date = None

        async def heartbeat(self, state: Any, envelope: HeartbeatEnvelope) -> HeartbeatAck:
            sent.append(envelope)
            return HeartbeatAck(
                runner_id=uuid4(),
                floor_state=FloorState.OK,
                contracts_floor=contracts_version,
                tag_set_version=1,
                accepts_new_directives=True,
                llm_contracts=[
                    ContractLlmTerms(contract_id=CONTRACT, subscription=SubscriptionRight.NO_RIGHT)
                ],
            )

    async def accepts(_slot: CredentialSlot) -> bool:
        return True

    runner_id = uuid4()
    stream = service.ControlPlaneStream(
        client=_Registration(),  # type: ignore[arg-type]
        state=RunnerState(
            runner_id=runner_id,
            identity_id="id",
            private_key_pem="unused",
            temporal_namespace="org-x",
            task_queue=f"runner.{runner_id}",
            operator_user_profile_id=uuid4(),
        ),
        isolation=IsolationMode.NONE,
        proxy=LlmProxy(
            slots=SlotStore(probe=accepts), ceilings=CeilingStore(), outbox=UsageOutbox()
        ),
        sealed=SealedCredentialStream(RecipientKeyStore(tmp_path / "keys")),
        credentials=service.build_credential_resolver(config),
        load=service._LoadInterceptor(),
    )

    await stream.exchange([])
    await stream.exchange([])

    slot = stream.proxy.slots.resolve(CONTRACT, "anthropic", host_key_only=True)
    assert slot is not None and slot.value == "sk-ant-api03-typed"
    [report] = sent[-1].llm_access or []
    assert report == ContractLlmAccess(
        contract_id=CONTRACT,
        keys=[
            ProviderKeyReport(provider="anthropic", held=True, probe=SlotProbe.VALID),
            ProviderKeyReport(provider="openai", held=False, probe=SlotProbe.UNPROBED),
        ],
    )
    assert "sk-ant-api03-typed" not in sent[-1].model_dump_json()


def _no_prompt(prompt: str) -> str:
    raise AssertionError(f"asked: {prompt}")


# ------------------------------------------------------------------ the console script


def test_the_command_takes_no_contract_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "sign_in", _unexpected)

    with pytest.raises(SystemExit) as refused:
        cli.main(["sign-in", "--contract", str(CONTRACT)])
    assert refused.value.code == 2


@pytest.mark.parametrize("argv", [["sign-in"], ["sign-in", "--name", "macbook"]])
def test_the_command_refuses_a_missing_runtime_and_lists_both_forms(
    argv: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """QTS-2159: a bare `sign-in` asked about Claude Code first and read as a Claude sign-in."""

    monkeypatch.setattr(service, "sign_in", _unexpected)

    with pytest.raises(SystemExit) as refused:
        cli.main(argv)

    assert refused.value.code == 2
    err = capsys.readouterr().err
    assert "agentic-runner sign-in claude [--method" in err
    assert "agentic-runner sign-in codex [--name NAME]" in err
    assert "required: runtime" in err


def test_the_command_needs_a_terminal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(service, "sign_in", _unexpected)
    monkeypatch.setattr(os, "isatty", lambda _fd: False)

    assert cli.main(["sign-in", "claude"]) == 2
    assert "kubectl exec -it" in capsys.readouterr().err


def test_in_a_container_the_command_writes_the_state_directorys_own_store(
    runner_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str | None, Any, str]] = []

    async def sign_in(which: str | None, *, store: Any, method: str) -> int:
        calls.append((which, store, method))
        return 0

    monkeypatch.setattr(service, "sign_in", sign_in)
    monkeypatch.setattr(os, "isatty", lambda _fd: True)
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")

    assert cli.main(["sign-in", "codex"]) == 0
    assert cli.main(["sign-in", "claude", "--method", "claude_ai"]) == 0

    (first, store, method), (second, _, other_method) = calls
    assert (first, method, second, other_method) == ("codex", "oauth_token", "claude", "claude_ai")
    assert store.root == runner_env / "credentials"


async def _unexpected(*_args: Any, **_kwargs: Any) -> int:
    raise AssertionError("the command must not reach the sign-in")


# ------------------------------------------------------------------ where a command reaches it


@pytest.mark.parametrize(
    ("kubernetes", "docker", "hostname", "expected"),
    [
        (
            "10.0.0.1",
            False,
            "runner-0",
            RunnerInstall(target=InstallTarget.HELM, hostname="runner-0"),
        ),
        (
            None,
            True,
            "3f2a1b4c5d6e",
            RunnerInstall(target=InstallTarget.DOCKER, hostname="3f2a1b4c5d6e"),
        ),
        # `--network host` gives the machine's own name, `--hostname` the operator's: not sent.
        (None, True, "alices-laptop", RunnerInstall(target=InstallTarget.DOCKER)),
        # A name the schema refuses is sent as none rather than stop the Runner starting.
        ("10.0.0.1", False, "bad_name", RunnerInstall(target=InstallTarget.HELM)),
        # A bare `run` outside a container: a person's machine, whose name is never sent.
        (None, False, "alices-laptop", RunnerInstall(target=InstallTarget.WORKSTATION)),
    ],
)
def test_the_hostname_is_reported_only_in_a_container(
    monkeypatch: pytest.MonkeyPatch,
    kubernetes: str | None,
    docker: bool,
    hostname: str,
    expected: RunnerInstall,
) -> None:
    if kubernetes is None:
        monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    else:
        monkeypatch.setenv("KUBERNETES_SERVICE_HOST", kubernetes)
    monkeypatch.setattr(cli, "_in_docker", lambda: docker)
    monkeypatch.setattr(cli.socket, "gethostname", lambda: hostname)

    assert cli._run_install() == expected


def test_a_workstation_never_reports_its_hostname() -> None:
    assert RunnerInstall(target=InstallTarget.WORKSTATION).hostname is None
    with pytest.raises(ValueError, match="never reports its hostname"):
        RunnerInstall(target=InstallTarget.WORKSTATION, hostname="alices-macbook")


def test_a_runners_name_is_sent_only_when_it_has_one() -> None:
    """QTS-1773: a control plane below 3.27 forbids the key, so an unnamed Runner -- a
    container, or one whose control plane has not said it parses names -- omits it."""

    assert "name" not in RunnerInstall(target=InstallTarget.DOCKER).model_dump(mode="json")
    named = RunnerInstall(target=InstallTarget.WORKSTATION, name="cucubau")
    assert named.model_dump(mode="json")["name"] == "cucubau"
    with pytest.raises(ValueError, match="pattern"):
        RunnerInstall(target=InstallTarget.WORKSTATION, name="Alice's laptop")
