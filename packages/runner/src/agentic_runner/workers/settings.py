from __future__ import annotations

from pathlib import Path
from typing import Self

from pydantic import Field, HttpUrl, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from agentic_runner_contracts import public_metadata
from agentic_runner_contracts.activity_io import DEFAULT_BUDGET, Budget


class WorkerSettings(BaseSettings):
    """Runtime configuration for worker-only processes."""

    TEMPORAL_ADDRESS: str = Field(..., description="Temporal server address in host:port format")
    TEMPORAL_NAMESPACE: str = Field(default="default", description="Temporal namespace name")
    PLATFORM_WORKER_SHARD_INDEX: int = Field(
        default=0,
        ge=0,
        description="Which Platform Worker shard this Deployment serves (PRD issue 37).",
    )
    PLATFORM_WORKER_SHARD_COUNT: int = Field(
        default=1,
        ge=1,
        description="How many Platform Worker shards exist; 1 = every replica polls every org.",
    )
    PLATFORM_WORKER_MAX_ORGANISATIONS: int = Field(
        default=1_000,
        ge=1,
        description=("Organisations one shard polls before refusing to start (map ticket 27 §2)."),
    )
    PLATFORM_WORKER_RECONCILE_SECONDS: float = Field(
        default=60.0,
        gt=0,
        description=(
            "How often a Platform Worker replica re-reads the Organisation list and opens "
            "or closes Worker pairs (ADR-0013 §1)."
        ),
    )
    INTERNAL_FASTAPI_BASE_URL: HttpUrl = Field(..., description="Internal FastAPI HTTP(S) base URL")
    WORKSPACE_ROOT: Path = Field(
        default=Path("/var/lib/agentic-os/workspaces"),
        description="Worker-local root for isolated git workspaces",
    )
    CODEX_HOME: Path = Field(
        default=Path("/var/lib/agentic-os/codex"),
        description="Worker-local Codex CLI config directory mounted from PVC or pod environment",
    )
    WORKER_STATE_DIR: Path = Field(
        default=Path("/var/lib/agentic-os/state"),
        description=(
            "Runner-owned state directory (PRD issue 30). Holds the Contract->uid map, so a "
            "restart reuses the uid a Contract's files are already owned by. Never readable "
            "by a Contract uid."
        ),
    )
    CONTRACT_UID_MIN: int = Field(
        default=60_000,
        ge=1,
        description="Low end of the Runner-local uid range Contracts are allocated from",
    )
    CONTRACT_UID_MAX: int = Field(
        default=60_999,
        ge=1,
        description="High end of the Runner-local uid range Contracts are allocated from",
    )
    CONTRACT_MAX_PROCESSES: int = Field(
        default=512,
        ge=1,
        description=(
            "RLIMIT_NPROC applied per Contract uid at every spawn (ADR-0011 s12 sandbox "
            "floor, map ticket 17 A7): the noisy-neighbour floor, not attenuable by a Grant."
        ),
    )
    CONTRACT_MEMORY_LIMIT_BYTES: int = Field(
        default=4 * 1024**3,
        ge=1,
        description=(
            "Default per-process memory ceiling for a Contract's spawns. An Agent Runtime "
            "Profile's `command_policy.memory_limit_bytes` overrides it per Profile."
        ),
    )
    CODEX_CLI_PATH: str = Field(
        default="codex",
        description=(
            "Codex CLI executable, resolved on PATH by default. Mirrors AppSettings."
            "codex_cli_path so the fleet status probe (issue 06) spawns the same binary "
            "the FastAPI pod does."
        ),
    )
    CODEX_CLI_TIMEOUT_SECONDS: int = Field(
        default=900,
        ge=1,
        description="Maximum Codex CLI execution duration in seconds",
    )
    CODEX_CLI_OUTPUT_LIMIT_BYTES: int = Field(
        default=65_536,
        ge=1,
        description="Maximum retained stdout and stderr bytes from Codex CLI evidence",
    )
    CODEX_SANDBOX_MODE: str = Field(
        default="",
        description="Explicit Codex sandbox mode required before worker-local execution",
    )
    CODEX_ASK_FOR_APPROVAL: str = Field(
        default="",
        description="Explicit Codex approval mode required before worker-local execution",
    )
    CODEX_STRICT_CONFIG: bool = Field(
        default=False,
        description="Require Codex strict config mode before worker-local execution",
    )
    CODEX_POLICY_HOOK_CONFIGURED: bool = Field(
        default=False,
        description="Worker assertion that Codex policy hook or permission profile is configured",
    )
    ANTHROPIC_API_KEY: str = Field(
        default="",
        description="Claude Code runtime API key (api_key auth model; redacted from evidence)",
    )
    CLAUDE_MODEL: str = Field(
        default="claude-opus-4-8",
        description="Model id the Claude Code runtime runs with",
    )
    CLAUDE_CLI_TIMEOUT_SECONDS: int = Field(
        default=900,
        ge=1,
        description="Maximum Claude Code CLI execution duration in seconds",
    )
    CLAUDE_CLI_OUTPUT_LIMIT_BYTES: int = Field(
        default=65_536,
        ge=1,
        description="Maximum retained stdout and stderr bytes from Claude Code CLI evidence",
    )
    ACP_CLI_KINDS: str = Field(
        default="",
        description=(
            "Comma-separated cli_kinds (`codex_cli`, `claude_code`) this Runner serves through "
            "the pinned ACP bridge instead of the per-CLI runtime (local-agents 12)."
        ),
    )
    ACP_PROFILE_CLI_KINDS: str = Field(
        default="",
        description=(
            "Comma-separated cli_kinds, other than `codex_cli` and `claude_code`, for which "
            "this Runner's host opts in to run the ACP command an Agent Runtime Profile names "
            "(local-agents 18). Empty: no Profile-named command ever runs here."
        ),
    )
    CLAUDE_DANGEROUSLY_SKIP_PERMISSIONS: bool = Field(
        default=False,
        description=(
            "Worker assertion that the container sandbox makes autonomous Claude Code edits safe; "
            "fail-closed — the Claude runtime refuses until this is explicitly set"
        ),
    )
    TRIAGE_MODEL: str = Field(
        default="openai/gpt-5-mini",
        description="Model the Triage Directive's one-turn classification call runs on (issue 29)",
    )
    TRIAGE_DIRECTIVE_TIMEOUT_SECONDS: float = Field(
        default=30.0,
        gt=0,
        description=(
            "How long the Triage Directive waits on the proxy before falling back to the "
            "keyword classifier (PRD issue 29: a hang is treated exactly like a malformed "
            "answer, never an Incident)."
        ),
    )
    BUDGET_MAX_DIRECTIVES: int = Field(
        default=DEFAULT_BUDGET.max_directives,
        ge=1,
        description="Default per-Work-Record ceiling on Directives in a Ralph Loop",
    )
    BUDGET_MAX_WALL_CLOCK_SECONDS: float = Field(
        default=DEFAULT_BUDGET.max_wall_clock_seconds,
        gt=0,
        description="Default per-Work-Record ceiling on cumulative Directive runtime, in seconds",
    )
    BUDGET_MAX_WALL_CLOCK_SECONDS_USER_HOSTED: float = Field(
        # 23 item 4 asks for "a longer default" and fixes no value; twice the ordinary
        # default is this release's reading, and an operator's to change.
        default=DEFAULT_BUDGET.max_wall_clock_seconds * 2,
        gt=0,
        description=(
            "Default wall-clock ceiling for a Work Record whose Contract names "
            "`runner_host: user` (PRD issue 47): a workstation's sleep is an outage the "
            "wall-clock keeps counting through"
        ),
    )
    BUDGET_MAX_TOKENS: int = Field(
        default=DEFAULT_BUDGET.max_tokens,
        ge=0,
        description=(
            "Default per-Work-Record token Budget (map ticket 12 B1: tokens are the unit). "
            "0 means no token ceiling. A Work Record whose `budget_max_tokens` column is "
            "set overrides this; null means this default applies."
        ),
    )

    model_config = SettingsConfigDict(env_prefix="", frozen=True)

    @property
    def default_budget(self) -> Budget:
        """The per-Work-Record Budget the worker assigns when assembling context."""
        return Budget(
            max_directives=self.BUDGET_MAX_DIRECTIVES,
            max_wall_clock_seconds=self.BUDGET_MAX_WALL_CLOCK_SECONDS,
            max_tokens=self.BUDGET_MAX_TOKENS,
        )

    @field_validator("TEMPORAL_ADDRESS")
    @classmethod
    def validate_temporal_address(cls, value: str) -> str:
        """Validate host:port shape for Temporal server address."""

        stripped_value = value.strip()
        if stripped_value.count(":") < 1:
            raise ValueError("TEMPORAL_ADDRESS must follow host:port")

        host, port_text = stripped_value.rsplit(":", 1)
        if not host:
            raise ValueError("TEMPORAL_ADDRESS host may not be empty")
        if host.startswith("[") and host.endswith("]"):
            host = host[1:-1]
            if not host:
                raise ValueError("TEMPORAL_ADDRESS host may not be empty")

        if ":" in host:
            raise ValueError("TEMPORAL_ADDRESS host may not contain unescaped ':'")

        try:
            port = int(port_text)
        except ValueError as exception:
            raise ValueError("TEMPORAL_ADDRESS port must be an integer") from exception

        if not (1 <= port <= 65535):
            raise ValueError("TEMPORAL_ADDRESS port must be in range 1-65535")

        return stripped_value

    @field_validator("TEMPORAL_NAMESPACE")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        """Trim worker identifiers and reject empty values."""

        stripped_value = value.strip()
        if not stripped_value:
            raise ValueError("worker setting must not be empty")
        return stripped_value

    @field_validator("TEMPORAL_NAMESPACE")
    @classmethod
    def validate_temporal_namespace(cls, value: str) -> str:
        """Accept only the pre-cut `default` or an Organisation namespace the builder makes.

        Mirrors ``AppSettings``: the Release D cut (PRD issue 16) sets this side and the
        backend's in one commit, and a worker pointed at a namespace nobody provisioned
        polls an empty queue in silence. Failing at startup is the loud half.
        """
        if value != "default" and not public_metadata.is_namespace(value):
            raise ValueError(
                "TEMPORAL_NAMESPACE must be 'default' or an Organisation namespace "
                "built by agentic_runner_contracts.public_metadata.namespace()"
            )
        return value

    @field_validator("WORKSPACE_ROOT", "CODEX_HOME", "WORKER_STATE_DIR")
    @classmethod
    def validate_worker_local_path(cls, value: Path) -> Path:
        """Require absolute worker-local filesystem paths for worker-only mounts."""

        if not value.is_absolute():
            raise ValueError("worker-local paths must be absolute")
        return value

    @field_validator("CODEX_SANDBOX_MODE", "CODEX_ASK_FOR_APPROVAL")
    @classmethod
    def strip_codex_guard_text(cls, value: str) -> str:
        """Trim optional Codex guard settings while allowing empty fail-closed defaults."""

        return value.strip()

    @model_validator(mode="after")
    def validate_contract_uid_range(self) -> Self:
        """A Runner that hands two Contracts the same uid has no isolation at all."""

        if self.CONTRACT_UID_MAX < self.CONTRACT_UID_MIN:
            raise ValueError("CONTRACT_UID_MAX must not be below CONTRACT_UID_MIN")
        return self

    @model_validator(mode="after")
    def validate_worker_state_dir_isolated_from_workspaces(self) -> Self:
        """The Contract->uid map must not sit inside a tree a Contract uid can read."""

        if _paths_overlap(
            self.WORKSPACE_ROOT.resolve(strict=False), self.WORKER_STATE_DIR.resolve(strict=False)
        ):
            raise ValueError("WORKER_STATE_DIR must not overlap WORKSPACE_ROOT")
        return self

    @model_validator(mode="after")
    def validate_codex_home_isolated_from_workspaces(self) -> Self:
        """Prevent Codex auth/config state from overlapping editable workspaces."""

        workspace_root = self.WORKSPACE_ROOT.resolve(strict=False)
        codex_home = self.CODEX_HOME.resolve(strict=False)
        if _paths_overlap(workspace_root, codex_home):
            raise ValueError("CODEX_HOME must not overlap WORKSPACE_ROOT")
        return self


def get_worker_settings() -> WorkerSettings:
    """Create and return worker settings from the current environment."""

    return WorkerSettings()


def _paths_overlap(left: Path, right: Path) -> bool:
    return _is_relative_to(left, right) or _is_relative_to(right, left)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
