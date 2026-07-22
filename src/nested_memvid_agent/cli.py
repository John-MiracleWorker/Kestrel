from __future__ import annotations

import argparse
import importlib.util
import ipaddress
import json
import platform
import sqlite3
import subprocess  # nosec B404
import sys
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from importlib import metadata as importlib_metadata
from pathlib import Path
from time import monotonic, sleep
from typing import Any
from uuid import uuid4

from .agent import NestedMV2Agent
from .agent_backup import AgentBackupManager
from .behavior_delta_extractor import BehaviorDeltaExtractor
from .behavior_delta_ledger import BehaviorDeltaLedger
from .behavior_delta_skill import render_skill_candidate_preview
from .channels import ChannelManager, ChannelPayloadError
from .config import AgentConfig
from .context_compiler import ContextCompiler
from .context_packer import ContextPacker, ContextPackRequest
from .event_bus import RunEventBus
from .extension_runner import _digest_pinned_image as _is_digest_pinned_oci_image
from .layers import DEFAULT_LAYER_SPECS, LayerSpec, load_layer_specs
from .llm.model_catalog import PROVIDER_OPTIONS
from .mcp_manager import MCPManager
from .memory_backup import MemoryBackupError, MemoryBackupManager
from .models import MemoryKind, MemoryLayer, MemoryRecord, RetrievalQuery
from .nested_learning import direct_memory_write_allowed
from .orchestrator import build_memory_system
from .plugin_manager import PluginError, PluginManager
from .product_readiness import build_product_readiness_report
from .promotion_ledger import OUTCOME_KINDS, PromotionLedger
from .provider_certification import build_provider_certification_report
from .routines import RoutineService
from .run_manager import RunManager
from .runtime_models import LLMStreamEvent, ToolCall
from .runtime_ownership import PrimaryRuntimeOwnership, RuntimeOwnershipError
from .runtime_settings import default_runtime_settings_path
from .secret_broker import build_secret_broker
from .setup_readiness import build_setup_readiness_report
from .skill_manager import SkillManager
from .state_store import AgentStateStore, RoutineConflictError
from .support_bundle import export_support_bundle
from .task_capsule import summarize_run_capsule
from .tools.base import ToolContext
from .tools.builtin import build_default_tools


def _add_common_args(
    parser: argparse.ArgumentParser, *, default: object = argparse.SUPPRESS
) -> None:
    parser.add_argument("--backend", choices=["memory", "memvid"], default=default)
    parser.add_argument("--memory-dir", type=Path, default=default)
    parser.add_argument("--layer-config", type=Path, default=default)


