"""The setup skill for the person's own coding CLI (console-v2 issue 21).

What this pins, per acceptance criterion:

* ``skill install`` writes the skill where Claude Code and Codex each read a user skill,
  under a temporary HOME, and ``skill print`` writes the same text;
* the skill names no control plane address of its own and tells the CLI to call none: the
  only one it uses is the ``controlPlane.url`` the person pastes, passed to the Runner;
* the skill's own ``agentic-runner`` commands, run in the order it gives them against the
  fake control plane, leave a Runner registered and heartbeating.
"""

from __future__ import annotations

import asyncio
import io
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import pytest

from agentic_runner import cli, service, skill, workstation
from agentic_runner.host_store import FileCredentialStore
from agentic_runner.testing import FakeControlPlane
from agentic_runner_contracts.runner_registration import InstallChannel, SessionKind

CONTROL_PLANE = "http://control-plane.test"
AGENT_TOKEN = "agent-token-of-sixteen-plus"
SECRET_VALUE = "sk-never-printed"

# Where the skill may send the person: this repository's docs and the two CLIs' own.
ALLOWED_HOSTS = {"github.com", "developers.openai.com", "code.claude.com"}


def test_install_writes_the_skill_where_each_cli_reads_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)

    assert cli.main(["skill", "install"]) == 0

    claude = tmp_path / ".claude/skills/agentic-runner-setup/SKILL.md"
    codex = tmp_path / ".agents/skills/agentic-runner-setup/SKILL.md"
    assert claude.read_text() == codex.read_text() == skill.text()
    assert capsys.readouterr().out.split("\n")[:2] == [f"installed {claude}", f"installed {codex}"]


@pytest.mark.parametrize(
    ("flag", "relative"),
    [
        ("--claude", "config/skills/agentic-runner-setup/SKILL.md"),
        ("--codex", ".agents/skills/agentic-runner-setup/SKILL.md"),
    ],
)
def test_one_flag_installs_for_that_cli_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flag: str, relative: str
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "config"))

    assert cli.main(["skill", "install", flag]) == 0

    [written] = [path for path in tmp_path.rglob("SKILL.md")]
    assert written == tmp_path / relative


def test_print_writes_the_skill_to_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["skill", "print"]) == 0
    printed = capsys.readouterr().out
    assert printed == skill.text()
    assert printed.startswith("---\nname: agentic-runner-setup\n")


def test_the_skill_names_no_control_plane_address_and_calls_none() -> None:
    text = skill.text()

    hosts = {urlparse(url).hostname for url in re.findall(r"https?://[^\s)|`]+", text)}
    assert hosts <= ALLOWED_HOSTS
    assert "/api/" not in text
    assert not re.search(r"\b(curl|wget|httpie|Invoke-WebRequest)\b", text)
    assert "controlPlane.url" in text


def test_every_doc_link_names_a_section_the_docs_have() -> None:
    docs = Path(__file__).resolve().parents[2] / "docs"
    links = re.findall(r"blob/main/docs/(\w+\.md)(?:#([\w-]+))?", skill.text())
    assert links
    for document, anchor in links:
        headings = re.findall(r"^#+ (.+)$", (docs / document).read_text(), flags=re.M)
        anchors = {re.sub(r"[^\w\- ]", "", h).strip().lower().replace(" ", "-") for h in headings}
        assert not anchor or anchor in anchors, f"{document}#{anchor}"


def _skill_commands() -> list[list[str]]:
    """The ``agentic-runner`` lines of the skill's shell blocks, in the order it runs them."""

    blocks = re.findall(r"```sh\n(.*?)```", skill.text(), flags=re.S)
    commands = []
    for line in (line for block in blocks for line in block.splitlines()):
        words = shlex.split(line.split("|")[-1])
        while words and "=" in words[0]:
            words.pop(0)  # the environment of the one command, `AGENTIC_AGENT_TOKEN=…`
        if words[:1] == ["agentic-runner"]:
            commands.append(words[1:])
    return commands


