import { FolderCog } from "lucide-react";
import {
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import { ApiAuthError, getJson, postJson } from "../api";
import { Notice, Panel } from "../components";
import {
  MissionControl,
  type MissionControlProps,
} from "../mission/MissionControl";
import type {
  ProjectListResponse,
  ProjectProfile,
} from "../mission/types";
import { readDesktopBridge } from "../platform/desktopBridge";
import type {
  ProjectCreateInput,
  ProjectSetupDraft,
} from "../setup/types";
import { ProjectEditor } from "./ProjectEditor";
import { ProjectHistory } from "./ProjectHistory";
import {
  ProjectIndexStatus,
  type ProjectIndexStatusRecord,
} from "./ProjectIndexStatus";
import { ProjectOverview } from "./ProjectOverview";
import "./projects.css";

type FolderChoice =
  | { status: "selected"; path: string }
  | { status: "cancelled" }
  | { status: "unavailable" };

function parseFolderChoice(value: unknown): FolderChoice {
  if (
    typeof value === "object" &&
    value !== null &&
    "status" in value &&
    (value as { status: unknown }).status === "selected" &&
    "path" in value &&
    typeof (value as { path: unknown }).path === "string"
  ) {
    return {
      status: "selected",
      path: (value as { path: string }).path,
    };
  }
  return { status: "cancelled" };
}

export function ProjectsWorkspace(props: MissionControlProps) {
  const { onAuthRequired } = props;
  const [projects, setProjects] = useState<ProjectProfile[]>([]);
  const [projectsError, setProjectsError] = useState<string | null>(null);
  const [selectedProjectId, setSelectedProjectId] = useState<string | null>(
    null,
  );
  const [indexStatus, setIndexStatus] =
    useState<ProjectIndexStatusRecord | null>(null);
  const [indexError, setIndexError] = useState<string | null>(null);
  const [draft, setDraft] = useState<ProjectSetupDraft | null>(null);
  const [inspecting, setInspecting] = useState(false);
  const [pickerMessage, setPickerMessage] = useState<string | null>(null);
  const [saveNotice, setSaveNotice] = useState<string | null>(null);
  const projectsRequestRef = useRef(0);
  const indexRequestRef = useRef(0);

  const refreshProjects = useCallback(async () => {
    const generation = ++projectsRequestRef.current;
    try {
      const list = await getJson<ProjectListResponse>("/api/projects");
      if (projectsRequestRef.current !== generation) return;
      setProjects(list.items);
      setProjectsError(null);
      setSelectedProjectId((current) =>
        current && list.items.some((item) => item.project_id === current)
          ? current
          : (list.items[0]?.project_id ?? null),
      );
    } catch (value) {
      if (projectsRequestRef.current !== generation) return;
      if (value instanceof ApiAuthError) {
        onAuthRequired();
        return;
      }
      setProjectsError(
        value instanceof Error ? value.message : String(value),
      );
    }
  }, [onAuthRequired]);

  useEffect(() => {
    void refreshProjects();
  }, [refreshProjects]);

  useEffect(() => {
    if (!selectedProjectId) {
      setIndexStatus(null);
      setIndexError(null);
      return;
    }
    const generation = ++indexRequestRef.current;
    setIndexStatus(null);
    setIndexError(null);
    getJson<ProjectIndexStatusRecord>(
      `/api/projects/${encodeURIComponent(selectedProjectId)}/index`,
    )
      .then((status) => {
        if (indexRequestRef.current !== generation) return;
        setIndexStatus(status);
      })
      .catch((value: unknown) => {
        if (indexRequestRef.current !== generation) return;
        if (value instanceof ApiAuthError) {
          onAuthRequired();
          return;
        }
        setIndexError(
          value instanceof Error ? value.message : String(value),
        );
      });
  }, [selectedProjectId, onAuthRequired]);

  async function addProject() {
    setPickerMessage(null);
    setSaveNotice(null);
    const bridge = readDesktopBridge();
    if (!bridge) {
      setPickerMessage(
        "The native project picker requires the Kestrel desktop shell.",
      );
      return;
    }
    let choice: FolderChoice;
    try {
      choice = parseFolderChoice(await bridge.chooseProjectFolder());
    } catch {
      setPickerMessage("The native folder picker is unavailable.");
      return;
    }
    if (choice.status !== "selected") {
      setPickerMessage("Folder selection was cancelled.");
      return;
    }
    setInspecting(true);
    try {
      const inspected = await postJson<ProjectSetupDraft>(
        "/api/projects/setup-draft",
        {
          repository_path: choice.path,
          direct_estimated_cost_usd: null,
          cost_budget: null,
        },
      );
      setDraft(inspected);
    } catch (value) {
      if (value instanceof ApiAuthError) {
        onAuthRequired();
        return;
      }
      setPickerMessage(
        value instanceof Error ? value.message : String(value),
      );
    } finally {
      setInspecting(false);
    }
  }

  async function confirmSave() {
    if (!draft) return;
    setSaveNotice(null);
    try {
      const input: ProjectCreateInput = draft.create_input;
      const created = await postJson<ProjectProfile>("/api/projects", input);
      setDraft(null);
      setSaveNotice(
        `Project "${created.display_name}" saved at rev ${created.revision}.`,
      );
      await refreshProjects();
      setSelectedProjectId(created.project_id);
    } catch (value) {
      if (value instanceof ApiAuthError) {
        onAuthRequired();
        return;
      }
      setPickerMessage(
        value instanceof Error ? value.message : String(value),
      );
    }
  }

  const selectedProject =
    projects.find((item) => item.project_id === selectedProjectId) ?? null;

  return (
    <>
      <MissionControl {...props} />
      <section id="projects" className="content-grid wide-left">
        <ProjectOverview
          projects={projects}
          error={projectsError}
          selectedProjectId={selectedProjectId}
          onSelectProject={setSelectedProjectId}
        />
        <ProjectEditor
          nativePickerAvailable={readDesktopBridge() !== null}
          draft={draft}
          inspecting={inspecting}
          pickerMessage={pickerMessage}
          onAddProject={() => void addProject()}
          onConfirmSave={() => void confirmSave()}
          onDismissPreview={() => setDraft(null)}
        />
        <ProjectIndexStatus indexStatus={indexStatus} error={indexError} />
        <ProjectHistory project={selectedProject} />
        <Panel title="Storage location" icon={<FolderCog size={19} />}>
          <div className="projects-move-storage">
            <p className="muted">
              Project storage locations are owned by the runtime launch. This
              build cannot relocate live project or Memvid storage; the move
              flow ships with the transactional packaging/recovery plan.
            </p>
            {saveNotice ? (
              <Notice variant="success">{saveNotice}</Notice>
            ) : null}
            <div>
              <button
                type="button"
                disabled
                aria-describedby="projects-move-storage-plan"
              >
                Move storage
              </button>
            </div>
            <p id="projects-move-storage-plan" className="muted">
              Disabled until the packaging/recovery plan delivers a
              transactional server contract for moving storage.
            </p>
          </div>
        </Panel>
      </section>
    </>
  );
}
