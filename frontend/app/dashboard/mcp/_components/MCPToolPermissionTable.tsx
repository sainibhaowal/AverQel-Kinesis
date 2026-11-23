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
    <section className="mcp-tool-permissions-panel border-glass-border bg-surface-0/95 dark:bg-surface-1/40 rounded-2xl border p-5 shadow-sm sm:p-6">
      <div className="flex items-center justify-between gap-4">
        <div>
          <h2 className="text-foreground text-base font-bold">Tool permissions</h2>
          <p className="text-muted-foreground mt-0.5 text-xs">
            Individual changes override the master tool permission. Risk and connection safeguards
            can still require approval or block a tool.
          </p>
        </div>
        <span className="border-glass-border bg-surface-1 text-muted-foreground rounded-lg border px-2.5 py-1 text-xs font-bold shadow-sm">
          {items.length} tools
        </span>
      </div>
      {items.length === 0 ? (
        <p className="text-muted-foreground mt-5 text-sm italic">
          No tools are available in the current catalog.
        </p>
      ) : (
        <div className="mcp-tool-permissions-list mt-4 max-h-[min(38rem,65vh)] overflow-auto overscroll-contain pr-2 [scrollbar-width:thin]">
          <table className="w-full min-w-[620px] text-left text-sm">
            <thead className="bg-surface-1 text-muted-foreground border-glass-border sticky top-0 z-10 border-b text-xs font-bold tracking-wider uppercase">
              <tr>
                <th className="py-2.5 pr-4">Tool</th>
                <th className="py-2.5 pr-4">Category</th>
                <th className="py-2.5 pr-4">Risk</th>
                <th className="py-2.5">Permission</th>
              </tr>
            </thead>
            <tbody className="divide-glass-border divide-y">
              {items.map((tool) => (
                <tr key={tool.name} className="hover:bg-surface-1/40 transition-colors">
                  <td className="py-3 pr-4">
                    <MCPToolDescription tool={tool} />
                  </td>
                  <td className="text-muted-foreground py-3 pr-4 text-xs font-medium">
                    {tool.category || "General"}
                  </td>
                  <td className="py-3 pr-4">
                    <div className="flex flex-wrap gap-1">
                      {(tool.risk_labels.length ? tool.risk_labels : ["read"]).map((risk) => (
                        <span
                          key={risk}
                          className="border-glass-border bg-surface-1 text-foreground rounded-full border px-2.5 py-0.5 text-[11px] font-semibold shadow-sm"
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
                      className="border-glass-border bg-surface-1 text-foreground focus:border-primary rounded-lg border px-2.5 py-1.5 text-xs font-semibold shadow-sm focus:outline-none"
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
