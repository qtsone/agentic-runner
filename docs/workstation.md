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

   The ACP bridges are optional, and used only for the CLIs `install --acp-cli-kinds` names
   (none by default; see [Install](#install)). To try them, install the versions the Runner
   pins (`ACP_BRIDGES` in `agentic_runner/workers/acp_runtime.py`):

   ```sh
   npm install --global --omit=optional \
     @agentclientprotocol/codex-acp@2.1.1 @agentclientprotocol/claude-agent-acp@0.88.0
   ```

   They drive the `codex` and `claude` already on your `PATH`, so their bundled copies are
   left out.

   Any other ACP harness (Gemini, Cursor, …) runs the command its Agent Runtime Profile
   names, and only if you opt in: pin its `cli_kind` to the absolute path of the executable
   you trust in the Runner's `ACP_PROFILE_CLI_KINDS` setting, e.g.
   `ACP_PROFILE_CLI_KINDS=gemini_cli=/usr/local/bin/gemini` (empty by default; `codex_cli`
   and `claude_code` are refused there, their bridges are pinned). The Profile chooses
   only the arguments: a command whose program is not exactly that path is refused, so
   name only an executable you trust to run on your machine. It runs under the
   same Contract uid, sandbox, MCP servers, egress proxy and permission checks as a
   pinned bridge, and the Runner reports the kind so Work Records for it are routed here.
2. **A Runner Registration Token hosted by you.** Mint it yourself on **/me/runners → Add a Runner** in
   the console, labelled after the machine (`alice-laptop`). It is shown once and installs
   for 7 days. Your Organisation's Admin can also mint one on the Organisation Console's
   **Runners** page with **Hosted by** set to you, and send it over a channel you both
   trust, such as a password manager share. The Runner takes its host from the token: a
   token hosted by the Organisation makes an Organisation Runner, which never takes your
   Contracts' work.
3. **A Contract whose Runner is hosted by you.** Only your Contracts with `runner_host:
   user` route to your Runner. If yours says the Organisation, ask the Admin to change it.
4. **The install command.** **/me/runners** (after minting) and the Organisation
   Console's Runners page show it for your Organisation, with the control plane URL filled
   in. Copy it from there. The Runner learns its Temporal address when it registers;
   `--temporal-address` is a fallback for a control plane that does not send one.

## Install

```sh
uv tool install agentic-runner     # or: pipx install agentic-runner
agentic-runner --version
```

Then install one Runner per Organisation you work for. `acme` is your local name for it:
lowercase letters, digits and `-`.

```sh
agentic-runner install acme --control-plane <control-plane URL from the Runners page>
# Runner Registration Token (yours from /me/runners, or the Organisation Admin's for an Organisation Runner): ********
```

You paste the token at the prompt. It is never a flag, because every user on the machine
can read the process table, and it is never stored: `install` exchanges it once for this
Runner's own identity. Revoke the token on **/me/runners** once your Runner shows there.

An Organisation Runner whose Organisation stores LLM Keys also needs the Pool Key from the
Runners page. Add `--pool-key` and paste it at the second prompt, or set
`AGENTIC_RUNNER_POOL_KEY` on the install line the console printed. `install` keeps it in the
Runner's state directory, readable by you only, because the Runner needs it on every start.
After the Organisation creates a new Pool Key, run `install` again with the new one.

`install` registers the Runner, then installs and starts its login agent:

- **macOS:** `~/Library/LaunchAgents/agentic-runner.acme.plist`, loaded into your
  `gui/<uid>` session with `launchctl bootstrap`. It runs while you are logged in, which is
  what lets it use your login Keychain.
- **Linux:** `~/.config/systemd/user/agentic-runner-acme.service`, enabled with
  `systemctl --user enable --now`, plus `loginctl enable-linger` for your own user so it
  keeps running after a reboot with nobody logged in.

The Runner captures the `PATH` you installed from. If you install a CLI later, run
`agentic-runner install` again so the Runner can find it.

To drive a CLI through its ACP bridge, name its kind on `install`:
`--acp-cli-kinds codex_cli,claude_code`. The Runner keeps it in the Organisation's
`workstation.json`, and a later `install` without the flag keeps it too;
`--acp-cli-kinds ''` turns ACP off. Set it this way, not in the plist or the unit file:
`install` rewrites both. A kind with no pinned bridge is refused.

## Sign in to Codex and Claude Code

Give your Runner your LLM access with one command, in your terminal:

```sh
agentic-runner sign-in claude     # or: sign-in codex; with no argument it asks about each
```

It takes no ids: your Runner serves one Contract, and the Runner learned which from the
control plane. If this computer runs Runners for more than one Organisation, add
`--org acme`. For each CLI it offers two things:

- **Your subscription.** The command runs the vendor's own sign-in as the Contract, in the
  Contract's own directory under the state root, connected to your terminal. It does not
  read your own `~/.codex` or `~/.claude`. Do what the CLI asks: open the link and approve
  in your browser. Codex (`codex login --device-auth`) shows a code in the terminal to
  enter in the browser. The code and the login stay on this computer; nothing goes through
  the console. Claude Code signs in one of two ways:
  - **Long-lived token** (the default, `--method oauth_token`). Claude prints a token that
    lasts a year, and the Runner saves it in the Contract's directory, readable by that
    Contract only.
  - **Claude.ai sign-in** (`--method claude_ai`). A login that renews itself until you sign
    out. On macOS the Runner keeps it in a keychain of the Contract's own, not in your
    login keychain.

  The command refuses a subscription when your Contract does not allow your own
  subscription, or you have not acknowledged the subscription terms in the console, and
  says which.
- **An API key.** Typed without echo, and kept in this Organisation's store (the login
  keychain on macOS) under the provider's key name: `ANTHROPIC_API_KEY` or
  `OPENAI_API_KEY`. `credential set` puts one there too. The Runner checks it with the
  provider within one heartbeat and serves it to your Agents through its LLM proxy; the
  Agent's CLI never sees it.

The console shows which of the two each CLI has, from the Runner's heartbeat. To sign in
again, run the command again. A new sign-in replaces the old one.

## Give it work

`install` ends with a `next` line, and `agentic-runner status acme` repeats it while the
Runner is running:

1. Open **/me/runners** in the console. Your Runner is listed there, online.
2. Sign in, as above.
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
printf %s "$KEY" | agentic-runner credential set acme OPENAI_API_KEY    # your own LLM key
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

## Limit what Agents may use

A Runner Limit says what the Agents on your Runner must not use: tools, network
destinations and folders. It adds to the Grants, the Agent Runtime Profile's network policy
and the command policy. It never allows something they refuse. Each list is empty by
default, and an empty list limits nothing.

Write it in `config.toml` in the Organisation's directory (for example
`~/Library/Application Support/agentic-runner/acme/config.toml`), then restart the Runner:

