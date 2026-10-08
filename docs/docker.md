# Run a Runner with Docker

One container is one Runner, on any Docker host, from the published image
`ghcr.io/qtsone/agentic-runner`. Use `docker run` or the Compose file in
[`examples/`](../examples). CI runs both exactly as written here
(`tests/install/docker.sh`).

## Before you start

You need three values from your Organisation's Admin, all on the Organisation Console's
**Runners** page:

- the **Agent Token**, shown once when it is minted;
- the control plane's Runner edge, `https://api.agentic.<zone>`;
- the Temporal address, `temporal-grpc.<zone>:443`.

Pick an image version from the [releases](https://github.com/qtsone/agentic-runner/releases)
and pin it. `latest` moves.

## The env file

Copy [`examples/runner.env.example`](../examples/runner.env.example) to `runner.env`, fill in
the control plane URL, the Temporal address and the Agent Token, and keep it private:

```sh
curl -fsSLO https://raw.githubusercontent.com/qtsone/agentic-runner/main/examples/runner.env.example
cp runner.env.example runner.env && chmod 600 runner.env
```

The Agent Token is read once. On its first start the Runner exchanges it for its own
identity, which it keeps on the state volume. Once the Runner shows on the Runners page,
delete the `AGENTIC_AGENT_TOKEN` line.

## `docker run`

```sh
docker volume create agentic-runner-state
docker run -d --name agentic-runner \
  --restart unless-stopped \
  --env-file runner.env \
  -e AGENTIC_RUNNER_ISOLATION=contract_uid \
  --cap-drop ALL --cap-add SETUID --cap-add SETGID --cap-add CHOWN --cap-add FOWNER \
  --cap-add DAC_OVERRIDE \
  --read-only --tmpfs /tmp --tmpfs /run/agentic-runner \
  -v agentic-runner-state:/var/lib/agentic-os \
  --stop-timeout 2100 \
  ghcr.io/qtsone/agentic-runner:<version>
```

## Compose

```sh
curl -fsSLO https://raw.githubusercontent.com/qtsone/agentic-runner/main/examples/compose.yaml
AGENTIC_RUNNER_VERSION=<version> docker compose up -d
```

[`examples/compose.yaml`](../examples/compose.yaml) is the same container as the
`docker run` above.

## What each part does

- **The state volume** (`/var/lib/agentic-os`) holds the Runner's identity, its Recipient
  Key, the Workspaces and each Contract's harness root, which is where the Codex and
  Claude Code sign-ins made from the console are kept. One volume is enough. If you lose it,
  the Runner registers again as a new Runner, and every funder must deliver their sealed
  credentials again.
- **`restart: unless-stopped`** brings the Runner back after a crash or a host reboot. It
  re-reads its identity from the volume and does not register a second time.
- **`stop_grace_period: 35m`** (`--stop-timeout 2100`). On `SIGTERM` the Runner stops
  taking work and waits for the Directive in flight, which can take the CLI timeout
  (15 minutes) plus the Verifier's 20 minutes. A shorter grace period kills that
  Directive and the attempt is retried.
- **`--read-only` and the two `tmpfs` mounts** match the Helm chart: the Runner writes to
  the state volume, `/tmp` and its socket directory only.

## Isolation

`contract_uid`, the default, runs each Contract as its own OS user, so one Contract cannot
read another's Workspace or credentials. For that the container runs as root with **exactly
five capabilities and no others**:

```
--cap-drop ALL --cap-add SETUID --cap-add SETGID --cap-add CHOWN --cap-add FOWNER --cap-add DAC_OVERRIDE
```

Without them, the Runner **refuses to start**: it exits non-zero with
`refusing to start (isolation_unavailable)` naming `CAP_SETUID`, and registers nothing. It
never falls back to a weaker mode by itself.

If your host cannot grant those capabilities (a rootless or locked-down Docker, a platform
that forbids `cap_add`), run **`isolation: none`** instead, and know what you give up:

> **`isolation: none` gives no separation between Contracts.** The Runner and every
> Directive run as one unprivileged user (uid 65532) with no capabilities. The control
> plane therefore sends this Runner **one Contract at a time**. Use it only when that is
> acceptable.

```sh
# docker run: replace the isolation and capability lines with
  -e AGENTIC_RUNNER_ISOLATION=none --user 65532:65532 --cap-drop ALL \
  --security-opt no-new-privileges:true \

# Compose:
curl -fsSLO https://raw.githubusercontent.com/qtsone/agentic-runner/main/examples/compose.isolation-none.yaml
docker compose -f compose.yaml -f compose.isolation-none.yaml up -d
```

Changing the mode of an existing Runner needs a new state volume: a `contract_uid` Runner
writes its state as root, and uid 65532 cannot read it.

## Check it

```sh
docker logs agentic-runner | head -3            # "registered <id> in namespace org-… on runner.<id>"
docker inspect -f '{{.State.Health.Status}}' agentic-runner   # healthy once registered, heard and polling
```

The Runner appears on the Runners page within one heartbeat (30 seconds).

| Log line | Meaning |
| --- | --- |
| `refusing to start (isolation_unavailable)` | `contract_uid` without the five capabilities. Add them, or use `isolation: none`. |
| `refusing to start (unsafe_state_dir)` | The state directory or a file in it is a symlink, belongs to another uid, or is open to group or others. A volume made by an earlier release holds a `state` directory the image created `0755` as uid 65532: `chmod 700` it and `chmod 600` its files, keeping the uid the Runner runs as (root for `contract_uid`). |
| `registration refused (…)` | The Agent Token is revoked or expired, the Account is on hold, or the Organisation has reached its Runner limit. |
| `heartbeat failed before start` | The control plane URL is wrong or unreachable. The Runner retries every 30 seconds. |

## More configuration

Everything the [Helm chart](helm.md) sets is an environment variable, so it goes into
`runner.env`: `AGENTIC_RUNNER_TAGS` (routing tags), `AGENTIC_RUNNER_MAX_CONCURRENT_DIRECTIVES`,
`AGENTIC_RUNNER_EGRESS_POSTURE`, and the GitHub App values `GITHUB_APP_ID`,
`GITHUB_APP_INSTALLATION_ID` and `GITHUB_APP_PRIVATE_KEY_PEM`.

## Upgrade and remove

Upgrade: pull the new version and recreate the container, keeping the volume
(`docker compose up -d` after changing `AGENTIC_RUNNER_VERSION`).

Remove: `docker compose down -v` (or `docker rm -f agentic-runner && docker volume rm
agentic-runner-state`), then revoke the Runner on the Runners page so it stops counting
against your Organisation's limit.
