import { useEffect, useState } from "react";

import {
  updateMCPPolicy,
  type MCPConnectionPolicy,
  type MCPRiskLabel,
  type MCPToolMode,
} from "@/lib/mcp-api";

const RISK_OPTIONS: MCPRiskLabel[] = ["read", "write", "delete", "external_message"];
const MODES: MCPToolMode[] = ["always_allow", "needs_approval", "blocked"];

type EditablePolicy = Pick<
  MCPConnectionPolicy,
  | "allowed_tools"
  | "denied_tools"
  | "read_only"
  | "risk_ceiling"
  | "approval_rules"
  | "tool_modes"
  | "default_tool_mode"
  | "default_enabled"
  | "deepspace_overrides"
  | "conversation_overrides"
>;

export default function MCPConnectionPolicyPanel({
  serverId,
  policy,
  onSaved,
}: {
  serverId: string;
  policy: MCPConnectionPolicy;
  onSaved?: (policy: MCPConnectionPolicy) => void;
}) {
  const [draft, setDraft] = useState<EditablePolicy>(toEditable(policy));
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  useEffect(() => queueMicrotask(() => setDraft(toEditable(policy))), [policy]);

  const save = async () => {
    setSaving(true);
    setMessage(null);
    try {
      const saved = await updateMCPPolicy(serverId, draft);
      onSaved?.(saved);
      setMessage("Policy saved.");
    } catch (reason) {
      setMessage(reason instanceof Error ? reason.message : "Unable to save policy.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <section className="rounded-2xl border border-glass-border bg-surface-0/95 dark:bg-surface-1/40 p-5 sm:p-6 shadow-sm space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h2 className="text-base font-bold text-foreground">Connection policy</h2>
          <p className="mt-0.5 text-xs text-muted-foreground">
            These settings are scoped to your account and this MCP connection.
          </p>
        </div>
        <button
          type="button"
          onClick={() => void save()}
          disabled={saving}
          className="mcp-btn-emerald rounded-xl font-bold text-xs uppercase tracking-wider px-5 py-2.5 shadow-sm transition-all disabled:opacity-50"
        >
          {saving ? "Saving…" : "Save policy"}
        </button>
      </div>
      {message && (
        <p className="text-sm font-semibold text-emerald-600 dark:text-emerald-400" role="status">
          {message}
        </p>
      )}
      <div className="grid gap-4 sm:grid-cols-2">
        <label className="flex items-start gap-3 rounded-xl border border-glass-border bg-surface-1/50 p-4 cursor-pointer hover:border-primary/40 transition-colors shadow-sm">
          <input
            type="checkbox"
            checked={draft.default_enabled}
            onChange={(event) => setDraft({ ...draft, default_enabled: event.target.checked })}
            className="mt-0.5 h-4 w-4 rounded border-glass-border text-primary accent-primary"
          />
          <span>
            <span className="block text-sm font-bold text-foreground">Enable across DeepSpace</span>
            <span className="mt-1 block text-xs leading-relaxed text-muted-foreground">
              Connected accounts are available automatically in every conversation while enabled.
            </span>
          </span>
        </label>
        <label className="flex items-start gap-3 rounded-xl border border-glass-border bg-surface-1/50 p-4 cursor-pointer hover:border-primary/40 transition-colors shadow-sm">
          <input
            type="checkbox"
            checked={draft.read_only}
            onChange={(event) => setDraft({ ...draft, read_only: event.target.checked })}
            className="mt-0.5 h-4 w-4 rounded border-glass-border text-primary accent-primary"
          />
          <span>
            <span className="block text-sm font-bold text-foreground">Read-only mode</span>
            <span className="mt-1 block text-xs leading-relaxed text-muted-foreground">
              Blocks write, delete, and external-message tools; a blocked or disabled tool is
              removed before execution.
            </span>
          </span>
        </label>
      </div>
      <div className="grid gap-4 sm:grid-cols-2">
        <label className="block">
          <span className="block text-sm font-bold text-foreground">Master tool permission</span>
          <select
            aria-label="Master tool permission"
            value={draft.default_tool_mode}
            onChange={(event) =>
              setDraft({
                ...draft,
                default_tool_mode: event.target.value as MCPToolMode,
                tool_modes: {},
              })
            }
            className="mt-2 w-full rounded-xl border border-glass-border bg-surface-1 px-3.5 py-2.5 text-sm font-medium text-foreground focus:border-primary focus:outline-none transition-colors shadow-sm"
          >
            {MODES.map((mode) => (
              <option key={mode} value={mode}>
                {mode.replaceAll("_", " ")}
              </option>
            ))}
          </select>
          <span className="mt-1.5 block text-xs leading-relaxed text-muted-foreground">
            Applies to every current and future tool. Choosing a value clears individual overrides;
            you can customize tools again afterward.
          </span>
        </label>
        <label className="block">
          <span className="block text-sm font-bold text-foreground">Master risk ceiling</span>
          <select
            aria-label="Master risk ceiling"
            value={draft.risk_ceiling}
            onChange={(event) =>
              setDraft({ ...draft, risk_ceiling: event.target.value as MCPRiskLabel })
            }
            className="mt-2 w-full rounded-xl border border-glass-border bg-surface-1 px-3.5 py-2.5 text-sm font-medium text-foreground focus:border-primary focus:outline-none transition-colors shadow-sm"
          >
            {RISK_OPTIONS.map((risk) => (
              <option key={risk} value={risk}>
                {risk.replaceAll("_", " ")}
              </option>
            ))}
          </select>
          <span className="mt-1.5 block text-xs leading-relaxed text-muted-foreground">
            No tool can run above this risk level. Read-only mode remains an additional hard block.
          </span>
        </label>
      </div>
      <div>
        <h3 className="text-sm font-bold text-foreground">Risk safeguards</h3>
        <p className="mt-1 text-xs text-muted-foreground">
          These are risk guardrails, not a second per-tool permission list. Platform safety still
          requires confirmation for remote side effects, even when a tool is set to always allow.
        </p>
        <div className="mt-3 grid gap-3 sm:grid-cols-3">
          {RISK_OPTIONS.filter((risk) => risk !== "read").map((risk) => (
            <label key={risk} className="text-xs font-semibold capitalize text-foreground">
              {risk.replaceAll("_", " ")}
              <select
                value={draft.approval_rules[risk] || "needs_approval"}
                onChange={(event) =>
                  setDraft({
                    ...draft,
                    approval_rules: {
                      ...draft.approval_rules,
                      [risk]: event.target.value as MCPToolMode,
                    },
                  })
                }
                className="mt-1.5 w-full rounded-xl border border-glass-border bg-surface-1 px-3 py-2 text-xs font-medium text-foreground focus:border-primary focus:outline-none shadow-sm"
              >
                {MODES.map((mode) => (
                  <option key={mode} value={mode}>
                    {mode.replaceAll("_", " ")}
                  </option>
                ))}
              </select>
            </label>
          ))}
        </div>
      </div>
      <p className="rounded-xl border border-glass-border bg-surface-1/80 p-3.5 text-xs leading-relaxed text-muted-foreground shadow-sm">
        Decision order: connection ownership and provider safety first, then the master risk ceiling
        and read-only guard, then explicit tool blocks, then a per-tool override or the master tool
        permission. Every decision is checked again immediately before the remote call.
      </p>
    </section>
  );
}

function toEditable(policy: MCPConnectionPolicy): EditablePolicy {
  return {
    allowed_tools: [...policy.allowed_tools],
    denied_tools: [...policy.denied_tools],
    read_only: policy.read_only,
    risk_ceiling: policy.risk_ceiling,
    approval_rules: { ...policy.approval_rules },
    tool_modes: { ...policy.tool_modes },
    default_tool_mode: policy.default_tool_mode,
    default_enabled: policy.default_enabled,
    deepspace_overrides: { ...policy.deepspace_overrides },
    conversation_overrides: { ...policy.conversation_overrides },
  };
}