```toml
[limits]
# Network destinations, in the network policy's syntax: host, host:port or *.domain.
networks = ["pastebin.com", "*.internal.example.com:443"]
# Absolute paths. `~` and `$HOME` are your home folder.
folders = ["~/clients", "~/Documents/taxes"]

# Tools: a Grant entry with `deny` only. A resource type from the Verb Catalogue
# (`repo`, `channel`, `work`) or `mcp:<server>`, a selector, and the verbs or `*`.
[[limits.tools]]
resource_type = "repo"
selector = "*"
verbs = { "pr.merge" = "deny" }
```

```sh
agentic-runner stop acme && agentic-runner start acme
```

The Runner reads the limit only when it starts. It refuses to start, and says which entry,
when an entry is wrong: a verb or resource type the Verb Catalogue does not have, a
destination that is not one, a folder that is not absolute or that contains the Workspaces,
or a `[limits]` table that does not parse. It never starts with an empty limit instead.

What happens to an Agent:

- **Tools.** A denied verb is refused at the verb seam and through `agentic-runner`'s own
  callbacks. A denied MCP server is not started. The Evidence names `runner_limit` as the
  deciding link.
- **Networks.** The egress proxy refuses a denied destination, also when the Profile allows
  it. A Directive whose git remote, LLM endpoint or MCP server is denied fails when it
  starts, and the Evidence names the entry (`networks[0]`). A hostname entry denies that
  name only. It does not deny the host's addresses or its other names, so deny those too
  if a Profile allows them. `pastebin.com` does not deny `www.pastebin.com`, and
  `*.pastebin.com` does not deny `pastebin.com`: to deny both, list both.
- **Folders.** A command or a permission request that names a path in a denied folder is
  refused. Claude Code also gets the folders as its own `permissions.deny` rules. The
  Runner resolves symlinks, `..`, `~` and `$HOME`, compares whole folder names (`/a/b`
  denies `/a/b/c`, not `/a/bc`), ignores case on a case-insensitive disk, and refuses a
  path with a variable it cannot expand or a wildcard that could reach a denied folder.
  The Evidence names the entry (`folders[1]`), never the path.

The console shows your limit on **/me/runners**, and how strongly each part holds on your
computer:

| Part | Posture | What it means |
| --- | --- | --- |
| Tools | `enforced` | Every verb needs a credential only the Runner holds. |
| Networks | `unenforced` | The proxy refuses what asks it. A program that ignores the proxy settings and opens its own connection is not stopped. |
| Folders | `checked` or `unenforced`, per CLI | `checked`: the CLI asks before it reads a path, and the Runner and the CLI both refuse. `unenforced`: the CLI reads paths without asking, so only the commands it asks about are checked. |

Folders are `checked` only for **Claude Code through its ACP bridge**
(`--acp-cli-kinds claude_code` on `install`, above). These CLIs read paths without asking, so
their folders are `unenforced`: **Codex** (through ACP or not), **Claude Code** without ACP,
and any CLI the Profile runs by its own ACP command.

`checked` is not a boundary. A script in the Workspace, or an interpreter the Agent runs,
reads paths that no command names. Only running each Contract as its own OS user stops
that, which a workstation Runner does not do. Keep files an Agent must not read off the
computer it runs on, or in a folder your user cannot read.

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
