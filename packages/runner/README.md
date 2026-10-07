# agentic-runner

The Agentic OS **Runner** (ADR-0013): an activity-only Temporal worker that executes
exactly what must happen where the workspace, the Agent Runtime subprocess, a verb seam
or a credential the Runner holds is. The Ralph Loop — every `@workflow.defn` — runs on
platform-hosted Platform Workers and never ships here.

It depends on `agentic-runner-contracts` and third-party packages only: it imports no
platform module, enforced by `tests/test_package_partition.py`.

Without the platform — its namespace, Directive tokens, Grant snapshots, Evidence sink
and the loop itself — this package is an executor with no scheduler, no governance and
no intake. That inertness, not obfuscation, is the boundary (ADR-0013 §5).

## Running it

`agentic-runner run` is the process: it exchanges the Agent Token in
`AGENTIC_AGENT_TOKEN` for a durable identity at `AGENTIC_CONTROL_PLANE_URL` (once,
persisted under `AGENTIC_RUNNER_STATE_DIR`), heartbeats every 30 s, connects to the
namespace it was handed with the Runner Token the heartbeat refreshes, and polls its own
`runner.{runner_id}` queue. `AGENTIC_RUNNER_ISOLATION` is `contract_uid` (default: every
Contract its own uid, needs `CAP_SETUID` — refused otherwise) or `none`. Readiness is
`GET /healthz` on `AGENTIC_RUNNER_READINESS_PORT`.

It ships as `ghcr.io/qtsone/agentic-runner` (`Dockerfile.runner` at the repository
root: Python, git, `codex`, `claude`, nothing else) and installs with
`charts/agentic-runner`.

On a workstation it is a per-user login agent, one per Organisation:
`agentic-runner install | start | stop | status <org>` (LaunchAgent, systemd user unit or
logon task; never `sudo`), with Credential References in the OS credential store.

Licensed under AGPL-3.0-only.
