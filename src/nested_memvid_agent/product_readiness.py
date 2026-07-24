from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class ProductReadinessStatus(StrEnum):
    READY = "ready"
    PARTIAL = "partial"
    MISSING = "missing"


@dataclass(frozen=True)
class ProductReadinessCategory:
    category_id: str
    title: str
    status: ProductReadinessStatus
    evidence: tuple[str, ...]
    remaining_work: tuple[str, ...]
    next_action: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "category_id": self.category_id,
            "title": self.title,
            "status": self.status.value,
            "evidence": list(self.evidence),
            "remaining_work": list(self.remaining_work),
            "next_action": self.next_action,
        }


@dataclass(frozen=True)
class ProductReadinessHeadline:
    total_categories: int
    ready_count: int
    partial_count: int
    missing_count: int
    product_ready: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_categories": self.total_categories,
            "ready_count": self.ready_count,
            "partial_count": self.partial_count,
            "missing_count": self.missing_count,
            "product_ready": self.product_ready,
        }


@dataclass(frozen=True)
class ProductReadinessReport:
    schema: str
    scope: str
    headline: ProductReadinessHeadline
    categories: tuple[ProductReadinessCategory, ...]

    def category(self, category_id: str) -> ProductReadinessCategory:
        for category in self.categories:
            if category.category_id == category_id:
                return category
        raise KeyError(category_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "scope": self.scope,
            "headline": self.headline.to_dict(),
            "categories": [category.to_dict() for category in self.categories],
        }


