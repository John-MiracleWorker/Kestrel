from __future__ import annotations

import calendar
import time
import uuid
from dataclasses import dataclass, field
from typing import Any


VIEW_COCKPIT = "cockpit"
VIEW_CHAT = "chat"
VIEW_TASKS = "tasks"
VIEW_APPROVALS = "approvals"
VIEW_SKILLS = "skills"
VIEWS = (VIEW_COCKPIT, VIEW_CHAT, VIEW_TASKS, VIEW_APPROVALS, VIEW_SKILLS)


def fuzzy_match(text: str, query: str) -> bool:
    haystack = str(text or "").lower()
    needle = str(query or "").strip().lower()
    if not needle:
        return True
    index = 0
    for char in haystack:
        if index < len(needle) and char == needle[index]:
            index += 1
    return index == len(needle)


def component_summary(pack: dict[str, Any]) -> str:
    components = pack.get("components") or []
    if not isinstance(components, list) or not components:
        return "none"
    counts: dict[str, int] = {}
    for item in components:
        if not isinstance(item, dict):
            continue
        component_type = str(item.get("type") or "unknown")
        counts[component_type] = counts.get(component_type, 0) + 1
    return ", ".join(
        f"{component_type} x{count}"
        for component_type, count in sorted(counts.items(), key=lambda pair: pair[0])
    ) or "none"


def summarize_event(event: dict[str, Any]) -> str:
    event_type = str(event.get("type") or "").strip()
    content = str(event.get("content") or "").strip()
    if not content:
        return ""
    prefix = {
        "thinking": "THINK",
        "tool_call": "TOOL",
        "tool_result": "RESULT",
        "plan_created": "PLAN",
        "step_started": "STEP",
        "step_complete": "DONE",
        "approval_needed": "APPROVAL",
        "task_complete": "COMPLETE",
        "task_failed": "FAILED",
        "error": "ERROR",
        "status": "STATUS",
        "approval_resolved": "APPROVED",
        "approval_denied": "DENIED",
    }.get(event_type, event_type.upper() or "EVENT")
    return f"{prefix}: {content}"


def relative_age(iso_stamp: str) -> str:
    try:
        clean = str(iso_stamp or "").strip()
        if not clean:
            return "now"
        if clean.endswith("Z"):
            clean = clean[:-1]
        clean = clean.replace("T", " ")
        created = calendar.timegm(time.strptime(clean[:19], "%Y-%m-%d %H:%M:%S"))
    except Exception:
        return "now"
    delta = max(0, int(time.time() - created))
    if delta < 60:
        return "now"
    if delta < 3600:
        return f"{delta // 60}m"
    if delta < 86400:
        return f"{delta // 3600}h"
    return f"{delta // 86400}d"


@dataclass
class NotificationItem:
    id: str
    title: str
    text: str
    level: str = "info"
    created_at: float = field(default_factory=time.time)
    read: bool = False
    occurrences: int = 1


@dataclass
class ChatMessage:
    id: str
    role: str
    text: str
    meta: str = ""
    task_id: str = ""
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    approval: dict[str, Any] | None = None


@dataclass
class ProcessSnapshot:
    state: str = "idle"
    label: str = "READY"
    detail: str = ""
    thinking: str = ""
    phases: list[str] = field(default_factory=list)
    accent: str = "cyan"
    active: bool = False
    task_id: str = ""


@dataclass
class TuiState:
    active_view: str = VIEW_COCKPIT
    compact_mode: bool = False
    inspector_open: bool = True
    notice_text: str = "Booting operator cockpit"
    notice_level: str = "info"
    busy_domains: set[str] = field(default_factory=set)
    status: dict[str, Any] = field(default_factory=dict)
    runtime: dict[str, Any] = field(default_factory=dict)
    channels: dict[str, Any] = field(default_factory=dict)
    tasks: list[dict[str, Any]] = field(default_factory=list)
    approvals: list[dict[str, Any]] = field(default_factory=list)
    skills: list[dict[str, Any]] = field(default_factory=list)
    selected_task_id: str = ""
    selected_approval_id: str = ""
    selected_skill_id: str = ""
    task_details: dict[str, dict[str, Any]] = field(default_factory=dict)
    task_timelines: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    task_artifacts: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    task_approvals: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    chat_messages: list[ChatMessage] = field(default_factory=list)
    active_process: ProcessSnapshot = field(default_factory=ProcessSnapshot)
    notifications: list[NotificationItem] = field(default_factory=list)
    chat_draft: str = ""
    skill_query: str = ""
    motion_level: str = "high"
    reduced_motion: bool = False
    mouse_enabled: bool = True
    notification_verbosity: str = "all"


