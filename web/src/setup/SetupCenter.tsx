import {
  RotateCcw,
  Sprout,
} from "lucide-react";
import {
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  Button,
  Notice,
  Skeleton,
  StatusPill,
} from "../components";
import type { ProviderModelCatalog } from "../types";
import { defaultSetupCenterApi } from "./api";
import { updateSetupPresentation } from "./presentation";
import { SetupProgress } from "./SetupProgress";
import { CoreCheckStage } from "./stages/CoreCheckStage";
import { FirstMissionStage } from "./stages/FirstMissionStage";
import { IntelligenceStage } from "./stages/IntelligenceStage";
import { ProjectStage } from "./stages/ProjectStage";
import { SafetyStage } from "./stages/SafetyStage";
import type {
  IntelligenceSelection,
  ProjectCreateInput,
  ProjectSetupDraft,
  SetupCenterApi,
  SetupFirstMissionPreflight,
  SetupFolderChoice,
  SetupNavigation,
  SetupSnapshot,
  SetupStageId,
} from "./types";
import "./setup.css";

const stageDefinitions = [
  { id: "core", label: "Core" },
  { id: "intelligence", label: "Intelligence" },
  { id: "project", label: "Project" },
  { id: "safety", label: "Safety" },
  { id: "first_mission", label: "First mission" },
] as const;

const providerCheckIds = new Set([
  "provider_configuration",
  "provider_operational",
]);

const defaultNavigation: SetupNavigation = {
  openGeneralSettings: () => routeTo("#/settings/general"),
  openProviderSettings: () => routeTo("#/settings/general"),
  openSafetySettings: () => routeTo("#/settings/general"),
  openMission: () => routeTo("#/mission/command"),
};

