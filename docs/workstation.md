# Run a Runner on your own machine

On a laptop or desktop the Runner is a **per-user login agent, one per Organisation**,
installed from PyPI and run as you: a LaunchAgent on macOS, a `systemd --user` unit on
Linux. Never a system daemon, never `sudo`. CI installs the built wheel with
`uv tool install` and runs a Runner to its first heartbeat on Linux, and through launchd on
macOS (`tests/install/workstation.sh`).

## Before you start

1. **Codex or Claude Code.** Install `codex`, `claude`, or both, the usual way. The Runner
   finds them on your `PATH`; it does not bundle them. It serves every CLI it finds, and
   refuses to install if it finds neither.
2. **An Agent Token.** Your Organisation's Admin mints it on the Organisation Console's
   **Runners** page, labelled after you (`alice-laptop`), and sends it to you over a
   channel you both trust, such as a password manager share. It is shown once.
3. **The install command.** The same page shows it for your Organisation, with the
   control plane URL and the Temporal address filled in. Copy both values from there: they
   are set independently and need not share a domain, so neither can be guessed from the
   other.

## Install

```sh
uv tool install agentic-runner     # or: pipx install agentic-runner
agentic-runner --version
```

Then install one Runner per Organisation you work for. `acme` is your local name for it:
lowercase letters, digits and `-`.

```sh
agentic-runner install acme \
  --control-plane <control-plane URL from the Runners page> \
  --temporal-address <Temporal address from the Runners page>
# Agent Token (issued to you by the Organisation's Admin): ********
```

You paste the token at the prompt. It is never a flag, because every user on the machine
can read the process table, and it is never stored: `install` exchanges it once for this
Runner's own identity. Your Admin can revoke the token once your Runner shows on the
Runners page.

`install` registers the Runner, then installs and starts its login agent:

- **macOS:** `~/Library/LaunchAgents/agentic-runner.acme.plist`, loaded into your
  `gui/<uid>` session with `launchctl bootstrap`. It runs while you are logged in, which is
  what lets it use your login Keychain.
- **Linux:** `~/.config/systemd/user/agentic-runner-acme.service`, enabled with
  `systemctl --user enable --now`, plus `loginctl enable-linger` for your own user so it
  keeps running after a reboot with nobody logged in.

The Runner captures the `PATH` you installed from. If you install a CLI later, run
`agentic-runner install` again so the Runner can find it.

## Sign in to Codex and Claude Code

Each Contract's CLI runs under a sign-in made **from the console**: on the Contract's page,
start the sign-in and finish it in your browser. The Runner keeps that sign-in in the
Contract's own directory under its state root. It does not read your own `~/.codex` or
`~/.claude`, and there is no sign-in command in the terminal.

## Operate

```sh
agentic-runner status acme    # process, identity, heartbeat age
agentic-runner stop acme      # this Organisation only; every other one keeps running
agentic-runner start acme
printf %s "$VALUE" | agentic-runner credential set acme deploy_key   # a Credential Reference
```

`stop` drains: the Runner takes no new work and lets the Directive in flight finish.

Each Organisation has its own directory under the state root
(`~/Library/Application Support/agentic-runner/` on macOS, `~/.local/state/agentic-runner/`
on Linux; `AGENTIC_RUNNER_WORKSTATION_ROOT` or `--root` changes it). It holds the Runner's
identity, its Recipient Key, the Workspaces, the harness roots, Runner Hooks (`hooks/`),
`runner.log` and the fallback credential store. Deleting it means installing again and
asking every funder to deliver their sealed credentials again.

Credential References go to the macOS Keychain or the Linux Secret Service when one is
reachable, and otherwise to `0600` files in that directory.

## What to expect

- **One Contract at a time.** A workstation Runner runs `isolation: none`: Directives run as
  you, so the control plane sends it at most one Contract at a time.
- **Sleep is an outage.** A closed lid suspends the Runner. The Directive in flight fails
  its heartbeat and is retried on the same Runner after wake. The consoles show the Runner
  offline after three minutes without a heartbeat.
- **Disk encryption** (FileVault, LUKS) is recommended. Workspaces hold client source code.
- **Windows:** run the Linux steps under WSL 2. The Runner process does not run natively on
  Windows yet.

## Upgrade and remove

```sh
uv tool upgrade agentic-runner     # or: pipx upgrade agentic-runner
agentic-runner stop acme && agentic-runner start acme
```

There is no self-update. The control plane warns when a Runner falls one contracts minor
behind and stops sending it new work at two.

To remove a Runner: `agentic-runner stop acme`, delete the plist or the unit file, delete
the Organisation's directory under the state root, then ask your Admin to revoke the
Runner on the Runners page.
