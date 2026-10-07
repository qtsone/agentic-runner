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
__version__ = "2.5.0"

__all__ = ["__version__"]