def _add_agent_args(parser: argparse.ArgumentParser) -> None:
    _add_common_args(parser)
    parser.add_argument(
        "--provider",
        choices=list(PROVIDER_OPTIONS),
        default=argparse.SUPPRESS,
    )
    parser.add_argument("--model", default=argparse.SUPPRESS)
    parser.add_argument("--base-url", default=argparse.SUPPRESS)
    parser.add_argument("--api-key-env", default=argparse.SUPPRESS)
    parser.add_argument("--timeout-seconds", type=int, default=argparse.SUPPRESS)
    parser.add_argument("--max-retries", type=int, default=argparse.SUPPRESS)
    parser.add_argument("--temperature", type=float, default=argparse.SUPPRESS)
    parser.add_argument(
        "--codex-sandbox",
        choices=["read-only", "workspace-write", "danger-full-access"],
        default=argparse.SUPPRESS,
    )
    parser.add_argument("--codex-profile", default=argparse.SUPPRESS)
    parser.add_argument(
        "--codex-skip-git-repo-check", action="store_true", default=argparse.SUPPRESS
    )
    parser.add_argument("--codex-persist-session", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument("--workspace", type=Path, default=argparse.SUPPRESS)
    parser.add_argument("--log-dir", type=Path, default=argparse.SUPPRESS)
    parser.add_argument("--state-path", type=Path, default=argparse.SUPPRESS)
    parser.add_argument("--secret-store-path", type=Path, default=argparse.SUPPRESS)
    parser.add_argument("--secret-backend", choices=["json", "keyring"], default=argparse.SUPPRESS)
    parser.add_argument("--skills-dir", type=Path, default=argparse.SUPPRESS)
    parser.add_argument("--plugins-dir", type=Path, default=argparse.SUPPRESS)
    parser.add_argument("--mcp-config", type=Path, default=argparse.SUPPRESS)
    parser.add_argument("--channels-config", type=Path, default=argparse.SUPPRESS)
    parser.add_argument("--enable-channel-delivery", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument("--channel-send-timeout-seconds", type=int, default=argparse.SUPPRESS)
    parser.add_argument("--require-api-auth", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument("--api-auth-token-env", default=argparse.SUPPRESS)
    parser.add_argument("--allow-shell", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument("--allow-file-write", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument("--allow-policy-writes", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument("--allow-codex-cli", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument(
        "--validation-container-image",
        default=argparse.SUPPRESS,
        help=(
            "Preloaded immutable name@sha256:<64 hex> OCI image used by test.run, "
            "lint.run, repair validation, and codex.exec; there is no host fallback."
        ),
    )
    parser.add_argument("--allow-plugin-install", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument("--allow-git-commit", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument("--allow-git-push", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument("--allow-remote-mutation", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument("--git-write-mode", default=argparse.SUPPRESS)
    parser.add_argument("--protected-branches", default=argparse.SUPPRESS)
    parser.add_argument("--allow-memory-import", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument("--allow-executable-skills", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument(
        "--allow-mcp-network-endpoints", action="store_true", default=argparse.SUPPRESS
    )
    parser.add_argument("--allow-web", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument("--allow-self-modification", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument("--web-backend", choices=["direct", "mock"], default=argparse.SUPPRESS)
    parser.add_argument("--web-timeout-seconds", type=int, default=argparse.SUPPRESS)
    parser.add_argument("--web-max-results", type=int, default=argparse.SUPPRESS)
    parser.add_argument("--web-max-bytes", type=int, default=argparse.SUPPRESS)
    parser.add_argument(
        "--enable-semantic-orchestration", action="store_true", default=argparse.SUPPRESS
    )
    parser.add_argument(
        "--enable-autonomous-scheduler", action="store_true", default=argparse.SUPPRESS
    )
    parser.add_argument("--max-scheduler-tasks", type=int, default=argparse.SUPPRESS)
    parser.add_argument("--max-scheduler-cycles", type=int, default=argparse.SUPPRESS)
    parser.add_argument(
        "--enable-proactive-routines", action="store_true", default=argparse.SUPPRESS
    )
    parser.add_argument("--routine-poll-interval-seconds", type=float, default=argparse.SUPPRESS)
    parser.add_argument("--routine-claim-ttl-seconds", type=float, default=argparse.SUPPRESS)
    parser.add_argument("--max-routines-per-tick", type=int, default=argparse.SUPPRESS)
    parser.add_argument("--enable-worker-isolation", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument("--worker-worktree-dir", type=Path, default=argparse.SUPPRESS)
    parser.add_argument("--worker-branch-prefix", default=argparse.SUPPRESS)
    parser.add_argument("--stream", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument("--max-tool-rounds", type=int, default=argparse.SUPPRESS)
    parser.add_argument("--context-budget-chars", type=int, default=argparse.SUPPRESS)
    parser.add_argument("--disable-task-capsules", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument(
        "--enable-auto-consolidation", action="store_true", default=argparse.SUPPRESS
    )
    parser.add_argument(
        "--auto-consolidation-write", action="store_true", default=argparse.SUPPRESS
    )
    parser.add_argument("--enable-auto-compact", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument("--auto-compact-apply", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument("--context-pack-token-budget", type=int, default=argparse.SUPPRESS)
    parser.add_argument("--context-pack-expand-raw", action="store_true", default=argparse.SUPPRESS)


def main() -> None:
    parser = argparse.ArgumentParser(prog="nested-memvid")
    _add_common_args(parser, default=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="cmd", required=True)

    init = sub.add_parser("init")
    _add_common_args(init)

    put = sub.add_parser("put")
    _add_common_args(put)
    put.add_argument("--layer", choices=[layer.value for layer in MemoryLayer], required=True)
    put.add_argument(
        "--kind", choices=[kind.value for kind in MemoryKind], default=MemoryKind.OBSERVATION.value
    )
    put.add_argument("--title", required=True)
    put.add_argument("--text", required=True)
    put.add_argument("--confidence", type=float, default=0.8)
    put.add_argument("--importance", type=float, default=0.5)

    search = sub.add_parser("search")
    _add_common_args(search)
    search.add_argument("--query", required=True)
    search.add_argument("--k", type=int, default=8)

    memory_cmd = sub.add_parser("memory")
    memory_sub = memory_cmd.add_subparsers(dest="memory_cmd", required=True)
    memory_search = memory_sub.add_parser("search")
    _add_common_args(memory_search)
    memory_search.add_argument("query")
    memory_search.add_argument("--k", type=int, default=8)
    memory_search.add_argument(
        "--mode", choices=["auto", "lex", "vec", "vector", "hybrid"], default="auto"
    )
    memory_verify = memory_sub.add_parser("verify")
    _add_common_args(memory_verify)
    memory_doctor = memory_sub.add_parser("doctor")
    _add_common_args(memory_doctor)
    memory_doctor.add_argument(
        "--repair", action="store_true", help="Allow backend-supported repair instead of dry-run."
    )
    memory_inspect = memory_sub.add_parser("inspect")
    _add_common_args(memory_inspect)
    memory_inspect.add_argument("query")
    memory_inspect.add_argument("--k", type=int, default=8)
    memory_inspect.add_argument(
        "--mode", choices=["auto", "lex", "vec", "vector", "hybrid"], default="auto"
    )
    memory_inspect.add_argument("--include-inactive", action="store_true")
    memory_vector = memory_sub.add_parser("vector")
    vector_sub = memory_vector.add_subparsers(dest="vector_cmd", required=True)
    vector_status = vector_sub.add_parser("status")
    _add_common_args(vector_status)
    vector_status.add_argument("--json", action="store_true")
    vector_rebuild = vector_sub.add_parser("rebuild")
    _add_common_args(vector_rebuild)
    vector_rebuild.add_argument("--layer", choices=[layer.value for layer in MemoryLayer])
    vector_rebuild.add_argument("--json", action="store_true")
    memory_consolidate = memory_sub.add_parser("consolidate")
    _add_common_args(memory_consolidate)
    memory_consolidate.add_argument("query")
    memory_consolidate.add_argument(
        "--source-layer", choices=[layer.value for layer in MemoryLayer]
    )
    memory_consolidate.add_argument("--validation-score", type=float, default=0.7)
    memory_consolidate.add_argument("--repeat-count", type=int, default=1)
    memory_consolidate.add_argument("--explicit-instruction", action="store_true")
    memory_consolidate.add_argument("--dry-run", action="store_true")
    memory_correct = memory_sub.add_parser("correct")
    _add_common_args(memory_correct)
    memory_correct.add_argument("target_record_id")
    memory_correct.add_argument("correction_text")
    memory_correct.add_argument("--evidence-source", default="cli")
    memory_correct.add_argument("--evidence-locator", default="memory.correct")
    memory_correct.add_argument("--dry-run", action="store_true")
    memory_correct.add_argument(
        "--allow-memory-import", action="store_true", default=argparse.SUPPRESS
    )
    memory_compact = memory_sub.add_parser("compact")
    _add_common_args(memory_compact)
    memory_compact.add_argument(
        "--layer", choices=[layer.value for layer in MemoryLayer], default=MemoryLayer.WORKING.value
    )
    memory_compact.add_argument("--apply", action="store_true")
    memory_backup = memory_sub.add_parser("backup")
    _add_common_args(memory_backup)
    memory_backup.add_argument("--state-path", type=Path, default=argparse.SUPPRESS)
    memory_backup.add_argument("--backup-dir", type=Path, default=Path(".nest/backups/memory"))
    memory_backup.add_argument("--retain", type=int, default=7)
    memory_backup_list = memory_sub.add_parser("backup-list")
    memory_backup_list.add_argument("--memory-dir", type=Path, default=Path(".nest/memory"))
    memory_backup_list.add_argument("--backup-dir", type=Path, default=Path(".nest/backups/memory"))
    memory_backup_verify = memory_sub.add_parser("backup-verify")
    memory_backup_verify.add_argument("backup_id")
    memory_backup_verify.add_argument("--memory-dir", type=Path, default=Path(".nest/memory"))
    memory_backup_verify.add_argument(
        "--backup-dir", type=Path, default=Path(".nest/backups/memory")
    )
    memory_restore = memory_sub.add_parser("restore")
    _add_common_args(memory_restore)
    memory_restore.add_argument("--state-path", type=Path, default=argparse.SUPPRESS)
    memory_restore.add_argument("backup_id")
    memory_restore.add_argument("--backup-dir", type=Path, default=Path(".nest/backups/memory"))
    memory_restore.add_argument("--retain", type=int, default=7)
    memory_restore.add_argument(
        "--yes", action="store_true", help="Confirm destructive memory replacement."
    )
    memory_ledger = memory_sub.add_parser("ledger")
    memory_ledger.add_argument("--state-path", type=Path, default=Path(".nest/state/agent.db"))
    memory_ledger.add_argument("--since", default="30d")
    memory_ledger.add_argument("--layer", choices=[layer.value for layer in MemoryLayer])
    memory_ledger.add_argument("--outcome", choices=list(OUTCOME_KINDS))
    memory_ledger.add_argument("--json", action="store_true")
    memory_deltas = memory_sub.add_parser("deltas")
    deltas_sub = memory_deltas.add_subparsers(dest="deltas_cmd", required=True)
    deltas_propose = deltas_sub.add_parser("propose")
    deltas_propose.add_argument("--run-id", required=True)
    deltas_propose.add_argument("--runs-dir", type=Path, default=Path(".nest/runs"))
    deltas_propose.add_argument("--state-path", type=Path, default=Path(".nest/state/agent.db"))
    deltas_propose.add_argument("--backend", choices=["memory", "memvid"], default="memory")
    deltas_propose.add_argument("--dry-run", action="store_true")
    deltas_propose.add_argument("--json", action="store_true")
    deltas_ledger = deltas_sub.add_parser("ledger")
    deltas_ledger.add_argument("--state-path", type=Path, default=Path(".nest/state/agent.db"))
    deltas_ledger.add_argument("--since", default="30d")
    deltas_ledger.add_argument("--json", action="store_true")
    deltas_skill_preview = deltas_sub.add_parser("skill-preview")
    deltas_skill_preview.add_argument("delta_id")
    deltas_skill_preview.add_argument(
        "--state-path", type=Path, default=Path(".nest/state/agent.db")
    )
    deltas_skill_preview.add_argument("--skills-dir", type=Path, default=Path(".nest/skills"))
    deltas_skill_preview.add_argument("--skill-id")
    deltas_skill_preview.add_argument("--json", action="store_true")

    learning_cmd = sub.add_parser("learning")
    learning_sub = learning_cmd.add_subparsers(dest="learning_cmd", required=True)
    learning_dashboard = learning_sub.add_parser("dashboard")
    learning_dashboard.add_argument("--state-path", type=Path, default=Path(".nest/state/agent.db"))
    learning_dashboard.add_argument("--since", default="30d")
    learning_dashboard.add_argument("--json", action="store_true")

    product_cmd = sub.add_parser("product")
    product_sub = product_cmd.add_subparsers(dest="product_cmd", required=True)
    product_readiness = product_sub.add_parser("readiness")
    product_readiness.add_argument("--json", action="store_true")
    product_setup = product_sub.add_parser("setup")
    _add_agent_args(product_setup)
    product_setup.add_argument("--json", action="store_true")
    product_setup.add_argument(
        "--check",
        action="store_true",
        help="Exit nonzero when any required setup check fails.",
    )
    product_provider_certification = product_sub.add_parser("provider-certification")
    _add_agent_args(product_provider_certification)
    product_provider_certification.add_argument("--json", action="store_true")
    product_support = product_sub.add_parser("support-bundle")
    _add_agent_args(product_support)
    product_support.add_argument("--output", type=Path)
    product_support.add_argument("--log-tail", type=int, default=100)
    product_support.add_argument("--json", action="store_true")

    backup_cmd = sub.add_parser(
        "backup",
        help="Create or restore a coherent agent snapshot (memory, state, capsules, config, skills, plugins).",
    )
    backup_sub = backup_cmd.add_subparsers(dest="backup_cmd", required=True)
    backup_create = backup_sub.add_parser("create")
    _add_agent_args(backup_create)
    backup_create.add_argument("--backup-dir", type=Path, default=Path(".nest/backups/agent"))
    backup_create.add_argument("--retain", type=int, default=7)
    backup_list = backup_sub.add_parser("list")
    _add_agent_args(backup_list)
    backup_list.add_argument("--backup-dir", type=Path, default=Path(".nest/backups/agent"))
    backup_verify = backup_sub.add_parser("verify")
    _add_agent_args(backup_verify)
    backup_verify.add_argument("backup_id")
    backup_verify.add_argument("--backup-dir", type=Path, default=Path(".nest/backups/agent"))
    backup_restore = backup_sub.add_parser("restore")
    _add_agent_args(backup_restore)
    backup_restore.add_argument("backup_id")
    backup_restore.add_argument("--backup-dir", type=Path, default=Path(".nest/backups/agent"))
    backup_restore.add_argument("--retain", type=int, default=7)
    backup_restore.add_argument(
        "--yes",
        action="store_true",
        help="Confirm replacement of backed-up runtime components.",
    )

    compile_cmd = sub.add_parser("compile-context")
    _add_common_args(compile_cmd)
    compile_cmd.add_argument("--objective", required=True)
    compile_cmd.add_argument("--query")

    context_cmd = sub.add_parser("context")
    _add_common_args(context_cmd)
    context_cmd.add_argument("query")

    tools_cmd = sub.add_parser("tools")
    tools_cmd.add_argument("--json", action="store_true")

    plugins_cmd = sub.add_parser("plugins")
    plugins_sub = plugins_cmd.add_subparsers(dest="plugins_cmd", required=True)
    plugins_list = plugins_sub.add_parser("list")
    _add_agent_args(plugins_list)
    plugins_list.add_argument("--json", action="store_true")
    plugins_review = plugins_sub.add_parser("review")
    _add_agent_args(plugins_review)
    plugins_review.add_argument("source")
    plugins_review.add_argument("--ref")
    plugins_review.add_argument("--json", action="store_true")
    plugins_install = plugins_sub.add_parser("install")
    _add_agent_args(plugins_install)
    plugins_install.add_argument("source")
    plugins_install.add_argument("--ref")
    plugins_install.add_argument("--enable", action="store_true")
    plugins_install.add_argument("--overwrite", action="store_true")
    plugins_install.add_argument("--json", action="store_true")
    plugins_inspect = plugins_sub.add_parser("inspect")
    _add_agent_args(plugins_inspect)
    plugins_inspect.add_argument("plugin_id")
    plugins_inspect.add_argument("--json", action="store_true")
    plugins_enable = plugins_sub.add_parser("enable")
    _add_agent_args(plugins_enable)
    plugins_enable.add_argument("plugin_id")
    plugins_enable.add_argument("--json", action="store_true")
    plugins_disable = plugins_sub.add_parser("disable")
    _add_agent_args(plugins_disable)
    plugins_disable.add_argument("plugin_id")
    plugins_disable.add_argument("--json", action="store_true")
    plugins_update = plugins_sub.add_parser("update")
    _add_agent_args(plugins_update)
    plugins_update.add_argument("plugin_id")
    plugins_update.add_argument("--ref")
    plugins_update.add_argument("--json", action="store_true")
    plugins_remove = plugins_sub.add_parser("remove")
    _add_agent_args(plugins_remove)
    plugins_remove.add_argument("plugin_id")
    plugins_remove.add_argument("--json", action="store_true")

    chat = sub.add_parser(
        "chat",
        epilog=(
            "With --message, exit status is 0 for completion, 2 for an operator-action "
            "block, and 1 for failure or cancellation. Interactive chat reports each "
            "turn without terminating the session."
        ),
    )
    _add_agent_args(chat)
    chat.add_argument("--message", help="Run one chat turn. If omitted, enter interactive mode.")
    chat.add_argument("--session-id", default="cli")

    run_cmd = sub.add_parser(
        "run",
        description="Create one durable run and wait for its terminal outcome.",
        epilog=(
            "Exit status: 0 when the run completed; 2 when it is blocked and needs "
            "operator action; 1 when it failed, was cancelled, or returned an unknown "
            "state. The run payload is printed before any nonzero exit. Local --no-wait "
            "is rejected because a terminating CLI cannot own background execution."
        ),
    )
    _add_agent_args(run_cmd)
    run_cmd.add_argument("message")
    run_cmd.add_argument("--session-id", default="cli")
    run_cmd.add_argument("--json", action="store_true")
    run_cmd.add_argument(
        "--no-wait",
        action="store_true",
        help="Rejected locally; submit asynchronous work through the authenticated server/API.",
    )
    run_cmd.add_argument("--events", action="store_true")

    routines_cmd = sub.add_parser("routines")
    routines_sub = routines_cmd.add_subparsers(dest="routines_cmd", required=True)
    routines_list = routines_sub.add_parser("list")
    _add_agent_args(routines_list)
    routines_list.add_argument("--json", action="store_true")
    routines_create = routines_sub.add_parser("create")
    _add_agent_args(routines_create)
    routines_create.add_argument("--id", dest="routine_id")
    routines_create.add_argument("--name", dest="routine_name", required=True)
    routines_create.add_argument("--prompt", dest="routine_prompt", required=True)
    routines_create.add_argument(
        "--schedule-kind", choices=["once", "interval"], default="interval"
    )
    routines_create.add_argument("--start-at")
    routines_create.add_argument("--interval-seconds", type=int)
    routines_create.add_argument("--routine-workspace")
    routines_create.add_argument("--routine-provider")
    routines_create.add_argument("--routine-model")
    routines_create.add_argument(
        "--routine-autonomy",
        choices=["background", "manual", "autonomous"],
        default="background",
    )
    routines_create.add_argument("--misfire-grace-seconds", type=int, default=60)
    routines_create.add_argument("--json", action="store_true")
    routines_show = routines_sub.add_parser("show")
    _add_agent_args(routines_show)
    routines_show.add_argument("routine_id")
    routines_show.add_argument("--json", action="store_true")
    routines_update = routines_sub.add_parser("update")
    _add_agent_args(routines_update)
    routines_update.add_argument("routine_id")
    routines_update.add_argument("--expected-revision", type=int, required=True)
    routines_update.add_argument("--name", dest="routine_name")
    routines_update.add_argument("--prompt", dest="routine_prompt")
    routines_update.add_argument("--schedule-kind", choices=["once", "interval"])
    routines_update.add_argument("--start-at")
    routines_update.add_argument("--interval-seconds", type=int)
    routines_update.add_argument("--routine-workspace")
    routines_update.add_argument("--routine-provider")
    routines_update.add_argument("--routine-model")
    routines_update.add_argument(
        "--routine-autonomy",
        choices=["background", "manual", "autonomous"],
    )
    routines_update.add_argument("--misfire-grace-seconds", type=int)
    routines_update.add_argument("--json", action="store_true")
    for action in ("enable", "disable", "delete"):
        routine_mutation = routines_sub.add_parser(action)
        _add_agent_args(routine_mutation)
        routine_mutation.add_argument("routine_id")
        routine_mutation.add_argument("--expected-revision", type=int, required=True)
        routine_mutation.add_argument("--json", action="store_true")
    routines_tick = routines_sub.add_parser("tick")
    _add_agent_args(routines_tick)
    routines_tick.add_argument("--json", action="store_true")
    routines_history = routines_sub.add_parser("history")
    _add_agent_args(routines_history)
    routines_history.add_argument("routine_id")
    routines_history.add_argument("--limit", type=int, default=100)
    routines_history.add_argument("--json", action="store_true")

    status_cmd = sub.add_parser("status")
    _add_agent_args(status_cmd)
    status_cmd.add_argument("run_id", nargs="?")
    status_cmd.add_argument("--json", action="store_true")
    status_cmd.add_argument("--events", action="store_true")

    approvals_cmd = sub.add_parser("approvals")
    _add_agent_args(approvals_cmd)
    approvals_cmd.add_argument("--status")
    approvals_cmd.add_argument("--json", action="store_true")

    approve_cmd = sub.add_parser("approve")
    _add_agent_args(approve_cmd)
    approve_cmd.add_argument("approval_id")
    approve_cmd.add_argument(
        "--arguments", help="Optional JSON object replacing the originally requested arguments."
    )
    approve_cmd.add_argument("--json", action="store_true")
    approve_cmd.add_argument(
        "--no-wait",
        action="store_true",
        help="Rejected locally; resume approvals through the authenticated server/API.",
    )

    deny_cmd = sub.add_parser("deny")
    _add_agent_args(deny_cmd)
    deny_cmd.add_argument("approval_id")
    deny_cmd.add_argument("--json", action="store_true")

    eval_cmd = sub.add_parser("eval")
    _add_common_args(eval_cmd)
    eval_cmd.add_argument(
        "--provider",
        choices=list(PROVIDER_OPTIONS),
        default=argparse.SUPPRESS,
    )
    eval_cmd.add_argument("--model", default=argparse.SUPPRESS)
    eval_cmd.add_argument("--workspace", type=Path, default=argparse.SUPPRESS)

    doctor = sub.add_parser("doctor")
    _add_agent_args(doctor)

    server = sub.add_parser("server")
    _add_agent_args(server)
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", type=int, default=8765)
    server.add_argument("--access-log", action=argparse.BooleanOptionalAction, default=True)

    channel = sub.add_parser("channel")
    _add_agent_args(channel)
    channel.add_argument(
        "channel_provider", help="Channel provider: telegram, discord, webhook, or custom."
    )
    channel.add_argument("--channel-id", help="Configured channel id. Defaults to provider.")
    channel.add_argument(
        "--send", action="store_true", help="Request outbound delivery after the agent responds."
    )
    channel.add_argument(
        "--telegram-webhook-action",
        choices=["info", "set", "delete", "test"],
        help="Run Telegram webhook setup/status action instead of ingesting a payload.",
    )
    channel.add_argument(
        "--webhook-url", help="Public Telegram webhook URL for --telegram-webhook-action set."
    )
    channel.add_argument(
        "--test-chat-id", help="Telegram chat id for --telegram-webhook-action test."
    )
    channel.add_argument("--test-text", default="Kestrel Telegram channel test.")
    payload_group = channel.add_mutually_exclusive_group(required=False)
    payload_group.add_argument("--payload", help="Inbound channel payload as JSON.")
    payload_group.add_argument(
        "--payload-file", type=Path, help="Path to inbound channel payload JSON, or '-' for stdin."
    )

    args = parser.parse_args()
    if args.cmd == "run" and args.no_wait:
        raise SystemExit(
            "Local `run --no-wait` is unsafe because the CLI process cannot own "
            "background execution after it exits. Submit asynchronous work through "
            "the authenticated server API at POST /api/runs."
        )
    if args.cmd == "approve" and args.no_wait:
        raise SystemExit(
            "Local `approve --no-wait` is unsafe because the CLI process cannot own "
            "approval continuation after it exits. Resume through the authenticated "
            "server API at POST /api/approvals/{approval_id}/decision."
        )
    config = _agent_config_from_args(args)
    backend = config.backend
    memory_dir = config.memory_dir

    if args.cmd == "chat":
        manager = _build_run_manager(config)
        exit_code = 0
        try:
            if args.message:
                if _handle_slash_command_for_manager(
                    manager, config, args.message.strip(), args.session_id
                ):
                    return
                exit_code = _run_manager_chat_and_print(
                    manager, args.message, session_id=args.session_id
                )
            else:
                print("Kestrel chat. Type /exit to quit.")
                while True:
                    user_message = input("you> ").strip()
                    if user_message in {"/exit", "/quit"}:
                        break
                    if not user_message:
                        continue
                    if _handle_slash_command_for_manager(
                        manager, config, user_message, args.session_id
                    ):
                        continue
                    _run_manager_chat_and_print(
                        manager,
                        user_message,
                        session_id=args.session_id,
                        prefix="agent> ",
                    )
        finally:
            _shutdown_run_manager(manager)
        if exit_code:
            raise SystemExit(exit_code)
        return

    if args.cmd == "run":
        manager = _build_run_manager(config)
        try:
            exit_code = _create_run_and_print(
                manager,
                args.message,
                session_id=args.session_id,
                json_output=args.json,
                wait=not args.no_wait,
                include_events=args.events,
            )
        finally:
            _shutdown_run_manager(manager)
        if exit_code:
            raise SystemExit(exit_code)
        return

    if args.cmd == "routines":
        _handle_routines_command(args, config)
        return

    if args.cmd == "status":
        manager = _build_run_manager(config, read_only_observer=True)
        try:
            _print_status(
                manager, run_id=args.run_id, json_output=args.json, include_events=args.events
            )
        finally:
            _shutdown_run_manager(manager)
        return

    if args.cmd == "approvals":
        manager = _build_run_manager(config, read_only_observer=True)
        try:
            _print_approvals(manager, status=args.status, json_output=args.json)
        finally:
            _shutdown_run_manager(manager)
        return

    if args.cmd in {"approve", "deny"}:
        manager = _build_run_manager(config)
        try:
            exit_code = _decide_approval_and_print(
                manager,
                approval_id=args.approval_id,
                approved=args.cmd == "approve",
                arguments_json=getattr(args, "arguments", None),
                json_output=args.json,
                wait=not getattr(args, "no_wait", False),
            )
        finally:
            _shutdown_run_manager(manager)
        if exit_code:
            raise SystemExit(exit_code)
        return

    if args.cmd == "server":
        try:
            import uvicorn
        except ImportError as exc:
            raise RuntimeError("Install server extras with `pip install -e '.[server]'`.") from exc
        from .server import create_app

        _validate_server_bind(args.host, config)
        try:
            app = create_app(config)
        except RuntimeOwnershipError as exc:
            raise SystemExit(_runtime_ownership_message()) from exc
        uvicorn.run(app, host=args.host, port=args.port, access_log=args.access_log)
        return

    if args.cmd == "channel":
        channel_run_manager: RunManager | None = None
        if not args.telegram_webhook_action:
            if args.payload is None and args.payload_file is None:
                raise SystemExit(
                    "channel payload required: pass --payload/--payload-file or --telegram-webhook-action"
                )
            channel_run_manager = _build_run_manager(config)
        channel_manager = ChannelManager(config, run_manager=channel_run_manager)
        try:
            if args.telegram_webhook_action:
                channel_id = args.channel_id or args.channel_provider
                if args.telegram_webhook_action == "info":
                    webhook_result = channel_manager.telegram_webhook_info(channel_id)
                elif args.telegram_webhook_action == "set":
                    if not args.webhook_url:
                        raise SystemExit(
                            "--webhook-url is required for --telegram-webhook-action set"
                        )
                    webhook_result = channel_manager.telegram_set_webhook(
                        channel_id, url=args.webhook_url
                    )
                elif args.telegram_webhook_action == "delete":
                    webhook_result = channel_manager.telegram_delete_webhook(channel_id)
                else:
                    if not args.test_chat_id:
                        raise SystemExit(
                            "--test-chat-id is required for --telegram-webhook-action test"
                        )
                    webhook_result = channel_manager.telegram_test_message(
                        channel_id,
                        chat_id=args.test_chat_id,
                        text=args.test_text,
                    )
                print(json.dumps(webhook_result, indent=2))
                return
            process_result = channel_manager.handle_payload(
                provider=args.channel_provider,
                channel_id=args.channel_id,
                payload=_load_channel_payload(args),
                send=args.send,
            )
        except ChannelPayloadError as exc:
            raise SystemExit(str(exc)) from exc
        finally:
            try:
                channel_manager.close()
            finally:
                if channel_run_manager is not None:
                    _shutdown_run_manager(channel_run_manager)
        print(json.dumps(process_result.to_public_dict(), indent=2))
        return

    if args.cmd == "tools":
        specs = [spec.to_public_dict() for spec in build_default_tools().specs()]
        if args.json:
            print(json.dumps(specs, indent=2))
        else:
            for spec in specs:
                approval = "approval required" if spec["requires_approval"] else "allowed"
                print(f"{spec['name']} [{spec['risk']}, {approval}] - {spec['description']}")
        return

    if args.cmd == "plugins":
        manager = _build_run_manager(
            config,
            read_only_observer=args.plugins_cmd in {"list", "inspect"},
        )
        try:
            _handle_plugins_command(args, manager, backend=backend, memory_dir=memory_dir)
        finally:
            _shutdown_run_manager(manager)
        return

    if args.cmd == "doctor":
        report = _doctor_runtime(config)
        print(json.dumps(report, indent=2))
        if report.get("ok") is not True:
            raise SystemExit(1)
        return

    if args.cmd == "eval":
        _run_eval_command(config)
        return

    if args.cmd == "learning" and args.learning_cmd == "dashboard":
        _print_learning_dashboard(args)
        return

    if args.cmd == "product" and args.product_cmd == "readiness":
        _print_product_readiness(args)
        return

    if args.cmd == "product" and args.product_cmd == "setup":
        _print_setup_readiness(config, args)
        return

    if args.cmd == "product" and args.product_cmd == "provider-certification":
        _print_provider_certification(config, args)
        return

    if args.cmd == "product" and args.product_cmd == "support-bundle":
        _print_support_bundle(config, args)
        return

    if args.cmd == "backup":
        _handle_agent_backup_command(config, args)
        return

    if args.cmd == "memory" and args.memory_cmd in {
        "backup",
        "backup-list",
        "backup-verify",
        "restore",
    }:
        _handle_memory_backup_command(config, args)
        return

    if args.cmd == "memory" and args.memory_cmd == "ledger":
        _print_promotion_ledger(args)
        return

    if args.cmd == "memory" and args.memory_cmd == "deltas":
        _handle_behavior_deltas_command(args)
        return

    ledger = _ledger_for_memory_command(args)
    memory = build_memory_system(
        backend, memory_dir, specs=_specs_from_config(config), ledger=ledger
    )
    try:
        if args.cmd == "memory":
            if args.memory_cmd == "vector":
                if args.vector_cmd == "status":
                    _print_vector_status(memory, json_output=args.json)
                    return
                if args.vector_cmd == "rebuild":
                    layers = (MemoryLayer(args.layer),) if args.layer else None
                    _print_vector_rebuild(memory, layers=layers, json_output=args.json)
                    return
                raise SystemExit(f"Unknown vector command: {args.vector_cmd}")
            if args.memory_cmd in {"search", "inspect"}:
                hits = memory.retrieve(
                    RetrievalQuery(
                        query=args.query,
                        k_per_layer=args.k,
                        mode=args.mode,
                        include_inactive=bool(getattr(args, "include_inactive", False)),
                    )
                )
                for hit in hits:
                    memory_payload: dict[str, object] = {
                        "layer": hit.record.layer.value,
                        "kind": hit.record.kind.value,
                        "score": hit.score,
                        "source_backend": hit.source_backend,
                        "title": hit.record.title,
                        "id": hit.record.id,
                        "snippet": hit.snippet or hit.record.content[:500],
                    }
                    if args.memory_cmd == "inspect":
                        memory_payload["metadata"] = hit.record.metadata
                        memory_payload["evidence"] = [
                            {
                                "source": evidence.source,
                                "locator": evidence.locator,
                                "quote": evidence.quote,
                            }
                            for evidence in hit.record.evidence
                        ]
                    print(json.dumps(memory_payload, indent=2))
                if not hits:
                    print("No memory hits.")
                return
            if args.memory_cmd == "verify":
                _print_verify_results(memory.verify_all())
                return
            if args.memory_cmd == "doctor":
                print(json.dumps(_doctor_memory(memory, dry_run=not args.repair), indent=2))
                return
            if args.memory_cmd == "consolidate":
                execution = build_default_tools().execute(
                    ToolCall(
                        name="memory.consolidate",
                        arguments={
                            "query": args.query,
                            "source_layer": args.source_layer,
                            "validation_score": args.validation_score,
                            "repeat_count": args.repeat_count,
                            "explicit_instruction": args.explicit_instruction,
                            "dry_run": args.dry_run,
                        },
                    ),
                    ToolContext(
                        memory=memory,
                        config=config,
                        workspace=config.workspace,
                        session_id="cli",
                    ),
                )
                print(execution.content)
                if not execution.success:
                    raise SystemExit(1)
                return
            if args.memory_cmd == "correct":
                arguments = {
                    "target_record_id": args.target_record_id,
                    "correction_text": args.correction_text,
                    "evidence": [
                        {
                            "source": args.evidence_source,
                            "locator": args.evidence_locator,
                        }
                    ],
                    "dry_run": args.dry_run,
                }
                call = ToolCall(name="memory.correct", id="cli_memory_correct", arguments=arguments)
                execution = build_default_tools().execute(
                    call,
                    ToolContext(
                        memory=memory,
                        config=config,
                        workspace=config.workspace,
                        session_id="cli",
                        approved_tool_call_ids=frozenset({call.id}),
                        approved_tool_call_arguments={call.id: arguments},
                    ),
                )
                print(execution.content)
                if not execution.success:
                    raise SystemExit(1)
                return
            if args.memory_cmd == "compact":
                execution = build_default_tools().execute(
                    ToolCall(
                        name="memory.compact",
                        arguments={
                            "layer": args.layer,
                            "apply": args.apply,
                        },
                    ),
                    ToolContext(
                        memory=memory,
                        config=config,
                        workspace=config.workspace,
                        session_id="cli",
                    ),
                )
                print(execution.content)
                if not execution.success:
                    raise SystemExit(1)
                return

        if args.cmd == "init":
            memory.seal_all()
            print(f"Initialized {backend} memory at {memory_dir}")
            return

        if args.cmd == "put":
            target_layer = MemoryLayer(args.layer)
            if not direct_memory_write_allowed(target_layer):
                raise SystemExit(
                    f"Direct CLI writes to {target_layer.value} memory are rejected. "
                    "Use a validated promotion path with durable source evidence."
                )
            record = MemoryRecord(
                layer=target_layer,
                kind=MemoryKind(args.kind),
                title=args.title,
                content=args.text,
                confidence=args.confidence,
                importance=args.importance,
            )
            record_id = memory.put(record)
            memory.seal_all()
            print(record_id)
            return

        if args.cmd == "search":
            hits = memory.retrieve(RetrievalQuery(query=args.query, k_per_layer=args.k))
            for hit in hits:
                print(f"[{hit.record.layer.value}] score={hit.score:.3f} {hit.record.title}")
                print(hit.snippet or hit.record.content[:500])
                print()
            return

        if args.cmd == "compile-context":
            compiler = ContextCompiler(memory)
            compiled = compiler.compile(objective=args.objective, query=args.query)
            print(compiled.prompt)
            return

        if args.cmd == "context":
            compiler = ContextCompiler(memory)
            compiled = compiler.compile(objective=args.query, query=args.query)
            print(compiled.prompt)
            return

    finally:
        memory.close_all()


_CONFIG_ARG_FIELDS = {
    "provider": "provider",
    "model": "model",
    "base_url": "base_url",
    "api_key_env": "api_key_env",
    "timeout_seconds": "timeout_seconds",
    "max_retries": "max_retries",
    "temperature": "temperature",
    "codex_sandbox": "codex_sandbox",
    "codex_profile": "codex_profile",
    "codex_skip_git_repo_check": "codex_skip_git_repo_check",
    "backend": "backend",
    "memory_dir": "memory_dir",
    "layer_config": "layer_config_path",
    "workspace": "workspace",
    "log_dir": "log_dir",
    "state_path": "state_path",
    "secret_store_path": "secret_store_path",  # nosec B105
    "secret_backend": "secret_backend",  # nosec B105
    "skills_dir": "skills_dir",
    "plugins_dir": "plugins_dir",
    "mcp_config": "mcp_config_path",
    "channels_config": "channel_config_path",
    "enable_channel_delivery": "enable_channel_delivery",
    "channel_send_timeout_seconds": "channel_send_timeout_seconds",
    "require_api_auth": "require_api_auth",
    "api_auth_token_env": "api_auth_token_env",  # nosec B105
    "allow_shell": "allow_shell",
    "allow_file_write": "allow_file_write",
    "allow_policy_writes": "allow_policy_writes",
    "allow_codex_cli": "allow_codex_cli",
    "validation_container_image": "validation_container_image",
    "allow_plugin_install": "allow_plugin_install",
    "allow_git_commit": "allow_git_commit",
    "allow_git_push": "allow_git_push",
    "allow_remote_mutation": "allow_remote_mutation",
    "git_write_mode": "git_write_mode",
    "allow_memory_import": "allow_memory_import",
    "allow_executable_skills": "allow_executable_skills",
    "allow_mcp_network_endpoints": "allow_mcp_network_endpoints",
    "allow_web": "allow_web",
    "allow_self_modification": "allow_self_modification",
    "web_backend": "web_backend",
    "web_timeout_seconds": "web_timeout_seconds",
    "web_max_results": "web_max_results",
    "web_max_bytes": "web_max_bytes",
    "enable_semantic_orchestration": "enable_semantic_orchestration",
    "enable_autonomous_scheduler": "enable_autonomous_scheduler",
    "max_scheduler_tasks": "max_scheduler_tasks",
    "max_scheduler_cycles": "max_scheduler_cycles",
    "enable_proactive_routines": "enable_proactive_routines",
    "routine_poll_interval_seconds": "routine_poll_interval_seconds",
    "routine_claim_ttl_seconds": "routine_claim_ttl_seconds",
    "max_routines_per_tick": "max_routines_per_tick",
    "enable_worker_isolation": "enable_worker_isolation",
    "worker_worktree_dir": "worker_worktree_dir",
    "worker_branch_prefix": "worker_branch_prefix",
    "stream": "stream",
    "max_tool_rounds": "max_tool_rounds",
    "context_budget_chars": "context_budget_chars",
    "enable_auto_consolidation": "enable_auto_consolidation",
    "enable_auto_compact": "enable_auto_compact",
    "auto_compact_apply": "auto_compact_apply",
    "context_pack_token_budget": "context_pack_token_budget",  # nosec B105
    "context_pack_expand_raw": "context_pack_expand_raw",
}


def _agent_config_from_args(args: argparse.Namespace) -> AgentConfig:
    config = AgentConfig.from_env()
    overrides: dict[str, Any] = {}
    for arg_name, field_name in _CONFIG_ARG_FIELDS.items():
        if hasattr(args, arg_name):
            overrides[field_name] = getattr(args, arg_name)
    if hasattr(args, "protected_branches"):
        overrides["protected_branches"] = tuple(
            part.strip() for part in args.protected_branches.split(",") if part.strip()
        )
    if hasattr(args, "codex_persist_session"):
        overrides["codex_ephemeral"] = False
    if hasattr(args, "disable_task_capsules"):
        overrides["enable_task_capsules"] = False
    if hasattr(args, "auto_consolidation_write"):
        overrides["auto_consolidation_dry_run"] = False
    return replace(config, **overrides)


def _specs_from_config(config: AgentConfig) -> dict[MemoryLayer, Any] | None:
    layer_config = config.layer_config_path
    return load_layer_specs(layer_config) if layer_config else None


def _handle_memory_backup_command(config: AgentConfig, args: argparse.Namespace) -> None:
    command = str(args.memory_cmd)
    if command == "backup-list":
        manager = MemoryBackupManager(
            memory_dir=config.memory_dir,
            backup_root=Path(args.backup_dir),
            specs=_specs_from_config(config),
        )
        print(json.dumps(manager.list_backups(), indent=2))
        return
    if command == "backup-verify":
        manager = MemoryBackupManager(
            memory_dir=config.memory_dir,
            backup_root=Path(args.backup_dir),
            specs=_specs_from_config(config),
        )
        result = manager.validate(str(args.backup_id))
        print(json.dumps(result, indent=2))
        if not result["ok"]:
            raise SystemExit(1)
        return
    if config.backend != "memvid":
        raise SystemExit("Memory backup and restore require --backend memvid.")
    if command == "restore" and not bool(args.yes):
        raise SystemExit("Memory restore is destructive; rerun with --yes after stopping Kestrel.")
    ownership = PrimaryRuntimeOwnership(config.state_path)
    try:
        ownership.acquire()
    except RuntimeOwnershipError as exc:
        raise SystemExit(_backup_runtime_ownership_message()) from exc
    try:
        manager = MemoryBackupManager(
            memory_dir=config.memory_dir,
            backup_root=Path(args.backup_dir),
            specs=_specs_from_config(config),
        )
        active = AgentStateStore(config.state_path).list_nonterminal_runs()
        if active:
            raise SystemExit("Refusing memory backup/restore while non-terminal runs exist.")
        if command == "backup":
            _seal_and_verify_memory(config)
            print(json.dumps(manager.create(retain=max(1, int(args.retain))), indent=2))
            return
        if command == "restore":
            result = manager.restore(
                str(args.backup_id),
                retain=max(2, int(args.retain)),
                verify_staging=lambda path: _seal_and_verify_memory(
                    replace(config, memory_dir=path)
                ),
            )
            print(json.dumps(result, indent=2))
            return
        raise SystemExit(f"Unknown memory backup command: {command}")
    finally:
        ownership.release()


def _handle_agent_backup_command(config: AgentConfig, args: argparse.Namespace) -> None:
    command = str(args.backup_cmd)
    layer_config_path = config.layer_config_path or (
        config.memory_dir.parent / "config" / "layers.json"
    )
    manager = AgentBackupManager(
        memory_dir=config.memory_dir,
        state_path=config.state_path,
        backup_root=Path(args.backup_dir),
        runs_dir=config.memory_dir.parent / "runs",
        skills_dir=config.skills_dir,
        plugins_dir=config.plugins_dir,
        mcp_config_path=config.mcp_config_path,
        channel_config_path=config.channel_config_path,
        runtime_settings_path=default_runtime_settings_path(config),
        layer_config_path=layer_config_path,
        repair_artifact_root=config.workspace / ".nest",
    )
    if command == "list":
        print(json.dumps(manager.list_backups(), indent=2))
        return
    if command == "verify":
        result = manager.validate(str(args.backup_id))
        print(json.dumps(result, indent=2))
        if not result["ok"]:
            raise SystemExit(1)
        return
    if config.backend != "memvid":
        raise SystemExit("Agent backup and restore require --backend memvid.")
    if command == "restore" and not bool(args.yes):
        raise SystemExit(
            "Agent restore replaces memory and control-plane state; rerun with --yes after stopping Kestrel."
        )

    def preflight() -> None:
        current_specs = load_layer_specs(layer_config_path) if layer_config_path.is_file() else None
        manager.specs = current_specs or DEFAULT_LAYER_SPECS
        try:
            active = AgentStateStore(config.state_path).list_nonterminal_runs()
        except Exception:
            if command != "restore":
                raise
            active = []
        if active:
            raise SystemExit("Refusing agent backup/restore while non-terminal runs exist.")
        if command == "create":
            _seal_and_verify_memory(config, specs=current_specs)

    if command == "create":
        try:
            result = manager.create(
                retain=max(1, int(args.retain)),
                preflight=preflight,
            )
        except RuntimeOwnershipError as exc:
            raise SystemExit(_backup_runtime_ownership_message()) from exc
        print(json.dumps(result, indent=2))
        return
    if command == "restore":
        try:
            result = manager.restore(
                str(args.backup_id),
                retain=max(2, int(args.retain)),
                preflight=preflight,
                verify_memory_staging=lambda memory_path, staged_config, layer_files: (
                    _seal_and_verify_memory(
                        replace(config, memory_dir=memory_path),
                        specs=_staged_agent_backup_specs(
                            memory_path=memory_path,
                            staged_layer_config=staged_config,
                            layer_files=layer_files,
                        ),
                    )
                ),
            )
        except RuntimeOwnershipError as exc:
            raise SystemExit(_backup_runtime_ownership_message()) from exc
        print(json.dumps(result, indent=2))
        return
    raise SystemExit(f"Unknown agent backup command: {command}")


def _staged_agent_backup_specs(
    *,
    memory_path: Path,
    staged_layer_config: Path | None,
    layer_files: dict[MemoryLayer, str],
) -> dict[MemoryLayer, LayerSpec]:
    canonical_config = memory_path / "layers.json"
    if staged_layer_config is not None:
        specs = load_layer_specs(staged_layer_config)
    elif canonical_config.is_file():
        specs = load_layer_specs(canonical_config)
    else:
        specs = {
            layer: replace(DEFAULT_LAYER_SPECS[layer], mv2_file=mv2_file)
            for layer, mv2_file in layer_files.items()
        }
    configured_files = {layer: spec.mv2_file for layer, spec in specs.items()}
    if configured_files != layer_files:
        raise MemoryBackupError(
            "Backed-up layer configuration does not match the backup memory layer map"
        )
    return specs


def _seal_and_verify_memory(
    config: AgentConfig,
    *,
    specs: dict[MemoryLayer, LayerSpec] | None = None,
) -> None:
    memory = build_memory_system(
        config.backend,
        config.memory_dir,
        specs=specs if specs is not None else _specs_from_config(config),
    )
    try:
        memory.seal_all()
        results = memory.verify_all()
        failed = [layer.value for layer, ok in results.items() if not ok]
        if failed:
            raise MemoryBackupError(f"Memvid verification failed for layers: {', '.join(failed)}")
    finally:
        memory.close_all()


def _ledger_for_memory_command(args: argparse.Namespace) -> PromotionLedger | None:
    if getattr(args, "cmd", "") != "memory":
        return None
    if getattr(args, "memory_cmd", "") not in {"consolidate", "correct", "compact"}:
        return None
    state_path = Path(getattr(args, "state_path", Path(".nest/state/agent.db")))
    return PromotionLedger(AgentStateStore(state_path))


def _handle_behavior_deltas_command(args: argparse.Namespace) -> None:
    if args.deltas_cmd == "propose":
        summary = summarize_run_capsule(
            runs_dir=args.runs_dir, run_id=args.run_id, backend=args.backend
        )
        ledger = BehaviorDeltaLedger(AgentStateStore(args.state_path))
        extractor = BehaviorDeltaExtractor(ledger=ledger)
        proposals = extractor.propose_from_signals(
            summary.learning_signals,
            run_id=args.run_id,
            dry_run=bool(args.dry_run),
        )
        payload = {
            "run_id": args.run_id,
            "dry_run": bool(args.dry_run),
            "proposal_count": len(proposals),
            "proposals": [delta.to_metadata() for delta in proposals],
        }
        if args.json:
            print(json.dumps(payload, indent=2))
            return
        action = "Would record" if args.dry_run else "Recorded"
        print(f"{action} {len(proposals)} behavior delta proposal(s) for run {args.run_id}.")
        for delta in proposals:
            print(f"- {delta.id} [{delta.kind.value}/{delta.risk.value}] {delta.title}")
        return
    if args.deltas_cmd == "ledger":
        _print_behavior_delta_ledger(args)
        return
    if args.deltas_cmd == "skill-preview":
        _print_behavior_delta_skill_preview(args)
        return
    raise SystemExit(f"Unknown behavior-delta command: {args.deltas_cmd}")


def _print_learning_dashboard(args: argparse.Namespace) -> None:
    ledger = PromotionLedger(AgentStateStore(args.state_path))
    dashboard = ledger.learning_dashboard(since=_parse_since(args.since))
    payload = dashboard.to_payload()
    if args.json:
        print(json.dumps(payload, indent=2))
        return
    scope = f"last {args.since}" if _parse_since(args.since) is not None else "all time"
    headline = payload["headline"]
    print(f"Learning dashboard - {scope}")
    print(f"Auto-activations: {headline['auto_activations']}")
    print(f"Rollbacks: {headline['rollbacks']}")
    print(f"False-positive rate: {headline['false_positive_rate']:.1%}")
    print(f"Activations then rolled back: {headline['activations_then_rolled_back']}")
    avg = headline["average_time_to_rollback_hours"]
    print(f"Average time to rollback: {'n/a' if avg is None else str(avg) + 'h'}")
    if not payload["layers"]:
        print("No learning activity recorded.")
        return
    print()
    print(f"{'Layer':<14} {'Acts':>5} {'Auto':>5} {'Rollbacks':>10} {'FP Rate':>8}")
    for row in payload["layers"]:
        print(
            f"{row['layer']:<14} {row['activations']:>5} {row['auto_activations']:>5} "
            f"{row['rollbacks']:>10} {row['false_positive_rate']:>7.1%}"
        )


def _print_product_readiness(args: argparse.Namespace) -> None:
    report = build_product_readiness_report()
    payload = report.to_dict()
    if args.json:
        print(json.dumps(payload, indent=2))
        return
    headline = payload["headline"]
    print("Full product roadmap")
    print(
        f"Full hosted/team product roadmap complete: {'yes' if headline['product_ready'] else 'no'}"
    )
    print(
        f"Categories: {headline['ready_count']} ready, "
        f"{headline['partial_count']} partial, {headline['missing_count']} missing"
    )
    print()
    for category in payload["categories"]:
        print(f"{category['title']}: {category['status']}")
        print(f"  Next: {category['next_action']}")


def _print_setup_readiness(config: AgentConfig, args: argparse.Namespace) -> None:
    report = build_setup_readiness_report(config)
    payload = report.to_dict()
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print("First-run setup readiness")
        print(f"Ready: {'yes' if payload['ready'] else 'no'}")
        print(
            f"Checks: {payload['pass_count']} pass, {payload['warn_count']} warn, {payload['fail_count']} fail"
        )
        print(f"Next: {payload['next_action']}")
        print()
        for check in payload["checks"]:
            print(f"{check['title']}: {check['status']}")
            print(f"  Detail: {check['detail']}")
            if check["status"] != "pass":
                print(f"  Recovery: {check['recovery']}")
    if getattr(args, "check", False) and not payload["ready"]:
        raise SystemExit(1)


def _print_provider_certification(config: AgentConfig, args: argparse.Namespace) -> None:
    report = build_provider_certification_report(config)
    payload = report.to_dict()
    if args.json:
        print(json.dumps(payload, indent=2))
        return
    headline = payload["headline"]
    print("Provider certification")
    print(f"Policy: {payload['policy_version']}")
    subject = payload["subject"]
    print(f"Subject: commit={subject['commit']} tree={subject['tree_digest']}")
    print(f"Release certified: {'yes' if headline['release_certified'] else 'no'}")
    state_counts = headline["state_counts"]
    print(
        f"Assurance: {headline['release_certified_count']} release certified; "
        + ", ".join(f"{state}={state_counts[state]}" for state in sorted(state_counts))
    )
    readiness_counts = headline["readiness_counts"]
    print(
        "Readiness: "
        + ", ".join(
            f"{status}={readiness_counts[status]}" for status in sorted(readiness_counts)
        )
    )
    print()
    for provider in payload["providers"]:
        print(
            f"{provider['provider']}: readiness={provider['readiness']['status']} "
            f"certification={provider['certification_state']}"
        )
        print(
            "  Checks: "
            f"generate={provider['generate']['status']} "
            f"stream={provider['stream']['status']} "
            f"native_tools={provider['native_tools']['status']} "
            f"tool_normalization={provider['tool_normalization']['status']} "
            f"learning_e2e={provider['learning_e2e']['status']}"
        )
        print(f"  Last tested: {provider['last_tested'] or 'never'}")
        if provider["tested_models"]:
            print(f"  Models: {', '.join(provider['tested_models'])}")
        if provider["tested_profiles"]:
            print(f"  Profiles: {', '.join(provider['tested_profiles'])}")
        if provider["missing_requirements"]:
            print(f"  Missing: {', '.join(provider['missing_requirements'])}")
        print(f"  Next: {provider['next_action']}")


def _print_support_bundle(config: AgentConfig, args: argparse.Namespace) -> None:
    result = export_support_bundle(
        config,
        output_path=args.output,
        log_tail=args.log_tail,
    )
    payload = result.to_dict()
    if args.json:
        print(json.dumps(payload, indent=2))
        return
    print("Support bundle exported")
    print(f"Path: {payload['bundle_path']}")
    print(f"Entries: {len(payload['entries'])}")


def _print_behavior_delta_skill_preview(args: argparse.Namespace) -> None:
    ledger = BehaviorDeltaLedger(AgentStateStore(args.state_path))
    delta = ledger.get_delta(args.delta_id)
    if delta is None:
        raise SystemExit(f"Unknown behavior delta: {args.delta_id}")
    preview = render_skill_candidate_preview(delta, skill_id=args.skill_id)
    payload = preview.to_payload()
    payload["skills_dir"] = str(args.skills_dir)
    payload["message"] = (
        "Preview only; no skill files were written and no executable code was generated."
    )
    if args.json:
        print(json.dumps(payload, indent=2))
        return
    print(f"Skill candidate preview for {delta.id}")
    print(f"Installable: {preview.installable}")
    print(f"Validation: {'ok' if preview.validation['ok'] else 'failed'}")
    if preview.validation["errors"]:
        print("Errors: " + ", ".join(preview.validation["errors"]))
    print()
    print("Manifest:")
    print(json.dumps(preview.manifest, indent=2))
    print()
    print(preview.instructions)


def _print_behavior_delta_ledger(args: argparse.Namespace) -> None:
    ledger = BehaviorDeltaLedger(AgentStateStore(args.state_path))
    report = ledger.report_deltas(since=_parse_since(args.since))
    payload = report.to_payload()
    if args.json:
        print(json.dumps(payload, indent=2))
        return
    scope = f"last {args.since}" if _parse_since(args.since) is not None else "all time"
    print(f"Behavior delta ledger - {scope}")
    print()
    rows = payload["deltas"]
    if not rows:
        print("No behavior delta ledger entries.")
        return
    summary = payload["summary"]
    print(
        "Summary: "
        f"total={summary['total_deltas']} active={summary['active_deltas']} "
        f"useful_rate={summary['useful_rate']:.1%} failure_rate={summary['failure_rate']:.1%} "
        f"rollback_rate={summary['rollback_rate']:.1%} never_activated_rate={summary['never_activated_rate']:.1%}"
    )
    print()
    for row in rows:
        print(
            f"- {row['delta_id']} [{row['kind']}/{row['risk']}/{row['status']}] "
            f"activations={row['activation_count']} useful={row['outcome_counts'].get('useful', 0)} "
            f"failures={row['outcome_counts'].get('caused_failure', 0) + row['outcome_counts'].get('contradicted', 0)} "
            f"rollback={row['outcome_counts'].get('rolled_back', 0)}"
        )
    if payload["recommendations"]:
        print()
        for recommendation in payload["recommendations"]:
            print(f"Recommendation: {recommendation}")


def _print_promotion_ledger(args: argparse.Namespace) -> None:
    ledger = PromotionLedger(AgentStateStore(args.state_path))
    since = _parse_since(args.since)
    target_layer = MemoryLayer(args.layer) if args.layer else None
    summary = ledger.summarize(since=since, target_layer=target_layer, outcome=args.outcome)
    if args.json:
        print(json.dumps(summary.to_payload(), indent=2))
        return
    scope = f"last {args.since}" if since is not None else "all time"
    layer_label = args.layer or "all layers"
    print(f"Promotion ledger - {scope}, {layer_label}")
    if args.outcome:
        print(f"Outcome filter: {args.outcome}")
    print()
    if not summary.rows:
        print("No promotion ledger entries.")
        return
    header = (
        f"{'Layer':<18} {'Promoted':>8} {'Useful':>8} {'Corrected':>10} "
        f"{'Contradicted':>13} {'Tombstoned':>11} {'Never-Used':>11}"
    )
    print(header)
    for row in summary.rows:
        counts = row.outcome_counts
        print(
            f"{row.label:<18} {row.promoted:>8} {counts.get('useful', 0):>8} "
            f"{counts.get('corrected', 0):>10} {counts.get('contradicted', 0):>13} "
            f"{counts.get('tombstoned', 0):>11} {counts.get('never_retrieved', 0):>11}"
        )
    print()
    print("False-positive rate (corrected + contradicted) by gate:")
    for row in summary.rows:
        print(f"  {row.label}: {row.false_positive_rate * 100:.1f}%")
    if summary.recommendations:
        print()
        for recommendation in summary.recommendations:
            print(f"Recommendation: {recommendation}")


def _parse_since(raw: str | None) -> datetime | None:
    if raw is None:
        return None
    value = raw.strip()
    if not value or value.lower() in {"all", "all-time", "all_time"}:
        return None
    now = datetime.now(UTC)
    if value.endswith("d") and value[:-1].isdigit():
        return now - timedelta(days=int(value[:-1]))
    if value.endswith("h") and value[:-1].isdigit():
        return now - timedelta(hours=int(value[:-1]))
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _validate_server_bind(host: str, config: AgentConfig) -> None:
    if _is_loopback_host(host):
        return
    if not config.require_api_auth or not _env_has_value(config.api_auth_token_env):
        raise SystemExit(
            "unsafe_bind: non-loopback server hosts require --require-api-auth "
            f"and a configured {config.api_auth_token_env} token."
        )


def _is_loopback_host(host: str) -> bool:
    normalized = host.strip().lower()
    if normalized in {"localhost", "localhost.localdomain"}:
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _load_channel_payload(args: argparse.Namespace) -> dict[str, Any]:
    if args.payload:
        raw = args.payload
    elif str(args.payload_file) == "-":
        raw = sys.stdin.read()
    else:
        raw = args.payload_file.read_text(encoding="utf-8")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise SystemExit("Channel payload must be a JSON object.")
    return payload


def _print_verify_results(results: dict[MemoryLayer, bool]) -> None:
    for layer, ok in results.items():
        print(f"{layer.value}: {'ok' if ok else 'failed'}")


def _print_vector_status(memory: Any, *, json_output: bool) -> None:
    payload = {
        "layers": {
            layer.value: status.to_payload()
            for layer, status in memory.vector_index_status().items()
        }
    }
    if json_output:
        print(json.dumps(payload, indent=2))
        return
    for layer, status in payload["layers"].items():
        enabled = "enabled" if status["enabled"] else f"disabled: {status['disabled_reason']}"
        print(
            f"{layer}: {enabled}, indexed={status['indexed_count']}, "
            f"stale={status['stale_count']}, missing={status['missing_count']}"
        )


def _print_vector_rebuild(
    memory: Any,
    *,
    layers: tuple[MemoryLayer, ...] | None,
    json_output: bool,
) -> None:
    payload = {
        "rebuilt": {
            layer.value: status.to_payload()
            for layer, status in memory.rebuild_vector_indexes(layers).items()
        }
    }
    if json_output:
        print(json.dumps(payload, indent=2))
        return
    for layer, status in payload["rebuilt"].items():
        enabled = "rebuilt" if status["enabled"] else f"skipped: {status['disabled_reason']}"
        print(
            f"{layer}: {enabled}, indexed={status['indexed_count']}, "
            f"stale={status['stale_count']}, missing={status['missing_count']}"
        )


def _doctor_memory(memory: object, *, dry_run: bool) -> dict[str, Any]:
    backends = getattr(memory, "backends", {})
    report: dict[str, Any] = {}
    if not isinstance(backends, dict):
        return {"ok": False, "error": "memory system does not expose backends"}
    for layer, backend in backends.items():
        layer_name = layer.value if isinstance(layer, MemoryLayer) else str(layer)
        doctor = getattr(backend, "doctor", None)
        try:
            if callable(doctor):
                report[layer_name] = doctor(dry_run=dry_run)
            else:
                verify = getattr(backend, "verify", None)
                report[layer_name] = {
                    "ok": bool(verify()) if callable(verify) else False,
                    "doctor_available": False,
                }
        except Exception as exc:  # noqa: BLE001 - CLI doctor must report every layer honestly
            report[layer_name] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return report


def _doctor_runtime(config: AgentConfig) -> dict[str, Any]:
    memory_dir_exists = config.memory_dir.exists()
    report: dict[str, Any] = {
        "python": _doctor_python(),
        "package": _doctor_package(),
        "optional_extras": _doctor_optional_extras(),
        "provider": _doctor_provider(config),
        "workspace": _doctor_workspace(config),
        "tool_config": _doctor_tool_config(config),
        "validation_container": _doctor_validation_container(config),
        "server": _doctor_server(),
        "tests": _doctor_tests(),
    }
    report["memory"] = _doctor_memory_runtime(config, existed_before=memory_dir_exists)
    report["ok"] = all(_section_ok(value) for value in report.values() if isinstance(value, dict))
    return report


def _doctor_python() -> dict[str, Any]:
    version = tuple(sys.version_info[:3])
    return {
        "ok": version >= (3, 11, 0),
        "version": platform.python_version(),
        "executable": sys.executable,
        "requires": ">=3.11",
    }


def _doctor_package() -> dict[str, Any]:
    try:
        version = importlib_metadata.version("nested-memvid-agent")
        installed = True
    except importlib_metadata.PackageNotFoundError:
        version = None
        installed = False
    return {
        "ok": importlib.util.find_spec("nested_memvid_agent") is not None,
        "distribution_installed": installed,
        "version": version,
        "module_importable": importlib.util.find_spec("nested_memvid_agent") is not None,
    }


def _doctor_optional_extras() -> dict[str, Any]:
    extras = {
        "memvid": "memvid_sdk",
        "openai": "openai",
        "server": "fastapi",
        "uvicorn": "uvicorn",
        "mcp": "mcp",
    }
    return {
        "ok": True,
        "extras": {
            name: _doctor_optional_module(module_name) for name, module_name in extras.items()
        },
    }


def _doctor_optional_module(module_name: str) -> dict[str, Any]:
    available = importlib.util.find_spec(module_name) is not None
    report: dict[str, Any] = {"available": available, "importable": False}
    if not available:
        return report
    try:
        importlib.import_module(module_name)
    except Exception as exc:  # noqa: BLE001 - readiness must expose native-loader failures
        report["error"] = f"{type(exc).__name__}: {exc}"
        return report
    report["importable"] = True
    return report


def _doctor_provider(config: AgentConfig) -> dict[str, Any]:
    key_env = config.api_key_env
    if key_env is None and config.provider == "openai":
        key_env = "OPENAI_API_KEY"
    api_key_present = bool(key_env and _env_has_value(key_env))
    needs_key = config.provider == "openai"
    needs_base_url = config.provider == "openai-compatible"
    return {
        "ok": (not needs_key or api_key_present) and (not needs_base_url or bool(config.base_url)),
        "provider": config.provider,
        "model": config.model,
        "base_url_configured": bool(config.base_url),
        "api_key_env": key_env,
        "api_key_present": api_key_present,
        "timeout_seconds": config.timeout_seconds,
        "max_retries": config.max_retries,
        "stream": config.stream,
    }


def _doctor_workspace(config: AgentConfig) -> dict[str, Any]:
    return {
        "ok": config.workspace.exists() and config.workspace.is_dir(),
        "path": str(config.workspace),
        "exists": config.workspace.exists(),
        "is_dir": config.workspace.is_dir(),
    }


def _doctor_tool_config(config: AgentConfig) -> dict[str, Any]:
    return {
        "ok": True,
        "allow_shell": config.allow_shell,
        "allow_file_write": config.allow_file_write,
        "allow_policy_writes": config.allow_policy_writes,
        "allow_codex_cli": config.allow_codex_cli,
        "allow_plugin_install": config.allow_plugin_install,
        "allow_git_commit": config.allow_git_commit,
        "allow_git_push": config.allow_git_push,
        "allow_remote_mutation": config.allow_remote_mutation,
        "git_write_mode": config.git_write_mode,
        "protected_branches": list(config.protected_branches),
        "secret_store_path": str(config.secret_store_path),
        "secret_backend": config.secret_backend,
        "allow_memory_import": config.allow_memory_import,
        "allow_executable_skills": config.allow_executable_skills,
        "allow_mcp_network_endpoints": config.allow_mcp_network_endpoints,
        "allow_web": config.allow_web,
        "allow_self_modification": config.allow_self_modification,
        "require_approval_for_high_risk_tools": config.require_approval_for_high_risk_tools,
        "approval_ttl_seconds": config.approval_ttl_seconds,
        "web_backend": config.web_backend,
        "max_tool_rounds": config.max_tool_rounds,
        "context_budget_chars": config.context_budget_chars,
    }


def _doctor_validation_container(config: AgentConfig) -> dict[str, Any]:
    image = str(config.validation_container_image or "").strip()
    configured = bool(image)
    digest_pinned = configured and _is_digest_pinned_oci_image(image)
    required_by: list[str] = []
    if config.allow_shell:
        required_by.extend(
            [
                "test.run",
                "lint.run",
                "repair.validate",
                "repair.orchestrate_validate",
            ]
        )
    if config.allow_codex_cli:
        required_by.append("codex.exec")
    required = bool(required_by)
    if not configured:
        detail = (
            "A validation image is required by enabled arbitrary-code tools."
            if required
            else "No OCI-only arbitrary-code tool master gate is enabled."
        )
    elif not digest_pinned:
        detail = "The configured image is not an immutable name@sha256:<64 hex> reference."
    else:
        detail = (
            "The image reference is digest-pinned. Local preload and command dependencies "
            "are verified only when a contained tool runs."
        )
    return {
        "ok": digest_pinned if configured or required else True,
        "required": required,
        "required_by_config_gates": required_by,
        "configured": configured,
        "image": image or None,
        "digest_pinned": digest_pinned,
        "required_format": "name@sha256:<64 hex>",
        "preload_check": "deferred_until_execution",
        "execution_mode": "networkless_secret_free_private_read_only_snapshot",
        "host_fallback": False,
        "detail": detail,
    }


def _doctor_server() -> dict[str, Any]:
    fastapi_available = importlib.util.find_spec("fastapi") is not None
    uvicorn_available = importlib.util.find_spec("uvicorn") is not None
    return {
        "ok": True,
        "fastapi_available": fastapi_available,
        "uvicorn_available": uvicorn_available,
    }


def _doctor_tests() -> dict[str, Any]:
    return {
        "ok": True,
        "pytest_available": importlib.util.find_spec("pytest") is not None,
        "default_command": "pytest -q",
    }


def _doctor_memory_runtime(config: AgentConfig, *, existed_before: bool) -> dict[str, Any]:
    memvid_available = importlib.util.find_spec("memvid_sdk") is not None
    report: dict[str, Any] = {
        "backend": config.backend,
        "path": str(config.memory_dir),
        "directory_existed_before_doctor": existed_before,
        "memvid_required": config.backend == "memvid",
        "memvid_available": memvid_available,
    }
    if config.backend == "memvid" and not memvid_available:
        report["ok"] = False
        report["error"] = "memvid-sdk is not installed"
        return report
    if config.backend == "memvid":
        try:
            importlib.import_module("memvid_sdk")
        except Exception as exc:  # noqa: BLE001 - readiness must expose native-loader failures
            report["ok"] = False
            report["memvid_importable"] = False
            report["error"] = f"memvid-sdk import failed: {type(exc).__name__}: {exc}"
            return report
        report["memvid_importable"] = True

    memory = None
    try:
        specs = load_layer_specs(config.layer_config_path) if config.layer_config_path else None
        memory = build_memory_system(config.backend, config.memory_dir, specs=specs)
        verify = {layer.value: ok for layer, ok in memory.verify_all().items()}
        report["verify"] = verify
        report["ok"] = all(verify.values())
        if config.backend == "memvid":
            report["expected_files"] = {
                layer.value: str(config.memory_dir / f"{layer.value}.mv2") for layer in MemoryLayer
            }
    except Exception as exc:  # noqa: BLE001 - doctor should report readiness failures
        report["ok"] = False
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if memory is not None:
            memory.close_all()
    return report


def _section_ok(value: dict[str, Any]) -> bool:
    ok = value.get("ok")
    if isinstance(ok, bool):
        return ok
    return True


def _env_has_value(name: str) -> bool:
    import os

    return bool(os.getenv(name, "").strip())


def _build_run_manager(
    config: AgentConfig,
    *,
    recover_startup_work: bool = True,
    enforce_single_owner: bool | None = None,
    read_only_observer: bool = False,
) -> RunManager:
    workspace = config.workspace.expanduser().resolve()
    secret_store_path = config.secret_store_path.expanduser()
    if not secret_store_path.is_absolute():
        secret_store_path = workspace / secret_store_path
    secret_store_path = secret_store_path.resolve()
    secret_resolver = None
    if not read_only_observer:
        secret_resolver = build_secret_broker(
            secret_store_path,
            backend=config.secret_backend,
        ).resolve
    state = AgentStateStore(config.state_path)
    events = RunEventBus(state)
    mcp = MCPManager(
        state,
        allow_network_endpoints=config.allow_mcp_network_endpoints,
        secret_resolver=secret_resolver,
        workspace=workspace,
        secret_store_path=secret_store_path,
        secret_backend=config.secret_backend,
    )
    skills = SkillManager(config.skills_dir, state)
    plugins = PluginManager(config.plugins_dir, state)
    try:
        resolved_recovery = False if read_only_observer else recover_startup_work
        resolved_owner = (
            False
            if read_only_observer
            else (resolved_recovery if enforce_single_owner is None else enforce_single_owner)
        )
        return RunManager(
            config=config,
            state=state,
            events=events,
            mcp=mcp,
            skills=skills,
            plugins=plugins,
            secret_resolver=secret_resolver,
            recover_startup_work=resolved_recovery,
            enforce_single_owner=resolved_owner,
            read_only_observer=read_only_observer,
        )
    except RuntimeOwnershipError as exc:
        mcp.shutdown()
        raise SystemExit(_runtime_ownership_message()) from exc


def _runtime_ownership_message() -> str:
    return (
        "Another Kestrel runtime already owns this state database. Use its authenticated "
        "server API, or stop that runtime before starting another CLI worker."
    )


def _backup_runtime_ownership_message() -> str:
    return (
        "Another Kestrel runtime already owns this state database. Stop it cleanly "
        "before creating or restoring a backup."
    )


def _shutdown_run_manager(manager: Any) -> None:
    """Drain a CLI-owned manager before closing its external sessions."""

    shutdown = getattr(manager, "shutdown", None)
    stopped = True
    mcp_stopped = True
    try:
        if callable(shutdown):
            stopped = bool(shutdown(timeout_seconds=5.0))
    finally:
        mcp = getattr(manager, "mcp", None)
        mcp_shutdown = getattr(mcp, "shutdown", None)
        if callable(mcp_shutdown):
            # Third-party/fake managers historically returned ``None`` here;
            # only an explicit false result means bounded MCP termination was
            # not verified.
            mcp_stopped = mcp_shutdown() is not False
    if not stopped:
        raise SystemExit(
            "Kestrel cancelled the run, but its worker did not stop within the bounded "
            "shutdown window. The CLI is exiting with a failure instead of abandoning "
            "background work."
        )
    if not mcp_stopped:
        raise SystemExit(
            "Kestrel stopped the run, but an MCP worker did not stop within the bounded "
            "shutdown window. The CLI is exiting with a failure instead of reporting "
            "a clean shutdown."
        )


def _handle_routines_command(args: argparse.Namespace, config: AgentConfig) -> None:
    state = AgentStateStore(config.state_path)
    manager: RunManager | None = None
    try:
        command = str(args.routines_cmd)
        if command == "list":
            payload: object = [asdict(item) for item in state.list_routines()]
        elif command == "create":
            if args.schedule_kind == "interval" and args.interval_seconds is None:
                raise SystemExit("--interval-seconds is required for interval routines.")
            payload = asdict(
                state.create_routine(
                    routine_id=args.routine_id or f"routine_{uuid4().hex}",
                    name=args.routine_name,
                    prompt=args.routine_prompt,
                    schedule_kind=args.schedule_kind,
                    start_at=args.start_at or datetime.now(UTC),
                    interval_seconds=args.interval_seconds,
                    enabled=False,
                    workspace=args.routine_workspace,
                    provider=args.routine_provider,
                    model=args.routine_model,
                    autonomy_mode=args.routine_autonomy,
                    misfire_grace_seconds=args.misfire_grace_seconds,
                )
            )
        elif command == "show":
            payload = asdict(state.get_routine(args.routine_id))
        elif command == "update":
            fields: dict[str, object] = {}
            for argument, field_name in (
                ("routine_name", "name"),
                ("routine_prompt", "prompt"),
                ("schedule_kind", "schedule_kind"),
                ("start_at", "start_at"),
                ("interval_seconds", "interval_seconds"),
                ("routine_workspace", "workspace"),
                ("routine_provider", "provider"),
                ("routine_model", "model"),
                ("routine_autonomy", "autonomy_mode"),
                ("misfire_grace_seconds", "misfire_grace_seconds"),
            ):
                value = getattr(args, argument)
                if value is not None:
                    fields[field_name] = value
            if args.schedule_kind == "once":
                fields["interval_seconds"] = None
            if not fields:
                raise SystemExit("Routine update requires at least one changed field.")
            payload = asdict(
                state.update_routine(
                    args.routine_id,
                    expected_revision=args.expected_revision,
                    **fields,
                )
            )
        elif command in {"enable", "disable"}:
            payload = asdict(
                state.update_routine(
                    args.routine_id,
                    expected_revision=args.expected_revision,
                    enabled=command == "enable",
                )
            )
        elif command == "delete":
            payload = asdict(
                state.delete_routine(
                    args.routine_id,
                    expected_revision=args.expected_revision,
                )
            )
        elif command == "tick":
            if not config.enable_proactive_routines:
                raise SystemExit(
                    "Proactive routine dispatch is disabled. Pass --enable-proactive-routines or set NEST_AGENT_ENABLE_PROACTIVE_ROUTINES=1."
                )
            manager = _build_run_manager(
                config,
                recover_startup_work=False,
                enforce_single_owner=True,
            )
            service = RoutineService(
                manager.state,
                manager,
                claim_ttl_seconds=config.routine_claim_ttl_seconds,
                max_occurrences_per_tick=config.max_routines_per_tick,
            )
            recovered_run_ids = manager.recover_queued_scheduled_routine_runs()
            for recovered_run_id in recovered_run_ids:
                _wait_for_run(manager, recovered_run_id)
            result = service.tick()
            for dispatch in result.dispatches:
                if dispatch.status == "running":
                    _wait_for_run(manager, dispatch.run_id)
            reconciled = tuple(dict.fromkeys((*result.reconciled, *service.reconcile())))
            payload = {
                **result.to_payload(),
                "reconciled": list(reconciled),
                "recovered_run_ids": list(recovered_run_ids),
                "occurrences": [
                    asdict(manager.state.get_routine_occurrence(item.occurrence_id))
                    for item in result.dispatches
                ],
            }
        elif command == "history":
            state.get_routine(args.routine_id)
            payload = [
                asdict(item)
                for item in state.list_routine_occurrences(
                    args.routine_id,
                    limit=max(1, min(args.limit, 500)),
                )
            ]
        else:
            raise SystemExit(f"Unsupported routines command: {command}")
    except RoutineConflictError as exc:
        raise SystemExit(
            "Routine revision conflict. Current record: "
            + json.dumps(asdict(exc.current), sort_keys=True)
        ) from exc
    except KeyError as exc:
        raise SystemExit(str(exc)) from exc
    except sqlite3.IntegrityError as exc:
        raise SystemExit(
            "Routine id already exists, including a tombstoned prior routine."
        ) from exc
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    finally:
        if manager is not None:
            _shutdown_run_manager(manager)
    _print_routine_payload(payload, json_output=bool(args.json))


def _print_routine_payload(payload: object, *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(payload, indent=2))
        return
    if isinstance(payload, list):
        if not payload:
            print("No routines or occurrences found.")
            return
        for item in payload:
            if isinstance(item, dict):
                identifier = item.get("routine_id") or item.get("occurrence_id") or "routine"
                print(f"{identifier}: {item.get('status', item.get('name', ''))}")
            else:
                print(item)
        return
    print(json.dumps(payload, indent=2))


def _create_run_and_print(
    manager: RunManager,
    message: str,
    *,
    session_id: str,
    json_output: bool,
    wait: bool,
    include_events: bool,
) -> int:
    run = manager.create_run(message=message, session_id=session_id)
    if wait:
        _wait_for_run(manager, run.run_id)
    payload = _run_payload(manager, run.run_id, include_events=include_events)
    if json_output:
        print(json.dumps(payload, indent=2))
    else:
        _print_run_payload(payload)
    return _run_exit_code(payload)


def _run_exit_code(payload: dict[str, Any]) -> int:
    """Map the observed run state to the public ``nest-agent run`` contract."""

    status = str(payload.get("status") or "").strip().lower()
    if status == "completed":
        return 0
    if status == "blocked":
        return 2
    return 1


def _run_manager_chat_and_print(
    manager: RunManager,
    message: str,
    *,
    session_id: str,
    prefix: str = "",
) -> int:
    run = manager.create_run(message=message, session_id=session_id)
    _wait_for_run(manager, run.run_id)
    payload = _run_payload(manager, run.run_id, include_events=False)
    assistant_message = str(payload.get("assistant_message") or "")
    print(f"{prefix}{assistant_message}" if prefix else assistant_message)
    print(f"run_id: {payload['run_id']}")
    print(f"status: {payload['status']}")
    if payload.get("stop_reason"):
        print(f"stop_reason: {payload['stop_reason']}")
    _print_pending_approvals(payload)
    return _run_exit_code(payload)


def _print_status(
    manager: RunManager,
    *,
    run_id: str | None,
    json_output: bool,
    include_events: bool,
) -> None:
    if run_id:
        payload = _run_payload(manager, run_id, include_events=include_events)
        if json_output:
            print(json.dumps(payload, indent=2))
        else:
            _print_run_payload(payload)
        return

    payload = {"runs": manager.list_runs(), "sessions": manager.list_sessions()}
    if json_output:
        print(json.dumps(payload, indent=2))
        return
    if not payload["runs"]:
        print("No runs found.")
        return
    for run in payload["runs"]:
        print(
            f"{run['run_id']} [{run['status']}] "
            f"session={run['session_id']} stop={run['stop_reason'] or '-'}"
        )


def _print_approvals(manager: RunManager, *, status: str | None, json_output: bool) -> None:
    approvals = manager.list_approvals(status=status)
    if json_output:
        print(json.dumps({"approvals": approvals}, indent=2))
        return
    if not approvals:
        print("No approvals found.")
        return
    for approval in approvals:
        print(
            f"{approval['approval_id']} [{approval['status']}] "
            f"run={approval['run_id']} tool={approval['tool_name']} risk={approval['risk']}"
        )


def _decide_approval_and_print(
    manager: RunManager,
    *,
    approval_id: str,
    approved: bool,
    arguments_json: str | None,
    json_output: bool,
    wait: bool,
) -> int:
    arguments = _json_object_or_none(arguments_json)
    decision = manager.decide_approval(approval_id, approved=approved, arguments=arguments)
    run_payload: dict[str, Any] | None = None
    if approved and wait:
        _wait_for_run(manager, str(decision["run_id"]))
        run_payload = _run_payload(manager, str(decision["run_id"]), include_events=False)
    elif not approved:
        run_payload = _run_payload(manager, str(decision["run_id"]), include_events=False)

    payload = {"approval": decision, "run": run_payload}
    if json_output:
        print(json.dumps(payload, indent=2))
    else:
        print(f"{approval_id}: {'approved' if approved else 'denied'}")
        if run_payload is not None:
            print(f"run_id: {run_payload['run_id']}")
            print(f"status: {run_payload['status']}")
            if run_payload.get("stop_reason"):
                print(f"stop_reason: {run_payload['stop_reason']}")
    if approved and run_payload is not None:
        return _run_exit_code(run_payload)
    return 0


def _run_payload(manager: RunManager, run_id: str, *, include_events: bool) -> dict[str, Any]:
    payload = manager.get_run(run_id)
    if include_events:
        payload["events"] = manager.state.list_run_steps(run_id)
    return payload


def _print_run_payload(payload: dict[str, Any]) -> None:
    assistant_message = str(payload.get("assistant_message") or "")
    if assistant_message:
        print(assistant_message)
    print(f"run_id: {payload['run_id']}")
    print(f"session_id: {payload['session_id']}")
    print(f"status: {payload['status']}")
    print(f"stop_reason: {payload.get('stop_reason') or '-'}")
    print(f"context_chars: {payload.get('context_chars', 0)}")
    print(f"tool_count: {payload.get('tool_count', 0)}")
    if payload.get("error"):
        print(f"error: {payload['error']}")
    _print_pending_approvals(payload)


def _print_pending_approvals(payload: dict[str, Any]) -> None:
    approvals = [
        item
        for item in payload.get("approvals", [])
        if isinstance(item, dict) and item.get("status") == "pending"
    ]
    if not approvals:
        return
    print("pending_approvals:")
    for approval in approvals:
        print(f"- {approval['approval_id']} tool={approval['tool_name']} risk={approval['risk']}")


def _handle_plugins_command(
    args: argparse.Namespace,
    manager: RunManager,
    *,
    backend: str,
    memory_dir: Path,
) -> None:
    try:
        if args.plugins_cmd == "list":
            _print_plugins(manager.plugins.list_plugins(), json_output=args.json)
            return
        if args.plugins_cmd == "review":
            _require_plugin_install_enabled(manager.config)
            review = manager.plugins.review(args.source, ref=args.ref)
            _print_plugin_review(review, json_output=args.json)
            return
        if args.plugins_cmd == "install":
            _require_plugin_install_enabled(manager.config)
            plugin = manager.plugins.install(
                args.source,
                ref=args.ref,
                enable=args.enable,
                overwrite=args.overwrite,
            )
            _write_plugin_audit(
                manager, backend=backend, memory_dir=memory_dir, action="install", plugin=plugin
            )
            _print_plugin(plugin, json_output=args.json)
            return
        if args.plugins_cmd == "inspect":
            _print_plugin(manager.plugins.get_plugin(args.plugin_id), json_output=args.json)
            return
        if args.plugins_cmd == "enable":
            _require_plugin_install_enabled(manager.config)
            plugin = manager.plugins.set_enabled(args.plugin_id, True)
            _write_plugin_audit(
                manager, backend=backend, memory_dir=memory_dir, action="enable", plugin=plugin
            )
            _print_plugin(plugin, json_output=args.json)
            return
        if args.plugins_cmd == "disable":
            plugin = manager.plugins.set_enabled(args.plugin_id, False)
            _write_plugin_audit(
                manager, backend=backend, memory_dir=memory_dir, action="disable", plugin=plugin
            )
            _print_plugin(plugin, json_output=args.json)
            return
        if args.plugins_cmd == "update":
            _require_plugin_install_enabled(manager.config)
            plugin = manager.plugins.update(args.plugin_id, ref=args.ref)
            _write_plugin_audit(
                manager, backend=backend, memory_dir=memory_dir, action="update", plugin=plugin
            )
            _print_plugin(plugin, json_output=args.json)
            return
        if args.plugins_cmd == "remove":
            result = manager.plugins.remove(args.plugin_id)
            if args.json:
                print(json.dumps(result, indent=2))
            else:
                print(f"removed: {result['plugin_id']}")
            return
    except (PluginError, FileExistsError, KeyError) as exc:
        raise SystemExit(str(exc)) from exc


def _require_plugin_install_enabled(config: AgentConfig) -> None:
    if not config.allow_plugin_install:
        raise SystemExit("plugin_install_disabled")


def _write_plugin_audit(
    manager: RunManager,
    *,
    backend: str,
    memory_dir: Path,
    action: str,
    plugin: dict[str, Any],
) -> None:
    memory = build_memory_system(backend, memory_dir)
    try:
        manager.plugins.write_audit_memory(memory, action=action, plugin=plugin)
    finally:
        memory.close_all()


def _print_plugins(plugins: list[dict[str, Any]], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps({"plugins": plugins}, indent=2))
        return
    if not plugins:
        print("No plugins installed.")
        return
    for plugin in plugins:
        state = "enabled" if plugin["enabled"] else "not enabled"
        capabilities = ", ".join(str(item) for item in plugin.get("capabilities", [])) or "none"
        print(
            f"{plugin['id']} [{state}] {plugin['source_url']} @ {plugin['commit_sha'][:12]} capabilities={capabilities}"
        )


def _print_plugin(plugin: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(plugin, indent=2))
        return
    state = "enabled" if plugin["enabled"] else "not enabled"
    print(f"{plugin['id']} [{state}]")
    print(f"name: {plugin['name']}")
    print(f"source: {plugin['source_url']}")
    print(f"commit: {plugin['commit_sha']}")
    print(f"format: {plugin['format']}")
    print(
        f"capabilities: {', '.join(str(item) for item in plugin.get('capabilities', [])) or 'none'}"
    )
    warnings = plugin.get("risk_report", {}).get("warnings", [])
    unsupported = plugin.get("risk_report", {}).get("unsupported_features", [])
    if warnings:
        print(f"warnings: {', '.join(str(item) for item in warnings)}")
    if unsupported:
        print(f"unsupported: {', '.join(str(item) for item in unsupported)}")


def _print_plugin_review(review: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(review, indent=2))
        return
    manifest = review.get("manifest", {})
    plugin_id = manifest.get("id", "unknown") if isinstance(manifest, dict) else "unknown"
    print(f"{plugin_id} [review]")
    print(f"source: {review['source_url']}")
    print(f"commit: {review['commit_sha']}")
    blockers = [str(item) for item in review.get("enable_blockers", [])]
    print(f"enable blockers: {', '.join(blockers) if blockers else 'none'}")


def _cli_run_idle_timeout_seconds(config: Any) -> float:
    """Bound one no-progress interval without racing provider retry policy.

    ``timeout_seconds`` is a per-attempt provider timeout. A CLI wait that only
    allows one attempt can cancel a healthy cold-starting local model while its
    configured retry is already in flight. Each durable run step starts a new
    interval, so multi-round tool turns remain bounded without multiplying one
    giant wall-clock deadline up front.
    """

    timeout_seconds = max(float(getattr(config, "timeout_seconds", 0.0)), 0.0)
    max_retries = max(int(getattr(config, "max_retries", 0)), 0)
    provider_calls = 2 if bool(getattr(config, "llm_turn_summaries", False)) else 1
    return max(
        (timeout_seconds * (max_retries + 1) * provider_calls) + 15.0,
        15.0,
    )


def _wait_for_run(manager: RunManager, run_id: str) -> dict[str, Any]:
    idle_timeout_seconds = _cli_run_idle_timeout_seconds(manager.config)
    deadline = monotonic() + idle_timeout_seconds
    terminal = {"completed", "failed", "blocked", "cancelled"}
    last_step_id = 0
    thread = manager._threads.get(
        run_id
    )  # CLI wait is in-process; yield to the worker before polling state.
    while monotonic() < deadline:
        if thread is not None and thread.is_alive():
            thread.join(timeout=0.25)
        run = manager.get_run(run_id)
        if str(run["status"]) in terminal:
            return run
        state = getattr(manager, "state", None)
        list_run_steps = getattr(state, "list_run_steps", None)
        if callable(list_run_steps):
            steps = list_run_steps(run_id, after_id=last_step_id, limit=200)
            if steps:
                last_step_id = max(int(step["id"]) for step in steps)
                deadline = monotonic() + idle_timeout_seconds
        sleep(0.25)
    raise SystemExit(
        f"Run {run_id} made no durable progress within the CLI wait timeout "
        f"({idle_timeout_seconds:g}s)."
    )


def _json_object_or_none(raw: str | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Approval arguments must be a JSON object: {exc}") from exc
    if not isinstance(value, dict):
        raise SystemExit("Approval arguments must be a JSON object.")
    return {str(key): val for key, val in value.items()}


def _run_eval_command(config: AgentConfig) -> None:
    script = Path(__file__).resolve().parents[2] / "scripts" / "run_golden_evals.py"
    command = [
        sys.executable,
        str(script),
        "--backend",
        config.backend,
        "--memory-dir",
        str(config.memory_dir),
        "--provider",
        config.provider,
        "--model",
        config.model,
        "--workspace",
        str(config.workspace),
    ]
    completed = subprocess.run(command, check=False)  # nosec B603
    raise SystemExit(completed.returncode)


def _chat_and_print(
    agent: NestedMV2Agent, user_message: str, *, session_id: str, prefix: str = ""
) -> None:
    streamed = False
    prefix_printed = False

    def stream_handler(event: LLMStreamEvent) -> None:
        nonlocal streamed, prefix_printed
        if event.type != "token":
            return
        content = event.content
        if not content:
            return
        if prefix and not prefix_printed:
            print(prefix, end="", flush=True)
            prefix_printed = True
        print(content, end="", flush=True)
        streamed = True

    result = agent.chat(user_message, session_id=session_id, stream_handler=stream_handler)
    if streamed:
        print()
        return
    if prefix_printed:
        print(result.assistant_message)
        return
    print(f"{prefix}{result.assistant_message}" if prefix else result.assistant_message)


def _handle_slash_command_for_manager(
    manager: RunManager,
    config: AgentConfig,
    command: str,
    session_id: str,
) -> bool:
    if not command.startswith("/"):
        return False
    agent = manager.build_runtime_agent(config)
    try:
        return _handle_slash_command(agent, command, session_id, manager=manager)
    finally:
        manager.close_runtime_agent(agent)


def _handle_slash_command(
    agent: NestedMV2Agent,
    command: str,
    session_id: str,
    *,
    manager: RunManager | None = None,
) -> bool:
    if not command.startswith("/"):
        return False
    name, _, rest = command.partition(" ")
    query = rest.strip()

    if name in {"/exit", "/quit"}:
        return True

    if name == "/help":
        print(_slash_help())
        return True

    if name in {"/self", "/soul"}:
        execution = agent.tools.execute(
            ToolCall(name="self.inspect", arguments={"include_tools": True}),
            ToolContext(
                memory=agent.memory,
                config=agent.config,
                workspace=agent.config.workspace,
                event_log=agent.event_log,
                session_id=session_id,
            ),
        )
        if not execution.success:
            print(execution.content)
            return True
        data = execution.data
        identity = data.get("identity", {}) if isinstance(data.get("identity"), dict) else {}
        print(f"{identity.get('display_name', 'Soul')} ({identity.get('name', 'Kestrel')})")
        print(identity.get("description", ""))
        print("Memory layers:")
        for layer in data.get("memory_layers", []):
            if isinstance(layer, dict):
                print(f"- {layer.get('layer')}: {layer.get('mv2_file')}")
        print(f"Tools: {len(data.get('tools', []))}")
        return True

    if name == "/capabilities":
        execution = agent.tools.execute(
            ToolCall(name="self.inspect", arguments={"include_tools": True}),
            ToolContext(
                memory=agent.memory,
                config=agent.config,
                workspace=agent.config.workspace,
                event_log=agent.event_log,
                session_id=session_id,
            ),
        )
        if not execution.success:
            print(execution.content)
            return True
        for spec in execution.data.get("tools", []):
            if isinstance(spec, dict):
                approval = "approval required" if spec.get("requires_approval") else "allowed"
                print(
                    f"{spec.get('name')} [{spec.get('risk')}, {approval}] - {spec.get('description')}"
                )
        return True

    if name == "/web":
        if not query:
            print("Usage: /web <query>")
            return True
        execution = agent.tools.execute(
            ToolCall(
                name="web.search",
                arguments={"query": query, "max_results": agent.config.web_max_results},
            ),
            ToolContext(
                memory=agent.memory,
                config=agent.config,
                workspace=agent.config.workspace,
                event_log=agent.event_log,
                session_id=session_id,
            ),
        )
        if not execution.success:
            print(execution.content)
            return True
        for item in execution.data.get("results", []):
            if isinstance(item, dict):
                print(f"{item.get('title')}\n{item.get('url')}\n{item.get('snippet')}\n")
        return True

    if name == "/tools":
        for spec in agent.tools.specs():
            approval = "approval required" if spec.requires_approval else "allowed"
            print(f"{spec.name} [{spec.risk}, {approval}] - {spec.description}")
        return True

    if name == "/plugins":
        if manager is None:
            print("Plugin status requires CLI run-manager mode.")
            return True
        _print_plugins(manager.plugins.list_plugins(), json_output=False)
        return True

    if name == "/context":
        if not query:
            print("Usage: /context <query>")
            return True
        compiled = agent.compiler.compile(objective=query, query=query)
        print(compiled.prompt)
        return True

    if name == "/pack":
        if not query:
            print("Usage: /pack <query>")
            return True
        packed = ContextPacker(agent.memory).pack(
            ContextPackRequest(
                objective=query,
                query=query,
                token_budget=agent.config.context_pack_token_budget,
                expand_raw=agent.config.context_pack_expand_raw,
            )
        )
        print(packed.prompt)
        return True

    if name == "/conflicts":
        if not query:
            print("Usage: /conflicts <query>")
            return True
        execution = agent.tools.execute(
            ToolCall(name="memory.conflicts", arguments={"query": query, "k": 8}),
            ToolContext(
                memory=agent.memory,
                config=agent.config,
                workspace=agent.config.workspace,
                event_log=agent.event_log,
                session_id=session_id,
            ),
        )
        print(execution.content)
        return True

    if name == "/capsule":
        run_id = query or session_id
        summary = summarize_run_capsule(
            runs_dir=agent.config.memory_dir.parent / "runs",
            run_id=run_id,
            backend=agent.config.backend,
        )
        print(json.dumps(summary.to_payload(), indent=2))
        return True

    if name == "/memory":
        if not query:
            print("Usage: /memory <query>")
            return True
        hits = agent.memory.retrieve(RetrievalQuery(query=query))
        for hit in hits:
            print(f"[{hit.record.layer.value}] score={hit.score:.3f} {hit.record.title}")
            print(hit.snippet or hit.record.content[:500])
            print()
        if not hits:
            print("No memory hits.")
        return True

    if name == "/doctor":
        results = agent.memory.verify_all()
        for layer, ok in results.items():
            print(f"{layer.value}: {'ok' if ok else 'failed'}")
        return True

    if name == "/status":
        if manager is not None:
            _print_status(manager, run_id=query or None, json_output=False, include_events=False)
        else:
            print(f"session_id: {session_id}")
            print(f"provider: {agent.config.provider}")
            print(f"model: {agent.config.model}")
            print(f"backend: {agent.config.backend}")
            print(f"memory_dir: {agent.config.memory_dir}")
            print(f"tools: {len(agent.tools.specs())}")
        return True

    if name in {"/approve", "/deny"}:
        if manager is None:
            print("Approval decisions require CLI run-manager mode.")
            print("Use `nest-agent approve <approval_id>` or `nest-agent deny <approval_id>`.")
            return True
        approval_id, _, raw_arguments = query.partition(" ")
        if not approval_id:
            print(f"Usage: {name} <approval_id> [arguments-json]")
            return True
        _decide_approval_and_print(
            manager,
            approval_id=approval_id,
            approved=name == "/approve",
            arguments_json=raw_arguments.strip() or None,
            json_output=False,
            wait=True,
        )
        return True

    if name == "/session":
        print(f"session_id: {session_id}")
        if agent.event_log is not None:
            events = agent.event_log.tail(limit=5)
            print(f"recent_events: {len(events)}")
            for event in events:
                print(f"- {event.created_at} {event.type}")
        return True

    print(f"Unknown slash command: {name}")
    return True


def _slash_help() -> str:
    return "\n".join(
        [
            "Available slash commands:",
            "/help",
            "/self",
            "/soul",
            "/capabilities",
            "/web <query>",
            "/tools",
            "/plugins",
            "/context <query>",
            "/pack <query>",
            "/conflicts <query>",
            "/memory <query>",
            "/doctor",
            "/status",
            "/session",
            "/capsule [run_id]",
            "/approve <approval_id>",
            "/deny <approval_id>",
            "/exit",
        ]
    )


if __name__ == "__main__":
    main()
