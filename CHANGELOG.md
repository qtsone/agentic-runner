# Changelog

All notable changes to this project will be documented in this file.

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
