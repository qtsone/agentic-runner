# Prove a Runner build: the conformance kit

A fork, a patched build or a new package of the Runner must still work with the control
plane. You can prove that without the platform. The `testing` extra ships a fake control
plane and a scenario suite that runs your installed Runner against it. CI runs this guide
as written (`tests/install/conformance.sh`), on Linux and macOS.

## Run the suite

In a clean virtualenv, with Python 3.12 or later:

```sh
python -m venv conformance && . conformance/bin/activate
pip install 'agentic-runner[testing]'
pytest --pyargs agentic_runner.testing
```

To test your own build, install your wheels instead of the published package:

```sh
pip install dist/agentic_runner_contracts-*.whl "$(ls dist/agentic_runner-*.whl)[testing]"
```

Install the wheels by path. If you install by name, pip can take the release on PyPI that
has the same version, and the suite then tests that release, not your build.

Each scenario starts a local Temporal dev server. If the `temporal` CLI is on `PATH`, the
suite uses it. If not, the Temporal SDK downloads the dev server on the first run, so that
run needs network access.

## What it proves

Each scenario starts your installed Runner as a real process (`agentic-runner run`),
with `isolation: none`, against the fake control plane and a local Temporal server.

| Scenario | The Runner must |
|---|---|
| register | register once, with its own runner and contracts versions. After a restart it reads its identity back from the state directory and does not register again. |
| heartbeat | heartbeat with every request signed by the identity it was given, and report its own `runner.{runner_id}` queue. |
| Directive round trip | take one activity from its `runner.{runner_id}` queue and return the answer. A test workflow dispatches `wipe_contract_residue` there. |
| revoke | stop with a non-zero exit when the control plane refuses its heartbeat with `runner_revoked`. |
| drain | exit 0 on `SIGTERM`, and send one last heartbeat that reports the stop. |

The fake checks what the platform checks on the Runner's routes: the bootstrap body, the
Ed25519 signature on every signed request, revocation and the hosted queue. It refuses
any route outside the public Runner surface, `/api/runner/v1`. A build that calls any
other platform route fails here before it fails in production.

## What it does not prove

- Every activity. The round trip runs one activity, and that activity needs no
  Workspace, Agent Runtime or GitHub.
- `contract_uid` isolation. That needs `CAP_SETUID`. The repository's own CI tests it in a
  container (`tests/integration/test_contract_uid_isolation.py`).
- The platform's side of the contract. The fake answers the way the platform does today.
  The platform's own tests hold the platform to that.

## Use the fake on its own

The fake needs only the Runner's own dependencies, so every Runner install has it, the
image too:

```sh
python -m agentic_runner.testing 8000            # serve it; GET /stats reports what it saw
docker run --rm -p 8000:8000 --entrypoint python \
  ghcr.io/qtsone/agentic-runner:<version> -m agentic_runner.testing 8000
```

The Helm, Docker and workstation tests in this repository register Runners against it
this way (`charts/agentic-runner/test/run.sh`, `tests/install/`).

To run a Directive on an API key, deliver the key with `--deliver OPENAI_API_KEY=<value>`
(or `FakeControlPlane.deliver(slot, value)`). The fake seals it to every Runner's
Recipient Key and answers the runtime context for its one Contract. It also serves
`/v1` as the LLM provider: point the Runner there with `AGENTIC_RUNNER_OPENAI_BASE_URL`,
and `/stats` lists the credential each provider call presented.

In your own tests, the extra's pytest plugin gives you two fixtures:
`fake_control_plane` (the fake on a free port, with `.plane` and `.url`) and
`runner_launcher` (starts the installed Runner against it). For in-process tests,
`FakeControlPlane().transport()` returns an `httpx.MockTransport`.