export function SetupCenter({
  api = defaultSetupCenterApi,
  navigation = defaultNavigation,
}: {
  api?: SetupCenterApi;
  navigation?: SetupNavigation;
}) {
  const [snapshot, setSnapshot] = useState<SetupSnapshot | null>(null);
  const [currentStage, setCurrentStage] =
    useState<SetupStageId>("core");
  const [maxUnlockedIndex, setMaxUnlockedIndex] = useState(0);
  const [completedStages, setCompletedStages] = useState<
    ReadonlySet<SetupStageId>
  >(() => new Set());
  const [loading, setLoading] = useState(true);
  const [pending, setPending] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [stageError, setStageError] = useState<string | null>(null);
  const [provider, setProvider] = useState("mock");
  const [model, setModel] = useState("mock");
  const [folder, setFolder] = useState<SetupFolderChoice>({
    status: "cancelled",
  });
  const [projectName, setProjectName] = useState("");
  const [budget, setBudget] = useState("0");
  const [estimatedCost, setEstimatedCost] = useState("");
  const [projectDraft, setProjectDraft] =
    useState<ProjectSetupDraft | null>(null);
  const [projectSkipped, setProjectSkipped] = useState(false);
  const [safetyReviewed, setSafetyReviewed] = useState(false);
  const [firstMissionPreflight, setFirstMissionPreflight] =
    useState<SetupFirstMissionPreflight | null>(null);
  const [preflightError, setPreflightError] =
    useState<string | null>(null);
  const stagePanelRef = useRef<HTMLDivElement | null>(null);
  const focusNextStageRef = useRef(false);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setLoadError(null);
    api
      .load(controller.signal)
      .then((next) => {
        if (controller.signal.aborted) return;
        updateSetupPresentation({ seen: true });
        acceptSnapshot(next, {
          setSnapshot,
          setCurrentStage,
          setMaxUnlockedIndex,
          setCompletedStages,
          setProvider,
          setModel,
        });
      })
      .catch((error: unknown) => {
        if (!controller.signal.aborted) {
          setLoadError(errorMessage(error));
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [api]);

  useLayoutEffect(() => {
    if (!focusNextStageRef.current) return;
    focusNextStageRef.current = false;
    // React has committed the new stage DOM before layout effects run. Focus
    // it in that same commit so accessibility restoration never depends on an
    // animation frame that can be throttled or withheld in CI/background tabs.
    stagePanelRef.current
      ?.querySelector<HTMLElement>("[data-setup-stage-heading]")
      ?.focus();
  }, [currentStage]);

  const progressItems = useMemo(
    () =>
      stageDefinitions.map((stage, index) => ({
        id: stage.id,
        label: stage.label,
        state:
          stage.id === currentStage
            ? currentStage === "core" && snapshot && !coreCanContinue(snapshot)
              ? ("attention" as const)
              : ("current" as const)
            : stage.id === "project" &&
                projectSkipped &&
                snapshot?.projects.length === 0 &&
                index <= maxUnlockedIndex
              ? ("skipped" as const)
            : completedStages.has(stage.id)
              ? ("complete" as const)
            : index <= maxUnlockedIndex
              ? ("available" as const)
              : ("upcoming" as const),
      })),
    [
      completedStages,
      currentStage,
      maxUnlockedIndex,
      projectSkipped,
      snapshot,
    ],
  );

  async function reload() {
    setPending("reload");
    setLoadError(null);
    setStageError(null);
    try {
      const next = await api.load();
      setSnapshot(next);
      synchronizeIntelligence(next, setProvider, setModel);
      setProjectDraft(null);
      setFirstMissionPreflight(null);
      setPreflightError(null);
      setSafetyReviewed(false);
      const required = firstRequiredStage(next, {
        projectSkipped,
        safetyReviewed: false,
      });
      relockTo(required, next);
    } catch (error) {
      setLoadError(errorMessage(error));
    } finally {
      setPending(null);
    }
  }

  function moveTo(stage: SetupStageId) {
    const index = stageIndex(stage);
    if (pending !== null || index > maxUnlockedIndex) return;
    focusNextStageRef.current = stage !== currentStage;
    setCurrentStage(stage);
    setStageError(null);
  }

  function advanceTo(
    stage: SetupStageId,
    options: { completeCurrent?: boolean } = {},
  ) {
    const index = stageIndex(stage);
    if (options.completeCurrent !== false) {
      setCompletedStages((previous) => {
        const next = new Set(previous);
        next.add(currentStage);
        return next;
      });
    }
    focusNextStageRef.current = stage !== currentStage;
    setCurrentStage(stage);
    setMaxUnlockedIndex((previous) =>
      Math.max(previous, index),
    );
    setStageError(null);
  }

  function relockTo(
    stage: SetupStageId,
    authoritativeSnapshot: SetupSnapshot,
  ) {
    const index = stageIndex(stage);
    setCompletedStages(
      completedBefore(
        stage,
        authoritativeSnapshot,
        projectSkipped,
      ),
    );
    setMaxUnlockedIndex(index);
    if (stageIndex(currentStage) > index || currentStage === stage) {
      focusNextStageRef.current = stage !== currentStage;
      setCurrentStage(stage);
    }
    if (index < stageIndex("safety")) {
      setSafetyReviewed(false);
    }
  }

  async function saveIntelligence(
    selection: IntelligenceSelection,
  ) {
    setPending("intelligence");
    setStageError(null);
    try {
      const next = await api.saveIntelligence(selection);
      setSnapshot(next);
      synchronizeIntelligence(next, setProvider, setModel);
      setProjectDraft(null);
      setFirstMissionPreflight(null);
      if (
        next.readiness.experience_mode !== "demo" &&
        next.readiness.experience_mode !== "connected"
      ) {
        setStageError(
          next.readiness.next_action ||
            "The selected model is saved but is not connected yet.",
        );
        return;
      }
      const required = firstRequiredStage(next, {
        projectSkipped: false,
        safetyReviewed: false,
      });
      if (stageIndex(required) < stageIndex("project")) {
        relockTo(required, next);
        return;
      }
      setProjectSkipped(false);
      setSafetyReviewed(false);
      advanceTo("project");
    } catch (error) {
      setStageError(errorMessage(error));
    } finally {
      setPending(null);
    }
  }

  async function storeCredential(nextProvider: string) {
    setPending("credential");
    setStageError(null);
    try {
      const next = await api.storeProviderCredential(nextProvider);
      setSnapshot(next);
      synchronizeIntelligence(next, setProvider, setModel);
      setProjectDraft(null);
      setFirstMissionPreflight(null);
    } catch (error) {
      setStageError(errorMessage(error));
    } finally {
      setPending(null);
    }
  }

  async function repairCore(checkId: string) {
    if (!snapshot) return;
    setPending(`core:${checkId}`);
    setStageError(null);
    try {
      const next = await api.repairCore(
        checkId,
        snapshot.runtime.expectedRevision,
      );
      setSnapshot(next);
      synchronizeIntelligence(next, setProvider, setModel);
      setProjectDraft(null);
      setFirstMissionPreflight(null);
      setSafetyReviewed(false);
      const required = firstRequiredStage(next);
      relockTo(required, next);
      const remaining = next.readiness.checks.find(
        (check) =>
          check.check_id === checkId &&
          check.status === "fail",
      );
      if (remaining) {
        setStageError(
          `${remaining.title} is still blocked: ${remaining.detail}`,
        );
      }
    } catch (error) {
      setStageError(errorMessage(error));
    } finally {
      setPending(null);
    }
  }

  function reviewCoreSettings(checkId: string) {
    if (checkId === "memory_storage") {
      (
        navigation.openMemorySettings ??
        navigation.openGeneralSettings
      )();
      return;
    }
    if (checkId === "api_auth") {
      (
        navigation.openApiAccessSettings ??
        navigation.openGeneralSettings
      )();
      return;
    }
    if (
      checkId === "permission_gates" ||
      checkId === "validation_container" ||
      checkId === "repair_isolation"
    ) {
      navigation.openSafetySettings();
      return;
    }
    navigation.openGeneralSettings();
  }

  async function chooseFolder() {
    setPending("folder");
    setStageError(null);
    try {
      const choice = await api.chooseProjectFolder();
      setFolder(choice);
      setProjectDraft(null);
      if (choice.status === "selected") {
        setProjectName(choice.displayLabel);
        await inspectProjectChoice(choice.path);
      }
    } catch (error) {
      setStageError(errorMessage(error));
    } finally {
      setPending(null);
    }
  }

  function updateFolder(path: string) {
    setProjectDraft(null);
    setFirstMissionPreflight(null);
    if (!path) {
      setFolder({ status: "cancelled" });
      return;
    }
    setFolder({
      status: "selected",
      path,
      displayLabel: folderLabel(path),
    });
    if (!projectName.trim()) setProjectName(folderLabel(path));
  }

  async function inspectProjectChoice(path?: string) {
    const selectedPath =
      path ??
      (folder.status === "selected" ? folder.path : "");
    if (!selectedPath) return;
    setPending("inspect_project");
    setStageError(null);
    try {
      const draft = await api.inspectProject({
        repositoryPath: selectedPath,
        directEstimatedCostUsd: optionalAmount(estimatedCost),
        costBudget: optionalAmount(budget),
      });
      setFolder({
        status: "selected",
        path: draft.inspection.canonical_path,
        displayLabel: folderLabel(draft.inspection.canonical_path),
      });
      if (!projectName.trim()) {
        setProjectName(draft.create_input.display_name);
      }
      setProjectDraft(draft);
    } catch (error) {
      setProjectDraft(null);
      setStageError(errorMessage(error));
    } finally {
      setPending(null);
    }
  }

  async function createProject(input: ProjectCreateInput) {
    if (!snapshot) return;
    setPending("project");
    setStageError(null);
    try {
      const created = await api.createProject(input);
      setSnapshot({
        ...snapshot,
        projects: [
          created,
          ...snapshot.projects.filter(
            (item) => item.project_id !== created.project_id,
          ),
        ],
      });
      setProjectSkipped(false);
      setSafetyReviewed(false);
      setFirstMissionPreflight(null);
      setPreflightError(null);
      advanceTo("safety");
    } catch (error) {
      setStageError(errorMessage(error));
    } finally {
      setPending(null);
    }
  }

  async function reviewSafety() {
    setPending("preflight");
    setStageError(null);
    setPreflightError(null);
    let next: SetupSnapshot;
    try {
      next = await api.load();
      setSnapshot(next);
      synchronizeIntelligence(next, setProvider, setModel);
    } catch (error) {
      setStageError(
        `Current server truth could not be refreshed: ${errorMessage(error)}`,
      );
      setPending(null);
      return;
    }
    const required = firstRequiredStage(next, {
      projectSkipped,
      safetyReviewed: false,
    });
    if (stageIndex(required) < stageIndex("safety")) {
      setSafetyReviewed(false);
      setFirstMissionPreflight(null);
      relockTo(required, next);
      setStageError(
        "Current server checks changed. Repair the newly required stage before continuing.",
      );
      setPending(null);
      return;
    }
    try {
      const selectedProject = next.projects[0] ?? null;
      const preflight = selectedProject
        ? await api.preflightFirstMission(selectedProject.project_id)
        : null;
      setFirstMissionPreflight(preflight);
      setSafetyReviewed(true);
      advanceTo("first_mission");
    } catch (error) {
      const message = errorMessage(error);
      setPreflightError(message);
      setSafetyReviewed(true);
      advanceTo("first_mission");
    } finally {
      setPending(null);
    }
  }

  function selectProvider(nextProvider: string) {
    if (!snapshot) return;
    setProvider(nextProvider);
    setModel(firstModelForProvider(nextProvider, snapshot.catalogs));
    setProjectDraft(null);
    setFirstMissionPreflight(null);
    setStageError(null);
  }

  if (loading) {
    return (
      <section
        className="setup-center setup-center-loading"
        aria-labelledby="setup-center-title"
      >
        <SetupHeader />
        <Skeleton label="Loading Setup Center" lines={5} />
      </section>
    );
  }

  if (!snapshot || loadError) {
    return (
      <section
        className="setup-center setup-center-error"
        aria-labelledby="setup-center-title"
      >
        <SetupHeader />
        <Notice variant="danger" title="Setup Center is unavailable">
          {loadError ?? "Kestrel did not return setup state."}
        </Notice>
        <Button
          variant="primary"
          pending={pending === "reload"}
          onClick={() => void reload()}
        >
          <RotateCcw size={16} aria-hidden="true" />
          Retry
        </Button>
      </section>
    );
  }

  return (
    <section
      className="setup-center"
      aria-labelledby="setup-center-title"
    >
      <SetupHeader snapshot={snapshot} />
      <div className="setup-center-layout">
        <SetupProgress
          items={progressItems}
          onSelect={(stage) => moveTo(stage)}
        />
        <div className="setup-stage-panel" ref={stagePanelRef}>
          {currentStage === "core" ? (
            <CoreCheckStage
              readiness={snapshot.readiness}
              pending={pending === "reload"}
              pendingCheckId={
                pending?.startsWith("core:")
                  ? pending.slice("core:".length)
                  : null
              }
              error={stageError}
              supportsNativePathRepair={
                api.supportsNativeWorkspacePicker === true
              }
              onRefresh={() => void reload()}
              onRepair={(checkId) =>
                void repairCore(checkId)
              }
              onReviewSettings={reviewCoreSettings}
              onContinue={() => advanceTo("intelligence")}
            />
          ) : null}
          {currentStage === "intelligence" ? (
            <IntelligenceStage
              catalogs={snapshot.catalogs}
              secrets={snapshot.secrets}
              runtime={snapshot.runtime}
              provider={provider}
              model={model}
              pending={
                pending === "intelligence" ||
                pending === "credential"
              }
              error={stageError}
              nativeCredentialEntry={
                api.supportsNativeCredentialDialog === true
              }
              onProviderChange={selectProvider}
              onModelChange={setModel}
              onContinueDemo={(selection) =>
                void saveIntelligence(selection)
              }
              onUseSelection={(selection) =>
                void saveIntelligence(selection)
              }
              onStoreCredential={(nextProvider) =>
                void storeCredential(nextProvider)
              }
              onOpenProviderSettings={
                navigation.openProviderSettings
              }
            />
          ) : null}
          {currentStage === "project" ? (
            <ProjectStage
              projects={snapshot.projects}
              supportsNativePicker={
                api.supportsNativeProjectPicker
              }
              folder={folder}
              displayName={projectName}
              budget={budget}
              estimatedCost={estimatedCost}
              draft={projectDraft}
              pending={
                pending === "folder" ||
                pending === "inspect_project" ||
                pending === "project"
              }
              error={stageError}
              onChooseFolder={() => void chooseFolder()}
              onFolderChange={updateFolder}
              onDisplayNameChange={setProjectName}
              onBudgetChange={(value) => {
                setBudget(value);
                setProjectDraft(null);
              }}
              onEstimatedCostChange={(value) => {
                setEstimatedCost(value);
                setProjectDraft(null);
              }}
              onInspect={() => void inspectProjectChoice()}
              onSave={(input) => void createProject(input)}
              onContinueExisting={() => {
                setProjectSkipped(false);
                setSafetyReviewed(false);
                setFirstMissionPreflight(null);
                advanceTo("safety");
              }}
              onOpenCapabilities={
                navigation.openCapabilitiesSettings ??
                navigation.openGeneralSettings
              }
              onSkip={() => {
                setProjectSkipped(true);
                setSafetyReviewed(false);
                setFirstMissionPreflight(null);
                advanceTo("safety", {
                  completeCurrent: false,
                });
              }}
            />
          ) : null}
          {currentStage === "safety" ? (
            <SafetyStage
              readiness={snapshot.readiness}
              onOpenSafetySettings={
                navigation.openSafetySettings
              }
              pending={pending === "preflight"}
              error={stageError}
              onContinue={() => void reviewSafety()}
            />
          ) : null}
          {currentStage === "first_mission" ? (
            <FirstMissionStage
              snapshot={snapshot}
              projectSkipped={projectSkipped}
              preflight={firstMissionPreflight}
              preflightError={preflightError}
              onOpenMission={navigation.openMission}
              onOpenSettings={navigation.openGeneralSettings}
            />
          ) : null}
        </div>
      </div>
    </section>
  );
}

function SetupHeader({ snapshot }: { snapshot?: SetupSnapshot }) {
  return (
    <header className="setup-center-head">
      <div>
        <p className="page-eyebrow">Wildflower Workshop</p>
        <h1 id="setup-center-title">
          Setup Center<em>.</em>
        </h1>
        <p>
          Five calm checks from bundled core to first mission. Revisit this
          workspace any time; server state remains the authority.
        </p>
      </div>
      <div className="setup-center-mark" aria-hidden="true">
        <Sprout size={28} />
      </div>
      {snapshot ? (
        <StatusPill
          state={
            coreCanContinue(snapshot) ? "healthy" : "blocked"
          }
          className="setup-center-status"
        >
          {coreCanContinue(snapshot)
            ? snapshot.readiness.experience_mode === "demo"
              ? "Demo ready"
              : "Core ready"
            : "Repair needed"}
        </StatusPill>
      ) : null}
    </header>
  );
}

function firstRequiredStage(
  snapshot: SetupSnapshot,
  session: {
    projectSkipped: boolean;
    safetyReviewed: boolean;
  } = {
    projectSkipped: false,
    safetyReviewed: false,
  },
): SetupStageId {
  if (!coreCanContinue(snapshot)) return "core";
  if (
    snapshot.readiness.experience_mode === "model_not_connected"
  ) {
    return "intelligence";
  }
  if (
    snapshot.projects.length === 0 &&
    !session.projectSkipped
  ) {
    return "project";
  }
  if (!session.safetyReviewed) return "safety";
  return "first_mission";
}

function coreCanContinue(snapshot: SetupSnapshot): boolean {
  return !snapshot.readiness.checks.some(
    (check) =>
      check.status === "fail" &&
      !providerCheckIds.has(check.check_id),
  );
}

function acceptSnapshot(
  snapshot: SetupSnapshot,
  setters: {
    setSnapshot: (snapshot: SetupSnapshot) => void;
    setCurrentStage: (stage: SetupStageId) => void;
    setMaxUnlockedIndex: (index: number) => void;
    setCompletedStages: (
      stages: ReadonlySet<SetupStageId>,
    ) => void;
    setProvider: (provider: string) => void;
    setModel: (model: string) => void;
  },
) {
  const required = firstRequiredStage(snapshot);
  setters.setSnapshot(snapshot);
  setters.setCurrentStage(required);
  setters.setMaxUnlockedIndex(stageIndex(required));
  setters.setCompletedStages(
    completedBefore(required, snapshot, false),
  );
  synchronizeIntelligence(
    snapshot,
    setters.setProvider,
    setters.setModel,
  );
}

function completedBefore(
  required: SetupStageId,
  snapshot: SetupSnapshot,
  projectSkipped: boolean,
): ReadonlySet<SetupStageId> {
  const requiredIndex = stageIndex(required);
  return new Set(
    stageDefinitions
      .filter(
        (stage, index) =>
          index < requiredIndex &&
          !(
            stage.id === "project" &&
            projectSkipped &&
            snapshot.projects.length === 0
          ),
      )
      .map((stage) => stage.id),
  );
}

function synchronizeIntelligence(
  snapshot: SetupSnapshot,
  setProvider: (provider: string) => void,
  setModel: (model: string) => void,
) {
  const selected = snapshot.catalogs.some(
    (catalog) => catalog.provider === snapshot.runtime.provider,
  )
    ? snapshot.runtime.provider
    : snapshot.catalogs.find(
        (catalog) => catalog.provider !== "mock",
      )?.provider ?? "mock";
  setProvider(selected);
  setModel(
    selected === snapshot.runtime.provider
      ? snapshot.runtime.model
      : firstModelForProvider(selected, snapshot.catalogs),
  );
}

function firstModelForProvider(
  provider: string,
  catalogs: ProviderModelCatalog[],
): string {
  const catalog = catalogs.find(
    (candidate) => candidate.provider === provider,
  );
  return (
    catalog?.models[0] ??
    catalog?.fallback_models[0] ??
    (provider === "mock" ? "mock" : "")
  );
}

function stageIndex(stage: SetupStageId): number {
  return stageDefinitions.findIndex(
    (definition) => definition.id === stage,
  );
}

function folderLabel(path: string): string {
  const parts = path.replace(/[\\/]+$/, "").split(/[\\/]/);
  return parts.at(-1) || "Project";
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function optionalAmount(value: string): number | null {
  if (!value.trim()) return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : null;
}

function routeTo(hash: string) {
  if (typeof window !== "undefined") {
    window.location.hash = hash;
  }
}
