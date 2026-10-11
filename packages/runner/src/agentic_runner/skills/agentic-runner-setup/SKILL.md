---
name: agentic-runner-setup
description: Install and register an Agentic OS Runner on this machine, a Docker host or a Kubernetes cluster, put the Contract's LLM credential in place, and confirm the Runner is online. Use when the person asks to set up, install or connect a Runner, or pastes the prompt from the console's welcome page.
license: AGPL-3.0-only
---

# Set up an Agentic OS Runner

You help a person who has never run a Runner. A **Runner** is the program that does their
Agents' work: it runs Codex or Claude Code on a machine they choose and connects out to the
Agentic OS control plane. It belongs to one **Organisation** (the company the work is for).

The install documents in the `qtsone/agentic-runner` repository are the source for every
command. Do not reconstruct a command from memory: open the section named in each step and
follow it.

## What the person gives you

The prompt they pasted from the console holds these. Ask for any that is missing.

- **Organisation**: a short local name for it, lowercase letters, digits and `-` (`acme`).
- **Control plane URL**: the Runner's `controlPlane.url`, copied from the console's
  Runners page. It is the only control plane address you use, and only as a value you
  pass to the Runner. Never call it or any other control plane address yourself.
- **Temporal address**: `host:port`, from the same page.
- **Agent Token**: the one-time credential the Runner exchanges for its own identity when
  it registers. It may be absent: the person can paste it themselves at the Runner's
  prompt.

## Rules

- **Say before you run.** Before each command, show the person the exact command and what
  it does, in one line.
- **Stop on the first failure.** Show the exact error, link the doc section below that
  covers it, and stop. Do not retry with other flags, do not work around it.
- **The Agent Token and every credential value stay secret.** Never print, echo, log or
  repeat one, never put one in a command's arguments, and never write one to a file. The
  only way you hand a value to the Runner is the environment of the one command that reads
  it, or its standard input. When the person's install way needs the token in a file or a
  Kubernetes Secret, the person types it there, not you.
- You use only the `agentic-runner` command, the standard tools named in the docs (`uv`,
  `docker`, `kubectl`, `helm`) and read-only checks such as `command -v`.

## Steps

### 1. Look at the machine

Run and report, in one short list:

```sh
uname -sm
command -v uv python3 docker kubectl helm codex claude
```

A Runner does its work with **Codex** or **Claude Code** (each is an Agent Runtime). If the
way the person picks runs on this machine and neither `codex` nor `claude` is found, say
so and link their install pages
(https://developers.openai.com/codex/cli, https://code.claude.com/docs/en/setup). You do
not install them.

### 2. Ask which way

Ask the person to pick one, and open the matching document:

| Way | When | Document |
| --- | --- | --- |
| This machine | A laptop or desktop they use every day. Needs `uv`. | https://github.com/qtsone/agentic-runner/blob/main/docs/workstation.md |
| Docker | A server or always-on machine with Docker. | https://github.com/qtsone/agentic-runner/blob/main/docs/docker.md |
| Kubernetes | A cluster they run, with `kubectl` and `helm`. | https://github.com/qtsone/agentic-runner/blob/main/docs/helm.md |

Read its "Before you start" section with the person, and stop if something on it is
missing.

### 3. Install and register

**This machine.** Follow
https://github.com/qtsone/agentic-runner/blob/main/docs/workstation.md#install. Install the
Runner, then register it for the Organisation, giving the Agent Token in the environment of
that one command:

```sh
uv tool install agentic-runner
AGENTIC_AGENT_TOKEN='<agent-token>' agentic-runner install <org> --control-plane <control-plane-url> --temporal-address <temporal-address>
```

If the person did not give you the token, run the same command without
`AGENTIC_AGENT_TOKEN=…` and ask them to run it in their own terminal, where the Runner asks
for the token without showing it.

**Docker.** Follow
https://github.com/qtsone/agentic-runner/blob/main/docs/docker.md#the-env-file and then
https://github.com/qtsone/agentic-runner/blob/main/docs/docker.md#docker-run. Create
`runner.env` with the URL and the address filled in, and ask the person to add the Agent
Token line to it themselves.

**Kubernetes.** Follow
https://github.com/qtsone/agentic-runner/blob/main/docs/helm.md#install. The person creates
the token Secret themselves (the `read -rs` lines in that section). You run the `helm`
command.

`registration refused (…)` means the token is revoked or expired, the Account is on hold,
or the Organisation has reached its Runner limit: the person mints a new token in the
console. Other errors: the "Check it" section of the same document.

### 4. Put the Contract's LLM credential in place

A **Contract** is the agreement under which the person works for the Organisation. It says
whose LLM key or subscription the Agents spend. Ask which of the two the Contract uses.

- **An API key (a Credential Reference).** A Credential Reference is the name of a secret
  whose value lives only on the Runner. On this machine, ask the person to run, in their own
  terminal, the line from
  https://github.com/qtsone/agentic-runner/blob/main/docs/workstation.md#operate:

  ```sh
  printf %s "$VALUE" | agentic-runner credential set <org> <reference>
  ```

  For Docker and Kubernetes the value is delivered from the console, sealed to the Runner:
  https://github.com/qtsone/agentic-runner/blob/main/docs/helm.md#the-recipient-key.
- **A Codex or Claude Code subscription.** This works only on a Runner the person hosts
  themselves. Send them to the Contract's page in the console and its **Connect** button:
  the console opens the vendor's sign-in in their browser and the Runner finishes it. See
  https://github.com/qtsone/agentic-runner/blob/main/docs/workstation.md#sign-in-to-codex-and-claude-code.
  You have no sign-in of your own to run, and you do not run `codex login` or
  `claude auth login`.

### 5. Wait for the Runner to come online

On this machine, run every 10 seconds, for at most 3 minutes:

```sh
agentic-runner status <org>
```

The Runner is online when `process` says `running` and `heartbeat` shows a number of
seconds, not `never`. For Docker and Kubernetes, use the commands in the document's "Check
it" section. If 3 minutes pass, stop and show the last output and the "Check it" link.

### 6. Report

One screen, nothing more:

- the way, the Organisation and the Runner's identity line from `status`;
- whether the LLM credential is in place, or the one thing the person still does in the
  console;
- the `next` line `status` prints: where in the console to see the Runner and give it work.