class TuiStore:
    def __init__(self, *, config: dict[str, Any]):
        tui_config = config.get("tui") if isinstance(config.get("tui"), dict) else {}
        active_view = str(tui_config.get("default_view") or VIEW_COCKPIT)
        self.state = TuiState(
            active_view=active_view if active_view in VIEWS else VIEW_COCKPIT,
            motion_level=str(tui_config.get("motion_level") or "high"),
            reduced_motion=bool(tui_config.get("reduced_motion", False)),
            mouse_enabled=bool(tui_config.get("mouse_enabled", True)),
            notification_verbosity=str(tui_config.get("notification_verbosity") or "all"),
        )

    def set_view(self, view: str) -> bool:
        if view not in VIEWS or self.state.active_view == view:
            return False
        self.state.active_view = view
        return True

    def set_compact_mode(self, compact: bool) -> bool:
        if self.state.compact_mode == compact:
            return False
        self.state.compact_mode = compact
        if compact:
            self.state.inspector_open = False
        return True

    def set_motion_level(self, motion_level: str) -> None:
        self.state.motion_level = motion_level

    def toggle_inspector(self) -> bool:
        self.state.inspector_open = not self.state.inspector_open
        return self.state.inspector_open

    def set_notice(self, text: str, level: str = "info") -> None:
        self.state.notice_text = text
        self.state.notice_level = level

    def mark_busy(self, domain: str, busy: bool) -> None:
        if busy:
            self.state.busy_domains.add(domain)
        else:
            self.state.busy_domains.discard(domain)

    def add_notification(self, title: str, text: str, *, level: str = "info") -> None:
        if self.state.notification_verbosity == "errors" and level not in {"error", "warning"}:
            return
        normalized_title = str(title or "").strip()
        normalized_text = str(text or "").strip()
        normalized_level = str(level or "info").strip() or "info"
        now = time.time()
        for index, existing in enumerate(self.state.notifications):
            if (
                existing.title == normalized_title
                and existing.text == normalized_text
                and existing.level == normalized_level
            ):
                existing.created_at = now
                existing.read = False
                existing.occurrences += 1
                if index != 0:
                    self.state.notifications.insert(0, self.state.notifications.pop(index))
                return
        item = NotificationItem(
            id=str(uuid.uuid4()),
            title=normalized_title,
            text=normalized_text,
            level=normalized_level,
            created_at=now,
        )
        self.state.notifications.insert(0, item)
        del self.state.notifications[100:]

    def unread_notifications(self) -> int:
        return sum(0 if item.read else 1 for item in self.state.notifications)

    def mark_notifications_read(self) -> None:
        for item in self.state.notifications:
            item.read = True

    def apply_runtime(self, *, status: dict[str, Any], runtime: dict[str, Any], channels: dict[str, Any]) -> bool:
        changed = False
        if status != self.state.status:
            self.state.status = status
            changed = True
        if runtime != self.state.runtime:
            self.state.runtime = runtime
            changed = True
        if channels != self.state.channels:
            self.state.channels = channels
            changed = True
        return changed

    def apply_tasks(self, tasks: list[dict[str, Any]]) -> bool:
        previous = {str(task.get("id")): str(task.get("status") or "") for task in self.state.tasks}
        current = {str(task.get("id")): str(task.get("status") or "") for task in tasks}
        if current == previous and len(tasks) == len(self.state.tasks):
            return False

        for task in tasks:
            task_id = str(task.get("id") or "")
            new_status = str(task.get("status") or "")
            old_status = previous.get(task_id)
            if old_status and old_status != new_status and new_status in {"completed", "failed"}:
                level = "success" if new_status == "completed" else "error"
                self.add_notification(
                    f"Task {new_status}",
                    str(task.get("goal") or task_id),
                    level=level,
                )

        self.state.tasks = tasks
        if self.state.selected_task_id and not any(str(task.get("id")) == self.state.selected_task_id for task in tasks):
            self.state.selected_task_id = ""
        if not self.state.selected_task_id and tasks:
            self.state.selected_task_id = str(tasks[0].get("id") or "")
        return True

    def apply_approvals(self, approvals: list[dict[str, Any]]) -> bool:
        previous_ids = {str(item.get("id") or "") for item in self.state.approvals}
        current_ids = {str(item.get("id") or "") for item in approvals}
        if current_ids == previous_ids and approvals == self.state.approvals:
            return False

        new_ids = current_ids - previous_ids
        for approval in approvals:
            if str(approval.get("id") or "") in new_ids:
                self.add_notification(
                    "Approval required",
                    str(approval.get("command") or approval.get("operation") or "Action pending approval"),
                    level="warning",
                )

        self.state.approvals = approvals
        if self.state.selected_approval_id and not any(str(item.get("id")) == self.state.selected_approval_id for item in approvals):
            self.state.selected_approval_id = ""
        if not self.state.selected_approval_id and approvals:
            self.state.selected_approval_id = str(approvals[0].get("id") or "")
        return True

    def apply_skills(self, skills: list[dict[str, Any]]) -> bool:
        previous = [(str(item.get("pack_id") or ""), bool(item.get("enabled"))) for item in self.state.skills]
        current = [(str(item.get("pack_id") or ""), bool(item.get("enabled"))) for item in skills]
        if current == previous and len(skills) == len(self.state.skills):
            return False
        self.state.skills = skills
        if self.state.selected_skill_id and not any(str(item.get("pack_id")) == self.state.selected_skill_id for item in skills):
            self.state.selected_skill_id = ""
        if not self.state.selected_skill_id and skills:
            self.state.selected_skill_id = str(skills[0].get("pack_id") or "")
        return True

    def cache_task_bundle(
        self,
        task_id: str,
        *,
        detail: dict[str, Any] | None = None,
        timeline: list[dict[str, Any]] | None = None,
        artifacts: list[dict[str, Any]] | None = None,
        approvals: list[dict[str, Any]] | None = None,
    ) -> None:
        if detail is not None:
            self.state.task_details[task_id] = detail
        if timeline is not None:
            self.state.task_timelines[task_id] = timeline
        if artifacts is not None:
            self.state.task_artifacts[task_id] = artifacts
        if approvals is not None:
            self.state.task_approvals[task_id] = approvals

    def task_bundle(self, task_id: str) -> dict[str, Any]:
        return {
            "detail": self.state.task_details.get(task_id) or {},
            "timeline": self.state.task_timelines.get(task_id) or [],
            "artifacts": self.state.task_artifacts.get(task_id) or [],
            "approvals": self.state.task_approvals.get(task_id) or [],
        }

    def select_task(self, task_id: str) -> None:
        self.state.selected_task_id = task_id

    def select_approval(self, approval_id: str) -> None:
        self.state.selected_approval_id = approval_id

    def select_skill(self, pack_id: str) -> None:
        self.state.selected_skill_id = pack_id

    def filtered_skills(self) -> list[dict[str, Any]]:
        query = self.state.skill_query.strip()
        if not query:
            return self.state.skills
        return [
            pack
            for pack in self.state.skills
            if fuzzy_match(str(pack.get("pack_id") or ""), query)
            or fuzzy_match(str(pack.get("name") or ""), query)
            or fuzzy_match(str(pack.get("description") or ""), query)
        ]

    def selected_task(self) -> dict[str, Any]:
        for task in self.state.tasks:
            if str(task.get("id") or "") == self.state.selected_task_id:
                return task
        return {}

    def selected_task_approvals(self) -> list[dict[str, Any]]:
        if not self.state.selected_task_id:
            return []
        approvals = self.state.task_approvals.get(self.state.selected_task_id)
        if approvals is not None:
            return approvals
        return [
            approval
            for approval in self.state.approvals
            if str(approval.get("task_id") or "") == self.state.selected_task_id
        ]

    def pending_approval_for_task(self, task_id: str) -> dict[str, Any]:
        approvals = self.state.task_approvals.get(task_id)
        for approval in approvals or []:
            if str(approval.get("status") or "pending") == "pending":
                return approval
        for approval in self.state.approvals:
            if str(approval.get("task_id") or "") == task_id:
                return approval
        return {}

    def selected_approval(self) -> dict[str, Any]:
        for approval in self.state.approvals:
            if str(approval.get("id") or "") == self.state.selected_approval_id:
                return approval
        return {}

    def selected_skill(self) -> dict[str, Any]:
        for pack in self.filtered_skills():
            if str(pack.get("pack_id") or "") == self.state.selected_skill_id:
                return pack
        return {}

    def append_chat(
        self,
        *,
        role: str,
        text: str,
        meta: str = "",
        task_id: str = "",
        artifacts: list[dict[str, Any]] | None = None,
        approval: dict[str, Any] | None = None,
    ) -> ChatMessage:
        message = ChatMessage(
            id=str(uuid.uuid4()),
            role=role,
            text=text,
            meta=meta,
            task_id=task_id,
            artifacts=list(artifacts or []),
            approval=approval,
        )
        self.state.chat_messages.append(message)
        del self.state.chat_messages[:-200]
        return message

    def latest_artifacts(self) -> list[dict[str, Any]]:
        for message in reversed(self.state.chat_messages):
            if message.artifacts:
                return message.artifacts
        return []

    def set_process(
        self,
        *,
        state: str,
        label: str,
        detail: str = "",
        thinking: str = "",
        phases: list[str] | None = None,
        accent: str = "cyan",
        active: bool = False,
        task_id: str = "",
    ) -> None:
        self.state.active_process = ProcessSnapshot(
            state=state,
            label=label,
            detail=detail,
            thinking=thinking,
            phases=list(phases or []),
            accent=accent,
            active=active,
            task_id=task_id,
        )

    def clear_process(self, *, label: str = "READY") -> None:
        self.state.active_process = ProcessSnapshot(label=label)

    def update_process_from_event(self, task_id: str, event: dict[str, Any]) -> None:
        event_type = str(event.get("type") or "")
        content = str(event.get("content") or "")
        phases = list(self.state.active_process.phases)
        if event_type == "thinking":
            self.set_process(
                state="thinking",
                label="THINKING",
                detail=content[:120],
                thinking=content[:160],
                phases=phases or ["thinking"],
                accent="purple",
                active=True,
                task_id=task_id,
            )
            return
        if event_type == "tool_call":
            tool_name = str(event.get("toolName") or "tool")
            phases.append("tool")
            self.set_process(
                state="executing",
                label=f"USING {tool_name.upper()}",
                detail=str(event.get("toolArgs") or "")[:120],
                phases=phases[-4:],
                accent="green",
                active=True,
                task_id=task_id,
            )
            return
        if event_type == "tool_result":
            phases.append("result")
            self.set_process(
                state="executing",
                label="TOOL RESULT",
                detail=content[:120],
                phases=phases[-4:],
                accent="green",
                active=True,
                task_id=task_id,
            )
            return
        if event_type == "plan_created":
            phases.append("plan")
            self.set_process(
                state="planning",
                label="PLAN READY",
                detail=content[:120],
                phases=phases[-4:],
                accent="cyan",
                active=True,
                task_id=task_id,
            )
            return
        if event_type in {"step_started", "status"}:
            phases.append("step")
            self.set_process(
                state="executing",
                label="EXECUTING",
                detail=content[:120],
                phases=phases[-4:],
                accent="cyan",
                active=True,
                task_id=task_id,
            )
            return
        if event_type == "approval_needed":
            phases.append("approval")
            self.set_process(
                state="approval",
                label="APPROVAL REQUIRED",
                detail=content[:120],
                phases=phases[-4:],
                accent="warning",
                active=True,
                task_id=task_id,
            )
            return
        if event_type == "task_complete":
            phases.append("done")
            self.set_process(
                state="complete",
                label="COMPLETE",
                detail=content[:120],
                phases=phases[-4:],
                accent="green",
                active=False,
                task_id=task_id,
            )
            return
        if event_type in {"task_failed", "error"}:
            phases.append("error")
            self.set_process(
                state="error",
                label="FAILED",
                detail=content[:120],
                phases=phases[-4:],
                accent="error",
                active=False,
                task_id=task_id,
            )
            return
        if content:
            phases.append("event")
            self.set_process(
                state="executing",
                label="RUNNING",
                detail=content[:120],
                phases=phases[-4:],
                accent="cyan",
                active=True,
                task_id=task_id,
            )
