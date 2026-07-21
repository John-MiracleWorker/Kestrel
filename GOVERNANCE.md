# Kestrel Governance

Kestrel is an open-source, maintainer-led project. The goal of this document is to make decision
rights and contribution paths predictable without adding process that the current community does
not need.

## Roles

- **Users** run Kestrel, report problems, and propose improvements.
- **Contributors** submit documentation, tests, code, designs, or operational evidence.
- **Maintainers** triage issues, review changes, protect the safety contracts, and prepare releases.
- **Project lead** is an optional, explicitly appointed role with final responsibility for project
  direction, maintainer appointments, security decisions, and releases.

No separate project-lead appointment is currently recorded. Repository owner
[@John-MiracleWorker](https://github.com/John-MiracleWorker) is the default code owner for review
routing. Repository ownership and the checked-in [CODEOWNERS](.github/CODEOWNERS) file do not, by
themselves, appoint a person to a governance role.

## How Decisions Are Made

Routine fixes and small features are decided through issue and pull-request review. Maintainers aim
for consensus, using technical evidence, safety, compatibility, maintenance cost, and alignment with
the local-first product direction as the primary criteria.

Changes that alter architecture invariants, persistent formats, trust boundaries, compatibility,
governance, or the supported deployment profile should begin with a design issue. The proposal
should describe the problem, alternatives, risks, migration or rollback plan, and validation evidence.

When consensus is not reached, the proposal remains pending unless an explicitly appointed project
lead makes the final decision and records the rationale in the issue or pull request. Silence is not
approval.

## Maintainers

Maintainer appointments require an explicit, human-approved governance change based on sustained,
constructive contributions and sound judgment around Kestrel's memory and safety boundaries.
Maintainers may step down at any time. Inactive maintainers may be removed from CODEOWNERS after
reasonable private outreach and human approval.

## Releases

Maintainers prepare releases from reviewed commits. A release requires the validation in
[docs/RELEASE_CHECKLIST.md](docs/RELEASE_CHECKLIST.md), tag/version agreement, and deliberate
publication by a human who already has repository release permission. CI success does not grant an
agent or contributor permission to push, tag, publish, or change repository settings.

Release notes are summarized in [CHANGELOG.md](CHANGELOG.md). Security fixes may use a private
embargoed process until a patched release is available.

## Security and Conduct

Vulnerabilities must follow the confidential reporting process in [SECURITY.md](SECURITY.md).
Community behavior is governed by [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md). Other project decisions
should be documented publicly when practical.

## Changing This Document

Governance changes use the normal pull-request process and require explicit human approval from an
existing repository administrator or an appointed maintainer. As the contributor community grows,
Kestrel may adopt a broader maintainer or steering model through an explicit governance proposal.
