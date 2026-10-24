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
# 3.14.0 (QTS-1448): the sign-in-code relay is gone -- `HeartbeatAck.sign_in_codes`,
# `HeartbeatEnvelope.sign_in_codes`, `SealedSignInCode`, `SignInCodeRelay`,
# `SignInCodeOutcome`, `sign_in_code_binding`, `ContractDeviceLoginInput.method` and
# `ContractDeviceLoginResult.sign_in_id` (Temporal drops a key a dataclass lacks). A
# removal, and a minor on purpose: a major would refuse every 3.x Runner at bootstrap, and
# the wire stays compatible both ways once the control plane relays no code (QTS-1350). A
# Runner on 3.13 parses an ack without the key, and sends none while no code reaches it. A
# Runner on 3.14 drops the empty `"sign_in_codes": []` a control plane on 3.13 still
# sends, and refuses an ack that carries a code. A Claude Code sign-in is now
# `agentic-runner sign-in`, on the Runner.
# 3.16.0 (QTS-1494, ADR-0021): the first version that runs the Agent's model. The runtime
# context's `model` is now the model to run, blank for the Runner's default for its
# `cli_kind`; the field's shape is unchanged, so only routing tells the versions apart, and
# the platform sends a Work Record whose Agent names a model to no Runner below 3.16.
# `BootstrapRequest.registration_token` beside `agent_token`, exactly one of them, and
# `BootstrapRequest.token`. A Runner on 3.16 sends `registration_token`, which a control
# plane on 3.15 refuses -- upgrade the control plane before the Runner.
# 3.17.0 (QTS-1526, ADR-0022 §4, §11, §22, §36): `routing.SelectorRequirement` and the
# set operators; `DirectiveRouting.requirements` and `ProductReach.runner_requirements`
# beside the legacy maps, so a Runner on 3.16 still decodes every payload and checks the
# legacy map alone; `DirectiveRouting.funding_party`; Grant catalogue v6 (`work.start`,
# `work.name_runner`); `BootstrapResponse.temporal_address`. The response forbids extra
# keys, so the control plane must send the address only to a Runner whose bootstrap
# names contracts 3.17 or later. A minor although the Python surface breaks --
# `selector_satisfied` and `narrow_selector` take and return requirements, and
# `GrantSnapshot.runner_selector_of` returns them -- because the wire does not: the only
# caller of those functions is the control plane, which moves both pins in one PR (RR-04).
# A Runner on 3.16 ignores the requirements and checks the legacy map alone, which is
# safe only because routing itself is the control plane's.
# 3.19.0 (QTS-1531, ADR-0022 §7): `HeartbeatAck.credential_manifests` and
# `CredentialManifest`; `CredentialValidity.contract_id`, required, and the envelope's
# `credentials` bound raised to one entry per reported reference. A Runner on 3.18 sent
# `credentials` empty, so the required field breaks no envelope that parsed. The ack
# forbids extra keys, so the control plane sends `credential_manifests` only to a Runner
# whose heartbeat names contracts 3.19 or later, and omits the key while it is empty.
# 3.20.0 (QTS-1553, ADR-0022 §13): `HeartbeatAck.tags`, the Runner's Tags as the console
# last edited them; the Runner checks routing against them. Additive, and omitted while
# None. The ack forbids extra keys, so the control plane sends `tags` only to a Runner
# whose heartbeat names contracts 3.20 or later, and refuses a Tag edit on any other: a
# Runner below 3.20 keeps checking its bootstrap Tags and would refuse work routed on an
# added one.
# 3.21.0 (QTS-1561, ADR-0022 §52-§58): the Runner Limit -- `runner_limit.RunnerLimit`,
# `RunnerLimitReport` and `RunnerLimitPosture` on `BootstrapRequest.runner_limit` and
# `HeartbeatEnvelope.runner_limit`; `GrantLink.RUNNER_LIMIT` and the evaluator's
# `runner_limit` link, also taken by `decide_verb` and `decide_mcp_server`. Additive and
# omitted while None, but both bodies forbid extra keys, so the control plane moves to
# 3.21 before a Runner reports a limit. The new Runner compatibility floor.
# 3.23.0 (QTS-1706): `GrantPush.contract_id` and `AppliedSnapshot.contract_id` -- the
# Runner holds one Grant snapshot per Agent and Contract, so an Organisation Agent runs
# under two Owners' Contracts at once. Additive and omitted while None. The ack forbids
# extra keys, so the control plane keys its pushes only for a Runner whose heartbeat names
# contracts 3.23 or later; a Runner on 3.23 under an older control plane reads an unkeyed
# snapshot only for the Contract the snapshot itself names.
# 3.25.0 (QTS-1736, Spec: Runners rev 3): a Runner uses and reports its host's own LLM
# access. `llm_access` -- the key-name table (`RUNTIME_PROVIDERS`, `PROVIDER_KEY_NAMES`)
# and `LlmAccess`; `DirectiveRouting.llm_access` in place of `funding_party`;
# `HeartbeatEnvelope.llm_access` (`ContractLlmAccess`) and `.install` (`RunnerInstall`);
# `HeartbeatAck.llm_contracts` (`ContractLlmTerms`, `SubscriptionRight`). Removed:
# `CredentialManifest.funding_party` and `CeilingPush.org_funded`, both still read from a
# control plane on 3.24 (an `org_funded: false` push carries no Organisation ceiling).
# The ack forbids extra keys, so the control plane sends `llm_contracts` only to a Runner
# whose heartbeat names contracts 3.25 or later; the Runner sends its report only once an
# ack carried it. Raises the floor in practice: once the control plane is on 3.25, a
# Runner that reports no LLM access gets no work.
# 3.26.0 (QTS-1745, Spec: Runners rev 3): a pool Runner opens the pool's LLM Keys with its
# Pool Key. `llm_key` -- the two shares (`split_llm_key`, `join_llm_key`), their bindings
# under `agentic-os/llm-key/v1`, `LlmKeyReleaseRequest` and `LlmKeyRelease`, and
# `fixtures/llm-key.fixture.json` for the console's tests; `HeartbeatAck.pool`
# (`PoolTerms`) and `HeartbeatEnvelope.pool_key_fingerprint`. Additive and omitted while
# None. Both bodies forbid extra keys, so the control plane sends `pool` only to a Runner
# whose heartbeat names contracts 3.26 or later, and the Runner reports its fingerprint
# only once an ack carried `pool`. Raises the floor for pool Runners in practice: one
# below 3.26 cannot open an LLM Key, so routing sends it no `llm_key` Directive.
# 3.27.0 (QTS-1773, Spec: Runners rev 4): a workstation Runner's name. `RunnerInstall.name`
# (`RunnerName`, `RUNNER_NAME_PATTERN`) and `HeartbeatAck.contracts_version`, the control
# plane's own. Additive and omitted while None. The control plane sends its version only
# to a Runner whose heartbeat names contracts 3.27 or later, and the Runner reports its
# name only once an ack named a control plane on 3.27 or later. Does not raise the floor.
# 3.29.0 (QTS-1909): names for `agentic-runner list`. `HeartbeatAck.organisation_name` and
# `ContractLlmTerms.contract_name` (`DisplayName`). Additive and omitted while None. The
# control plane sends them only to a Runner whose heartbeat names contracts 3.29 or later;
# a Runner without them lists the ids. Does not raise the floor.
# 3.31.0 (QTS-2252, Spec: Runners rev 14): a person's Runner and its connections.
# `runner_login` -- the device-flow login (`DeviceLoginRequest`, `DeviceAuthorization`,
# `DeviceTokenRequest`, `LoginResponse`) and `connect` (`ConnectableOrganisations`,
# `ConnectRequest`) with their paths; `RunnerConnection` on `BootstrapResponse.connections`
# and `HeartbeatAck.connections`, with `operator_user_profile_id` on both; and
# `ContractLlmTerms.organisation_id` / `organisation_name`. Additive and omitted while None.
# Both bodies forbid extra keys, so the control plane sends the new fields only to a Runner
# whose bootstrap or heartbeat names contracts 3.31 or later; a Runner given no connections
# polls the one namespace it registered in, as before. Does not raise the floor, but only a
# Runner on 3.31 can log in or serve more than one Organisation.
# 3.33.0 (QTS-2392, Spec: chat intake rev 2): Listeners on a member's own connected Slack.
# `listeners` -- `ListenerAssignment`, `ListenerMatchSpec`, `ListenerContextOptions`,
# `ListenerDraftRequest` on `INTAKE_DRAFTS_PATH`, and the firing prompt, template fill and
# tool-call shape the platform's own Tool Server uses; `UserSourceAssignment.listeners`,
# `.product_name` and `.member_ids`; `IntakeWorkRequest.listener_id` and
# `IntakeIgnoredReport.listener_id`; `classification_source: automatic`. Additive:
# `intake_filter` still reads. The ack forbids extra keys, so the control plane sends the
# three assignment fields only to a Runner whose heartbeat names contracts 3.33 or later;
# the Runner sends `listener_id` only on a Listener's firing, which only that ack starts.
__version__ = "3.34.0"

__all__ = ["__version__"]
