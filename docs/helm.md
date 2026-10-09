# Run Runners on Kubernetes with Helm

The chart `oci://ghcr.io/qtsone/charts/agentic-runner` installs a StatefulSet in which
**N replicas are N Runners**: each pod registers its own identity, keeps it in its own
`state` volume, and polls its own task queue. CI installs the chart on kind with two
replicas against a fake control plane (`charts/agentic-runner/test/run.sh`).

## Before you start

- An **Agent Token** from the Organisation Console's **Runners** page. It is shown once.
- The control plane's Runner edge, `https://api.agentic.<zone>`, and the Temporal address,
  `temporal-grpc.<zone>:443` over TLS. Both are on the same page.
- A namespace whose Pod Security level allows a root container with five capabilities,
  or `isolation: none` (see [Isolation](#isolation)).
- A chart version from the [releases](https://github.com/qtsone/agentic-runner/releases).
  The chart's `appVersion` is the image tag it installs.

## Install

Keep the Agent Token out of your shell history and out of Helm's release record: put it in
a Secret first.

```sh
kubectl create namespace agentic-runner
read -rs AGENT_TOKEN   # paste the token, then Enter
printf %s "$AGENT_TOKEN" | kubectl -n agentic-runner create secret generic agentic-runner-token \
  --from-file=token=/dev/stdin
unset AGENT_TOKEN

helm upgrade --install runner oci://ghcr.io/qtsone/charts/agentic-runner \
  --version <version> -n agentic-runner \
  --set controlPlane.url=https://api.agentic.<zone> \
  --set temporal.address=temporal-grpc.<zone>:443 --set temporal.tls=true \
  --set agentToken.existingSecret=agentic-runner-token \
  --set replicas=2 \
  --set tags.region=eu-west-1
```

`agentToken.value=<token>` also works and renders the Secret for you.

Verify the release before you install it, if you like:

```sh
cosign verify ghcr.io/qtsone/charts/agentic-runner:<version> \
  --certificate-identity "https://github.com/qtsone/agentic-runner/.github/workflows/publish-pypi.yml@refs/tags/v<version>" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

## Values

| Value | What it does |
| --- | --- |
| `replicas` | N Runners. Add capacity with more replicas under the same tags. |
| `tags` | Free-form `key=value` routing data, matched against a Product's Runner selector. |
| `isolation` | `contract_uid` (default) or `none`. See below. |
| `maxConcurrentDirectives` | How many Directives one Runner runs at once. More wait on its own queue. |
| `egressPosture` | What the Runner reports it can reach: `unrestricted`, `allowlisted`, `blocked` or `unenforced`. `allowlisted` holds only if your NetworkPolicy also blocks what bypasses the proxy. |
| `hooks` | Runner Hooks: script bodies by catalogue name, mounted `0755`. |
| `credentials.existingSecret` | Credential References: one key per reference name, mounted `0400`. |
| `extraEnvFrom` | A Secret with the GitHub App values (`GITHUB_APP_ID`, `GITHUB_APP_INSTALLATION_ID`, `GITHUB_APP_PRIVATE_KEY_PEM`) and anything else the Runner reads from its environment. |
| `image.tag` | Your own image built `FROM ghcr.io/qtsone/agentic-runner` with your toolchain. Name the same tag in your Agent Runtime Profile. |
| `storage.size`, `storage.storageClassName` | Each replica's `state` volume: identity, Workspaces, harness roots. |
| `terminationGracePeriodSeconds` | 2100 by default: the drain on pod deletion waits for the Directive in flight. Do not lower it below the CLI timeout plus the Verifier's 20 minutes. |

`values.yaml` in the chart documents every value.

## Isolation

`contract_uid` runs each Contract as its own OS user. The pod runs as root with
capabilities dropped to exactly `SETUID`, `SETGID`, `CHOWN`, `FOWNER` and `DAC_OVERRIDE`.

A `restricted` Pod Security namespace cannot grant those. There, set
`--set isolation=none`: the pod runs as uid 65532 with no capabilities, and **the control
plane sends that Runner one Contract at a time**, because nothing separates Contracts on
it. The chart never chooses for you. A `contract_uid` Runner that cannot change uid
refuses to start, logs `refusing to start (isolation_unavailable)` naming `CAP_SETUID`, and
stays NotReady.

Do not put `codex_cli` in `ACP_CLI_KINDS` on a Runner pod. ACP Codex runs every command in
its bwrap sandbox, and the default `RuntimeDefault` seccomp profile stops bwrap creating a
namespace in either isolation mode, so every command fails. ACP Codex runs only on a host
where bwrap starts; Codex on a pod uses the per-CLI runtime.

## The Recipient Key

Funders seal credentials to a Runner's Recipient Key. The chart keeps **one key per
release**, in the Secret `<release>-agentic-runner-recipient-key`: the first pod to start
creates it, every other pod reads it, so all replicas show the same key fingerprint.

To rotate it, delete the Secret and restart the pods
(`kubectl rollout restart statefulset/<release>-agentic-runner`). Each funder then delivers
their credentials again, as after a reinstall.

A value delivered under **`OPENAI_API_KEY`** or **`ANTHROPIC_API_KEY`** is also the
Contract's LLM key. The Runner checks it with one models-list call and, if the provider
accepts it, spends it on every Codex or Claude Code Directive of that Contract. A Runner
hosted by an Organisation is shared, and a shared Runner never signs in to a
subscription, so this key is the only way it runs a Contract's Directives. Without one,
the Directive is refused `shared_runner_api_key_only`. The heartbeat reports the key as
`<reference>@v<version>`. To spend it at a gateway rather than at the vendor, set
`AGENTIC_RUNNER_OPENAI_BASE_URL` / `AGENTIC_RUNNER_ANTHROPIC_BASE_URL` in `extraEnv`.

## Check it

```sh
kubectl -n agentic-runner get pods -l app.kubernetes.io/instance=runner          # 2/2 Ready
kubectl -n agentic-runner logs runner-agentic-runner-0 -c runner | head -3        # "registered <id> in namespace org-… on runner.<id>"
```

Ready means registered, heard by the control plane and polling. Both Runners appear on the
Runners page within one heartbeat (30 seconds).

| Symptom | Cause |
| --- | --- |
| NotReady, `refusing to start (isolation_unavailable)` | The namespace cannot grant `CAP_SETUID`. Set `isolation: none`. |
| NotReady, `refusing to start (unsafe_state_dir)` | The state directory or a file in it is a symlink, belongs to another uid, or is open to group or others. A volume made by an earlier release holds a `state` directory kubelet created with the volume's mode: `chown` it to the Runner's uid, `chmod 700` it and `chmod 600` its files. |
| NotReady, `registration refused (…)` | The Agent Token is revoked or expired, the Account is on hold, or the Organisation has reached its Runner limit. |
| `heartbeat failed before start` | The control plane URL is wrong or unreachable. The Runner retries every 30 seconds. |
| Ready, but no Directives arrive | The tags do not match the Product's selector, or an `isolation: none` Runner already holds another Contract's work. |
| A hook does nothing | It was mounted without the executable bit and exits `126`. |

## Upgrade, roll back, remove

```sh
helm upgrade runner oci://ghcr.io/qtsone/charts/agentic-runner --version <new> -n agentic-runner --reuse-values
helm rollback runner -n agentic-runner
helm uninstall runner -n agentic-runner
```

After `helm uninstall`, revoke the Runners on the Runners page so routing stops choosing
them. Work pinned to a removed Runner starts again on another matching Runner from its
branch. The `state` volumes outlive the release: delete the PVCs if you will not reinstall.
