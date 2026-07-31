import { type FormEvent, useState } from "react";
import { postJson } from "../api";
import type { Run } from "../types";

export type AdvancedRunRequest = {
  objective: string;
  sessionId: string;
  workspace: string | null;
};

export type AdvancedOperationsOptions = {
  activeRun: Pick<Run, "run_id" | "session_id"> | null;
  activeSessionId: string | null;
  workspace: string;
  enqueueRun: (request: AdvancedRunRequest) => Promise<void>;
  refreshCore: () => Promise<void>;
  refreshRunDetails: (runId: string) => Promise<void>;
  refreshAll: () => Promise<void>;
  createSessionId: () => string;
  onError: (error: unknown) => void;
  onNotice: (notice: string) => void;
};

export function useAdvancedOperations({
  activeRun,
  activeSessionId,
  workspace,
  enqueueRun,
  refreshCore,
  refreshRunDetails,
  refreshAll,
  createSessionId,
  onError,
  onNotice,
}: AdvancedOperationsOptions) {
  const [operatorMessage, setOperatorMessage] = useState("");
  const [sessionId, setSessionId] = useState("");
  const [subagentProfile, setSubagentProfile] = useState("worker");
  const [subagentGoal, setSubagentGoal] = useState("");
  const [schedulerTasks, setSchedulerTasks] = useState("3");
  const [schedulerCycles, setSchedulerCycles] = useState("5");
  const [schedulerResult, setSchedulerResult] =
    useState<Record<string, unknown> | null>(null);
  const [selfTitle, setSelfTitle] = useState("");
  const [selfContent, setSelfContent] = useState("");
  const [selfSchema, setSelfSchema] =
    useState("user_workflow_preference");
  const [selfRememberResult, setSelfRememberResult] =
    useState<Record<string, unknown> | null>(null);
  const [webQuery, setWebQuery] = useState("");
  const [webResult, setWebResult] =
    useState<Record<string, unknown> | null>(null);

  async function guarded(
    action: () => Promise<void>,
    success?: string,
  ) {
    try {
      await action();
      if (success) onNotice(success);
    } catch (error) {
      onError(error);
    }
  }

  async function submitRun(event: FormEvent) {
    event.preventDefault();
    await guarded(async () => {
      await enqueueRun({
        objective: operatorMessage,
        sessionId:
          sessionId.trim() || activeSessionId || createSessionId(),
        workspace: workspace.trim() || null,
      });
      setOperatorMessage("");
    }, "Run queued.");
  }

  async function runScheduler(mode: "step" | "run") {
    if (!activeRun) return;
    await guarded(async () => {
      const payload =
        mode === "step"
          ? { max_tasks: Number(schedulerTasks) || null }
          : {
              max_tasks: Number(schedulerTasks) || null,
              max_cycles: Number(schedulerCycles) || null,
            };
      const result = await postJson<Record<string, unknown>>(
        `/api/runs/${activeRun.run_id}/scheduler/${mode}`,
        payload,
      );
      setSchedulerResult(result);
      await refreshCore();
    }, mode === "step" ? "Scheduler step complete." : "Scheduler drain complete.");
  }

  async function submitSubagent(event: FormEvent) {
    event.preventDefault();
    if (!activeRun) return;
    await guarded(async () => {
      await postJson("/api/subagents", {
        run_id: activeRun.run_id,
        profile: subagentProfile,
        goal: subagentGoal,
      });
      setSubagentGoal("");
      await refreshRunDetails(activeRun.run_id);
    }, "Subagent queued.");
  }

  async function rememberSelf(event: FormEvent) {
    event.preventDefault();
    await guarded(async () => {
      const result = await postJson<Record<string, unknown>>(
        "/api/self/remember",
        {
          title: selfTitle,
          content: selfContent,
          schema: selfSchema,
          validation_status: "user_confirmed",
          confidence: 0.88,
        },
      );
      setSelfRememberResult(result);
      await refreshAll();
    }, "Soul memory reviewed.");
  }

  async function searchWeb(event: FormEvent) {
    event.preventDefault();
    await guarded(async () => {
      const result = await postJson<Record<string, unknown>>(
        "/api/web/search",
        { query: webQuery, max_results: 5 },
      );
      setWebResult(result);
    });
  }

  return {
    operatorMessage,
    setOperatorMessage,
    sessionId,
    setSessionId,
    subagentProfile,
    setSubagentProfile,
    subagentGoal,
    setSubagentGoal,
    schedulerTasks,
    setSchedulerTasks,
    schedulerCycles,
    setSchedulerCycles,
    schedulerResult,
    selfTitle,
    setSelfTitle,
    selfContent,
    setSelfContent,
    selfSchema,
    setSelfSchema,
    selfRememberResult,
    setSelfRememberResult,
    webQuery,
    setWebQuery,
    webResult,
    submitRun,
    runScheduler,
    submitSubagent,
    rememberSelf,
    searchWeb,
  };
}
