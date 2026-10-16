"""The ``agentic-runner`` console entry point (ADR-0013 §4, PRD issue 23's name).

``--version`` prints the Runner's own version and the **contracts** version, because the
contracts version is the compatibility floor the control plane checks before it mints a
Directive token (ADR-0013 §7). An operator debugging "why did this Runner stop getting
Directives" needs both numbers from one command.

``register`` is first boot (PRD issue 41): it presents the Runner Registration Token from
the environment, persists the durable identity the exchange hands back, and prints the
namespace and queue this process was assigned. It is idempotent by state file -- a
restart re-reads what is already there rather than registering a second Runner against
the Organisation's cap.

``install | start | stop | status <org>`` and ``credential set`` are the workstation
Runner (PRD issue 47, ``workstation.py``): one login agent per Organisation, managed by
the user who hosts it, with no ``sudo`` anywhere.

``sign-in [claude|codex]`` gives a person's own Runner their LLM access: their subscription,
through the vendor's own sign-in in this terminal, or an API key stored in the Runner's
own store (``sign_in.py``, Spec: Runners rev 3). No ids, and no code or token passes
through the control plane.

``skill install | print`` hands the setup skill to the person's own Claude Code or Codex
(``skill.py``, console-v2 issue 21).

The remaining subcommands are what a Directive calls back with, over its own attempt
socket (``callback.py``): ``annotate``, ``artifact upload``, ``verb`` and ``message send |
list`` (the Channel seam, PRD issue 52) and ``ask`` (a Question, issue 60). They read the
socket path and the bearer from the environment the Runner gave the Agent Runtime
subprocess, so inside a Directive they take no credentials and no endpoint. There is no
``pipeline upload`` and no ``meta-data``: map ticket 26 §4 declines both.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import re
import socket
import sys
from collections.abc import Sequence
from pathlib import Path

from agentic_runner import __version__ as runner_version
from agentic_runner import service, skill, workstation
from agentic_runner.callback import (
    CALLBACK_SOCKET_ENV,
    CALLBACK_TOKEN_ENV,
    CallbackError,
    call,
)
from agentic_runner.config import load as load_config
from agentic_runner.host_store import open_workstation_store
from agentic_runner.recipient_key_secret import ensure_recipient_key, in_cluster
from agentic_runner.registration import RunnerRegistrationError, can_separate_uids, load_state
from agentic_runner.sealed_box import RECIPIENT_KEY_FILENAME
from agentic_runner.service import (
    AGENT_TOKEN_ENV,
    CONTROL_PLANE_ENV,
    ISOLATION_ENV,
    RECIPIENT_KEY_ID_ENV,
    RECIPIENT_PUBLIC_KEY_ENV,
    REGISTRATION_TOKEN_ENV,
    TAGS_ENV,
    parse_tags,
    registration_token_from_env,
)
from agentic_runner.sign_in import AGENT_RUNTIMES, KeyStore
from agentic_runner.workers.claude_sign_in import OAUTH_TOKEN, SIGN_IN_METHODS
from agentic_runner_contracts import __version__ as contracts_version
from agentic_runner_contracts.runner_registration import (
    HostAttestation,
    InstallChannel,
    InstallTarget,
    RunnerInstall,
    SessionKind,
    StoreKind,
)
from agentic_runner_contracts.swarm import ROLES

__all__ = [
    "AGENT_TOKEN_ENV",
    "CONTROL_PLANE_ENV",
    "ISOLATION_ENV",
    "RECIPIENT_KEY_ID_ENV",
    "RECIPIENT_PUBLIC_KEY_ENV",
    "REGISTRATION_TOKEN_ENV",
    "STATE_DIR_ENV",
    "TAGS_ENV",
    "main",
    "parse_tags",
    "status_lines",
    "version_line",
]

# `AGENTIC_RUNNER_STATE_DIR`: the same env var `config.RunnerConfig.state_dir` reads, so
# the state directory has exactly one spelling (flag > env > file, map ticket 26 §2).
STATE_DIR_ENV = "AGENTIC_RUNNER_STATE_DIR"


def _state_dir() -> Path:
    return load_config().state_dir


def status_lines() -> list[str]:
    """What this installation is, including the fingerprint a funder compares (22 A4).

    The console prints the same string beside every slot; a funder about to seal a value
    reads it here, over the shoulder or over the phone, and checks the two match before
    typing anything. That comparison is the only thing standing between a funder and
    sealing to a key somebody else substituted.
    """

    state_dir = _state_dir()
    state = load_state(state_dir)
    # A read must not mint: `current()` writes a fresh key file on first call, which would
    # leave a status probe on an unregistered box holding a key nobody registered.
    configured = all(
        os.environ.get(name, "").strip()
        for name in (RECIPIENT_KEY_ID_ENV, RECIPIENT_PUBLIC_KEY_ENV)
    )
    fingerprint = (
        service.recipient_key(state_dir)[1]
        if configured or (state_dir / RECIPIENT_KEY_FILENAME).exists()
        else "not generated yet (registration creates it)"
    )
    return [
        version_line(),
        f"recipient key  {fingerprint}",
        (
            f"runner         {state.runner_id} on {state.task_queue}"
            if state is not None
            else "runner         not registered"
        ),
    ]


def version_line() -> str:
    return f"agentic-runner {runner_version} (contracts {contracts_version})"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentic-runner", description=__doc__)
    parser.add_argument(
        "--version", action="store_true", help="print runner and contracts versions"
    )
    parser.add_argument("--config", default=None, help="path to the Runner config file")
    parser.add_argument("--hooks-path", default=None, help="directory Runner Hooks are read from")
    subcommands = parser.add_subparsers(dest="command")

    subcommands.add_parser(
        "register",
        help="exchange the Runner Registration Token for this process's durable identity",
    )

    run = subcommands.add_parser("run", help="register if needed, then heartbeat and poll for work")
    run.add_argument("--org", default=None, help="run an installed workstation Organisation")
    run.add_argument("--root", default=None, type=Path, help=argparse.SUPPRESS)
    run.add_argument("--login-agent", action="store_true", help=argparse.SUPPRESS)

    install = subcommands.add_parser(
        "install", help="register a workstation Runner for one Organisation (issue 47)"
    )
    install.add_argument("org")
    install.add_argument(
        "--control-plane", default=os.environ.get(CONTROL_PLANE_ENV), help="control plane URL"
    )
    install.add_argument(
        "--temporal-address",
        default="",
        help="host:port; a fallback only, the Runner uses the address its registration gives",
    )
    install.add_argument(
        "--temporal-plaintext",
        action="store_true",
        help="no TLS to Temporal -- a local development server only",
    )
    install.add_argument(
        "--acp-cli-kinds",
        default=None,
        help="comma-separated cli_kinds served through the pinned ACP bridge (codex_cli, "
        "claude_code); omitted keeps the previous install's, '' turns ACP off",
    )
    install.add_argument("--no-start", action="store_true", help="write the agent, do not start")
    install.add_argument("--root", default=None, type=Path, help="state root (default per OS)")
    for service_verb in ("start", "stop"):
        driven = subcommands.add_parser(
            service_verb, help=f"{service_verb} one Organisation's login agent"
        )
        driven.add_argument("org")
        driven.add_argument("--root", default=None, type=Path)

    credential = subcommands.add_parser(
        "credential", help="install a Credential Reference value into this host's store"
    )
    credential.add_argument("action", choices=("set",))
    credential.add_argument("org")
    credential.add_argument("reference")
    credential.add_argument("--root", default=None, type=Path)

    recipient = subcommands.add_parser(
        "recipient-key", help="the installation's Recipient Key (PRD issue 48, 22 A4)"
    )
    recipient.add_argument("action", choices=("ensure-secret",))
    recipient.add_argument(
        "--secret", required=True, help="Kubernetes Secret holding the release's key"
    )

    status = subcommands.add_parser(
        "status", help="print this installation's versions and Recipient Key fingerprint"
    )
    status.add_argument("org", nargs="?", help="a workstation Organisation (issue 47)")
    status.add_argument("--root", default=None, type=Path)

    sign_in = subcommands.add_parser(
        "sign-in",
        help="give your own Runner your subscription or an API key, for Claude Code or Codex",
    )
    sign_in.add_argument(
        "runtime",
        nargs="?",
        choices=tuple(AGENT_RUNTIMES),
        help="one Agent Runtime; omitted asks about each one this Runner serves",
    )
    sign_in.add_argument(
        "--method",
        default=OAUTH_TOKEN,
        choices=SIGN_IN_METHODS,
        help="Claude subscriptions only. oauth_token: a long-lived token (claude setup-token); "
        "claude_ai: a Claude.ai login (claude auth login --claudeai)",
    )
    sign_in.add_argument(
        "--org",
        default=None,
        help="the workstation Organisation, when this workstation runs more than one",
    )
    sign_in.add_argument("--root", default=None, type=Path, help=argparse.SUPPRESS)

    annotate = subcommands.add_parser("annotate", help="attach an annotation to this Work Record")
    annotate.add_argument("--context", default="default")
    annotate.add_argument(
        "--style", default="info", choices=("info", "success", "warning", "error")
    )
    annotate.add_argument("body", nargs="?", help="annotation body; omitted reads stdin")

    artifact = subcommands.add_parser("artifact", help="offer a Workspace file as an artifact")
    artifact.add_argument("action", choices=("upload",))
    artifact.add_argument("path")
    artifact.add_argument("--label", default="")

    verb = subcommands.add_parser("verb", help="ask the Runner to evaluate one privileged verb")
    verb.add_argument("name")
    verb.add_argument("--resource", required=True)

    message = subcommands.add_parser("message", help="post to, or read, a Channel (issue 52)")
    message.add_argument("action", choices=("send", "list"))
    message.add_argument("--channel", required=True, help="the Channel's id")
    message.add_argument(
        "--kind", default="note", choices=("note", "request", "result", "handoff", "verdict")
    )
    message.add_argument("--to", default=None, help="the addressee Agent's id (issue 53)")
    message.add_argument(
        "--role",
        default=None,
        choices=ROLES,
        help="address a role instead of an Agent: a `request` wakes whichever Agent holds "
        "it, a `handoff` transfers it to `--to`",
    )
    message.add_argument(
        "--ref",
        action="append",
        default=[],
        metavar="KIND=VALUE",
        help="a reference by platform id: branch, pull_request, evidence_event, message, ...",
    )
    message.add_argument("body", nargs="?", help="markdown body for `send`; omitted reads stdin")

    ask = subcommands.add_parser(
        "ask", help="ask a human; the Work Record waits up to 24 h for the answer (issue 60)"
    )
    ask.add_argument("text", nargs="?", help="the question; omitted reads stdin")

    repo = subcommands.add_parser(
        "repo",
        help="check out a repository in reach; `branch` binds an Organisation-wide Work Record "
        "to its Product (ADR-0018)",
    )
    repo.add_argument("action", choices=("read", "branch"))
    repo.add_argument("repository", help="owner/name")
    repo.add_argument(
        "--base", default="", help="`branch` only: the base branch; default the repository's"
    )

    skill_parser = subcommands.add_parser(
        "skill", help="the setup skill for your own Claude Code or Codex (console-v2 issue 21)"
    )
    skill_parser.add_argument("action", choices=("install", "print"))
    only = skill_parser.add_mutually_exclusive_group()
    only.add_argument(
        "--claude", dest="targets", action="store_const", const=("claude",), help="Claude only"
    )
    only.add_argument(
        "--codex", dest="targets", action="store_const", const=("codex",), help="Codex only"
    )

    subcommands.add_parser("config", help="print the resolved configuration (flag > env > file)")

    arguments = parser.parse_args(argv)
    if arguments.version:
        print(version_line())
        return 0
    if arguments.command == "register":
        try:
            # A `contract_uid` Runner that cannot change uid fails closed here rather
            # than silently sharing one uid between Contracts (17 A2).
            _, outcome = asyncio.run(
                service.register(state_dir=_state_dir(), can_separate_uids=can_separate_uids())
            )
            print(outcome)
        except RunnerRegistrationError as error:
            print(f"registration refused ({error.reason}): {error}")
            return 1
        return 0
    if arguments.command == "run":
        if arguments.org:
            return workstation.run_process(_org_paths(arguments), login_agent=arguments.login_agent)
        return asyncio.run(service.run(attestation=_image_attestation(), install=_run_install()))
    if arguments.command in {"install", "start", "stop", "credential"}:
        return _workstation(arguments)
    if arguments.command == "recipient-key":
        api, kube_ns = in_cluster()
        pair = ensure_recipient_key(
            api, secret_name=arguments.secret, kube_ns=kube_ns, state_dir=_state_dir()
        )
        print(
            f"recipient key {pair.fingerprint} installed from Secret {kube_ns}/{arguments.secret}"
        )
        return 0
    if arguments.command == "status":
        lines = workstation.status_lines(_org_paths(arguments)) if arguments.org else status_lines()
        for line in lines:
            print(line)
        return 0
    if arguments.command == "sign-in":
        return _sign_in(arguments)
    if arguments.command == "skill":
        if arguments.action == "print":
            sys.stdout.write(skill.text())
            return 0
        for path in skill.install(arguments.targets or skill.TARGETS):
            print(f"installed {path}")
        return 0
    if arguments.command == "config":
        config = load_config(config_file=arguments.config, hooks_path=arguments.hooks_path)
        print(config.model_dump_json(indent=2))
        return 0
    if arguments.command is None:
        parser.print_help()
        return 0
    return _callback(arguments)


def _org_paths(arguments: argparse.Namespace) -> workstation.OrgPaths:
    return workstation.OrgPaths(
        root=arguments.root or workstation.default_root(),
        org=workstation.validate_org(arguments.org),
    )


def _workstation(arguments: argparse.Namespace) -> int:
    paths = _org_paths(arguments)
    try:
        if arguments.command == "install":
            return _install(arguments, paths)
        if arguments.command == "start":
            workstation.start(paths)
            print(f"started {paths.org}")
            return 0
        if arguments.command == "stop":
            workstation.stop(paths)
            print(f"stopped {paths.org}; every other Organisation's Runner keeps polling")
            return 0
        # The value arrives on stdin so it is never in argv or shell history.
        store = open_workstation_store(paths.org, fallback=paths.credentials)
        store.put(arguments.reference, sys.stdin.read().strip())
        print(f"{arguments.reference} stored in the {store.kind.value} store")
        return 0
    except (FileNotFoundError, RuntimeError, ValueError) as error:
        print(error, file=sys.stderr)
        return 1


def _image_attestation() -> HostAttestation:
    """What a container Runner says about itself, where issue 47 gave the workstation its
    own: the CLIs it found are what the registry serves and routing matches on."""

    return workstation.collect_attestation(
        channel=(
            InstallChannel.HELM
            if os.environ.get("KUBERNETES_SERVICE_HOST")
            else InstallChannel.MANUAL
        ),
        session=SessionKind.CONTAINER,
        # Always a store: the state directory's own, where `sign-in` keeps a key, read
        # before any mounted Secret (`service.build_credential_resolver`).
        store=StoreKind.FILE,
        path=os.environ.get("PATH", ""),
    )


def _run_install() -> RunnerInstall:
    """Where a command reaches this ``run``: Helm with the pod name, Docker with the
    container id, and anything else as a workstation, whose hostname can name its person
    and is never reported (rev 3).

    Docker's hostname is sent only in the container-id shape Docker gives it: with
    ``--network host`` it is the machine's own name, and with ``--hostname`` whatever the
    operator chose. A name the schema refuses is sent as none rather than stop the start.
    """

    if os.environ.get("KUBERNETES_SERVICE_HOST"):
        target, hostname = InstallTarget.HELM, socket.gethostname()
    elif _in_docker():
        target, hostname = InstallTarget.DOCKER, socket.gethostname()
        if not _CONTAINER_ID.fullmatch(hostname):
            hostname = ""
    else:
        return RunnerInstall(target=InstallTarget.WORKSTATION)
    try:
        return RunnerInstall(target=target, hostname=hostname or None)
    except ValueError:
        return RunnerInstall(target=target)


_CONTAINER_ID = re.compile(r"[0-9a-f]{12}")


def _in_docker() -> bool:
    return Path("/.dockerenv").exists()


def _in_container() -> bool:
    return bool(os.environ.get("KUBERNETES_SERVICE_HOST")) or _in_docker()


def _sign_in(arguments: argparse.Namespace) -> int:
    if not (os.isatty(0) and os.isatty(1)):
        print(
            "sign-in is interactive: run it in a terminal (`kubectl exec -it`, `docker exec -it`)",
            file=sys.stderr,
        )
        return 2
    store: KeyStore
    if arguments.org or not _in_container():
        try:
            paths = _sign_in_org(arguments)
        except ValueError as error:
            print(error, file=sys.stderr)
            return 2
        os.environ.update(
            workstation.run_environment(paths, workstation.WorkstationSettings.load(paths))
        )
        store = open_workstation_store(paths.org, fallback=paths.credentials)
    else:
        store = service.own_store(load_config())
    return asyncio.run(service.sign_in(arguments.runtime, store=store, method=arguments.method))


def _sign_in_org(arguments: argparse.Namespace) -> workstation.OrgPaths:
    """``--org``, or the one Organisation this workstation runs -- no id to type."""

    if arguments.org:
        return _org_paths(arguments)
    root = arguments.root or workstation.default_root()
    installed = (
        sorted(path.name for path in root.iterdir() if (path / "workstation.json").is_file())
        if root.is_dir()
        else []
    )
    if len(installed) != 1:
        raise ValueError(
            "no workstation Runner is installed here"
            if not installed
            else f"this workstation runs {', '.join(installed)}: name one with --org"
        )
    return workstation.OrgPaths(root=root, org=installed[0])


def _install(arguments: argparse.Namespace, paths: workstation.OrgPaths) -> int:
    if not arguments.control_plane:
        print(f"--control-plane (or {CONTROL_PLANE_ENV}) is required", file=sys.stderr)
        return 2
    try:
        acp_cli_kinds = _acp_cli_kinds(arguments.acp_cli_kinds, paths)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2
    # Pasted, never a flag: a flag is in the process table for every uid on the box.
    token = registration_token_from_env() or getpass.getpass(
        "Runner Registration Token (yours from /me/runners, or the Organisation Admin's for "
        "an Organisation Runner): "
    )
    settings = workstation.WorkstationSettings(
        org=paths.org,
        control_plane_url=arguments.control_plane,
        temporal_address=arguments.temporal_address,
        temporal_tls=not arguments.temporal_plaintext,
        path=os.environ.get("PATH", ""),
        install_channel=workstation.install_channel(),
        acp_cli_kinds=acp_cli_kinds,
    )
    try:
        lines = asyncio.run(
            workstation.install(
                paths, settings, registration_token=token, start_service=not arguments.no_start
            )
        )
    except (workstation.CliMissingError, RunnerRegistrationError, RuntimeError) as error:
        print(f"install refused: {error}", file=sys.stderr)
        return 1
    for line in lines:
        print(line)
    return 0


def _acp_cli_kinds(flag: str | None, paths: workstation.OrgPaths) -> str:
    # A reinstall to pick up a new PATH or retry a token must not turn ACP off (QTS-1355).
    if flag is not None:
        return workstation.parse_acp_cli_kinds(flag)
    if not paths.settings_file.is_file():
        return ""
    return workstation.WorkstationSettings.load(paths).acp_cli_kinds


def _callback(arguments: argparse.Namespace) -> int:
    socket_path = os.environ.get(CALLBACK_SOCKET_ENV, "")
    token = os.environ.get(CALLBACK_TOKEN_ENV, "")
    if not socket_path or not token:
        print(
            f"{CALLBACK_SOCKET_ENV} and {CALLBACK_TOKEN_ENV} are set only inside a Directive",
            file=sys.stderr,
        )
        return 2
    path, payload = _request(arguments)
    try:
        answer = call(socket_path=socket_path, token=token, path=path, payload=payload)
    except CallbackError as error:
        print(error.detail, file=sys.stderr)
        return 1
    print(json.dumps(answer))
    return 0


def _request(arguments: argparse.Namespace) -> tuple[str, dict[str, object]]:
    if arguments.command == "annotate":
        body = arguments.body if arguments.body is not None else sys.stdin.read()
        return "/v0/annotate", {
            "context": arguments.context,
            "body": body,
            "style": arguments.style,
        }
    if arguments.command == "artifact":
        return "/v0/artifact", {"path": arguments.path, "label": arguments.label}
    if arguments.command == "ask":
        return "/v0/ask", {
            "text": arguments.text if arguments.text is not None else sys.stdin.read()
        }
    if arguments.command == "repo":
        return f"/v0/repo/{arguments.action}", {
            "repository": arguments.repository,
            "base_ref": arguments.base,
        }
    if arguments.command == "message":
        if arguments.action == "list":
            return "/v0/message/list", {"channel_id": arguments.channel}
        body = arguments.body if arguments.body is not None else sys.stdin.read()
        return "/v0/message/send", {
            "channel_id": arguments.channel,
            "kind": arguments.kind,
            "body": body,
            "references": [_reference(item) for item in arguments.ref],
            "recipient_agent_id": arguments.to,
            "recipient_role": arguments.role,
        }
    return "/v0/verb", {"verb": arguments.name, "resource": arguments.resource}


def _reference(item: str) -> dict[str, str]:
    kind, separator, value = item.partition("=")
    if not separator or not kind or not value:
        raise SystemExit(f"--ref expects KIND=VALUE, got {item!r}")
    return {"kind": kind, "value": value}


if __name__ == "__main__":  # pragma: no cover - exercised through the console script
    raise SystemExit(main())
