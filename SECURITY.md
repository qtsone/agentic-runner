# Security policy

The Runner executes agent work on infrastructure you control and holds credentials there, so we
treat a vulnerability in it as serious.

## Reporting a vulnerability

Report it privately through GitHub: **Security → Report a vulnerability** on this repository.
Do not open a public issue, pull request or discussion for it.

Include the affected version (`agentic-runner --version`), how you run it (Helm, Docker or
workstation, and the `isolation` mode), and the steps that reproduce it. We reply on the
advisory and keep you informed until it is fixed and released.

## Supported versions

Only the latest release receives security fixes. A Runner more than one contracts minor behind
the control plane already stops receiving new Directives, so upgrading is the fix path in every
case.

## Scope

In scope: this repository's packages, image and chart — for example a way for one Contract's
Directive to read another Contract's Workspace or credentials on the same Runner, a verb seam
that acts beyond its Grant, or a credential that leaves the host.

Out of scope: the hosted Agentic OS control plane (report those the same way and we route
them), and the third-party CLIs the image bundles (`codex`, `claude`), which belong to their
vendors.