def _fill(words: list[str], root: Path) -> list[str]:
    values = {
        "<org>": "acme",
        "<control-plane-url>": CONTROL_PLANE,
        "<temporal-address>": "temporal-grpc.test:443",
        "<reference>": "OPENAI_API_KEY",
    }
    for placeholder, value in values.items():
        words = [word.replace(placeholder, value) for word in words]
    return [*words, "--root", str(root)]


@pytest.mark.asyncio
async def test_the_skills_commands_in_order_leave_a_runner_registered_and_heartbeating(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    commands = _skill_commands()
    assert [words[:1] for words in commands] == [["install"], ["credential"], ["status"]]

    root = tmp_path / "state"
    paths = workstation.OrgPaths(root=root, org="acme")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "claude").write_text("#!/bin/sh\necho '2.1.279 (Claude Code)'\n")
    (bin_dir / "claude").chmod(0o755)
    plane = FakeControlPlane(host_party="user")
    real_install = workstation.install

    async def install_against_the_fake(*args: Any, **kwargs: Any) -> list[str]:
        async with httpx.AsyncClient(transport=plane.transport()) as http:
            return await real_install(
                *args,
                **kwargs,
                platform="linux",
                home=tmp_path / "home",
                run=lambda argv, **_: subprocess.CompletedProcess(argv, 0, "", ""),
                http_client=http,
            )

    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setattr(cli.workstation, "install", install_against_the_fake)
    monkeypatch.setattr(
        cli, "open_workstation_store", lambda _org, *, fallback: FileCredentialStore(fallback)
    )

    install, credential, status = (_fill(words, root) for words in commands)
    monkeypatch.setenv(cli.AGENT_TOKEN_ENV, AGENT_TOKEN)
    assert await asyncio.to_thread(cli.main, install) == 0
    monkeypatch.delenv(cli.AGENT_TOKEN_ENV)
    monkeypatch.setattr("sys.stdin", io.StringIO(SECRET_VALUE))
    assert cli.main(credential) == 0
    assert FileCredentialStore(paths.credentials).get("OPENAI_API_KEY") == SECRET_VALUE

    await _start_the_login_agent(paths, plane, monkeypatch=monkeypatch)
    assert cli.main(status) == 0

    out = capsys.readouterr().out
    assert len(plane.bootstraps) == 1 and plane.heartbeats
    assert re.search(r"^identity +\S+ in org-\S+ on runner\.", out, flags=re.M)
    assert re.search(r"^heartbeat +\d+s ago$", out, flags=re.M)
    assert AGENT_TOKEN not in out and SECRET_VALUE not in out


async def _start_the_login_agent(
    paths: workstation.OrgPaths, plane: FakeControlPlane, *, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What the login agent ``install`` wrote does: ``run --org``, until it is heard once."""

    settings = workstation.WorkstationSettings.load(paths)
    for name, value in workstation.run_environment(paths, settings).items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("AGENTIC_RUNNER_SOCKET_DIR", str(paths.state_dir / "s"))
    monkeypatch.delenv("INTERNAL_FASTAPI_BASE_URL", raising=False)
    stop = asyncio.Event()

    class Worker:
        def __init__(self, *_: Any, **__: Any) -> None:
            self._done = asyncio.Event()

        async def run(self) -> None:
            await self._done.wait()

        async def shutdown(self) -> None:
            self._done.set()

    async def connect(*_: Any, **__: Any) -> Any:
        return type("Client", (), {"api_key": None})()

    async def stop_once_heard() -> None:
        await plane.wait_for(lambda: bool(plane.heartbeats))
        stop.set()

    stopper = asyncio.create_task(stop_once_heard())
    async with httpx.AsyncClient(transport=plane.transport()) as http:
        code = await service.run(
            http_client=http,
            connect=connect,
            worker_factory=Worker,
            can_change_uid=False,
            stop=stop,
            heartbeat_interval=0.05,
            host_store=FileCredentialStore(paths.credentials),
            attestation=workstation.collect_attestation(
                channel=InstallChannel.SYSTEMD_USER,
                session=SessionKind.LOGIN_AGENT,
                store=FileCredentialStore(paths.credentials).kind,
                path=settings.path,
            ),
        )
    await stopper
    assert code == 0
