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

   The ACP bridges are optional, and used only for the CLIs the Runner's `ACP_CLI_KINDS`
   setting names (empty by default). To try them, install the versions the Runner pins
   (`ACP_BRIDGES` in `agentic_runner/workers/acp_runtime.py`):

   ```sh
   npm install --global --omit=optional \
     @agentclientprotocol/codex-acp@2.1.1 @agentclientprotocol/claude-agent-acp@0.88.0
   ```

   They drive the `codex` and `claude` already on your `PATH`, so their bundled copies are
   left out.
2. **An Agent Token hosted by you.** Mint it yourself on **/me/runners → Add a Runner** in
   the console, labelled after the machine (`alice-laptop`). It is shown once and installs
   for 7 days. Your Organisation's Admin can also mint one on the Organisation Console's
   **Runners** page with **Hosted by** set to you, and send it over a channel you both
   trust, such as a password manager share. The Runner takes its host from the token: a
   token hosted by the Organisation makes an Organisation Runner, which never takes your
   Contracts' work.
3. **A Contract whose Runner is hosted by you.** Only your Contracts with `runner_host:
   user` route to your Runner. If yours says the Organisation, ask the Admin to change it.
4. **The install command.** **/me/runners** (after minting) and the Organisation
   Console's Runners page show it for your Organisation, with the
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
# Agent Token (yours from /me/runners, or the Organisation Admin's for an Organisation Runner): ********
```

You paste the token at the prompt. It is never a flag, because every user on the machine
can read the process table, and it is never stored: `install` exchanges it once for this
Runner's own identity. Revoke the token on **/me/runners** once your Runner shows there.

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

## Give it work

`install` ends with a `next` line, and `agentic-runner status acme` repeats it while the
Runner is running:

1. Open **/me/runners** in the console. Your Runner is listed there, online.
2. Sign in the Contract's CLI from the Contract's page, as above.
3. Open **/me/work/new** and pick a Contract the page says *runs on a Runner you host*.
   Describe the work and create the Work Record.
4. On **/me/work/[id]**, the Evidence names the Runner that took it.

The Contract's **Runner hosted by** decides which Runner gets the work, not the Runner:
a Contract hosted by the Organisation runs on an Organisation Runner even while yours is
idle. If no Contract on /me/work/new runs on a Runner you host, ask the Admin to set the
Contract's Runner to be hosted by you.

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

### Switch to a new token

Running `install` again over the same Organisation's directory keeps the identity it
already holds: it prints `already registered …` and `token not used`, and the token you
pasted never reaches the control plane. There is no `uninstall`. To swap an Organisation
Runner for one hosted by you (or any Runner for a new token):

1. `agentic-runner stop acme`.
2. Move the Organisation's directory aside rather than deleting it, so the old identity,
   Recipient Key and credentials stay recoverable:
   `mv "<state root>/acme" "<state root>/acme.organisation"`.
3. `agentic-runner install acme …` again with the new token.
4. Check `agentic-runner status acme` shows the new identity and a `next` line, and that
   the Runner shows on **/me/runners**. Then ask your Admin to revoke the old Runner on the
   Runners page.

To remove a Runner: `agentic-runner stop acme`, delete the plist or the unit file, delete
the Organisation's directory under the state root, then ask your Admin to revoke the
Runner on the Runners page.
