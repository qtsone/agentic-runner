# Changelog

All notable changes to this project will be documented in this file.

# [3.5.0](https://github.com/qtsone/agentic-runner/compare/v3.4.2...v3.5.0) (2026-10-09)


### Features

* **runner:** the ACP Agent Runtime drives the pinned codex-acp and claude-agent-acp bridges (QTS-893) ([#32](https://github.com/qtsone/agentic-runner/issues/32)) ([c954c59](https://github.com/qtsone/agentic-runner/commit/c954c59e496c5963f1ab98ad2e86539c43301a4f))

## [3.4.2](https://github.com/qtsone/agentic-runner/compare/v3.4.1...v3.4.2) (2026-10-08)


### Bug Fixes

* **runner:** spawn without RLIMIT_NPROC where no Contract uid holds it (QTS-1321) ([#35](https://github.com/qtsone/agentic-runner/issues/35)) ([eaedae8](https://github.com/qtsone/agentic-runner/commit/eaedae8d1424de192e581d1a3bdf9ad4a2888383))

## [3.4.1](https://github.com/qtsone/agentic-runner/compare/v3.4.0...v3.4.1) (2026-10-08)


### Bug Fixes

* **runner:** spawn without the memory ceiling where macOS refuses RLIMIT_DATA (QTS-1319) ([#34](https://github.com/qtsone/agentic-runner/issues/34)) ([63fe3ca](https://github.com/qtsone/agentic-runner/commit/63fe3ca1b5be628a3e292352f69c857c2f7922ca))

# [3.4.0](https://github.com/qtsone/agentic-runner/compare/v3.3.0...v3.4.0) (2026-10-08)


### Features

* **runner:** classify a subscription usage limit and an expired sign-in (QTS-891) ([#33](https://github.com/qtsone/agentic-runner/issues/33)) ([218c329](https://github.com/qtsone/agentic-runner/commit/218c329a87ce4129d051c74ede42d7395cb3110d))
* **runner:** harness capability descriptor and per-Contract self-test in the heartbeat (QTS-894) ([#31](https://github.com/qtsone/agentic-runner/issues/31)) ([1f15b21](https://github.com/qtsone/agentic-runner/commit/1f15b2199fe965c84a43bcd701ac92494742a840))

# [3.3.0](https://github.com/qtsone/agentic-runner/compare/v3.2.0...v3.3.0) (2026-10-08)


### Features

* **runner:** a shared Runner runs API keys only; Codex API-key mode; LLM slot from a delivered key (QTS-885, QTS-1287) ([#30](https://github.com/qtsone/agentic-runner/issues/30)) ([632b9b3](https://github.com/qtsone/agentic-runner/commit/632b9b30e2fd70c611db5dfe0989c96988e47958)), closes [#321](https://github.com/qtsone/agentic-runner/issues/321) [#322](https://github.com/qtsone/agentic-runner/issues/322) [#321](https://github.com/qtsone/agentic-runner/issues/321) [#317](https://github.com/qtsone/agentic-runner/issues/317) [#321](https://github.com/qtsone/agentic-runner/issues/321)

# [3.2.0](https://github.com/qtsone/agentic-runner/compare/v3.1.1...v3.2.0) (2026-10-08)


### Features

* **workstation:** install and status point to the console's next step (QTS-1314) ([#29](https://github.com/qtsone/agentic-runner/issues/29)) ([efe0f36](https://github.com/qtsone/agentic-runner/commit/efe0f362eadda0a41a7cfd5e451d0617b4620c72))

## [3.1.1](https://github.com/qtsone/agentic-runner/compare/v3.1.0...v3.1.1) (2026-10-08)


### Bug Fixes

* **runner:** name a control plane older than the Runner instead of a raw 422 (RR-13) ([#24](https://github.com/qtsone/agentic-runner/issues/24)) ([950ad65](https://github.com/qtsone/agentic-runner/commit/950ad6587be55f11590956f88388b120ede8c51b))

# [3.1.0](https://github.com/qtsone/agentic-runner/compare/v3.0.0...v3.1.0) (2026-10-08)


### Features

* **runner:** every Runner reports a 64-hex build_id the release publishes (RR-06) ([#21](https://github.com/qtsone/agentic-runner/issues/21)) ([a48f573](https://github.com/qtsone/agentic-runner/commit/a48f573676cdeee01b9e73b56ec7a2d863f97eb5))

# [3.0.0](https://github.com/qtsone/agentic-runner/compare/v2.8.0...v3.0.0) (2026-10-08)


### Bug Fixes

* **state:** private, atomic state directory; refuse an unsafe one at start (RR-08) ([#16](https://github.com/qtsone/agentic-runner/issues/16)) ([8babcb4](https://github.com/qtsone/agentic-runner/commit/8babcb4669666fcfe7157311d8d370d729510f3c))


### Features

* **runner:** resume the harness session when a Directive attempt retries (local-agents 16) ([#19](https://github.com/qtsone/agentic-runner/issues/19)) ([55146de](https://github.com/qtsone/agentic-runner/commit/55146de334b5cd3a04b19baab41f9f12713f590f))


### BREAKING CHANGES

* **state:** the Runner refuses to start on a state directory that is not 0700, or on a state file that is not 0600, that is a symlink, or that another uid owns. Fix a volume from an earlier release with `chmod 700 <state>`, `chmod 600 <state>/*.json <state>/runner.pid`, and `chown` to the Runner's uid.

# [2.8.0](https://github.com/qtsone/agentic-runner/compare/v2.7.0...v2.8.0) (2026-10-08)


### Features

* **testing:** publish the fake control plane as a conformance kit (RR-07) ([#17](https://github.com/qtsone/agentic-runner/issues/17)) ([0c70412](https://github.com/qtsone/agentic-runner/commit/0c704126ae83a4f2ce529dcd041fff8fca2284fe)), closes [#15](https://github.com/qtsone/agentic-runner/issues/15)

# [2.7.0](https://github.com/qtsone/agentic-runner/compare/v2.6.1...v2.7.0) (2026-10-08)


### Features

* **runner:** deliver attached Skills to the Agent Runtime (contracts 2.7.0) ([#18](https://github.com/qtsone/agentic-runner/issues/18)) ([504fd32](https://github.com/qtsone/agentic-runner/commit/504fd32b8e13aaf75abb131765af50ea2bfa2d82)), closes [#3](https://github.com/qtsone/agentic-runner/issues/3) [#3](https://github.com/qtsone/agentic-runner/issues/3) [#11](https://github.com/qtsone/agentic-runner/issues/11)

## [2.6.1](https://github.com/qtsone/agentic-runner/compare/v2.6.0...v2.6.1) (2026-10-08)


### Bug Fixes

* **runner:** refuse a retry while the prior attempt's process group lives (RR-05) ([#15](https://github.com/qtsone/agentic-runner/issues/15)) ([48ff4bb](https://github.com/qtsone/agentic-runner/commit/48ff4bbae760b868d023ce278060c8e29ebee60a))

# [2.6.0](https://github.com/qtsone/agentic-runner/compare/v2.5.0...v2.6.0) (2026-10-08)


### Features

* **heartbeat:** report whether each Tool Server started (console-v2 issue 29) ([#11](https://github.com/qtsone/agentic-runner/issues/11)) ([dfcb9ae](https://github.com/qtsone/agentic-runner/commit/dfcb9ae4b10c83be93195f8d1cd938df6035d27f)), closes [#3](https://github.com/qtsone/agentic-runner/issues/3)
