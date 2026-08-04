import type { FormEvent } from "react";
import { PlugZap, Wrench } from "lucide-react";
import { Field, InlineMeta, JsonBlock, Panel, StatusBadge } from "../components";
import type { Capability, McpServer, Tool } from "../types";
import { CapabilitySwitch } from "./CapabilityControls";
import { capabilityForMcpServer, schemaDefault } from "./extendUtils";

export type McpToolOption = {
  server: McpServer;
  tool: Tool & { remote_name?: string };
  value: string;
};

export function McpPanel({
  mcpServers,
  capabilities,
  capabilityPending,
  setCapabilityEnabled,
  loadMcp,
  controlMcp,
  deleteMcp,
  saveMcp,
  mcpId,
  setMcpId,
  mcpName,
  setMcpName,
  mcpTransport,
  setMcpTransport,
  mcpEndpoint,
  setMcpEndpoint,
  mcpArgs,
  setMcpArgs,
  mcpEnv,
  setMcpEnv,
  mcpSecretEnv,
  setMcpSecretEnv,
  mcpRiskPolicy,
  setMcpRiskPolicy,
  mcpArgsTouched,
  setMcpArgsTouched,
  mcpEnvTouched,
  setMcpEnvTouched,
  mcpSecretEnvTouched,
  setMcpSecretEnvTouched,
  loadedMcpServer,
  mcpToolOptions,
  mcpToolSelection,
  setMcpToolSelection,
  mcpToolArgs,
  setMcpToolArgs,
  mcpResult,
  invokeMcp,
  selectedMcpToolEnabled,
}: {
  mcpServers: McpServer[];
  capabilities: Capability[];
  capabilityPending: Set<string>;
  setCapabilityEnabled: (capability: Capability, enabled: boolean) => Promise<void>;
  loadMcp: (server: McpServer) => void;
  controlMcp: (server: McpServer, action: "connect" | "disconnect" | "restart" | "sync" | "test") => Promise<void>;
  deleteMcp: (server: McpServer) => Promise<void>;
  saveMcp: (event: FormEvent) => Promise<void>;
  mcpId: string;
  setMcpId: (value: string) => void;
  mcpName: string;
  setMcpName: (value: string) => void;
  mcpTransport: string;
  setMcpTransport: (value: string) => void;
  mcpEndpoint: string;
  setMcpEndpoint: (value: string) => void;
  mcpArgs: string;
  setMcpArgs: (value: string) => void;
  mcpEnv: string;
  setMcpEnv: (value: string) => void;
  mcpSecretEnv: string;
  setMcpSecretEnv: (value: string) => void;
  mcpRiskPolicy: string;
  setMcpRiskPolicy: (value: string) => void;
  mcpArgsTouched: boolean;
  setMcpArgsTouched: (value: boolean) => void;
  mcpEnvTouched: boolean;
  setMcpEnvTouched: (value: boolean) => void;
  mcpSecretEnvTouched: boolean;
  setMcpSecretEnvTouched: (value: boolean) => void;
  loadedMcpServer: McpServer | null;
  mcpToolOptions: McpToolOption[];
  mcpToolSelection: string;
  setMcpToolSelection: (value: string) => void;
  mcpToolArgs: string;
  setMcpToolArgs: (value: string) => void;
  mcpResult: Record<string, unknown> | null;
  invokeMcp: (event: FormEvent) => Promise<void>;
  selectedMcpToolEnabled: boolean;
}) {
  return (
    <>
      <Panel title="MCP Servers" icon={<PlugZap size={19} />}>
        <form onSubmit={saveMcp} className="stack-form">
          <div className="field-row">
            <Field label="Server ID"><input value={mcpId} onChange={(event) => setMcpId(event.target.value)} /></Field>
            <Field label="Name"><input value={mcpName} onChange={(event) => setMcpName(event.target.value)} /></Field>
            <Field label="Transport">
              <select value={mcpTransport} onChange={(event) => setMcpTransport(event.target.value)}>
                <option value="stdio">stdio</option>
                <option value="streamable_http">streamable_http</option>
                <option value="sse">sse</option>
              </select>
            </Field>
            <Field label="Command or URL"><input value={mcpEndpoint} onChange={(event) => setMcpEndpoint(event.target.value)} /></Field>
            <Field label="Risk policy">
              <select value={mcpRiskPolicy} onChange={(event) => setMcpRiskPolicy(event.target.value)}>
                <option value="approval_by_default">approval_by_default</option>
                <option value="trust_manifest">trust_manifest</option>
              </select>
            </Field>
          </div>
          <div className="check-row">
            <StatusBadge value={loadedMcpServer?.enabled ?? false} />
            <span>Enable or disable this server with its capability switch after saving.</span>
          </div>
          <Field
            label="Args JSON"
            hint={loadedMcpServer && !mcpArgsTouched ? `${loadedMcpServer.argument_count ?? 0} stored arguments are hidden. Edit to replace them.` : undefined}
          >
            <textarea value={mcpArgs} onChange={(event) => { setMcpArgs(event.target.value); setMcpArgsTouched(true); }} rows={3} />
          </Field>
          <Field
            label="Env JSON"
            hint={loadedMcpServer && !mcpEnvTouched ? `${loadedMcpServer.env_keys?.length ?? 0} stored environment names are hidden. Edit to replace them.` : undefined}
          >
            <textarea value={mcpEnv} onChange={(event) => { setMcpEnv(event.target.value); setMcpEnvTouched(true); }} rows={3} />
          </Field>
          <Field
            label="Secret env names JSON"
            hint={loadedMcpServer && !mcpSecretEnvTouched ? `${Object.keys(loadedMcpServer.secret_env_status ?? {}).length} secret bindings are hidden. Edit to replace them.` : undefined}
          >
            <textarea value={mcpSecretEnv} onChange={(event) => { setMcpSecretEnv(event.target.value); setMcpSecretEnvTouched(true); }} rows={3} />
          </Field>
          <button type="submit" disabled={!mcpId.trim()}>Save Server</button>
        </form>
        {mcpServers.map((server) => {
          const serverCapability = capabilityForMcpServer(capabilities, server.id);
          const childCapabilities = serverCapability
            ? capabilities.filter((capability) => capability.kind === "tool" && capability.parent_key === serverCapability.key)
            : [];
          return (
            <div className="data-row" key={server.id}>
              <button type="button" className="link-button" onClick={() => loadMcp(server)}>{server.name}</button>
              <InlineMeta
                items={[
                  server.id,
                  server.transport,
                  server.session_state,
                  `${server.tool_count ?? server.tools.length} tools`,
                  serverCapability?.effective_enabled ?? server.enabled ? "enabled" : "disabled"
                ]}
              />
              <div className="capability-inline-control">
                <StatusBadge value={server.status} />
                {serverCapability && (
                  <CapabilitySwitch
                    capability={serverCapability}
                    pending={capabilityPending.has(serverCapability.key)}
                    onChange={setCapabilityEnabled}
                    compact
                  />
                )}
              </div>
              {server.error && <p className="danger-text">{server.error}</p>}
              {childCapabilities.length > 0 && (
                <div className="capability-child-list" aria-label={`${server.name} tools`}>
                  {childCapabilities.map((capability) => (
                    <div className="capability-child-row" key={capability.key}>
                      <span>{capability.name}</span>
                      <StatusBadge value={capability.effective_enabled ? "effective on" : "effective off"} />
                      <CapabilitySwitch
                        capability={capability}
                        pending={capabilityPending.has(capability.key)}
                        onChange={setCapabilityEnabled}
                        compact
                      />
                    </div>
                  ))}
                </div>
              )}
              <div className="page-actions">
                {(["connect", "sync", "test", "restart", "disconnect"] as const).map((action) => (
                  <button type="button" key={action} onClick={() => void controlMcp(server, action)}>{action}</button>
                ))}
                <button type="button" className="btn danger" onClick={() => void deleteMcp(server)}>Delete</button>
              </div>
            </div>
          );
        })}
      </Panel>
      <Panel id="mcp-tool-invoke" title="MCP Tool Invoke" icon={<Wrench size={19} />}>
        <form onSubmit={invokeMcp} className="stack-form">
          <Field label="MCP tool">
            <select
              value={mcpToolSelection}
              onChange={(event) => {
                setMcpToolSelection(event.target.value);
                const option = mcpToolOptions.find((item) => item.value === event.target.value);
                setMcpToolArgs(JSON.stringify(schemaDefault(option?.tool.parameters), null, 2));
              }}
            >
              <option value="">Select tool</option>
              {mcpToolOptions.map(({ server, tool, value }) => (
                <option key={value} value={value}>{server.id} / {tool.remote_name ?? tool.name}</option>
              ))}
            </select>
          </Field>
          <Field label="Arguments JSON"><textarea value={mcpToolArgs} onChange={(event) => setMcpToolArgs(event.target.value)} rows={8} /></Field>
          <button type="submit" disabled={!mcpToolSelection || !selectedMcpToolEnabled}>Invoke MCP Tool</button>
        </form>
        {mcpResult && <JsonBlock value={mcpResult} maxHeight="420px" />}
      </Panel>
    </>
  );
}
