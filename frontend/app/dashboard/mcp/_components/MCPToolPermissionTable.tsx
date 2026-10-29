import { useEffect, useState } from "react";

import { updateMCPToolPolicy, type MCPTool, type MCPToolMode } from "@/lib/mcp-api";

import MCPToolDescription from "./MCPToolDescription";

const MODES: MCPToolMode[] = ["always_allow", "needs_approval", "blocked"];

export default function MCPToolPermissionTable({
  serverId,
  tools,
  onChanged,
}: {
  serverId: string;
  tools: MCPTool[];
  onChanged?: (tools: MCPTool[]) => void;
}) {
  const [items, setItems] = useState(tools);
  const [saving, setSaving] = useState<string | null>(null);
  useEffect(() => queueMicrotask(() => setItems(tools)), [tools]);

  const changeMode = async (tool: MCPTool, mode: MCPToolMode) => {
    const previous = items;
    const next = items.map((item) => (item.name === tool.name ? { ...item, mode } : item));
    setItems(next);
    setSaving(tool.name);
    try {
      const saved = await updateMCPToolPolicy(serverId, tool.name, mode);
      const finalItems = next.map((item) => (item.name === saved.name ? saved : item));
      setItems(finalItems);
      onChanged?.(finalItems);
    } catch {
      setItems(previous);
    } finally {
      setSaving(null);
    }
  };

  return (
    <section className="mcp-tool-permissions-panel rounded-2xl border border-glass-border bg-surface-0/95 dark:bg-surface-1/40 p-5 sm:p-6 shadow-sm">
      <div className="flex items-center justify-between gap-4">
        <div>
          <h2 className="text-base font-bold text-foreground">Tool permissions</h2>
          <p className="mt-0.5 text-xs text-muted-foreground">
            Individual changes override the master tool permission. Risk and connection safeguards
            can still require approval or block a tool.
          </p>
        </div>
        <span className="rounded-lg border border-glass-border bg-surface-1 px-2.5 py-1 text-xs font-bold text-muted-foreground shadow-sm">
          {items.length} tools
        </span>
      </div>
      {items.length === 0 ? (
        <p className="mt-5 text-sm text-muted-foreground italic">No tools are available in the current catalog.</p>
      ) : (
        <div className="mcp-tool-permissions-list mt-4 max-h-[min(38rem,65vh)] overflow-auto overscroll-contain pr-2 [scrollbar-width:thin]">
          <table className="w-full min-w-[620px] text-left text-sm">
            <thead className="sticky top-0 z-10 bg-surface-1 text-xs font-bold tracking-wider text-muted-foreground uppercase border-b border-glass-border">
              <tr>
                <th className="pr-4 py-2.5">Tool</th>
                <th className="pr-4 py-2.5">Category</th>
                <th className="pr-4 py-2.5">Risk</th>
                <th className="py-2.5">Permission</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-glass-border">
              {items.map((tool) => (
                <tr key={tool.name} className="hover:bg-surface-1/40 transition-colors">
                  <td className="py-3 pr-4">
                    <MCPToolDescription tool={tool} />
                  </td>
                  <td className="py-3 pr-4 text-xs font-medium text-muted-foreground">{tool.category || "General"}</td>
                  <td className="py-3 pr-4">
                    <div className="flex flex-wrap gap-1">
                      {(tool.risk_labels.length ? tool.risk_labels : ["read"]).map((risk) => (
                        <span
                          key={risk}
                          className="rounded-full border border-glass-border bg-surface-1 px-2.5 py-0.5 text-[11px] font-semibold text-foreground shadow-sm"
                        >
                          {risk.replaceAll("_", " ")}
                        </span>
                      ))}
                    </div>
                  </td>
                  <td className="py-3">
                    <select
                      aria-label={`Permission for ${tool.name}`}
                      value={tool.mode}
                      disabled={saving === tool.name}
                      onChange={(event) => void changeMode(tool, event.target.value as MCPToolMode)}
                      className="rounded-lg border border-glass-border bg-surface-1 px-2.5 py-1.5 text-xs font-semibold text-foreground focus:border-primary focus:outline-none shadow-sm"
                    >
                      {MODES.map((mode) => (
                        <option key={mode} value={mode}>
                          {mode.replaceAll("_", " ")}
                        </option>
                      ))}
                    </select>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
