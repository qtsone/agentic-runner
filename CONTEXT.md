# Context: the terms a Runner uses

An excerpt of the Agentic OS domain glossary, copied verbatim and in its order: the terms
this repository's code and docs use. The platform's glossary is the source; when a definition
here disagrees with it, the platform's wins and this file is corrected.

**Organisation**:
A party that owns Products and contracts User Profiles to deploy Agents into it. The isolation boundary between tenants at every size; one Organisation has many Products, and every Organisation is owned by exactly one Account. An Organisation that stands for an off-platform client is a Virtual Organisation.
_Avoid_: Tenant, Company, Customer, Client, OU (an Enterprise's organisational unit is simply an Organisation)

**Contract**:
The agreement between one Organisation and one contractor — a User Profile, or an Account acting through its own people — under which Agents are deployed into the Organisation; at most one is active per pair. Proposed by either side by direct action and negotiated as versions until one side accepts as it stands, it fixes the Products in scope, the ceilings the Agents work under, the Experience promotion policy, which Account pays its seat, which party funds the LLM usage (the funder holds the key, pays the provider and sets the ceiling) and who hosts the Runner (the Organisation or the contractor). It holds policy, never credentials; its authority is issued as Approval Mandates.
_Avoid_: Engagement, Employment, Agreement, Persona Contract, Offer (that is a Proposal)

**Work Record**:
The central unit of work: one request against a Product, or against its Organisation until its first write binds a Product (only a Routine with an Agent and no Product opens one this way, with every Product in the Agent's Contract scope available to it; see ADR-0018), tracked from intake through its gates to a terminal outcome. Its Outcome depends on the handling Agent's Specialisation — a pull request lifecycle is the *development* Specialisation's Outcome, not an intrinsic property of every Work Record.
_Avoid_: Task, Ticket, Job, Request

**Evidence Event**:
An entry in the ordered, append-only record of what happened to a Work Record. The audit trail.
_Avoid_: Log, History, Trace, Audit entry

**Agent**:
A user-owned instance of a Persona deployed under exactly one Contract: the autonomous actor that takes on a Work Record, alone or as a member of a Swarm, and drives it to its Outcome. Its Specialisation comes from its Persona; it carries its own Grant, model and provider, and runs an Agent Runtime inside a Worker on the Runner registered to that Organisation.
_Avoid_: Engineer, Bot, Assistant, Deployment, Persona (that is the class)

**Ralph Loop**:
The iterative execution that drives a Work Record toward its Completion Criteria — a series of Directives run in a loop, not a single shot. Temporal owns the loop and schedules the series across the members of the Work Record's Swarm; it continues until the Completion Criteria are met, a gate fails terminally, or the Work Record's Budget is exhausted. (Today's workflow is the degenerate one-Agent, one-Directive case — see ADR-0007.)
_Avoid_: Run, Workflow, Retry loop

**Directive**:
A single instruction — one runtime turn — executed within a Ralph Loop. The Agent Runtime executes exactly one Directive per step; the agentic behaviour emerges from the Temporal-orchestrated series, not from an opaque internal runtime loop.
_Avoid_: Step, Prompt, Turn, Command

**Agent Runtime**:
The pluggable engine an Agent uses to do its work inside a Worker — e.g. the Codex CLI or Claude Code. Selected per Product or repository by an Agent Runtime Profile.
_Avoid_: Engine, CLI, Backend

**Agent Runtime Profile**:
A configuration template that selects which Agent Runtime, image, command policy, and network policy an Agent runs under for a given Product or repository.
_Avoid_: Profile, Config, Runtime config

**Runner**:
The open-source, activity-only Temporal worker process — registered to exactly one Organisation and hosted by that Organisation or by its contractor (a user's workstation or an Account's infrastructure) as the Contract says — that polls its own task queue in that Organisation's namespace on the platform-hosted Temporal server, executes Directives for the Agents deployed into that Organisation (many at once, one at a time per Work Record), performs every privileged verb on their behalf, and meters their LLM usage. It never runs the loop itself. It connects outbound-only to the control plane and to its Organisation's namespace under a short-lived, control-plane-minted credential that reaches that namespace and no other, resolves the Credential References it is handed against its own host, and is the sole place a Grant or a token ceiling is enforced at run time. One Runner is one process and one identity; an Organisation adds capacity by registering more Runners. Inside a Runner every Contract is a separate operating-system user: its Directives, verifier runs, hooks and Workspaces are its own and unreadable to any other Contract's; a Runner that cannot separate them serves one Contract at a time. A Runner is hosted by exactly one party and executes only Contracts that name that party as host.
_Avoid_: Edge, Data plane, Worker (that is the Temporal-level term inside a Runner), Agent host, Executor

**Platform Worker**:
The platform-hosted Temporal worker that runs the Ralph Loop's deterministic code and the control plane's own activities for every Organisation's namespace. It holds no org credential and performs no privileged verb; those belong to the Runner.
_Avoid_: Loop host, Conductor, Control-plane worker

**Agent Token**:
The reusable, per-Organisation credential an operator gives a new Runner process so it can register; each process exchanges it once for its own durable Runner identity. Revocable, optionally expiring; never used after registration.
_Avoid_: Bootstrap token, Registration key, API key

**Runner Token**:
The short-lived credential a Runner presents to the Temporal server so that it reaches its own Organisation's namespace and no other: an RS256 JWT the control plane signs and Temporal verifies against the platform's published JWKS, granting `write` on that one namespace and expiring within the hour. Minted at registration and again on every heartbeat, and passed as the connection's API key, rotated in place without reconnecting. Revoking a Runner stops the next refresh; rolling the signing key ends every outstanding token at once.
_Avoid_: Namespace token, Temporal API key, Worker token (the `worker` role is deliberately not what it claims), Directive token (that is the per-Directive control-plane credential), Agent Token (that is the reusable registration credential it is exchanged for)

**Runner Tag**:
A free-form key–value pair declared by a Runner at registration and stored on its record, by which the control plane chooses which Runner receives a Directive. Tags describe the Runner (environment, region, what it can run), never the work; they never appear in a queue name, a Search Attribute or the heartbeat.
_Avoid_: Label, Queue (that is the Runner's identity, not a tag), Selector (that is the Product's or Contract's requirement matched against tags)

**Credential Reference**:
The name, held by the control plane on a Contract's credential manifest, of a secret whose value lives only in the Runner's host store, put there by the host operator or by Credential Delivery. The Runner resolves it at Directive time for its LLM proxy, verb seams, hooks and out-of-tree MCP servers; the value never reaches the control plane in the clear or the Agent Runtime at all.
_Avoid_: Secret (that is the value), Credential (ambiguous between name and value), Env var

**Credential Delivery**:
How a credential value reaches a Runner when its holder is not the Runner's host operator: the holder seals it in their browser to the installation's Recipient Key, the control plane stores and relays ciphertext it cannot open, and the Runner opens it on the host. Carries API keys only: a long-lived subscription bearer (`setup_token`) is never delivered and is refused on every Contract, and a subscription sign-in (device login) is never delivered either — it is created in place by the funder signing in inside the Contract's harness root on the person's own Runner (ADR-0015 §4 amendment, local-agents 03).
_Avoid_: Key upload, Secret sync, Relay, Key slot (that is the Credential Reference the delivery fills)

**Recipient Key**:
The keypair a Runner installation generates on its host and registers at bootstrap so that others may seal values to it; one per installation (a Helm release or a workstation process), shared by its Runners, renewed automatically with the installation re-sealing every delivered value itself. Distinct from the Runner identity, which authenticates the Runner and never encrypts anything.
_Avoid_: Runner public key, Sealing key, Identity key, Encryption certificate

**Runner Hook**:
An operator-supplied executable the Runner runs at one of a closed, platform-named set of points around a Directive, the Verifier or its own lifecycle. Operators fill slots; the platform names them.
_Avoid_: Plugin, Script, Lifecycle callback

**Callback Socket**:
The Unix socket a Runner opens for one Directive attempt, owned by that attempt's Contract uid and reached with a bearer minted for that attempt alone. It is how a running Directive annotates, offers an artifact or asks the Runner to evaluate a verb — the same evaluation and the same Evidence as the Runner's own seams, never a way around them — and it dies with the attempt.
_Avoid_: Job API, Agent API, Directive token, Callback endpoint

**Workspace**:
A Work Record's checkout of its repository on the Runner the Work Record is pinned to. It belongs to the Work Record's Contract, is shared by every member of the Swarm and by the Learner, and lives until the Organisation's retention period ends or the Contract is terminated, whichever comes first.
_Avoid_: Sandbox, Checkout directory, Build path; never the tenant sense that Account and Product list under their own _Avoid_

**Public Metadata**:
Every name the workflow engine or a Runner's heartbeat exposes that payload encryption never covers: workflow and activity type names, workflow ids, task-queue and namespace names, Search Attributes and memo. It may describe what the platform does, never whose work it is, so it holds only platform vocabulary, platform-issued ids and platform enums, whatever the Product's Data Class.
_Avoid_: Unencrypted fields, cleartext metadata, labels, opaque identifiers

**Grant**:
A set of entries, each naming a resource type, a resource selector and a decision per verb, that bounds what an actor may do to an Organisation's resources; deny by default. Three exist in a chain — the Contract's root grant (`allow`/`deny`), the user's grant to an Agent (`allow`/`confirm`/`deny`), and the Persona's allow-list — four when the Contract is a Leaf Contract, whose Account Contract's root grant sits above it — and none carries a tier: Grants bound verbs, Approval Mandates bound Outcomes.
_Avoid_: Permission, Scope, Capability, Policy, ACL

**Effective Grant**:
What an Agent may actually do: the intersection of the root grant (and, under a Leaf Contract, the Account Contract's root grant above it), the Agent's Grant and its Persona's allow-list, computed at every evaluation and never stored, so no actor can ever hold more than the link above it in the chain.
_Avoid_: Resolved permissions, Merged scope, Computed grant

**Verb Catalogue**:
The platform-defined, closed, versioned list of resource types and the verbs each supports. A verb exists only once the Runner has a seam that enforces it; verb wildcards in a Grant resolve against the catalogue version pinned when the Grant was written, while resource selectors resolve live.
_Avoid_: Capability list, Permission set, Scopes

**Verifier**:
The component that runs a Product's registered commands against a Work Record's pull request and reports a pass/fail result.
_Avoid_: Checker, Validator, CI

**Verifier Command**:
A concrete invocation of a Tool Registry Entry issued for a specific Work Record, with its result.
_Avoid_: Run, Job, Execution
