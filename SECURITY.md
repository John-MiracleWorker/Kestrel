# Security Policy

## Supported Branches

Kestrel is under active development and does not currently publish long-lived stable release branches from this repository.

| Branch or version                         | Security support |
| ----------------------------------------- | ---------------- |
| `main`                                    | Best effort      |
| Historical commits and untagged snapshots | Not supported    |

## Reporting A Vulnerability

Please do not post secrets, tokens, private infrastructure details, or full exploit chains in a public issue.

Preferred path:

1. Use GitHub Security Advisories or private vulnerability reporting for this repository if it is enabled.
2. If private reporting is not available, open a minimal public issue that says you need a private security contact and omit all sensitive details.

Include:

- Affected package or service
- Deployment mode: Docker-first, native, hybrid, CLI, or desktop
- Clear reproduction steps
- Impact assessment
- Any logs or traces that do not expose secrets

## High-Value Areas

Reports affecting these areas are especially important:

- Auth, session handling, API keys, and workspace scoping
- Tool execution policy and approval flows
- Native host execution and filesystem access
- Hands sandbox isolation and audit trails
- Channel webhook verification and cross-channel routing
- Memory isolation, provider credentials, and secret handling

## Response Expectations

Security review is handled on a best-effort basis. There is no guaranteed SLA in this repository today.

If the issue is confirmed, fixes should aim to:

- Reduce exposure first
- Preserve service-boundary clarity
- Add regression coverage where practical
- Update documentation when behavior or operator guidance changes
