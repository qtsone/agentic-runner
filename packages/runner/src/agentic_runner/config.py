"""The Runner's operator configuration, with the precedence rule written down.

**flag > env > config file**, for every setting (map ticket 26 §2). Buildkite never
documented its own order and it took reading the loader's source to establish it; that is
itself the finding, so ours is documented on day one, in the module that implements it,
and pinned by ``tests/unit/test_runner_config_precedence.py``.

Deliberately small. 26 §4 works out that the Runner's config surface collapses to the
paths and the log level: it has no plugins to switch off, no repository hooks, and no
operator-typed command — the Directive is dispatched, not scripted — and its endpoint,
namespace and task queues are delivered at bootstrap and never typed. ``WorkerSettings``
remains the pod's env-only surface for the Temporal/FastAPI wiring; this is the surface a
workstation Runner (PRD issue 47) and the Helm chart (issue 46) actually fill.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel, ConfigDict

__all__ = ["CONFIG_FILE_ENV", "DEFAULT_CONFIG_FILE", "ENV_PREFIX", "RunnerConfig", "load"]

ENV_PREFIX: Final[str] = "AGENTIC_RUNNER_"
CONFIG_FILE_ENV: Final[str] = f"{ENV_PREFIX}CONFIG"
DEFAULT_CONFIG_FILE: Final[Path] = Path("/etc/agentic-runner/config.toml")


class RunnerConfig(BaseModel):
    """What an operator may set on a Runner."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Where Runner Hooks are installed: a ConfigMap mounted with `defaultMode: 0755` on
    # Kubernetes (PRD issue 46), a directory baked into an org's layered image, or
    # `<state_dir>/<org>/hooks/` on a workstation Runner (issue 47). Unset means no hooks.
    hooks_path: Path | None = None
    workspace_root: Path = Path("/var/lib/agentic-os/workspaces")
    state_dir: Path = Path("/var/lib/agentic-os/state")
    # Per-attempt callback sockets. Its own short root because `sun_path` is ~104 bytes
    # and a Workspace path is already past that (`callback.MAX_SOCKET_PATH_BYTES`).
    socket_dir: Path = Path("/run/agentic-runner")
    # Where the host operator installed the Credential Reference values: a Secret mounted
    # `0400` on Kubernetes (PRD issue 46), a directory on a workstation Runner (issue 47).
    # One file per reference, named after it (`credentials.DirectoryCredentialStore`).
    # Unset is fail-closed rather than permissive: a Contract that declares a manifest
    # fails its Directives with Evidence naming the reference (22 A1), instead of running
    # without the credential it was told to use.
    credential_store: Path | None = None
    # Where a delivered OpenAI / Anthropic key is spent (local-agents 04b), when not at
    # the vendor itself: a gateway the host runs in front of it. The host already holds
    # the opened value, so pointing it elsewhere grants the host nothing it lacked.
    openai_base_url: str | None = None
    anthropic_base_url: str | None = None
    log_level: str = "info"


def load(
    *,
    config_file: Path | None = None,
    environ: Mapping[str, str] | None = None,
    **flags: object,
) -> RunnerConfig:
    """Resolve the Runner's settings: flag > env > config file.

    ``flags`` are the parsed command line — a ``None`` there means "not passed", so an
    unset flag falls through to the env var and then to the file rather than overwriting
    them with a default.
    """

    env = os.environ if environ is None else environ
    path = config_file or _path_from_env(env) or DEFAULT_CONFIG_FILE
    values: dict[str, Any] = _from_file(path)
    values.update(_from_env(env))
    values.update({name: value for name, value in flags.items() if value is not None})
    return RunnerConfig(**values)


def _path_from_env(env: Mapping[str, str]) -> Path | None:
    raw = env.get(CONFIG_FILE_ENV, "").strip()
    return Path(raw) if raw else None


def _from_file(path: Path) -> dict[str, Any]:
    """The config file's keys, ignoring any this release does not know.

    Unknown keys are dropped rather than refused: a file is shared across a fleet an
    operator upgrades one Runner at a time, and a key added for a newer Runner must not
    stop an older one from starting. A *flag* or env var is the opposite — it was typed
    for this process, so `extra="forbid"` on the model surfaces the typo.
    """

    if not path.is_file():
        return {}
    with path.open("rb") as handle:
        loaded = tomllib.load(handle)
    known = set(RunnerConfig.model_fields)
    return {key: value for key, value in loaded.items() if key in known}


def _from_env(env: Mapping[str, str]) -> dict[str, Any]:
    return {
        field: env[f"{ENV_PREFIX}{field.upper()}"]
        for field in RunnerConfig.model_fields
        if env.get(f"{ENV_PREFIX}{field.upper()}", "").strip()
    }
