"""``agentic-runner login`` and ``connect`` against the fake control plane (QTS-2252, rev 14).

What this pins:

* ``login`` is a device flow: a code shown, the browser opened on it, polls refused as
  pending until the person confirms, and then a Runner the person operates, connected
  nowhere -- with no token copied anywhere;
* an expired code, a state directory that already holds a Runner, and a sixth Runner of
  one person are refused by name;
* the page the control plane names is opened only over https on its own domain (or the
  console's, its parent), and nothing it sends reaches the terminal as an escape sequence;
* ``connect`` lists the Organisations the operator belongs to, connects to the one picked
  by number or by name, names a refusal (the Organisation's setting), and refuses on an
  Organisation's own Runner.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from agentic_runner import service
from agentic_runner.registration import RunnerRegistrationError, RunnerState, load_state
from agentic_runner.testing import FakeControlPlane
from agentic_runner.testing.control_plane import DEFAULT_OPERATOR, DEFAULT_ORGANISATIONS
from agentic_runner_contracts.runner_login import LOGIN_DEVICE_PATH

CONTROL_PLANE = "http://control-plane.test"
ACME, GLOBEX = DEFAULT_ORGANISATIONS


class _Terminal:
    def __init__(self, answers: list[str] | None = None) -> None:
        self.read: list[str] = []
        self.opened: list[str] = []
        self._answers = iter(answers or [])

    def say(self, line: str) -> None:
        self.read.append(line)

    def open_browser(self, url: str) -> None:
        self.opened.append(url)

    def ask(self, prompt: str) -> str:
        self.read.append(prompt)
        return next(self._answers)


async def _login(
    plane: FakeControlPlane,
    state_dir: Path,
    *,
    name: str | None = "laptop",
    terminal: _Terminal | None = None,
    on_poll: Callable[[int], None] | None = None,
) -> tuple[RunnerState, str]:
    polls = 0

    async def sleep(_: float) -> None:
        nonlocal polls
        polls += 1
        if on_poll is not None:
            on_poll(polls)

    terminal = terminal or _Terminal()
    async with httpx.AsyncClient(transport=plane.transport()) as http:
        return await service.login(
            state_dir=state_dir,
            control_plane=CONTROL_PLANE,
            name=name,
            say=terminal.say,
            open_browser=terminal.open_browser,
            client=http,
            sleep=sleep,
        )


async def _connect(
    plane: FakeControlPlane, state_dir: Path, organisation: str | None, terminal: _Terminal
) -> int:
    async with httpx.AsyncClient(transport=plane.transport()) as http:
        return await service.connect(
            state_dir=state_dir,
            control_plane=CONTROL_PLANE,
            organisation=organisation,
            ask=terminal.ask,
            say=terminal.say,
            client=http,
        )


@pytest.fixture(autouse=True)
def _environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTIC_RUNNER_ISOLATION", "none")
    monkeypatch.delenv("AGENTIC_RUNNER_TAGS", raising=False)
    monkeypatch.setenv("AGENTIC_RUNNER_MAX_CONCURRENT_DIRECTIVES", "3")


# ------------------------------------------------------------------ login


@pytest.mark.asyncio
async def test_login_waits_for_the_person_to_confirm_the_code_and_makes_the_runner_theirs(
    tmp_path: Path,
) -> None:
    plane = FakeControlPlane()
    terminal = _Terminal()

    def confirm_on_the_third_poll(polls: int) -> None:
        if polls == 3:
            plane.confirm_login("CODE-0001")

    state, outcome = await _login(
        plane, tmp_path, terminal=terminal, on_poll=confirm_on_the_third_poll
    )

    # The code shown, and the browser opened on the page that confirms it.
    assert "code           CODE-0001" in terminal.read
    assert terminal.opened == ["https://control-plane.test/device?code=CODE-0001"]
    # Two polls refused as pending before the confirmation, then the identity.
    assert [r.reason for r in plane.refusals] == ["authorization_pending"] * 2
    assert state == load_state(tmp_path)
    assert state.operator_user_profile_id == DEFAULT_OPERATOR
    # Connected nowhere: no namespace until `connect`.
    assert (state.temporal_namespace, state.task_queue) == ("", f"runner.{state.runner_id}")
    assert state.max_concurrent_directives == 3
    [request] = plane.logins
    assert (request.name, request.max_concurrent_directives) == ("laptop", 3)
    assert "agentic-runner connect" in outcome


@pytest.mark.asyncio
async def test_an_expired_code_is_refused_and_nothing_is_saved(tmp_path: Path) -> None:
    plane = FakeControlPlane()

    with pytest.raises(RunnerRegistrationError) as refused:
        await _login(plane, tmp_path, on_poll=lambda _: plane.expire_logins())

    assert refused.value.reason == "expired_token"
    assert load_state(tmp_path) is None


@pytest.mark.asyncio
async def test_a_state_directory_that_holds_a_runner_is_refused_before_any_code(
    tmp_path: Path,
) -> None:
    plane = FakeControlPlane(confirm_logins=True)
    await _login(plane, tmp_path)

    with pytest.raises(RunnerRegistrationError) as refused:
        await _login(plane, tmp_path)

    assert refused.value.reason == "already_registered"
    assert len(plane.logins) == 1


@pytest.mark.asyncio
async def test_a_persons_sixth_runner_is_refused(tmp_path: Path) -> None:
    plane = FakeControlPlane(confirm_logins=True)
    for number in range(5):
        await _login(plane, tmp_path / str(number), name=f"machine-{number}")

    with pytest.raises(RunnerRegistrationError) as refused:
        await _login(plane, tmp_path / "sixth", name="machine-6")

    assert refused.value.reason == "runner_limit"
    assert load_state(tmp_path / "sixth") is None


@pytest.mark.parametrize(
    "page",
    [
        "file:///etc/passwd",
        "x-custom-scheme://confirm",
        "javascript:alert(1)",
        "http://control-plane.test/device",
        "https://control-plane.test.evil.example/device",
        "https://evil-control-plane.test/device",
        "https://test/device",
        "https://control-plane.test/device?code=\x1b]0;owned\x07",
    ],
)
@pytest.mark.asyncio
async def test_a_page_off_the_control_planes_own_https_domain_is_neither_shown_nor_opened(
    tmp_path: Path, page: str
) -> None:
    plane = FakeControlPlane(confirm_logins=True)
    answered = plane.transport()

    def hostile(request: httpx.Request) -> httpx.Response:
        response = answered.handle_request(request)
        if request.url.path != LOGIN_DEVICE_PATH:
            return response
        body = json.loads(response.read())
        body["verification_uri"] = body["verification_uri_complete"] = page
        return httpx.Response(200, json=body)

    terminal = _Terminal()
    with pytest.raises(RunnerRegistrationError) as refused:
        async with httpx.AsyncClient(transport=httpx.MockTransport(hostile)) as http:
            await service.login(
                state_dir=tmp_path,
                control_plane=CONTROL_PLANE,
                name="laptop",
                say=terminal.say,
                open_browser=terminal.open_browser,
                client=http,
            )

    assert refused.value.reason == "verification_uri_refused"
    assert terminal.opened == []
    assert terminal.read == []
    assert "\x1b" not in str(refused.value)
    assert load_state(tmp_path) is None


@pytest.mark.parametrize(
    ("control_plane", "page"),
    [
        ("https://api.agentic.qts.one", "https://agentic.qts.one/runners/device?code=AB"),
        ("https://api.agentic.qts.one", "https://api.agentic.qts.one/device"),
        ("http://127.0.0.1:8080", "http://127.0.0.1:8080/device"),
        ("http://localhost:8080", "http://localhost/device"),
    ],
)
def test_the_console_at_the_parent_of_the_runner_edge_or_a_loopback_page_is_opened(
    control_plane: str, page: str
) -> None:
    assert service._verification_page(page, control_plane) == page


def test_a_user_code_with_an_escape_sequence_reaches_the_terminal_without_it() -> None:
    assert service._printable("AB\x1b[2JCD") == "AB[2JCD"


# ------------------------------------------------------------------ connect


@pytest.mark.asyncio
async def test_connect_lists_the_persons_organisations_and_connects_the_one_picked(
    tmp_path: Path,
) -> None:
    plane = FakeControlPlane(confirm_logins=True)
    state, _ = await _login(plane, tmp_path)
    terminal = _Terminal(answers=["2"])

    assert await _connect(plane, tmp_path, None, terminal) == 0

    assert "  [1] Acme" in terminal.read
    assert "  [2] Globex" in terminal.read
    assert plane.stats()["connections"][str(state.runner_id)] == [str(GLOBEX)]
    assert any("connected to Globex" in line for line in terminal.read)


@pytest.mark.asyncio
async def test_connect_takes_the_organisation_by_name_or_id_and_says_when_it_is_done(
    tmp_path: Path,
) -> None:
    plane = FakeControlPlane(confirm_logins=True)
    state, _ = await _login(plane, tmp_path)

    assert await _connect(plane, tmp_path, "Acme", _Terminal()) == 0
    assert await _connect(plane, tmp_path, str(GLOBEX), _Terminal()) == 0
    again = _Terminal()
    assert await _connect(plane, tmp_path, "Acme", again) == 0

    assert plane.stats()["connections"][str(state.runner_id)] == [str(ACME), str(GLOBEX)]
    assert again.read == ["already connected to Acme"]


@pytest.mark.asyncio
async def test_connect_names_the_organisations_refusal_and_connects_nothing(
    tmp_path: Path,
) -> None:
    setting = "Members can connect their own Runners is off in this Organisation"
    plane = FakeControlPlane(confirm_logins=True, connect_refusals={GLOBEX: setting})
    state, _ = await _login(plane, tmp_path)
    terminal = _Terminal(answers=["2"])

    assert await _connect(plane, tmp_path, None, terminal) == 1

    assert f"  [2] Globex ({setting})" in terminal.read
    assert terminal.read[-1] == f"refusing to connect to Globex: {setting}"
    assert plane.stats()["connections"][str(state.runner_id)] == []


@pytest.mark.asyncio
async def test_an_organisations_own_runner_does_not_connect_elsewhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTIC_RUNNER_REGISTRATION_TOKEN", "registration-token-of-sixteen-plus")
    plane = FakeControlPlane()
    async with httpx.AsyncClient(transport=plane.transport()) as http:
        await service.register(
            state_dir=tmp_path, can_separate_uids=False, client=http, control_plane=CONTROL_PLANE
        )
    terminal = _Terminal()

    assert await _connect(plane, tmp_path, "Globex", terminal) == 1

    assert "an Organisation's own Runner has its one connection" in terminal.read[0]
