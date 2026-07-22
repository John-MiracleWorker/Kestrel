# Security Policy

Kestrel's supported security profile is one trusted user running one local or privately networked
node. It is not currently a multi-user, multi-tenant Internet service. Read
[docs/SECURITY.md](docs/SECURITY.md) for the runtime threat model, safe defaults, authentication,
secret handling, tool approval, MCP, plugin, webhook, and memory boundaries.

## Supported Versions

Security fixes are made against the latest stable release and the current development branch.
Users should upgrade to the newest patch release before reporting a problem that may already be fixed.

| Version | Supported |
| --- | --- |
| Latest `0.3.x` patch | Yes |
| `0.2.x` and older | No |

## Confidential Reporting

Do not open a public issue, discussion, or pull request for a suspected vulnerability.

Use [GitHub private vulnerability reporting](https://github.com/John-MiracleWorker/Kestrel/security/advisories/new)
to send the maintainers a confidential report. Do not include live credentials; revoke or rotate
them with the provider first and describe them only by provider and identifier.

Include the affected version or commit, deployment profile, impact, reproduction steps or a minimal
proof of concept, and any suggested mitigation. Redact tokens, secrets, private memory, repository
contents, and identifying data from logs and screenshots.

Maintainers will coordinate validation, remediation, disclosure timing, and credit with the
reporter. No response-time SLA is currently promised.

## Research Guidelines

Good-faith research is welcome when it:

- uses systems and data you own or have explicit permission to test;
- avoids privacy violations, destructive actions, persistence, and service disruption;
- does not access, retain, or disclose another person's secrets or memory;
- stops when meaningful user data or unauthorized access is encountered;
- follows the confidential process above and withholds sensitive details until maintainers agree on
  disclosure timing.

These guidelines do not authorize testing third-party providers, MCP servers, channels, or other
services outside Kestrel's control.
