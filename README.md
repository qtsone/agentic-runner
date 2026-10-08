# agentic-runner

The Runner for [Agentic OS](https://github.com/qtsone): it executes an Organisation's agent
Directives on infrastructure the Organisation or person controls — a Kubernetes cluster (Helm),
any Docker host, or their own machine.

## What a Runner is

A **Runner** is an activity-only Temporal worker. The Agentic OS control plane owns the Ralph
Loop — every workflow, every Gate, the Grants, the Evidence record — and schedules the steps
that must happen *where the work is* onto the Runner's own task queue: preparing a Workspace,
driving an Agent Runtime (Codex or Claude Code) through a Directive, running the Verifier, and
the verb seams (`push`, `pr.open`, `pr.review`, `pr.merge`), each checked against the Grant
snapshot the Runner was handed. It returns facts and keeps no state between activities.

Without the control plane a Runner is inert: no scheduler, no governance, no intake. That
inertness, not obfuscation, is the boundary, and the reason it is open source. The terms used
here are defined in [CONTEXT.md](CONTEXT.md).

This repository holds two Python distributions:

| Distribution | Path | What it is |
| --- | --- | --- |
| `agentic-runner-contracts` | [`packages/contracts`](packages/contracts) | The wire between the control plane and a Runner: activity I/O, the payloads a Runner parses, the Grant evaluator and the Public Metadata name builders. |
| `agentic-runner` | [`packages/runner`](packages/runner) | The Runner process (`agentic-runner run`), Workspaces, Agent Runtimes, the GitHub client, the Verifier and the verb seams. |

It also holds the image ([`Dockerfile.runner`](Dockerfile.runner)) and the Helm chart
([`charts/agentic-runner`](charts/agentic-runner)).

## Three ways to run

Each needs an Agent Token, which your Organisation's Admin issues in the Organisation Console.

1. **Helm, on any Kubernetes cluster** — `charts/agentic-runner`; N replicas are N Runners.
   Guide: [docs/helm.md](docs/helm.md).
2. **Docker, on any Docker host** — the `ghcr.io/qtsone/agentic-runner` image, with
   `docker run` or Compose. Guide: [docs/docker.md](docs/docker.md).
3. **On your own machine** — a per-user login agent, one per Organisation
   (`agentic-runner install <org>`). Guide: [docs/workstation.md](docs/workstation.md).

Each guide installs only published artifacts, and CI runs each one to a Runner that
registers and heartbeats against a fake control plane. Before the first release, run from a
checkout: `uv sync && uv run agentic-runner --help`.

To prove a fork or your own build works without the platform, install
`agentic-runner[testing]` and run `pytest --pyargs agentic_runner.testing`. Guide:
[docs/conformance.md](docs/conformance.md).

## The compatibility floor

The version of `agentic-runner-contracts` is the compatibility floor between the control plane
and every installed Runner; `agentic-runner --version` prints both. Against the control plane's
version, a Runner with a different contracts major is refused at registration; one minor behind
works and is flagged; two or more minors behind finishes what it holds and receives no new
Directives until it is upgraded. A contracts change is therefore a release of this repository
first, and a version bump in the control plane after it.

## Releases

Both packages, the image and the chart release together at **one version**, which is also the
contracts version. Conventional commits on `main` pick it: `fix:` bumps the patch, `feat:` the
minor, and a breaking change (`feat!:` or a `BREAKING CHANGE:` footer) the major. A breaking
contracts change is always a major, and that major is the floor the control plane enforces.
Other commit types (`chore:`, `ci:`, `docs:`, `test:`, `refactor:`) release nothing.

Each merge to `main` runs semantic-release (`.github/workflows/release.yml`). When a release is
due it writes the version into both packages and the chart (`scripts/set-version.py`), commits
`CHANGELOG.md`, and pushes a `v<version>` tag. The tag starts `.github/workflows/publish-pypi.yml`,
which publishes:

| Artifact | Where |
|---|---|
| `agentic-runner-contracts`, `agentic-runner` | PyPI, by trusted publishing, with attestations |
| Runner image, `linux/amd64` and `linux/arm64` | `ghcr.io/qtsone/agentic-runner:<version>`, `:contracts-<version>`, `:latest` |
| Chart | `oci://ghcr.io/qtsone/charts/agentic-runner`, `appVersion` = `<version>` |

The image carries an SBOM and build provenance. The image and the chart are signed with keyless
cosign and carry a GitHub build provenance attestation. The GitHub Release lists the contracts
version and every artifact's digest. To verify a release:

```bash
cosign verify ghcr.io/qtsone/agentic-runner:<version> \
  --certificate-identity "https://github.com/qtsone/agentic-runner/.github/workflows/publish-pypi.yml@refs/tags/v<version>" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
gh attestation verify oci://ghcr.io/qtsone/agentic-runner:<version> --repo qtsone/agentic-runner

cosign verify ghcr.io/qtsone/charts/agentic-runner:<version> \
  --certificate-identity "https://github.com/qtsone/agentic-runner/.github/workflows/publish-pypi.yml@refs/tags/v<version>" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
gh attestation verify oci://ghcr.io/qtsone/charts/agentic-runner:<version> --repo qtsone/agentic-runner
```

Running the publish workflow by hand (`workflow_dispatch`) builds and checks everything and
publishes nothing.

## Development

```bash
uv sync                                      # both packages, editable, and the dev tools
uv run pytest -q                             # the suite
uv run ruff check packages/ tests/           # lint
uv run ruff format --check packages/ tests/  # format
uv run mypy packages/                        # strict type check
docker build -f Dockerfile.runner -t agentic-runner:dev .
./scripts/check-runner-images.sh agentic-runner:dev
```

See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md).

## Provenance

Seeded without history from `qtsone/agentic-os` at commit
`0b22f652d8990eae25aaa9918a666cdf5d04a608`. The ADR and issue numbers cited in the code refer
to that repository's design records.

## License

AGPL-3.0-only — see [LICENSE](LICENSE). Contributions are accepted under the
[Contributor License Agreement](.github/CLA.md).
