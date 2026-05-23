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
            "headline": self.headline.to_dict(),
            "categories": [category.to_dict() for category in self.categories],
        }


def build_product_readiness_report() -> ProductReadinessReport:
    """Return the static productization readiness baseline for the current alpha.

    This report is intentionally read-only: it does not inspect secrets, mutate
    runtime state, or promote any alpha feature to product-ready. Future slices
    can replace individual evidence strings with live checks as the product
    surface matures.
    """
    categories = (
        ProductReadinessCategory(
            category_id="local_product_stability",
            title="Local product stability",
            status=ProductReadinessStatus.PARTIAL,
            evidence=(
                "CLI, local FastAPI server, React workbench, installer checks, Docker/Compose artifacts, mock smoke tests, first-run setup readiness checks, support bundle export, and Memvid/memory backends exist.",
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
            ),
            remaining_work=(
                "Complete patch proposal, validation, review, approval, commit, and optional PR flow in one polished path.",
            ),
            next_action="Drive the demo repo fixture through an end-to-end repair workflow test that proves plan -> patch -> validation -> review -> approved commit.",
        ),
        ProductReadinessCategory(
            category_id="safe_autonomous_learning",
            title="Safe autonomous learning",
            status=ProductReadinessStatus.PARTIAL,
            evidence=(
                "Behavior-delta schema, ledger, proposal extraction, mutation gate, compiler, activation logs, outcomes, rollback, and learning dashboard exist.",
                "Runtime behavior-delta compilation is feature-flagged and high-risk/policy deltas remain approval-gated.",
            ),
            remaining_work=(
                "Auto-activate low-risk validated behavior deltas after validation thresholds and repeat evidence are satisfied.",
                "Add validation-window tracking before marking autonomous learning slices complete.",
            ),
            next_action="Implement the low-risk auto-activation path behind its default-off flag, with disabled-flag regression tests and rollback proof.",
        ),
        ProductReadinessCategory(
            category_id="production_auth_workspaces",
            title="Production auth, users, and workspaces",
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
                "Risk classification and approval gates exist for dangerous tools.",
            ),
            remaining_work=(
                "Add container-grade or equivalent sandboxing for executable skills and plugins.",
                "Add managed dependency installation and clearer permission scopes for filesystem, network, and secrets.",
            ),
            next_action="Ship a plugin/skill permission review model before expanding executable extension support.",
        ),
        ProductReadinessCategory(
            category_id="provider_certification",
            title="Provider certification",
            status=ProductReadinessStatus.PARTIAL,
            evidence=(
                "Mock provider is deterministic; OpenAI, OpenAI-compatible, Anthropic, Gemini, OpenRouter/Ollama aliases, Ollama Cloud, and Codex CLI paths exist.",
                "Live learning and provider integration harnesses are present but opt-in.",
            ),
            remaining_work=(
                "Run credentialed CI/release validation across the full provider matrix.",
                "Add provider-specific golden suites and release certification reports.",
            ),
            next_action="Create a provider certification command/report that records pass/fail status per provider without exposing secrets.",
        ),
        ProductReadinessCategory(
            category_id="product_ux_onboarding",
            title="Product UX and onboarding",
            status=ProductReadinessStatus.PARTIAL,
            evidence=(
                "React workbench exposes runs, approvals, memory, skills, plugins, MCP, behavior deltas, settings, and learning dashboard surfaces.",
                "A dark command-center/cockpit visual system has been integrated.",
            ),
            remaining_work=(
                "Add first-run onboarding, repo connection, provider validation, patch/diff review, rollback flows, empty states, and guided next actions.",
            ),
            next_action="Turn the current cockpit into a guided first-run flow centered on the golden repair journey.",
        ),
        ProductReadinessCategory(
            category_id="operations_release_engineering",
            title="Operations and release engineering",
            status=ProductReadinessStatus.PARTIAL,
            evidence=(
                "Makefile, Dockerfile, Docker Compose, .env example, deployment docs, memory operations docs, and release checklist exist.",
                "Support bundle export can gather redacted setup/readiness, runtime, git, state, and log-tail metadata from CLI/API.",
            ),
            remaining_work=(
                "Add upgrade/migration rollback checks, memory backup/restore validation, process supervision, and health-check gates.",
            ),
            next_action="Add upgrade and memory backup/restore validation to release gates.",
        ),
        ProductReadinessCategory(
            category_id="channels_ingress",
            title="Channels and external ingress",
            status=ProductReadinessStatus.PARTIAL,
            evidence=(
                "Telegram, Discord-shaped, and generic webhook ingress exist with dry-run replies and HMAC support for generic channels.",
            ),
            remaining_work=(
                "Add platform-native bot identity verification, rate-limit handling, threading correctness, attachment handling, per-channel permissions, and secret rotation.",
            ),
            next_action="Harden Telegram/channel production identity verification and rate-limit behavior before treating channels as product-ready.",
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
        schema="kestrel.product_readiness.v1",
        headline=ProductReadinessHeadline(
            total_categories=len(categories),
            ready_count=ready_count,
            partial_count=partial_count,
            missing_count=missing_count,
            product_ready=missing_count == 0 and partial_count == 0,
        ),
        categories=categories,
    )