def build_product_readiness_report() -> ProductReadinessReport:
    """Return the static roadmap baseline for the full hosted/team product scope.

    This report is intentionally read-only: it does not inspect secrets, mutate
    runtime state, or certify an exact build for deployment. Supported-profile
    readiness belongs to live setup/health checks and the exact-byte release
    review; this report also tracks hosted/team capabilities that are outside
    the supported single-user, single-node profile.
    """
    categories = (
        ProductReadinessCategory(
            category_id="local_product_stability",
            title="Local product stability",
            status=ProductReadinessStatus.PARTIAL,
            evidence=(
                "CLI, local FastAPI server, React workbench, installer checks, Docker/Compose artifacts, mock smoke tests, first-run setup readiness checks, provider certification report, support bundle export, and Memvid/memory backends exist.",
                "Current validation commands include compileall, pytest, golden evals, learning-architecture evals, CLI mock chat, web tests, and web build.",
            ),
            remaining_work=(
                "Make fresh install and first-run setup boringly reliable across supported environments.",
                "Keep tightening setup recovery, support bundle contents, and first-run golden workflow guidance.",
            ),
            next_action="Build the next guided first-run step around repository baseline scan and golden repair handoff.",
        ),
        ProductReadinessCategory(
            category_id="golden_repair_workflow",
            title="Golden repo repair workflow",
            status=ProductReadinessStatus.PARTIAL,
            evidence=(
                "Repair primitives, repair branch gates, review artifacts, rollback artifacts, and exact-call approval gates exist.",
                "Task graph and scheduler records exist with planner/executor/reviewer/recovery nodes.",
                "Repair/code-modification scheduler tasks now default to git worktree isolation when the workspace supports worktrees, even if general worker isolation is disabled.",
                "Repair DAG tasks reuse one coherent git worktree for the run instead of creating a separate worktree per scheduler worker.",
                "A real approved prepare -> patch -> validate -> signed review -> literal-tree commit flow is covered, with deterministic dependency artifact handoff and recoverable approval-bound rollback.",
            ),
            remaining_work=(
                "Add a dedicated candidate-diff preview API and optional remote PR publishing lane without weakening the local exact-call gates.",
            ),
            next_action="Polish candidate diff inspection and an explicitly separate optional PR handoff around the now-covered local repair flow.",
        ),
        ProductReadinessCategory(
            category_id="proactive_personal_routines",
            title="Proactive personal routines",
            status=ProductReadinessStatus.PARTIAL,
            evidence=(
                "Durable UTC one-shot and fixed-interval routines have revisioned owner controls, fenced occurrence leases, deterministic run admission, internal transcript provenance, bounded polling, CLI/API/workbench history, and a hashed-idempotency manual run-now action.",
                "Routine definitions start disabled, raw registered secrets are rejected, and scheduled runs retain the normal exact-call tool approval boundary.",
                "The workbench follows selected claimed/running occurrences without overlapping requests and refreshes accepted run-now state through terminal reconciliation.",
            ),
            remaining_work=(
                "Add cron/calendar and named-timezone DST schedules plus explicit result-delivery policies for enabled channels.",
                "Add connector-specific idempotency keys before claiming replay-safe external delivery; arbitrary side effects remain at-least-once tool concerns.",
            ),
            next_action="Add an explicitly configured, connector-idempotent channel delivery step without widening tool approval.",
        ),
        ProductReadinessCategory(
            category_id="safe_autonomous_learning",
            title="Safe autonomous learning",
            status=ProductReadinessStatus.READY,
            evidence=(
                "Behavior-delta schema, ledger, proposal extraction, mutation gate, compiler, activation logs, outcomes, rollback, and learning dashboard exist.",
                "Runtime behavior-delta compilation is feature-flagged and high-risk/policy deltas remain approval-gated.",
                "Default-off low-risk auto-activation runs before behavior compilation, requires explicit validation metadata through the mutation gate, and records auto_activated audit rows for the learning dashboard.",
            ),
            remaining_work=(
                "Continue expanding validation-window analytics and live-provider regression coverage as learning volume grows.",
            ),
            next_action="Exercise opt-in low-risk auto-activation in live-provider evals and monitor dashboard false-positive and rollback rates.",
        ),
        ProductReadinessCategory(
            category_id="production_auth_workspaces",
            title="Hosted/team auth, users, and workspaces",
            status=ProductReadinessStatus.MISSING,
            evidence=(
                "Local bearer/API-key auth exists for the control plane.",
            ),
            remaining_work=(
                "Add real users, sessions, workspace/project ownership, roles, and per-user/per-project memory boundaries.",
                "Add production-safe CORS/session policy and token rotation/revocation.",
            ),
            next_action="Design and implement a workspace-scoped auth model before hosted/team deployment.",
        ),
        ProductReadinessCategory(
            category_id="sandboxed_extensibility",
            title="Sandboxed skills, plugins, and MCP",
            status=ProductReadinessStatus.PARTIAL,
            evidence=(
                "Skill discovery, manifest validation, upload/install path, plugin review/install/enable routes, and MCP stdio lifecycle exist.",
                "Executable skills fail closed on host runtimes and use an opt-in digest-pinned OCI runner with verified private skill/read-scope snapshots, no live workspace binds, no network/secrets/writes, read-only nonroot execution, resource limits, and no host fallback.",
                "Risk classification and exact approval gates exist for dangerous tools.",
            ),
            remaining_work=(
                "Add quota-bounded staged extension writeback with reviewed no-follow host-side commit semantics.",
                "Add managed dependency installation, portable non-Docker engines, and narrowly reviewable opt-in network/secret scope grants.",
                "Apply an equivalent OS-level containment contract to MCP stdio servers and complete production soak testing.",
            ),
            next_action="Extend the default-deny scope model to MCP and dependency installation, then soak the container runner under failure injection.",
        ),
        ProductReadinessCategory(
            category_id="provider_certification",
            title="Provider certification",
            status=ProductReadinessStatus.PARTIAL,
            evidence=(
                "All registered provider adapters appear in one deterministic matrix; adapter availability is reported separately from current-machine readiness.",
                "Live learning and exact provider integration harnesses are present but remain opt-in and credential dependent.",
                "`nest-agent product provider-certification`, the matching API route, and `scripts/run_provider_certification.py` expose evidence-backed, redacted assurance without treating configuration as certification.",
            ),
            remaining_work=(
                "Run credentialed CI/release validation for every provider and model claimed by a release.",
                "Retain exact-subject certification reports as reviewed release artifacts and keep their evidence fresh.",
            ),
            next_action="Execute and authenticate the v2 evidence flow for each release-claimed provider without exposing secrets.",
        ),
        ProductReadinessCategory(
            category_id="product_ux_onboarding",
            title="Product UX and onboarding",
            status=ProductReadinessStatus.PARTIAL,
            evidence=(
                "React workbench exposes runs, approvals, proactive routines, memory, skills, plugins, MCP, behavior deltas, settings, and learning dashboard surfaces.",
                "A dark command-center/cockpit visual system has been integrated.",
                "Authenticated first-run setup exposes readiness checks, agent/user naming, bounded persona selection, collaboration preferences, and explicit adaptation consent.",
            ),
            remaining_work=(
                "Add guided repository connection and baseline scanning, exact candidate-diff inspection, richer rollback UX, and recovery-oriented empty states.",
            ),
            next_action="Extend the existing first-run flow into repository baseline scan and exact candidate-diff review for the golden repair journey.",
        ),
        ProductReadinessCategory(
            category_id="operations_release_engineering",
            title="Operations and release engineering",
            status=ProductReadinessStatus.PARTIAL,
            evidence=(
                "Makefile, Dockerfile, Docker Compose, .env example, deployment docs, memory operations docs, and release checklist exist.",
                "Support bundle export can gather redacted setup/readiness, runtime, git, state, and log-tail metadata from CLI/API.",
                "Fail-closed upgrade/rollback, checksummed Memvid backup/restore, launchd supervision, health/readiness gates, chaos recovery, and a bounded soak harness exist.",
                "CI and release validation preload a digest-pinned OCI image and require the real executable-skill containment integration to pass without skips.",
            ),
            remaining_work=(
                "Run exact candidate bytes through the supported cross-platform CI matrix and independent review.",
                "Publish deliberately versioned, tagged, and provenance-backed artifacts only after every release gate passes.",
            ),
            next_action="Commit the reviewed candidate and obtain exact-byte CI evidence before versioning or publication.",
        ),
        ProductReadinessCategory(
            category_id="channels_ingress",
            title="Channels and external ingress",
            status=ProductReadinessStatus.PARTIAL,
            evidence=(
                "Telegram, Discord-shaped, and generic webhook ingress exist. Telegram now has single-owner admin gating, natural-language read/admin intents, inline-confirmed writes for settings changes, webhook setup helpers, and Telegram secret-token verification.",
            ),
            remaining_work=(
                "Add durable per-channel permissions/workspace boundaries, platform-specific rate-limit behavior, threading correctness, attachment handling, and secret rotation.",
            ),
            next_action="Add durable channel permission boundaries and platform rate-limit handling before treating channels as product-ready.",
        ),
        ProductReadinessCategory(
            category_id="metrics_proof",
            title="Metrics and proof of improvement",
            status=ProductReadinessStatus.PARTIAL,
            evidence=(
                "Promotion ledger, behavior-delta outcomes, learning dashboard, golden evals, and learning-architecture evals exist.",
            ),
            remaining_work=(
                "Track time-to-first-success, repair success rate, repeated-failure reduction, cost per task, intervention count, and before/after learning deltas.",
            ),
            next_action="Add product metrics to the readiness dashboard and tie them to release gates.",
        ),
    )
    ready_count = sum(1 for category in categories if category.status == ProductReadinessStatus.READY)
    partial_count = sum(1 for category in categories if category.status == ProductReadinessStatus.PARTIAL)
    missing_count = sum(1 for category in categories if category.status == ProductReadinessStatus.MISSING)
    return ProductReadinessReport(
        schema="kestrel.product_readiness.v2",
        scope="full_product_including_hosted_team",
        headline=ProductReadinessHeadline(
            total_categories=len(categories),
            ready_count=ready_count,
            partial_count=partial_count,
            missing_count=missing_count,
            product_ready=missing_count == 0 and partial_count == 0,
        ),
        categories=categories,
    )
