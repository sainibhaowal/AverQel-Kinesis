"use client";

import { useState } from "react";
import { ChevronDown, ChevronRight, KeyRound, Terminal } from "lucide-react";

export interface DeepSpaceApprovalCardProps {
  approvalId: string;
  toolName?: string;
  data: Record<string, unknown>;
  busy?: boolean;
  className?: string;
  onResolve: (
    approvalId: string,
    decision: "approved" | "denied",
    options?: { enableFullAccessSession?: boolean },
  ) => Promise<void> | void;
}

function cleanWarningText(warning: string, hasCommand: boolean): string {
  const trimmed = warning.trim();
  if (!trimmed) return "";
  if (!hasCommand) return trimmed;
  const stripped = trimmed.replace(/\s*Command:\s*[\s\S]*$/i, "").trim();
  return stripped || trimmed;
}

function buildCapabilityBullets({
  resolvedTool,
  commandText,
  cwdValue,
  timeoutSeconds,
  isFullAccessEscalation,
}: {
  resolvedTool: string;
  commandText: string | null;
  cwdValue: string | null;
  timeoutSeconds: number | null;
  isFullAccessEscalation: boolean;
}): string[] {
  const bullets: string[] = [];
  if (commandText) {
    const compact = commandText.replace(/\s+/g, " ").trim();
    bullets.push(`Run ${compact.length > 96 ? `${compact.slice(0, 95)}…` : compact}`);
  } else {
    bullets.push(`Execute ${resolvedTool}`);
  }
  if (cwdValue) {
    bullets.push(
      `Working directory: ${cwdValue}${timeoutSeconds !== null ? ` (${timeoutSeconds}s budget)` : ""}`,
    );
  }
  if (isFullAccessEscalation) {
    bullets.push("Temporary Full Access outside the strict workspace fence");
  }
  return bullets.slice(0, 3);
}

