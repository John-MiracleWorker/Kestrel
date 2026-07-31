import type { FormEvent } from "react";
import { Bot, Sparkles } from "lucide-react";
import { EmptyState, Field, InlineMeta, JsonBlock, Panel } from "../components";
import type { Capability, Skill, SkillDiscoveryReport } from "../types";
import { CapabilitySwitch } from "./CapabilityControls";
import { capabilityForSkill } from "./extendUtils";

export function SkillsPanel({
  skills,
  capabilities,
  capabilityPending,
  onCapabilityEnabledChange,
  onToggleSkill,
  skillDiscovery,
  skillDiscovering,
  onDiscover,
  skillSelection,
  onSkillSelectionChange,
  enabledSkills,
  skillTask,
  onSkillTaskChange,
  onRunSkill,
  selectedSkillEnabled,
  skillManifest,
  onSkillManifestChange,
  skillInstructions,
  onSkillInstructionsChange,
  onInstallSkill,
  skillResult,
}: {
  skills: Skill[];
  capabilities: Capability[];
  capabilityPending: Set<string>;
  onCapabilityEnabledChange: (
    capability: Capability,
    enabled: boolean,
  ) => Promise<void>;
  onToggleSkill: (skill: Skill) => Promise<void>;
  skillDiscovery: SkillDiscoveryReport | null;
  skillDiscovering: boolean;
  onDiscover: () => Promise<void>;
  skillSelection: string;
  onSkillSelectionChange: (value: string) => void;
  enabledSkills: Skill[];
  skillTask: string;
  onSkillTaskChange: (value: string) => void;
  onRunSkill: (event: FormEvent) => Promise<void>;
  selectedSkillEnabled: boolean;
  skillManifest: string;
  onSkillManifestChange: (value: string) => void;
  skillInstructions: string;
  onSkillInstructionsChange: (value: string) => void;
  onInstallSkill: (event: FormEvent) => Promise<void>;
  skillResult: Record<string, unknown> | null;
}) {
  return (
    <>
      <Panel
        title="Skill registry"
        icon={<Sparkles size={19} />}
        actions={
          <button
            type="button"
            onClick={() => void onDiscover()}
            disabled={skillDiscovering}
          >
            {skillDiscovering ? "Discovering" : "Discover"}
          </button>
        }
      >
        {skillDiscovery ? (
          <div className="data-row compact">
            <strong>{skillDiscovery.message}</strong>
            <InlineMeta
              items={[
                skillDiscovery.skills_dir,
                `${skillDiscovery.discovered_count} discovered`,
                `${skillDiscovery.enabled_count} enabled`,
                `${skillDiscovery.validation_errors.length} rejected`,
              ]}
            />
          </div>
        ) : null}
        <div className="list">
          {skills.length === 0 ? (
            <EmptyState>No discovered skills in the registry.</EmptyState>
          ) : (
            skills.map((skill) => {
              const capability = capabilityForSkill(capabilities, skill.id);
              const effectiveEnabled =
                capability?.effective_enabled ?? skill.enabled;
              return (
                <div className="data-row" key={skill.id}>
                  <button
                    type="button"
                    className="link-button"
                    disabled={!effectiveEnabled}
                    onClick={() => onSkillSelectionChange(skill.id)}
                  >
                    {skill.name}
                  </button>
                  <InlineMeta
                    items={[
                      skill.id,
                      effectiveEnabled ? "enabled" : "disabled",
                    ]}
                  />
                  <p>{skill.description}</p>
                  {capability ? (
                    <CapabilitySwitch
                      capability={capability}
                      pending={capabilityPending.has(capability.key)}
                      onChange={onCapabilityEnabledChange}
                      compact
                    />
                  ) : (
                    <button
                      type="button"
                      onClick={() => void onToggleSkill(skill)}
                    >
                      {skill.enabled ? "Disable" : "Enable"}
                    </button>
                  )}
                </div>
              );
            })
          )}
        </div>
        {skillDiscovery?.validation_errors.length ? (
          <JsonBlock value={skillDiscovery.validation_errors} maxHeight="180px" />
        ) : null}
      </Panel>
      <Panel title="Run or Install Skill" icon={<Bot size={19} />}>
        <form onSubmit={onRunSkill} className="stack-form">
          <Field label="Skill">
            <select
              value={skillSelection}
              onChange={(event) => onSkillSelectionChange(event.target.value)}
            >
              <option value="">Select skill</option>
              {enabledSkills.map((skill) => (
                <option key={skill.id} value={skill.id}>
                  {skill.id}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Skill task">
            <textarea
              value={skillTask}
              onChange={(event) => onSkillTaskChange(event.target.value)}
              rows={3}
            />
          </Field>
          <button
            type="submit"
            disabled={
              !skillSelection || !skillTask.trim() || !selectedSkillEnabled
            }
          >
            Run Skill
          </button>
        </form>
        <form onSubmit={onInstallSkill} className="stack-form separated">
          <Field label="Skill manifest JSON">
            <textarea
              value={skillManifest}
              onChange={(event) => onSkillManifestChange(event.target.value)}
              rows={7}
            />
          </Field>
          <Field label="Skill instructions">
            <textarea
              value={skillInstructions}
              onChange={(event) =>
                onSkillInstructionsChange(event.target.value)
              }
              rows={5}
            />
          </Field>
          <button type="submit" disabled={!skillInstructions.trim()}>
            Install Skill
          </button>
        </form>
        {skillResult && <JsonBlock value={skillResult} maxHeight="360px" />}
      </Panel>
    </>
  );
}
