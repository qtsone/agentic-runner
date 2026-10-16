# Changelog

All notable changes to this project will be documented in this file.

# [3.24.0](https://github.com/qtsone/agentic-runner/compare/v3.23.1...v3.24.0) (2026-10-16)


### Features

* **claude:** run a Claude subscription on the default Claude Code runtime (QTS-1737) ([#70](https://github.com/qtsone/agentic-runner/issues/70)) ([57677c9](https://github.com/qtsone/agentic-runner/commit/57677c92c264ead881b1d436f9f13d9984e476db))

## [3.23.1](https://github.com/qtsone/agentic-runner/compare/v3.23.0...v3.23.1) (2026-10-15)


### Bug Fixes

* **image:** link the agentic-runner GHCR package to this repository (QTS-1711) ([#67](https://github.com/qtsone/agentic-runner/issues/67)) ([73b8936](https://github.com/qtsone/agentic-runner/commit/73b8936af64b06af087c005fd98b733a97d9d734))

# [3.23.0](https://github.com/qtsone/agentic-runner/compare/v3.22.0...v3.23.0) (2026-10-15)


### Features

* **runner:** hold a Grant snapshot per Agent and Contract (QTS-1706) ([#66](https://github.com/qtsone/agentic-runner/issues/66)) ([34803c8](https://github.com/qtsone/agentic-runner/commit/34803c82407323b5a14e696fc07824c8c2127851))

# [3.22.0](https://github.com/qtsone/agentic-runner/compare/v3.21.1...v3.22.0) (2026-10-14)


### Features

* **runner:** apply and report the Runner Limit (QTS-1562) ([#65](https://github.com/qtsone/agentic-runner/issues/65)) ([9521f99](https://github.com/qtsone/agentic-runner/commit/9521f99652f67fa0f80b47c52708034972b1db41)), closes [#63](https://github.com/qtsone/agentic-runner/issues/63) [#63](https://github.com/qtsone/agentic-runner/issues/63)

## [3.21.1](https://github.com/qtsone/agentic-runner/compare/v3.21.0...v3.21.1) (2026-10-14)


### Bug Fixes

* **runner:** the Learner prompt reads a hand-off as a hand-off, not an ending (QTS-1574) ([#64](https://github.com/qtsone/agentic-runner/issues/64)) ([f9a030a](https://github.com/qtsone/agentic-runner/commit/f9a030afe8b8d1862c130b26362f100708081f43)), closes [#477](https://github.com/qtsone/agentic-runner/issues/477)

# [3.21.0](https://github.com/qtsone/agentic-runner/compare/v3.20.0...v3.21.0) (2026-10-14)


### Features

* **contracts:** Runner Limit model, wire field and runner_limit link (QTS-1561) ([#63](https://github.com/qtsone/agentic-runner/issues/63)) ([0759bcf](https://github.com/qtsone/agentic-runner/commit/0759bcf1ae0e09bdb6806fe769be4540bf420df5))

# [3.20.0](https://github.com/qtsone/agentic-runner/compare/v3.19.0...v3.20.0) (2026-10-14)


### Features

* **runner:** adopt the Runner Tags each heartbeat ack carries (QTS-1553) ([#62](https://github.com/qtsone/agentic-runner/issues/62)) ([e381492](https://github.com/qtsone/agentic-runner/commit/e38149237491f1b1a9a66eb9509d078e19c4bd47))

# [3.19.0](https://github.com/qtsone/agentic-runner/compare/v3.18.3...v3.19.0) (2026-10-14)


### Features

* **runner:** report Credential References per Contract; lend host keys only to host-funded work (QTS-1531) ([#61](https://github.com/qtsone/agentic-runner/issues/61)) ([4f0d26d](https://github.com/qtsone/agentic-runner/commit/4f0d26d34bfff0e0c8c2b1bfc422b1fcb58b9695))

## [3.18.3](https://github.com/qtsone/agentic-runner/compare/v3.18.2...v3.18.3) (2026-10-13)


### Bug Fixes

* **runner:** install without --temporal-address (QTS-1540) ([#60](https://github.com/qtsone/agentic-runner/issues/60)) ([dbe31aa](https://github.com/qtsone/agentic-runner/commit/dbe31aac3ca9f0cd64a66e3ee09cf93634148cca))

## [3.18.2](https://github.com/qtsone/agentic-runner/compare/v3.18.1...v3.18.2) (2026-10-13)


### Bug Fixes

* **runner:** drop the workstation `add` command; a second Runner is a second install (QTS-1468) ([#59](https://github.com/qtsone/agentic-runner/issues/59)) ([ea7187f](https://github.com/qtsone/agentic-runner/commit/ea7187f7a52ceafcf662501e55001797abd92c8e)), closes [#56](https://github.com/qtsone/agentic-runner/issues/56)

## [3.18.1](https://github.com/qtsone/agentic-runner/compare/v3.18.0...v3.18.1) (2026-10-13)


### Bug Fixes

* **runner:** exit promptly on a SIGTERM during start-up (QTS-1536) ([#58](https://github.com/qtsone/agentic-runner/issues/58)) ([6c3fb38](https://github.com/qtsone/agentic-runner/commit/6c3fb38108b07b0fda4eed4e66dcb87c1c366bfa)), closes [#56](https://github.com/qtsone/agentic-runner/issues/56)

# [3.18.0](https://github.com/qtsone/agentic-runner/compare/v3.17.0...v3.18.0) (2026-10-13)


### Features

* **runner:** add a personal Runner beside an existing workstation install (QTS-1528) ([#56](https://github.com/qtsone/agentic-runner/issues/56)) ([3ab91e4](https://github.com/qtsone/agentic-runner/commit/3ab91e4ff22dae20d2d2aaecab3a7bad416b7d0c))

# [3.17.0](https://github.com/qtsone/agentic-runner/compare/v3.16.2...v3.17.0) (2026-10-13)


### Features

* **contracts:** selector requirements, Agent verbs, Temporal address and funding party (QTS-1526) ([#57](https://github.com/qtsone/agentic-runner/issues/57)) ([cb57343](https://github.com/qtsone/agentic-runner/commit/cb5734321024cd693cb1495617094152c40e000c))

## [3.16.2](https://github.com/qtsone/agentic-runner/compare/v3.16.1...v3.16.2) (2026-10-13)


### Bug Fixes

* **security:** keep the host provider key out of the Claude Code Agent Runtime (QTS-1520) ([#55](https://github.com/qtsone/agentic-runner/issues/55)) ([1d8d425](https://github.com/qtsone/agentic-runner/commit/1d8d4250e78da15c60a81670afca7e16f521e799))

## [3.16.1](https://github.com/qtsone/agentic-runner/compare/v3.16.0...v3.16.1) (2026-10-13)


### Bug Fixes

* **runner:** the Directive's Evidence names the Agent Runtime that ran (QTS-1498) ([#54](https://github.com/qtsone/agentic-runner/issues/54)) ([3ae68ad](https://github.com/qtsone/agentic-runner/commit/3ae68adc5c27382ec1cb36d539cf405bf9ad6973))

# [3.16.0](https://github.com/qtsone/agentic-runner/compare/v3.15.1...v3.16.0) (2026-10-12)


### Features

* **runner:** the Agent's model runs; registration takes a Runner Registration Token (QTS-1494) ([#52](https://github.com/qtsone/agentic-runner/issues/52)) ([9c2633e](https://github.com/qtsone/agentic-runner/commit/9c2633e02e758eadb183a1d752f022b75648109d))

## [3.15.1](https://github.com/qtsone/agentic-runner/compare/v3.15.0...v3.15.1) (2026-10-11)


### Bug Fixes

* **runner:** require the contracts of the runner's own release (QTS-1473) ([#51](https://github.com/qtsone/agentic-runner/issues/51)) ([869fc32](https://github.com/qtsone/agentic-runner/commit/869fc32279aabc97da3cbea35f0c01d9d5183736))

# [3.15.0](https://github.com/qtsone/agentic-runner/compare/v3.14.0...v3.15.0) (2026-10-11)


### Features

* **runner:** subscription backstop keyed on Contract uid and harness login, not host party (QTS-1450) ([#49](https://github.com/qtsone/agentic-runner/issues/49)) ([3a7c19f](https://github.com/qtsone/agentic-runner/commit/3a7c19f647675acf01f54ad8eaa7bd8239ff007b))

# [3.14.0](https://github.com/qtsone/agentic-runner/compare/v3.13.0...v3.14.0) (2026-10-11)


### Features

* **runner:** `agentic-runner sign-in` runs Claude Code's own sign-in; the sign-in-code relay is removed (QTS-1448) ([#48](https://github.com/qtsone/agentic-runner/issues/48)) ([278ddad](https://github.com/qtsone/agentic-runner/commit/278ddad176b44dc47292f0be44210eac5c6e39a9))

# [3.13.0](https://github.com/qtsone/agentic-runner/compare/v3.12.2...v3.13.0) (2026-10-11)


### Features

* **runner:** agentic-runner-setup skill and `skill install|print` (QTS-924) ([#47](https://github.com/qtsone/agentic-runner/issues/47)) ([b3f7662](https://github.com/qtsone/agentic-runner/commit/b3f766238084c920b1b520c49e6b07642638a89c)), closes [qtsone/agentic-os#434](https://github.com/qtsone/agentic-os/issues/434)

## [3.12.2](https://github.com/qtsone/agentic-runner/compare/v3.12.1...v3.12.2) (2026-10-09)


### Bug Fixes

* **runner:** keep ACP_CLI_KINDS across a workstation reinstall (QTS-1355) ([#45](https://github.com/qtsone/agentic-runner/issues/45)) ([36e97dc](https://github.com/qtsone/agentic-runner/commit/36e97dcd060d5c290843343fb7de468869593998))

## [3.12.1](https://github.com/qtsone/agentic-runner/compare/v3.12.0...v3.12.1) (2026-10-09)


### Bug Fixes

* **runner:** close ACP gaps 1, 2 and 4 — granted MCP tools, harness-root settings.json, Codex usage (QTS-1323) ([#42](https://github.com/qtsone/agentic-runner/issues/42)) ([782ad45](https://github.com/qtsone/agentic-runner/commit/782ad453fccd6c241829cc1096a851ac84ee75c3))

# [3.12.0](https://github.com/qtsone/agentic-runner/compare/v3.11.0...v3.12.0) (2026-10-09)


### Features

* **contracts:** the attestation names each CLI's pinned ACP bridge (QTS-1322) ([#43](https://github.com/qtsone/agentic-runner/issues/43)) ([9340d8b](https://github.com/qtsone/agentic-runner/commit/9340d8b94543455b8ad3879649a1eb8901bacd5d)), closes [#32](https://github.com/qtsone/agentic-runner/issues/32)

# [3.11.0](https://github.com/qtsone/agentic-runner/compare/v3.10.0...v3.11.0) (2026-10-09)


### Features

* **runner:** repo read/branch, Product binding and the per-repository tag check (ADR-0018 §5-§7, QTS-1106) ([#41](https://github.com/qtsone/agentic-runner/issues/41)) ([187abcb](https://github.com/qtsone/agentic-runner/commit/187abcb75aa172e4b3fcb3a4c1d8eafbce9fcf21))

# [3.10.0](https://github.com/qtsone/agentic-runner/compare/v3.9.0...v3.10.0) (2026-10-09)


### Features

* **contracts:** public_metadata.routine_schedule_id for one Routine's Schedule (QTS-921) ([#40](https://github.com/qtsone/agentic-runner/issues/40)) ([3bc3e57](https://github.com/qtsone/agentic-runner/commit/3bc3e576388e4bf83f6b9fba45ea663c95325f10))

# [3.9.0](https://github.com/qtsone/agentic-runner/compare/v3.8.0...v3.9.0) (2026-10-09)


### Features

* **runner:** the ACP runtime runs a Profile-named command for an unpinned cli_kind (QTS-899) ([#39](https://github.com/qtsone/agentic-runner/issues/39)) ([5a64edc](https://github.com/qtsone/agentic-runner/commit/5a64edc74ac069f395a7a8895a451f5daefbe404))

# [3.8.0](https://github.com/qtsone/agentic-runner/compare/v3.7.0...v3.8.0) (2026-10-09)


### Features

* **runner:** OAuth on the Runner -- PKCE start, sealed code exchange, refresh, revoke (QTS-909) ([#38](https://github.com/qtsone/agentic-runner/issues/38)) ([c35e239](https://github.com/qtsone/agentic-runner/commit/c35e239d40d967650380a40ecb60f242dd6b8c54))

# [3.7.0](https://github.com/qtsone/agentic-runner/compare/v3.6.0...v3.7.0) (2026-10-09)


### Features

* **runner:** subscription usage windows in the heartbeat (QTS-892) ([#37](https://github.com/qtsone/agentic-runner/issues/37)) ([5aa8be5](https://github.com/qtsone/agentic-runner/commit/5aa8be5650d992b8b45c5904d1a9f68948b7b862))

# [3.6.0](https://github.com/qtsone/agentic-runner/compare/v3.5.0...v3.6.0) (2026-10-09)


### Features

* **runner:** Claude Code in-place sign-in per Contract, long-lived token by default (QTS-890) ([#36](https://github.com/qtsone/agentic-runner/issues/36)) ([26a1f62](https://github.com/qtsone/agentic-runner/commit/26a1f62c969f23c88a8e6367787e7ff474fddcc8)), closes [#32](https://github.com/qtsone/agentic-runner/issues/32)

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
