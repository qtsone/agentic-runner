# Run Runners on Kubernetes with Helm

The chart `oci://ghcr.io/qtsone/charts/agentic-runner` installs a StatefulSet in which
**N replicas are N Runners**: each pod registers its own identity, keeps it in its own
`state` volume, and polls its own task queue. CI installs the chart on kind with two
replicas against a fake control plane (`charts/agentic-runner/test/run.sh`).

## Before you start

- A **Runner Registration Token** from the Organisation Console's **Runners** page. It is
  shown once.
- The control plane's Runner edge, `https://api.agentic.<zone>`, and the Temporal address,
  `temporal-grpc.<zone>:443` over TLS. Both are on the same page.
- A namespace whose Pod Security level allows a root container with five capabilities,
  or `isolation: none` (see [Isolation](#isolation)).
- The chart version your control plane admits, not the newest release: a release is
  published before the control plane admits it. Your Organisation's Admin knows it, and a
  registration refused with `unpublished_runner_build` names it. The chart's `appVersion` is
  the image tag it installs.

## Install

Keep the Runner Registration Token out of your shell history and out of Helm's release record: put it in
a Secret first.

```sh
kubectl create namespace agentic-runner
read -rs REGISTRATION_TOKEN   # paste the token, then Enter
printf %s "$REGISTRATION_TOKEN" | kubectl -n agentic-runner create secret generic agentic-runner-token \
  --from-file=token=/dev/stdin
unset REGISTRATION_TOKEN

helm upgrade --install runner oci://ghcr.io/qtsone/charts/agentic-runner \
  --version <version> -n agentic-runner \
  --set controlPlane.url=https://api.agentic.<zone> \
  --set temporal.address=temporal-grpc.<zone>:443 --set temporal.tls=true \
  --set registrationToken.existingSecret=agentic-runner-token \
  --set replicas=2 \
  --set tags.region=eu-west-1
```

`registrationToken.value=<token>` also works and renders the Secret for you. A release
installed with `agentToken` before 3.16 upgrades unchanged: the chart reads `agentToken`
whenever `registrationToken` is empty.

If your Organisation stores LLM Keys, give the release its Pool Key the same way, from the
Runners page, which shows it once. Every replica reads the one Secret:

```sh
read -rs POOL_KEY   # paste the Pool Key, then Enter
printf %s "$POOL_KEY" | kubectl -n agentic-runner create secret generic agentic-runner-pool-key \
  --from-file=poolKey=/dev/stdin
unset POOL_KEY
# then add to the helm command:  --set poolKey.existingSecret=agentic-runner-pool-key
```

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
| `limits` | The Runner Limit: the tools, network destinations and folders no Agent on these Runners may use. See below. |
| `hooks` | Runner Hooks: script bodies by catalogue name, mounted `0755`. |
| `credentials.existingSecret` | Credential References: one key per reference name, mounted `0400`. |
| `poolKey.existingSecret`, `poolKey.value` | The Organisation's Pool Key, under the key `poolKey`, for every replica. Only for a pool that stores LLM Keys. |
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

## Runner Limit

`limits` denies every Agent on the release's Runners some tools, network destinations and
folders, on top of the Grants, the Profile's network policy and the command policy. Each
list is empty by default, and an empty list limits nothing:

```yaml
limits:
  tools:                # Grant entries with `deny` only
    - resource_type: repo
      selector: "acme/*"
      verbs: {"pr.merge": deny}
  networks:             # network policy destinations: host, host:port or *.domain
    - pastebin.com
  folders:              # absolute paths in the pod, never one that holds the Workspaces
    - /mnt/shared-cache
```

The chart passes it to every replica as `AGENTIC_RUNNER_LIMITS`. A Runner reads it only at
start, so a change rolls the pods. A value the Runner cannot apply, such as a verb the Verb
Catalogue does not have, stops the pod with `refusing to start (runner_limit)` naming the
entry. The pod never runs with an empty limit instead.

The console shows the limit on **/admin/runners**. Tools are `enforced`. Networks report
the `egressPosture` value: the proxy refuses what asks it, and only your NetworkPolicy stops
a process that ignores it. A hostname entry denies that name only, not the host's
addresses or other names: `pastebin.com` does not deny `www.pastebin.com`, and
`*.pastebin.com` does not deny `pastebin.com`, so list both. Folders are `checked` for Claude Code through ACP and
`unenforced` for every other CLI, which reads paths without asking. `checked` is not a
boundary: `isolation: contract_uid` is, because each Contract's OS user can read only what
it is allowed to read. [Run a Runner on your own machine](workstation.md#limit-what-agents-may-use)
says what each part refuses and what Evidence it writes.

A Runner sends its limit to the control plane only while the limit is not empty, and the
control plane needs `agentic-runner-contracts` 3.21 or later to accept it.

## The Recipient Key

Funders seal credentials to a Runner's Recipient Key. The chart keeps **one key per
release**, in the Secret `<release>-agentic-runner-recipient-key`: the first pod to start
creates it, every other pod reads it, so all replicas show the same key fingerprint.

To rotate it, delete the Secret and restart the pods
(`kubectl rollout restart statefulset/<release>-agentic-runner`). Each funder then delivers
their credentials again, as after a reinstall.

## LLM access

A Runner uses only its host's own LLM access, and its heartbeat tells the control plane
which access it holds for each Contract it serves. The control plane names the access each
Directive uses; if the Runner no longer holds it, the Directive fails with the reason and
runs on nothing else.

**A key in the Runner's own store.** Put the provider's key in the `credentials` Secret
under its key name: `ANTHROPIC_API_KEY` for Claude Code, `OPENAI_API_KEY` for Codex. The
Runner reads it within one heartbeat, checks it with one models-list call, and serves it
from its LLM proxy to every Contract it runs. The heartbeat reports whether the store holds
each key and what the check said; never the value. The key never reaches an Agent Runtime.
To spend it at a gateway rather than at the vendor, set
`AGENTIC_RUNNER_OPENAI_BASE_URL` / `AGENTIC_RUNNER_ANTHROPIC_BASE_URL` in `extraEnv`.

**The Organisation's LLM Keys.** With `poolKey` set, a Runner opens one of the keys the
Organisation stored on its Connectors page when a Directive is routed to it: it asks the
control plane for the key's two shares, opens one with the Pool Key and the other with its
Recipient Key, and holds the key in memory for that Directive only. It never writes it to
disk or gives it to the Agent Runtime. The heartbeat reports the fingerprint of the Pool Key
the Runner holds. A Runner with no Pool Key, or an old one, opens none of them.

A value a funder sealed under one of those names is still served, to that Contract only,
until the control plane stops delivering it.

## Sign in on a person's own Runner

A person who runs their own Runner gives it their access with one command, in the pod:

```sh
kubectl -n agentic-runner exec -it runner-agentic-runner-0 -c runner -- \
  agentic-runner sign-in claude
```

`sign-in codex` does the same for Codex; `sign-in` alone is refused. It
takes no ids: a personal Runner serves one Contract. It offers:

- **Your subscription.** It runs the vendor's own sign-in, unmodified, as the Contract's
  own user in its harness root on that pod's volume: Claude Code's login (`--method
  oauth_token`, the default, is a token valid for a year; `--method claude_ai` a Claude.ai
  login that renews itself), or `codex login --device-auth`, which shows a code in the
  terminal. Nothing goes through the console. It is refused when the Contract does not
  allow your own subscription, or you have not acknowledged the subscription terms, and the
  refusal says which.
- **An API key.** Typed without echo and kept in the Runner's own store on the pod's
  volume, under the provider's key name.

An Organisation's or an Account's Runner refuses `sign-in`: it uses only its pool's keys.

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
| NotReady, `registration refused (…)` | The Runner Registration Token is unknown, revoked or expired, the Account is on hold, or the Organisation has reached its Runner limit. |
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
