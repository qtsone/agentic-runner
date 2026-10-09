"""Contracts shared by the platform and the Runner (ADR-0013 §4).

The distribution version is the Runner compatibility floor (ADR-0013 §7): major must
match, one minor behind warns, two behind holds new Directives. It ticks with this
package's own surface, never with a control-plane deploy — so it is declared here, in
the package the floor is *about*, and read back through ``agentic-runner --version``.
"""

# 2.1.0 (RR-09): new public names, no removals -- `github_port`, `redaction`, `incidents`
# and `runtime_context.WorkerRuntimeContextResolver`. A minor, so an installed Runner one
# behind only warns; a Runner built against 2.0 imported those names from its own package
# and keeps working until it is rebuilt.
# 2.2.0 (console-v2 issue 22): `SwarmSnapshot.participants` and the
# `swarm.participant_added` / `_removed` Evidence names. Additive with a default, so a
# Runner one minor behind only warns.
# 2.3.0 (console-v2 issue 23): the `report` Work Record kind -- `KIND_REPORT`,
# `WAKE_REPORT`, `EpicPlan.needs_human`, `EPIC_PLAN_REPORTED`, `report` in `OUTCOME_KINDS`
# and in the runtime context's `kind`. Additive; a Runner one behind refuses a report's
# runtime context, which fails that Directive closed rather than running it in a checkout.
# 2.4.0 (QTS-1253a): the Runner-side GitHub calls the platform App made -- the
# `RequestReview`/`ChangedFiles`/`PullRequestComment`/`PullRequestClose`/`RepositoryProbe`
# activity I/O, `GitHubCallError`, and `GitHubClient.read_repository`. Additive; but a
# Runner one minor behind has none of the five activities, so the platform must not
# schedule them on it -- roll the Runner image out before the platform change.
# 2.5.0 (ADR-0018 §3, §4): the Grant snapshot carries ``reach`` and each registry row its
# ``product_id``. Additive; an older payload parses with both empty.
# 2.6.0 (console-v2 issue 29): `HeartbeatEnvelope.tool_servers` and `ToolServerHealth`.
# Additive; the envelope omits the key while it is empty, so a control plane on 2.5 parses
# a beat unchanged until the Runner has started a Tool Server -- upgrade the control plane
# before the Runner.
# 2.7.0 (console-v2 issue 28): `WorkerRuntimeContext.skills` and `SkillVersionSpec`.
# Additive; an older payload parses with no Skills. But the context forbids extra keys,
# so a Runner one minor behind refuses a context that carries `skills` -- the platform
# must not send the field until its Runners are on 2.7.
# 3.5.0 (local-agents 12): `WorkerRuntimeContext.cli_kind` widens from the two known
# kinds to the registration pattern `^[a-z][a-z0-9_]{0,31}$`. Additive: every payload that
# parsed still parses; a Runner one minor behind refuses a context naming a new kind, which
# fails that Directive closed, and routing sends a kind only to a Runner that reports it.
# 3.6.0 (local-agents 05): `ContractDeviceLoginInput.method`,
# `ContractDeviceLoginResult.sign_in_id`, `ContractDeviceLoginStatusResult.auth_method` /
# `subscription_type`, `SealedSignInCode` on `HeartbeatAck.sign_in_codes` and
# `SignInCodeRelay` on `HeartbeatEnvelope.sign_in_codes`. Additive; the envelope omits the
# key while it is empty. But the ack forbids extra keys, so the platform must not relay a
# code to a Runner below 3.6.
# 3.7.0 (local-agents 10): `HarnessVersion.usage_windows` and `UsageWindow`. Additive; the
# row omits the key while it is absent, so a control plane on 3.6 parses a beat unchanged
# until a user-hosted Runner has read a subscription's windows -- upgrade the control
# plane before the Runner.
# 3.8.0 (console-v2 issue 30): `oauth_connector` -- `OAuthStart`, `SealedOAuthCode` and
# `OAuthDisconnect` on `HeartbeatAck.oauth_starts` / `oauth_codes` / `oauth_disconnects`,
# and `OAuthAuthorizeUrl`, token `SealedCredential`s and `OAuthOutcome` on
# `HeartbeatEnvelope.oauth_authorizations` / `oauth_tokens` / `oauth_outcomes`. Additive;
# the envelope omits each key while it is empty. But the ack forbids extra keys, so the
# platform must not send an OAuth item to a Runner below 3.8.
# 3.10.0 (console-v2 issue 15): `public_metadata.routine_schedule_id`. Additive, and only
# the platform builds the name; a Runner never sees a Routine.
# 3.11.0 (ADR-0018 §5-§7, QTS-1106): `ProductBinding` on `FixDirectiveOutput.product_binding`,
# `ContextAssemblyInput.after_binding`, `grants.READ_VERB` / `BRANCH_VERB`.
# Additive with a default. A Runner one minor behind has no `repo read` / `repo branch`
# callback, so an Organisation-scoped Work Record on it reads and writes nothing and ends
# Organisation-scoped; the platform's bind endpoint must ship before the Runner calls it.
# 3.12.0 (QTS-1322): `CliVersion.acp_bridge` and `AcpBridgeVersion`. Additive; the row
# omits the key while it is absent, so a control plane on 3.11 parses an attestation
# unchanged until a Runner serves a kind through its pinned ACP bridge (`ACP_CLI_KINDS`) --
# upgrade the control plane before the Runner.
__version__ = "3.12.0"

__all__ = ["__version__"]