export default function DeepSpaceApprovalCard({
  approvalId,
  toolName,
  data,
  busy = false,
  className,
  onResolve,
}: DeepSpaceApprovalCardProps) {
  const [showRawJson, setShowRawJson] = useState(false);

  const serverName = typeof data.mcp_server === "string" ? data.mcp_server.trim() : "";
  const resolvedTool = String(data.mcp_tool ?? data.tool_name ?? toolName ?? "tool").trim();
  const toolInput =
    data.tool_input && typeof data.tool_input === "object" && !Array.isArray(data.tool_input)
      ? (data.tool_input as Record<string, unknown>)
      : null;

  const commandText =
    toolInput && typeof toolInput.command === "string"
      ? toolInput.command
      : toolInput && typeof toolInput.script === "string"
        ? toolInput.script
        : null;
  const timeoutSeconds =
    toolInput && typeof toolInput.timeout_seconds === "number" ? toolInput.timeout_seconds : null;
  const cwdValue =
    toolInput && typeof toolInput.cwd === "string" && toolInput.cwd.trim()
      ? toolInput.cwd.trim()
      : null;

  const requiredMode = typeof data.required_mode === "string" ? data.required_mode.trim() : "";
  const isFullAccessEscalation = requiredMode === "danger-full-access";
  const rawWarning = typeof data.warning === "string" ? data.warning : "";
  const cleanWarning = cleanWarningText(rawWarning, Boolean(commandText));

  const requesterLabel =
    serverName && serverName.toLowerCase() !== resolvedTool.toLowerCase()
      ? `${serverName} · ${resolvedTool}`
      : resolvedTool;

  const titleText = isFullAccessEscalation
    ? "Full access required"
    : commandText
      ? "Command permission"
      : "Tool access";

  const bullets = buildCapabilityBullets({
    resolvedTool,
    commandText,
    cwdValue,
    timeoutSeconds,
    isFullAccessEscalation,
  });

  return (
    <section
      aria-label="Approval required"
      className={
        className ??
        "deepspace-approval-card relative mb-4 overflow-hidden rounded-2xl border border-l-4 border-slate-200/90 border-l-amber-500 bg-slate-100 shadow-[0_12px_32px_-20px_rgba(15,23,42,0.28)] backdrop-blur-md dark:border-white/10 dark:border-l-amber-300 dark:bg-amber-950/35 dark:shadow-[0_18px_44px_-22px_rgba(0,0,0,0.85)]"
      }
    >
      <div className="p-4 sm:p-5">
        {/* Header matching clean permission-grant layout */}
        <div className="flex items-start gap-3">
          <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl border border-slate-200 bg-slate-100 text-slate-700 dark:border-white/10 dark:bg-white/[0.06] dark:text-slate-300">
            <KeyRound size={16} />
          </div>
          <div className="min-w-0 flex-1">
            <div className="flex items-center justify-between gap-2">
              <h3 className="text-[14px] font-semibold tracking-[-0.01em] text-slate-950 dark:text-slate-100">
                Approval required
              </h3>
              {timeoutSeconds !== null ? (
                <span className="shrink-0 rounded-full border border-slate-200 px-2 py-0.5 font-mono text-[10px] text-slate-500 dark:border-white/10 dark:text-slate-400">
                  {timeoutSeconds}s timeout
                </span>
              ) : null}
            </div>
            <p className="mt-0.5 text-xs text-slate-500 dark:text-slate-400">
              {titleText} requested by <span className="font-mono">{requesterLabel}</span>
            </p>
          </div>
        </div>

        {/* Summary grants list */}
        <div className="mt-3.5">
          <div className="font-mono text-[11px] tracking-wide text-slate-500 dark:text-slate-400">
            this grants
          </div>
          <ul className="mt-1.5 space-y-1 text-[13px] text-slate-700 dark:text-slate-200">
            {bullets.map((bullet, idx) => (
              <li key={idx} className="flex items-start gap-2">
                <span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-slate-400 dark:bg-slate-500" />
                <span className="min-w-0 break-words">{bullet}</span>
              </li>
            ))}
          </ul>
        </div>

        {cleanWarning ? (
          <p className="mt-3 rounded-xl border border-amber-200/80 bg-white/90 px-3 py-2 text-xs text-slate-800 dark:border-amber-400/20 dark:bg-amber-400/10 dark:text-amber-100">
            {cleanWarning}
          </p>
        ) : null}

        {/* Full command preview when multi-line or long */}
        {commandText ? (
          <div className="mt-3 overflow-hidden rounded-xl border border-slate-200/90 bg-slate-50 text-slate-900 dark:border-white/10 dark:bg-black/45 dark:text-slate-100">
            <div className="flex items-center justify-between border-b border-slate-200/90 bg-slate-100/80 px-3 py-1.5 text-[10.5px] text-slate-600 dark:border-white/10 dark:bg-transparent dark:text-slate-400">
              <span className="inline-flex items-center gap-1.5 font-mono text-slate-700 dark:text-slate-300">
                <Terminal size={11} className="text-slate-500 dark:text-slate-400" />
                {resolvedTool}
              </span>
              {cwdValue ? <span className="truncate font-mono">{cwdValue}</span> : null}
            </div>
            <pre className="max-h-36 overflow-auto px-3 py-2 font-mono text-[11.5px] leading-relaxed whitespace-pre-wrap text-slate-900 dark:text-slate-100">
              {commandText}
            </pre>
          </div>
        ) : null}

        {/* Collapsible details */}
        {toolInput ? (
          <div className="mt-2.5">
            <button
              type="button"
              onClick={() => setShowRawJson((prev) => !prev)}
              className="inline-flex items-center gap-1 text-[11px] font-medium text-slate-500 transition hover:text-slate-800 dark:text-slate-400 dark:hover:text-slate-200"
            >
              {showRawJson ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
              {showRawJson ? "Hide details" : "Details"}
            </button>
            {showRawJson ? (
              <pre className="mt-1.5 max-h-36 overflow-auto rounded-xl border border-slate-200 bg-slate-50 p-2.5 font-mono text-[10.5px] leading-5 whitespace-pre-wrap text-slate-800 dark:border-white/10 dark:bg-black/50 dark:text-slate-300">
                {JSON.stringify(toolInput, null, 2)}
              </pre>
            ) : null}
          </div>
        ) : null}

        {/* Minimal right-aligned actions */}
        <div className="mt-4 flex flex-wrap items-center justify-end gap-2">
          <button
            type="button"
            disabled={busy}
            onClick={() => void onResolve(approvalId, "denied")}
            className="inline-flex items-center rounded-full px-3.5 py-1.5 text-xs font-medium text-slate-800 transition hover:bg-slate-100 hover:text-slate-900 focus-visible:ring-2 focus-visible:ring-slate-400/60 focus-visible:outline-none disabled:cursor-wait disabled:opacity-50 dark:text-slate-100 dark:hover:bg-white/[0.08] dark:hover:text-white"
          >
            Deny
          </button>
          {isFullAccessEscalation ? (
            <button
              type="button"
              disabled={busy}
              onClick={() =>
                void onResolve(approvalId, "approved", { enableFullAccessSession: true })
              }
              className="inline-flex items-center rounded-full px-3.5 py-1.5 text-xs font-medium text-slate-700 transition hover:bg-slate-100 hover:text-slate-950 focus-visible:ring-2 focus-visible:ring-slate-400/60 focus-visible:outline-none disabled:cursor-wait disabled:opacity-50 dark:text-slate-200 dark:hover:bg-white/[0.08] dark:hover:text-white"
            >
              This session
            </button>
          ) : null}
          <button
            type="button"
            disabled={busy}
            onClick={() => void onResolve(approvalId, "approved")}
            className="deepspace-approval-approve inline-flex items-center rounded-full bg-emerald-600 px-4 py-1.5 text-xs font-semibold text-white shadow-sm transition hover:bg-emerald-500 focus-visible:ring-2 focus-visible:ring-emerald-400/70 focus-visible:outline-none disabled:cursor-wait disabled:opacity-50 dark:bg-emerald-500 dark:text-slate-950 dark:hover:bg-emerald-400"
          >
            {isFullAccessEscalation ? "Approve once" : "Approve"}
          </button>
        </div>
      </div>
    </section>
  );
}
