"use client";

import { AverQelLoader } from "@/app/components/ui/AverQelLoading";

import { memo, useEffect, useId, useRef, useState, type ReactNode } from "react";
import { BrainCircuit } from "lucide-react";
import {
  Check,
  CheckCircle2,
  ChevronDown,
  CircleAlert,
  Copy,
  Eye,
  FileText,
  FlaskConical,
  ListChecks,
  Maximize2,
  MessageCircleQuestion,
  Minimize2,
  ShieldCheck,
  Terminal,
  Wrench,
  XCircle,
} from "lucide-react";
import { renderHighlightedLineTokens } from "../../query/_components/CodeBlock";
import DeepSpaceMarkdownRenderer from "./DeepSpaceMarkdownRenderer";
import {
  parseTimestampMs,
  type AgentStep,
  type SubagentActivityTimeline,
  type TimelineStep,
} from "../_lib/deepspace-stream";

const MAX_DETAIL_LENGTH = 2400;
const MAX_EXPANDED_DETAIL_LENGTH = 20000;

function truncateDetail(value: string, limit = MAX_DETAIL_LENGTH): string {
  if (value.length <= limit) return value;
  return `${value.slice(0, limit)}\n… (${value.length - limit} more characters)`;
}

function sanitizeDetail(value: unknown, depth = 0): unknown {
  if (depth > 4) return "[nested value hidden]";
  if (typeof value === "string") {
    return value
      .replace(/Bearer\s+[A-Za-z0-9._~-]+/gi, "Bearer [redacted]")
      .replace(/(sk-[A-Za-z0-9_-]{12,})/g, "[redacted key]");
  }
  if (Array.isArray(value))
    return value.slice(0, 40).map((item) => sanitizeDetail(item, depth + 1));
  if (value && typeof value === "object") {
    return Object.entries(value as Record<string, unknown>).reduce<Record<string, unknown>>(
      (result, [key, item]) => {
        if (
          /(token|secret|password|authorization|cookie|api[_-]?key|private[_-]?key|credential)/i.test(
            key,
          )
        ) {
          result[key] = "[redacted]";
        } else {
          result[key] = sanitizeDetail(item, depth + 1);
        }
        return result;
      },
      {},
    );
  }
  return value;
}

function humanizeKey(value: string): string {
  return value
    .replace(/[_-]+/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .replace(/\b\w/g, (character) => character.toUpperCase());
}

function readableValue(value: unknown, indent = ""): string {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  if (Array.isArray(value)) {
    if (!value.length) return "None";
    return value
      .map((item) => {
        const formatted = readableValue(item, `${indent}  `);
        return `${indent}• ${formatted.replace(/\n/g, `\n${indent}  `)}`;
      })
      .join("\n");
  }
  if (typeof value === "object") {
    return Object.entries(value as Record<string, unknown>)
      .map(([key, item]) => {
        const formatted = readableValue(item, `${indent}  `);
        return `${indent}${humanizeKey(key)}: ${formatted.replace(/\n/g, `\n${indent}`)}`;
      })
      .join("\n");
  }
  return String(value);
}

function readablePartialStructuredText(value: string): string | null {
  if (!/^\s*[\[{]/.test(value)) return null;
  const fields = [
    ...value.matchAll(/"([^"\\]+)"\s*:\s*(?:"([^"\\]*)|([-\d.]+)|(true|false|null))/g),
  ];
  if (!fields.length) return "Collecting tool details…";
  return fields
    .map(
      (field) => `${humanizeKey(field[1] ?? "Detail")}: ${field[2] ?? field[3] ?? field[4] ?? "—"}`,
    )
    .join("\n");
}

function formatDetail(value: unknown, limit = MAX_DETAIL_LENGTH): string | null {
  if (value === undefined || value === null || value === "") return null;
  if (typeof value === "string") {
    const sanitized = String(sanitizeDetail(value));
    try {
      return truncateDetail(readableValue(sanitizeDetail(JSON.parse(sanitized))), limit);
    } catch {
      return truncateDetail(readablePartialStructuredText(sanitized) ?? sanitized, limit);
    }
  }
  try {
    return truncateDetail(readableValue(sanitizeDetail(value)), limit);
  } catch {
    return "[detail unavailable]";
  }
}

function detailPreview(value: string | null): string | null {
  if (!value) return null;
  const firstLine = value
    .split("\n")
    .map((line) => line.trim())
    .find(Boolean);
  if (!firstLine) return null;
  return firstLine.length > 120 ? `${firstLine.slice(0, 117)}…` : firstLine;
}

export function formatElapsed(ms: number): string {
  const seconds = Math.max(0, Math.round(ms / 1000));
  if (seconds < 60) return `${seconds} second${seconds === 1 ? "" : "s"}`;
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds % 60;
  return `${minutes}m ${remainder}s`;
}

function timelineDurationMs(
  timeline: TimelineStep[],
  now: number,
  messageStartedAtValue: string | undefined,
  isStreaming: boolean,
): number | null {
  const stepTimestamps = timeline
    .flatMap((step) => [parseTimestampMs(step.startedAt), parseTimestampMs(step.completedAt ?? "")])
    .filter((timestamp) => Number.isFinite(timestamp));
  if (!stepTimestamps.length) return null;

  // For streaming: use message start time as baseline, current time as end
  if (isStreaming || timeline.some((step) => step.status === "running")) {
    const messageStartedAt = messageStartedAtValue
      ? parseTimestampMs(messageStartedAtValue)
      : Number.NaN;
    const startedTimestamp = Number.isFinite(messageStartedAt)
      ? messageStartedAt
      : Math.min(...stepTimestamps);
    return Math.max(0, now - startedTimestamp);
  }

  // For completed: use actual timeline step timestamps only
  // (not message.createdAt which may be from a previous turn)
  const startedTimestamp = Math.min(...stepTimestamps);
  const completedTimestamp = Math.max(...stepTimestamps);
  return Math.max(0, completedTimestamp - startedTimestamp);
}

type TimelineExpandSignal = {
  expanded: boolean;
  seq: number;
};

function AnimatedActivityDetails({
  label,
  preview,
  defaultOpen = false,
  forceSignal,
  children,
}: {
  label: string;
  preview?: string | null;
  defaultOpen?: boolean;
  forceSignal?: TimelineExpandSignal;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const lastSignalSeq = useRef(0);
  const id = `deepspace-detail-${useId().replace(/:/g, "")}`;

  useEffect(() => {
    if (!forceSignal || forceSignal.seq === 0 || forceSignal.seq === lastSignalSeq.current) {
      return;
    }
    lastSignalSeq.current = forceSignal.seq;
    setOpen(forceSignal.expanded);
  }, [forceSignal]);

  return (
    <div className="mt-2 border-t border-white/6 pt-1">
      <button
        type="button"
        aria-expanded={open}
        aria-controls={id}
        onClick={() => setOpen((value) => !value)}
        className="text-foreground/45 hover:text-foreground/75 flex w-full cursor-pointer items-center gap-1 py-1 text-left text-[10px] transition-colors duration-200 focus-visible:ring-2 focus-visible:ring-cyan-300/40 focus-visible:outline-none"
      >
        <ChevronDown
          size={12}
          className={`shrink-0 transition-transform duration-300 ease-out motion-reduce:transition-none ${open ? "rotate-0" : "-rotate-90"}`}
        />
        <span>{label}</span>
        {preview ? <span className="normal-case">— {preview}</span> : null}
      </button>
      <div
        id={id}
        aria-hidden={!open}
        className={`grid transition-[grid-template-rows,opacity] duration-300 ease-out motion-reduce:transition-none ${open ? "grid-rows-[1fr] opacity-100" : "grid-rows-[0fr] opacity-0"}`}
      >
        <div className="min-h-0 overflow-hidden">{children}</div>
      </div>
    </div>
  );
}

function stepTitle(step: AgentStep): string {
  if (step.data?.phase === "resolving_provider") return "Model Selection";
  if (step.data?.phase === "provider_ready") return "Provider Connection";
  if (step.data?.phase === "provider_unavailable") return "Provider Unavailable";
  if (step.data?.phase === "finalizing") return "Turn Finalization";
  if (step.type === "plan") return "Plan";
  if (step.type === "permission_request" || step.type === "ask_user_question")
    return "Approval or input required";
  if (step.type === "observing") return "Observation";
  if (step.type === "agent_testing") return "Testing";
  if (step.type === "agent_verifying") return "Verification";
  if (step.type === "agent_self_correct") return "Self-correction";
  if (step.type === "tool_error") return "Tool error";
  if (step.type === "tool_result") return "Tool result";
  return "Tool call";
}

function StepIcon({ step }: { step: AgentStep }) {
  if (step.status === "running")
    return <AverQelLoader size={13} className="animate-spin text-cyan-300" />;
  if (step.status === "awaiting_approval")
    return <ShieldCheck size={13} className="text-amber-300" />;
  if (step.status === "failed") return <XCircle size={13} className="text-red-300" />;
  if (step.data?.phase === "resolving_provider")
    return <BrainCircuit size={13} className="text-violet-300" />;
  if (step.data?.phase === "provider_ready")
    return <CheckCircle2 size={13} className="text-emerald-400" />;
  if (step.type === "plan") return <ListChecks size={13} className="text-violet-300" />;
  if (step.type === "observing") return <Eye size={13} className="text-cyan-300" />;
  if (step.type === "agent_testing" || step.type === "agent_verifying") {
    return <FlaskConical size={13} className="text-blue-300" />;
  }
  if (step.type === "permission_request" || step.type === "ask_user_question") {
    return <MessageCircleQuestion size={13} className="text-amber-300" />;
  }
  if (step.type === "tool_error") return <CircleAlert size={13} className="text-red-300" />;
  if (step.status === "completed") return <CheckCircle2 size={13} className="text-emerald-300" />;
  return <Wrench size={13} className="text-cyan-300" />;
}

const ActivityStep = memo(function ActivityStep({
  step,
  onOpenFileDiff,
}: {
  step: AgentStep;
  onOpenFileDiff?: (path: string) => void;
}) {
  const input = formatDetail(step.toolInput ?? step.data?.tool_input);
  const output = formatDetail(
    step.toolOutput ?? step.plan ?? step.data?.message,
    MAX_EXPANDED_DETAIL_LENGTH,
  );
  const outputPreview = detailPreview(output);
  const diffStats = step.diffStats;
  const toolName = step.toolName?.trim() ?? "";
  const isUserQuestion = step.type === "ask_user_question" || toolName === "ask_user";
  const statusLabel =
    step.status === "awaiting_approval"
      ? isUserQuestion
        ? "awaiting your answer"
        : "awaiting approval"
      : step.status === "running"
        ? "running"
        : step.status;

  return (
    <div
      className="motion-safe:animate-in motion-safe:fade-in motion-safe:slide-in-from-bottom-1 border-b border-white/8 pb-3 motion-safe:duration-300 motion-safe:ease-out"
      data-testid="deepspace-activity-step"
    >
      <div className="text-foreground/65 flex items-center gap-2 text-[10px] font-semibold tracking-[0.12em] uppercase">
        <StepIcon step={step} />
        <span>{stepTitle(step)}</span>
        {toolName ? (
          <span className="text-foreground/85 min-w-0 truncate font-mono normal-case">
            {toolName}
          </span>
        ) : null}
        <span className="text-foreground/40 ml-auto shrink-0 text-[9px] tracking-normal normal-case">
          {statusLabel}
        </span>
      </div>
      {input ? (
        <AnimatedActivityDetails label="Input">
          <pre className="text-foreground/60 max-h-48 overflow-auto py-2 text-[10px] leading-5 break-words whitespace-pre-wrap">
            {input}
          </pre>
        </AnimatedActivityDetails>
      ) : null}
      {isTerminalTool(toolName) ? (
        <TerminalBlock
          toolName={toolName}
          status={step.status}
          toolInput={step.toolInput}
          rawOutput={step.toolOutput}
          input={input}
          output={output}
        />
      ) : null}
      {isSubagentTool(toolName) ? <SubagentBlock step={step} /> : null}
      {isDiffTool(toolName) && diffStats ? (
        <DiffBlock diffStats={diffStats} onOpenDiff={onOpenFileDiff} />
      ) : null}
      {output &&
      !isTerminalTool(toolName) &&
      !isSubagentTool(toolName) &&
      !(isDiffTool(toolName) && diffStats) ? (
        <AnimatedActivityDetails
          label="Output / progress"
          preview={outputPreview}
          defaultOpen={step.status === "running"}
        >
          <pre className="text-foreground/60 max-h-56 overflow-auto py-2 text-[10px] leading-5 break-words whitespace-pre-wrap">
            {output}
          </pre>
        </AnimatedActivityDetails>
      ) : null}
    </div>
  );
});

function isTerminalTool(toolName: string): boolean {
  const normalized = toolName.trim().toLowerCase();
  return (
    [
      "bash",
      "pwsh",
      "powershell",
      "ssh_run",
      "terminal_open",
      "terminal_read",
      "terminal_write",
      "terminal_close",
      "terminal_list",
      "terminal_signal",
      "terminal_job",
    ].includes(normalized) ||
    normalized.startsWith("terminal_") ||
    normalized.startsWith("ssh_")
  );
}

function fileBasename(path: string): string {
  const parts = path.split(/[\\/]/).filter(Boolean);
  return parts[parts.length - 1] ?? path;
}

function formatTerminalBody(
  rawToolOutput: string | null | undefined,
  formattedOutput: string | null | undefined,
): string {
  const raw = typeof rawToolOutput === "string" ? rawToolOutput.trim() : "";
  if (raw.startsWith("{")) {
    try {
      const parsed = JSON.parse(raw) as Record<string, unknown>;
      const text =
        typeof parsed.output === "string"
          ? parsed.output
          : typeof parsed.stdout === "string"
            ? [parsed.stdout, typeof parsed.stderr === "string" ? parsed.stderr : ""]
                .filter(Boolean)
                .join("\n")
            : null;
      if (text !== null) return text;
    } catch {
      // Keep raw string when output is truncated JSON or plain text.
    }
  }
  return formattedOutput ?? raw;
}

function isSubagentTool(toolName: string): boolean {
  return ["subagent", "subagent_fork", "send_message", "interrupt_agent"].includes(toolName);
}

function isDiffTool(toolName: string): boolean {
  return [
    "write",
    "fs_write",
    "edit",
    "fs_edit",
    "fs_str_replace",
    "write_file",
    "file_write",
    "edit_file",
    "file_edit",
  ].includes(toolName);
}

function terminalCommand(input: Record<string, unknown> | undefined): string {
  if (!input) return "";
  const command = input["command"] ?? input["cmd"] ?? input["text"] ?? input["content"] ?? "";
  return typeof command === "string" ? command : "";
}

function extractTerminalExitCode(rawToolOutput: string | null | undefined): number | null {
  const raw = typeof rawToolOutput === "string" ? rawToolOutput.trim() : "";
  if (raw.startsWith("{")) {
    try {
      const parsed = JSON.parse(raw) as Record<string, unknown>;
      if (typeof parsed.exit_code === "number") return parsed.exit_code;
      if (typeof parsed.returncode === "number") return parsed.returncode;
    } catch {
      // Ignore partial JSON while streaming.
    }
  }
  return null;
}

/** Render a terminal/shell block matching assistant-ui terminal-block inside the timeline. */
function TerminalBlock({
  toolName,
  status,
  toolInput,
  rawOutput,
  input,
  output,
  forceExpand = false,
}: {
  toolName?: string;
  status: string;
  toolInput?: Record<string, unknown>;
  rawOutput?: string;
  input: string | null | undefined;
  output: string | null | undefined;
  forceExpand?: boolean;
}) {
  const [copied, setCopied] = useState(false);
  const [expanded, setExpanded] = useState(forceExpand);
  const command = toolInput ? terminalCommand(toolInput) : (input ?? "");
  const bodyText = formatTerminalBody(rawOutput, output);
  const exitCode = extractTerminalExitCode(rawOutput);
  const isRunning = status === "running";
  const isFailed = status === "failed" || (exitCode !== null && exitCode !== 0);
  const isClosed =
    toolName === "terminal_close" || (typeof output === "string" && output.includes("closed"));
  const isFullExpanded = expanded || forceExpand;

  const handleCopy = () => {
    const payload = [command, bodyText].filter(Boolean).join("\n");
    if (!payload || typeof navigator === "undefined" || !navigator.clipboard?.writeText) return;
    void navigator.clipboard.writeText(payload).then(() => {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    });
  };

  return (
    <div
      className="my-2 overflow-hidden rounded-xl border border-white/10 bg-[#141514]"
      data-testid="deepspace-activity-terminal"
    >
      <div className="flex items-start gap-2 border-b border-white/[0.07] px-3.5 py-2">
        <Terminal size={12} className="text-foreground/45 mt-0.5 shrink-0" />
        <span
          className={`text-foreground/90 min-w-0 flex-1 font-mono text-[11.5px] font-medium ${
            isFullExpanded ? "break-words whitespace-pre-wrap" : "truncate"
          }`}
        >
          {command || toolName || "terminal"}
        </span>
        {isRunning ? (
          <span className="inline-flex shrink-0 items-center gap-1.5 font-mono text-[11px] text-emerald-400">
            <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-emerald-400" />
            running
          </span>
        ) : isClosed ? (
          <span className="shrink-0 font-mono text-[11px] text-foreground/45">closed</span>
        ) : isFailed ? (
          <span className="inline-flex shrink-0 items-center gap-1 font-mono text-[11px] text-rose-400 tabular-nums">
            <XCircle size={11} />
            exit {exitCode ?? 1}
          </span>
        ) : (
          <span className="inline-flex shrink-0 items-center gap-1 font-mono text-[11px] text-emerald-400 tabular-nums">
            <Check size={11} />
            exit {exitCode ?? 0}
          </span>
        )}
        <button
          type="button"
          onClick={() => setExpanded((value) => !value)}
          aria-expanded={isFullExpanded}
          aria-label={isFullExpanded ? "Compact terminal view" : "Expand terminal end to end"}
          title={isFullExpanded ? "Compact terminal view" : "Expand end to end"}
          className="text-foreground/45 hover:text-foreground/90 inline-flex shrink-0 cursor-pointer items-center rounded p-1 transition hover:bg-white/[0.08]"
        >
          {isFullExpanded ? <Minimize2 size={12} /> : <Maximize2 size={12} />}
        </button>
        <button
          type="button"
          onClick={handleCopy}
          aria-label={copied ? "Copied terminal output" : "Copy terminal output"}
          title={copied ? "Copied" : "Copy"}
          className="text-foreground/45 hover:text-foreground/90 inline-flex shrink-0 cursor-pointer items-center rounded p-1 transition hover:bg-white/[0.08]"
        >
          {copied ? <Check size={12} className="text-emerald-400" /> : <Copy size={12} />}
        </button>
      </div>
      {bodyText ? (
        <pre
          className={`deepspace-code-scroll overscroll-contain px-3.5 py-2.5 font-mono text-[11.5px] leading-relaxed break-words whitespace-pre-wrap text-foreground/80 ${
            isFullExpanded ? "" : "max-h-56 overflow-auto"
          }`}
        >
          {bodyText}
        </pre>
      ) : null}
    </div>
  );
}

/** Render a green/red code-diff card matching assistant-ui elements/code-diff. */
function DiffBlock({
  diffStats,
  onOpenDiff,
  forceExpand = false,
}: {
  diffStats: NonNullable<AgentStep["diffStats"]>;
  onOpenDiff?: (path: string) => void;
  forceExpand?: boolean;
}) {
  const additions = diffStats.additions ?? 0;
  const deletions = diffStats.deletions ?? 0;
  const diffLines = diffStats.diffLines ?? [];

  return (
    <div
      className="my-2 overflow-hidden rounded-xl border border-white/10 bg-[#141514]"
      data-testid="deepspace-activity-diff"
    >
      <div className="flex items-center gap-2 px-3.5 py-2">
        <FileText size={12} className="shrink-0 text-foreground/45" />
        {onOpenDiff && diffStats.path ? (
          <button
            type="button"
            onClick={() => onOpenDiff(diffStats.path)}
            className="min-w-0 cursor-pointer truncate font-mono text-[11.5px] font-medium text-foreground/90 transition hover:text-cyan-300"
            title={`Open ${diffStats.path} in review panel`}
          >
            {diffStats.path}
          </button>
        ) : diffStats.path ? (
          <span className="min-w-0 truncate font-mono text-[11.5px] font-medium text-foreground/90">
            {diffStats.path}
          </span>
        ) : null}
        <span className="ml-auto flex shrink-0 items-center gap-2 font-mono text-[11px] font-medium tabular-nums">
          <span className="text-emerald-400">+{additions}</span>
          <span className="text-rose-400">-{deletions}</span>
          {onOpenDiff && diffStats.path ? (
            <button
              type="button"
              onClick={() => onOpenDiff(diffStats.path)}
              className="rounded-md px-1.5 py-0.5 text-[10px] text-foreground/55 transition hover:bg-white/[0.08] hover:text-foreground"
            >
              Review
            </button>
          ) : null}
        </span>
      </div>
      {diffLines.length > 0 ? (
        <div
          className={`deepspace-code-scroll overscroll-contain border-t border-white/[0.07] ${
            forceExpand ? "overflow-x-auto" : "max-h-64 overflow-auto"
          }`}
        >
          <table className="w-full font-mono text-[11.5px] leading-5">
            <tbody>
              {diffLines.map((line, idx) => {
                const isAdded = line.type === "added";
                const isRemoved = line.type === "removed";
                return (
                  <tr
                    key={`${diffStats.path}-${idx}`}
                    className={
                      isAdded
                        ? "bg-emerald-500/[0.13]"
                        : isRemoved
                          ? "bg-rose-500/[0.13]"
                          : ""
                    }
                  >
                    <td
                      className={`w-7 px-2 text-center select-none ${
                        isAdded
                          ? "text-emerald-400"
                          : isRemoved
                            ? "text-rose-400"
                            : "text-foreground/30"
                      }`}
                    >
                      {line.type === "unchanged" ? " " : isAdded ? "+" : "-"}
                    </td>
                    <td className="pr-3.5 whitespace-pre">
                      <span>
                        {renderHighlightedLineTokens(
                          line.text,
                          diffStats.path.split(".").pop()?.toLowerCase() ?? "typescript",
                        )}
                      </span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : null}
    </div>
  );
}

/** Render a subagent delegation block. */
function SubagentBlock({ step }: { step: AgentStep }) {
  const input = step.toolInput ?? {};
  const agentName =
    typeof input["description"] === "string"
      ? (input["description"] as string)
      : typeof input["agent_name"] === "string"
        ? (input["agent_name"] as string)
        : "";
  const mission =
    typeof input["task"] === "string"
      ? (input["task"] as string)
      : typeof input["mission"] === "string"
        ? (input["mission"] as string)
        : "";
  const mode =
    typeof input["mode"] === "string"
      ? (input["mode"] as string)
      : input["continuable"] === true
        ? "continuable"
        : "one-shot";
  const isRunning = step.status === "running";
  const liveProgress =
    typeof step.toolOutput === "string" && step.toolOutput.trim() ? step.toolOutput.trim() : "";

  return (
    <div
      className="mb-2 rounded-lg border border-white/8 bg-white/5 p-3"
      data-testid="deepspace-activity-subagent"
    >
      <div className="mb-1 flex items-center gap-2">
        <BotIcon size={11} className="text-amber-300" />
        <span className="text-foreground/60 text-[9px] font-semibold tracking-[0.12em] uppercase">
          Sub-agent
        </span>
        {isRunning ? (
          <span className="ml-auto h-1.5 w-1.5 animate-pulse rounded-full bg-amber-400" />
        ) : null}
      </div>
      {agentName ? (
        <div className="text-foreground/80 mb-1 text-[10px] leading-5">{agentName}</div>
      ) : null}
      {mission ? (
        <div className="text-foreground/60 max-h-48 overflow-auto overscroll-contain text-[10px] leading-5 break-words whitespace-pre-wrap">
          {mission}
        </div>
      ) : null}
      {liveProgress ? (
        <div
          className="text-cyan-200/75 mt-1.5 truncate font-mono text-[10px]"
          data-testid="deepspace-activity-subagent-progress"
        >
          {liveProgress}
        </div>
      ) : null}
      <div className="text-foreground/40 mt-2 flex items-center gap-2 text-[9px]">
        <span>{mode === "continuable" ? "Continuable" : "One-shot"}</span>
        <span className="w-px bg-white/8" />
        <span>{step.durationMs ? `${step.durationMs}ms` : "—"}</span>
      </div>
    </div>
  );
}

export function BotIcon(props: { size: number; className?: string }) {
  return (
    <svg
      width={props.size}
      height={props.size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={props.className}
    >
      <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
    </svg>
  );
}

function TimelineIcon({ step }: { step: TimelineStep }) {
  if (step.status === "running") {
    return <AverQelLoader size={13} className="animate-spin text-cyan-300" />;
  }
  if (step.status === "awaiting_approval") {
    return <ShieldCheck size={13} className="text-amber-300" />;
  }
  if (step.status === "failed") return <XCircle size={13} className="text-red-300" />;
  if (step.type === "thinking") return <BrainCircuit size={13} className="text-violet-300" />;
  if (step.type === "model_message")
    return <MessageCircleQuestion size={13} className="text-cyan-300" />;
  if (step.type === "plan") return <ListChecks size={13} className="text-violet-300" />;
  if (step.type === "observation") return <Eye size={13} className="text-cyan-300" />;
  if (step.type === "testing") return <FlaskConical size={13} className="text-blue-300" />;
  if (step.type === "permission") return <ShieldCheck size={13} className="text-amber-300" />;
  if (step.type === "error") return <CircleAlert size={13} className="text-red-300" />;
  if (step.toolName === "subagent_wait" && step.status === "completed") {
    return <CheckCircle2 size={13} className="text-emerald-400" />;
  }
  return <Wrench size={13} className="text-cyan-300" />;
}

function shortTargetValue(raw: unknown, maxLen = 72): string | undefined {
  if (typeof raw !== "string") return undefined;
  const compact = raw.replace(/\s+/g, " ").trim();
  if (!compact) return undefined;
  return compact.length > maxLen ? `${compact.slice(0, maxLen - 1)}…` : compact;
}

function shortUrlLabel(raw: unknown): string | undefined {
  const s = shortTargetValue(raw, 120);
  if (!s) return undefined;
  try {
    const u = new URL(s);
    const path = u.pathname && u.pathname !== "/" ? u.pathname : "";
    const combined = `${u.hostname.replace(/^www\./, "")}${path}`;
    return combined.length > 64 ? `${combined.slice(0, 63)}…` : combined;
  } catch {
    return shortTargetValue(s, 64);
  }
}

function formatTimelineStepDisplay(step: TimelineStep): {
  title: string;
  target?: string;
  filePath?: string;
  showToolName: boolean;
} {
  const providedTitle = step.title.trim();
  if (step.type === "thinking") {
    return { title: providedTitle || "Internal thought", showToolName: false };
  }
  if (step.type === "model_message") {
    return { title: providedTitle || "Model message", showToolName: false };
  }
  if (step.type === "permission") {
    const input = step.toolInput ?? {};
    const target = shortTargetValue(
      input.command ?? input.cmd ?? input.path ?? input.url ?? input.query,
    );
    return {
      title:
        providedTitle ||
        (step.toolName === "ask_user" ? "Clarification Needed" : "Clearance Required"),
      target,
      showToolName: false,
    };
  }
  const toolName = (step.toolName ?? "").trim();
  const normalized = toolName.toLowerCase();
  const input = step.toolInput ?? {};
  const isRunning = step.status === "running";
  const isFailed = step.status === "failed";

  if (normalized === "subagent_wait") {
    return { title: providedTitle || "Waiting for subagent", showToolName: false };
  }
  if (normalized === "get_agent") {
    return {
      title: providedTitle || (isRunning
        ? "Checking subagent status"
        : isFailed
          ? "Subagent check failed"
          : "Checked subagent status"),
      showToolName: false,
    };
  }
  if (normalized === "web_search" || normalized === "searxng_search") {
    const query = shortTargetValue(input.query ?? input.q);
    return {
      title: providedTitle || (isRunning ? "Searching the web" : isFailed ? "Web search failed" : "Searched the web"),
      target: query ? `"${query}"` : undefined,
      showToolName: false,
    };
  }
  if (normalized === "web_fetch" || normalized === "url_read" || normalized === "crawl_url") {
    const target = shortUrlLabel(input.url ?? input.target);
    return {
      title: providedTitle || (isRunning ? "Fetching page" : isFailed ? "Fetch failed" : "Fetched page"),
      target,
      showToolName: false,
    };
  }
  if (normalized === "read" || normalized === "fs_read" || normalized === "read_file") {
    const rawPath = String(input.path ?? input.filename ?? input.target ?? "").trim();
    const target = shortTargetValue(rawPath || input.query);
    return {
      title: providedTitle || (isRunning ? "Reading" : isFailed ? "Read failed" : "Read"),
      target,
      filePath: rawPath || undefined,
      showToolName: false,
    };
  }
  if (
    normalized === "write" ||
    normalized === "fs_write" ||
    normalized === "write_file" ||
    normalized === "file_write" ||
    normalized === "edit" ||
    normalized === "fs_edit" ||
    normalized === "edit_file" ||
    normalized === "file_edit" ||
    normalized === "fs_str_replace"
  ) {
    const rawPath = String(
      step.diffStats?.path ?? input.path ?? input.filename ?? input.target ?? "",
    ).trim();
    const target = shortTargetValue(rawPath);
    const isEdit = normalized.includes("edit") || normalized.includes("str_replace");
    return {
      title: providedTitle || (isRunning
        ? isEdit
          ? "Editing"
          : "Writing"
        : isFailed
          ? isEdit
            ? "Edit failed"
            : "Write failed"
          : isEdit
            ? "Edited"
            : "Wrote"),
      target,
      filePath: rawPath || undefined,
      showToolName: false,
    };
  }
  if (normalized === "bash" || normalized === "pwsh" || normalized === "powershell") {
    const cmd = shortTargetValue(input.command ?? input.cmd);
    return {
      title:
        (providedTitle && !/shell command/i.test(providedTitle) ? providedTitle : undefined) ||
        (isRunning ? "Running command" : isFailed ? "Command failed" : "Ran command"),
      target: cmd ? `$ ${cmd}` : undefined,
      showToolName: false,
    };
  }
  const titleClean = providedTitle;
  const titleMatchesTool =
    Boolean(toolName) &&
    titleClean.toLowerCase().includes(toolName.replace(/_/g, " ").toLowerCase());
  return {
    title: titleClean || (toolName ? toolName.replace(/_/g, " ") : "Tool call"),
    showToolName: Boolean(toolName) && !titleMatchesTool,
  };
}

function timelineStatus(step: TimelineStep): string {
  if (step.status === "awaiting_approval") {
    return step.type === "ask_user_question" || step.toolName === "ask_user"
      ? "awaiting your answer"
      : "awaiting approval";
  }
  if (step.status === "running") return "live";
  return step.status;
}

export const TimelineEntry = memo(function TimelineEntry({
  step,
  index,
  isLast,
  onOpenFileDiff,
  expandSignal,
}: {
  step: TimelineStep;
  index: number;
  isLast: boolean;
  onOpenFileDiff?: (path: string) => void;
  expandSignal?: TimelineExpandSignal;
}) {
  const [open, setOpen] = useState(
    step.status === "running" || step.status === "awaiting_approval",
  );
  const [expandFull, setExpandFull] = useState(false);
  const previousStatus = useRef(step.status);
  const lastSignalSeq = useRef(0);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const detailsRef = useRef<HTMLDivElement>(null);
  // Thinking is intentionally contained in a scrollable panel, but it must
  // remain complete for users who explicitly open an Internal Thought entry.
  // Keep redaction and structural sanitization; remove only the display-length
  // cap for this user-visible diagnostic stream.
  const details = formatDetail(
    step.details,
    step.type === "thinking" || expandFull ? Number.MAX_SAFE_INTEGER : MAX_EXPANDED_DETAIL_LENGTH,
  );
  const inputStream = formatDetail(step.toolInputStream);
  const input = formatDetail(
    step.toolInput,
    expandFull ? Number.MAX_SAFE_INTEGER : MAX_EXPANDED_DETAIL_LENGTH,
  );
  const output = formatDetail(
    step.toolOutput,
    expandFull ? Number.MAX_SAFE_INTEGER : MAX_EXPANDED_DETAIL_LENGTH,
  );
  const outputPreview = detailPreview(output);
  const toolName = step.toolName?.trim();
  const display = formatTimelineStepDisplay(step);

  useEffect(() => {
    if (!expandSignal || expandSignal.seq === 0 || expandSignal.seq === lastSignalSeq.current) {
      return;
    }
    lastSignalSeq.current = expandSignal.seq;
    setOpen(expandSignal.expanded);
    setExpandFull(expandSignal.expanded);
  }, [expandSignal]);

  useEffect(() => {
    const wasActive =
      previousStatus.current === "running" || previousStatus.current === "awaiting_approval";
    const isActive = step.status === "running" || step.status === "awaiting_approval";

    // A newly active entry is the current working turn. When it finishes,
    // collapse it into its banner automatically. Do not override a user's
    // manual choice while the entry remains in the same state.
    if (isActive) {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setOpen(true);
    } else if (wasActive && step.status === "completed") {
      // A completed entry hides its contents. A nested disclosure button can
      // still own focus at this point, so return focus to the visible trigger
      // before setting aria-hidden on its ancestor.
      if (detailsRef.current?.contains(document.activeElement)) {
        triggerRef.current?.focus();
      }
      setOpen(false);
    }
    previousStatus.current = step.status;
  }, [step.status]);

  // Persisted event streams can contain repeated ids when a provider emits a
  // lifecycle update without a distinct step id. The list position keeps the
  // disclosure target unique while preserving the event id in the DOM id.
  const detailsId = `deepspace-timeline-details-${step.id.replace(/[^a-zA-Z0-9_-]/g, "-")}-${index}`;

  return (
    <li
      className="motion-safe:animate-in motion-safe:fade-in motion-safe:slide-in-from-bottom-1 relative pl-7 motion-safe:duration-300 motion-safe:ease-out"
      data-testid="deepspace-timeline-step"
    >
      {!isLast ? (
        <span className="deepspace-timeline-connector absolute top-0 bottom-[-0.75rem] left-[0.4rem] w-px bg-white/8" />
      ) : null}
      <div className="border-b border-white/8 pb-3">
        <button
          ref={triggerRef}
          type="button"
          aria-expanded={open}
          aria-controls={detailsId}
          onClick={() => setOpen((value) => !value)}
          className="text-foreground/65 hover:text-foreground/85 relative flex w-full cursor-pointer items-center gap-2 rounded-sm text-left text-[10px] font-semibold tracking-[0.12em] uppercase transition-colors duration-200 focus-visible:ring-2 focus-visible:ring-cyan-300/40 focus-visible:outline-none"
        >
          <span className="deepspace-timeline-icon pointer-events-none absolute top-1/2 -left-7 flex h-4 w-4 -translate-y-1/2 items-center justify-center rounded-full border border-white/10 bg-[#101713]">
            <TimelineIcon step={step} />
          </span>
          <ChevronDown
            size={12}
            className={`shrink-0 transition-transform duration-300 ease-out motion-reduce:transition-none ${open ? "rotate-0" : "-rotate-90"}`}
          />
          <span className="text-foreground/30 tabular-nums">
            {String(index + 1).padStart(2, "0")}
          </span>
          <span className="shrink-0">{display.title}</span>
          {display.target ? (
            display.filePath && onOpenFileDiff ? (
              <span
                role="button"
                tabIndex={0}
                onClick={(event) => {
                  event.stopPropagation();
                  onOpenFileDiff(display.filePath!);
                }}
                onKeyDown={(event) => {
                  if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    event.stopPropagation();
                    onOpenFileDiff(display.filePath!);
                  }
                }}
                title={`Open ${display.filePath}`}
                className="min-w-0 truncate rounded bg-white/[0.06] px-1.5 py-0.5 font-mono text-[10px] font-normal tracking-normal text-cyan-300/90 normal-case transition hover:bg-white/10 hover:text-cyan-200"
              >
                {display.target}
              </span>
            ) : (
              <span
                className={`text-foreground/80 min-w-0 font-mono font-normal tracking-normal normal-case ${
                  open && expandFull ? "break-words whitespace-pre-wrap" : "truncate"
                }`}
              >
                {display.target}
              </span>
            )
          ) : display.showToolName && toolName ? (
            <span className="text-foreground/85 min-w-0 truncate font-mono normal-case">
              {toolName}
            </span>
          ) : null}
          {step.diffStats ? (
            <span className="flex shrink-0 items-center gap-1.5 font-mono text-[10px] tracking-normal normal-case tabular-nums">
              <span className="text-emerald-400">+{step.diffStats.additions}</span>
              <span className="text-rose-400">-{step.diffStats.deletions}</span>
            </span>
          ) : null}
          <span className="text-foreground/40 ml-auto shrink-0 text-[9px] tracking-normal normal-case">
            {timelineStatus(step)}
          </span>
        </button>
        <div
          id={detailsId}
          ref={detailsRef}
          aria-hidden={!open}
          className={`grid transition-[grid-template-rows,opacity] duration-300 ease-out motion-reduce:transition-none ${open ? "grid-rows-[1fr] opacity-100" : "grid-rows-[0fr] opacity-0"}`}
        >
          <div className="min-h-0 overflow-hidden">
            {details ? (
              <div
                className={`text-foreground/70 mt-2 ${
                  step.type === "thinking"
                    ? expandFull
                      ? ""
                      : "max-h-64 overflow-auto overscroll-contain"
                    : "leading-5 whitespace-pre-wrap"
                }`}
                data-thinking-scroll={step.type === "thinking" ? "true" : undefined}
              >
                {step.type === "thinking" || step.type === "model_message" ? (
                  <DeepSpaceMarkdownRenderer
                    content={details}
                    streaming={step.status === "running"}
                    compact
                  />
                ) : (
                  details
                )}
              </div>
            ) : null}
            {inputStream && (step.status === "running" || !input) ? (
              <div className="deepspace-tool-arguments mt-2 border-l py-1 pl-3">
                <div className="deepspace-tool-arguments-label mb-1 text-[9px] font-semibold tracking-[0.12em] uppercase">
                  Live tool arguments
                </div>
                <pre
                  className={`deepspace-tool-arguments-text text-[10px] leading-5 break-words whitespace-pre-wrap ${
                    expandFull ? "" : "max-h-40 overflow-auto overscroll-contain"
                  }`}
                >
                  {inputStream}
                </pre>
              </div>
            ) : null}
            {input ? (
              <AnimatedActivityDetails label="Request details" forceSignal={expandSignal}>
                <pre
                  className={`text-foreground/60 py-2 text-[10px] leading-5 break-words whitespace-pre-wrap ${
                    expandFull ? "" : "max-h-48 overflow-auto overscroll-contain"
                  }`}
                >
                  {input}
                </pre>
              </AnimatedActivityDetails>
            ) : null}
            {step.type !== "permission" && isTerminalTool(toolName ?? "") ? (
              <TerminalBlock
                toolName={toolName}
                status={step.status}
                toolInput={step.toolInput}
                rawOutput={step.toolOutput}
                input={input}
                output={output}
                forceExpand={expandFull}
              />
            ) : null}
            {step.type !== "permission" && isDiffTool(toolName ?? "") && step.diffStats ? (
              <DiffBlock
                diffStats={step.diffStats}
                onOpenDiff={onOpenFileDiff}
                forceExpand={expandFull}
              />
            ) : null}
            {output &&
            (step.type === "permission" ||
              (!isTerminalTool(toolName ?? "") &&
                !(isDiffTool(toolName ?? "") && step.diffStats))) ? (
              <AnimatedActivityDetails
                label={step.status === "running" ? "Live tool output" : "Tool result"}
                preview={outputPreview}
                defaultOpen={step.status === "running" || step.status === "failed"}
                forceSignal={expandSignal}
              >
                <pre
                  className={`text-foreground/60 py-2 text-[10px] leading-5 break-words whitespace-pre-wrap ${
                    expandFull ? "" : "max-h-56 overflow-auto overscroll-contain"
                  }`}
                >
                  {output}
                </pre>
              </AnimatedActivityDetails>
            ) : null}
          </div>
        </div>
      </div>
    </li>
  );
});

function subagentDurationMs(subagent: SubagentActivityTimeline, now: number): number | null {
  const startedTs = parseTimestampMs(subagent.startedAt);
  if (!Number.isFinite(startedTs)) return null;
  if (subagent.status === "running") {
    return Math.max(0, now - startedTs);
  }
  const completedTs = subagent.completedAt ? parseTimestampMs(subagent.completedAt) : Number.NaN;
  if (Number.isFinite(completedTs)) {
    return Math.max(0, completedTs - startedTs);
  }
  return null;
}

function subagentMetricsLine(subagent: SubagentActivityTimeline): string {
  const parts: string[] = [];
  if (subagent.mode) parts.push(subagent.mode);
  const rounds = subagent.rounds || 0;
  if (rounds > 0) parts.push(`${rounds} ${rounds === 1 ? "round" : "rounds"}`);
  const thoughts = subagent.steps.filter((s) => s.type === "thinking").length;
  if (thoughts > 0) parts.push(`${thoughts} ${thoughts === 1 ? "thought" : "thoughts"}`);
  const hasToolSteps = subagent.steps.some((s) => s.type === "tool_call");
  const tools = hasToolSteps
    ? subagent.steps.filter((s) => s.type === "tool_call").length
    : subagent.toolCount;
  parts.push(`${tools} ${tools === 1 ? "tool" : "tools"}`);
  if (subagent.status !== "running") parts.push(subagent.status);
  return parts.join(" · ");
}

function subagentLatestHeadline(subagent: SubagentActivityTimeline): {
  label: string;
  isLive: boolean;
} {
  const runningStep = [...subagent.steps].reverse().find((s) => s.status === "running");
  const latestStep = runningStep ?? subagent.steps[subagent.steps.length - 1];
  if (latestStep) {
    const display = formatTimelineStepDisplay(latestStep);
    const label = display.target ? `${display.title} · ${display.target}` : display.title;
    return { label, isLive: latestStep.status === "running" };
  }
  if (subagent.status === "running") {
    return { label: "Starting autonomous worker…", isLive: true };
  }
  if (subagent.summary) {
    return { label: "Subagent outcome ready", isLive: false };
  }
  return {
    label: subagent.status === "failed" ? "Subagent stopped with an error" : "Completed",
    isLive: false,
  };
}

function SubagentRailIcon({ status }: { status: SubagentActivityTimeline["status"] }) {
  if (status === "running") {
    return <AverQelLoader size={13} className="animate-spin text-cyan-300" />;
  }
  if (status === "failed") return <XCircle size={13} className="text-red-300" />;
  if (status === "cancelled") return <CircleAlert size={13} className="text-amber-400" />;
  return <CheckCircle2 size={13} className="text-emerald-400" />;
}

const SubagentTimelineNode = memo(function SubagentTimelineNode({
  subagent,
  index,
  isLast,
  clock,
  expandSignal,
}: {
  subagent: SubagentActivityTimeline;
  index: number;
  isLast: boolean;
  clock: number;
  expandSignal?: TimelineExpandSignal;
}) {
  const [open, setOpen] = useState(
    () => subagent.status === "running" || subagent.status === "failed",
  );
  const previousStatus = useRef(subagent.status);
  const lastSignalSeq = useRef(0);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const detailsRef = useRef<HTMLDivElement>(null);
  const detailsId = `deepspace-subagent-tree-${subagent.id.replace(/[^a-zA-Z0-9_-]/g, "-")}-${index}`;

  useEffect(() => {
    if (!expandSignal || expandSignal.seq === 0 || expandSignal.seq === lastSignalSeq.current) {
      return;
    }
    lastSignalSeq.current = expandSignal.seq;
    setOpen(expandSignal.expanded);
  }, [expandSignal]);

  useEffect(() => {
    const wasRunning = previousStatus.current === "running";
    if (subagent.status === "running") {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setOpen(true);
    } else if (wasRunning && subagent.status === "completed") {
      if (detailsRef.current?.contains(document.activeElement)) {
        triggerRef.current?.focus();
      }
      setOpen(false);
    }
    previousStatus.current = subagent.status;
  }, [subagent.status]);

  const durationMs = subagentDurationMs(subagent, clock);
  const durationLabel = durationMs !== null ? formatElapsed(durationMs) : null;
  const metricsText = subagentMetricsLine(subagent);
  const latest = subagentLatestHeadline(subagent);

  return (
    <li
      className="motion-safe:animate-in motion-safe:fade-in motion-safe:slide-in-from-bottom-1 relative pl-7 motion-safe:duration-300 motion-safe:ease-out"
      data-testid={`subagent-node-${subagent.id}`}
    >
      {!isLast ? (
        <span className="deepspace-timeline-connector absolute top-0 bottom-[-0.75rem] left-[0.4rem] w-px bg-white/8" />
      ) : null}
      <div className="rounded-lg border border-white/8 bg-white/[0.03] px-3 py-2.5">
        <button
          ref={triggerRef}
          type="button"
          aria-expanded={open}
          aria-controls={detailsId}
          data-testid={`subagent-trigger-${subagent.id}`}
          onClick={() => setOpen((value) => !value)}
          className="text-foreground/75 hover:text-foreground/95 relative flex w-full cursor-pointer items-center gap-2 rounded-sm text-left text-[10px] font-semibold tracking-[0.12em] uppercase transition-colors duration-200 focus-visible:ring-2 focus-visible:ring-cyan-300/40 focus-visible:outline-none"
        >
          <span className="deepspace-timeline-icon pointer-events-none absolute top-1/2 -left-10 flex h-4 w-4 -translate-y-1/2 items-center justify-center rounded-full border border-white/10 bg-[#101713]">
            <SubagentRailIcon status={subagent.status} />
          </span>
          <ChevronDown
            size={12}
            className={`shrink-0 transition-transform duration-300 ease-out motion-reduce:transition-none ${open ? "rotate-0" : "-rotate-90"}`}
          />
          <span className="text-foreground/30 tabular-nums">
            {String(index + 1).padStart(2, "0")}
          </span>
          <span
            className={
              subagent.status === "running"
                ? "text-cyan-300/90 shrink-0"
                : subagent.status === "failed"
                  ? "text-red-300/90 shrink-0"
                  : subagent.status === "cancelled"
                    ? "text-amber-300/90 shrink-0"
                    : "text-emerald-300/90 shrink-0"
            }
          >
            Subagent
          </span>
          <span className="text-foreground/90 min-w-0 truncate font-mono normal-case tracking-normal">
            {subagent.label}
          </span>
          <span className="text-foreground/45 ml-auto shrink-0 font-mono text-[9px] tracking-normal normal-case">
            {durationLabel ? `${durationLabel} · ` : ""}
            {subagent.status}
          </span>
        </button>

        {!open ? (
          <div className="mt-1.5 space-y-1 pl-5">
            {subagent.mission ? (
              <div className="text-foreground/55 line-clamp-2 text-[11px] leading-relaxed">
                {subagent.mission}
              </div>
            ) : null}
            <div className="flex items-center gap-2 text-[10px]">
              <span className={latest.isLive ? "text-cyan-300/85 truncate" : "text-foreground/45 truncate"}>
                {latest.isLive ? "Live: ↳ " : "Last: ↳ "}
                {latest.label}
              </span>
              <span className="text-foreground/35 ml-auto shrink-0 font-mono text-[9px]">
                {metricsText}
              </span>
            </div>
          </div>
        ) : (
          <div className="text-foreground/40 mt-1 pl-5 font-mono text-[9px]">{metricsText}</div>
        )}

        <div
          id={detailsId}
          ref={detailsRef}
          aria-hidden={!open}
          className={`grid transition-[grid-template-rows,opacity] duration-300 ease-out motion-reduce:transition-none ${open ? "grid-rows-[1fr] opacity-100" : "grid-rows-[0fr] opacity-0"}`}
        >
          <div className="min-h-0 overflow-hidden">
            <div
              data-testid={`subagent-tab-panel-${subagent.id}`}
              className="mt-2.5 ml-2 space-y-3 border-l border-white/10 pl-4"
            >
              {subagent.mission ? (
                <div className="border-b border-white/8 pb-2">
                  <div className="text-foreground/45 mb-1 text-[9px] font-semibold tracking-[0.12em] uppercase">
                    Task
                  </div>
                  <div className="text-foreground/70 text-xs leading-relaxed">
                    {subagent.mission}
                  </div>
                </div>
              ) : null}
              {subagent.steps.length ? (
                <ol
                  className="space-y-3 pl-2"
                  aria-label={`Subagent ${subagent.label} timeline`}
                >
                  {subagent.steps.map((childStep, childIdx) => (
                    <TimelineEntry
                      key={`subagent-${subagent.id}-${childStep.id}-${childIdx}`}
                      step={childStep}
                      index={childIdx}
                      isLast={childIdx === subagent.steps.length - 1}
                      expandSignal={expandSignal}
                    />
                  ))}
                </ol>
              ) : subagent.status === "running" ? (
                <div className="text-foreground/45 text-[11px]">
                  Subagent is starting autonomous execution…
                </div>
              ) : (
                <div className="text-foreground/45 text-[11px]">
                  Subagent finished without issuing child tool calls.
                </div>
              )}
              {subagent.summary && subagent.status !== "running" ? (
                <div className="border-t border-white/8 pt-2.5">
                  <div className="text-foreground/45 mb-1 text-[9px] font-semibold tracking-[0.12em] uppercase">
                    Subagent outcome
                  </div>
                  <div
                    className="text-foreground/80 text-xs leading-relaxed"
                    data-testid={`subagent-outcome-${subagent.id}`}
                  >
                    <DeepSpaceMarkdownRenderer content={subagent.summary} compact />
                  </div>
                </div>
              ) : null}
            </div>
          </div>
        </div>
      </div>
    </li>
  );
});

type UnifiedTimelineNode =
  | { kind: "step"; step: TimelineStep }
  | { kind: "subagent"; subagent: SubagentActivityTimeline; sourceStep?: TimelineStep };

function extractAgentIdFromOutput(toolOutput?: string): string | null {
  if (!toolOutput) return null;
  try {
    const parsed = JSON.parse(toolOutput) as Record<string, unknown>;
    if (typeof parsed.agent_id === "string" && parsed.agent_id.trim()) {
      return parsed.agent_id.trim();
    }
    const member = parsed.member as Record<string, unknown> | undefined;
    if (member && typeof member.child_id === "string" && member.child_id.trim()) {
      return member.child_id.trim();
    }
  } catch {
    const match = toolOutput.match(/"agent_id"\s*:\s*"([^"]+)"/);
    if (match?.[1]) return match[1].trim();
  }
  return null;
}

function buildUnifiedTimelineNodes(
  orderedTimeline: TimelineStep[],
  subagents: SubagentActivityTimeline[],
): UnifiedTimelineNode[] {
  if (!subagents.length) {
    return orderedTimeline.map((step) => ({ kind: "step", step }));
  }

  const byId = new Map(subagents.map((s) => [s.id, s]));
  const byParentToolId = new Map<string, SubagentActivityTimeline>();
  for (const sub of subagents) {
    if (sub.parentToolId) byParentToolId.set(sub.parentToolId, sub);
  }
  const usedSubagentIds = new Set<string>();
  const nodes: UnifiedTimelineNode[] = [];

  for (const step of orderedTimeline) {
    const toolName = (step.toolName ?? "").toLowerCase();
    const isDelegation =
      step.type === "tool_call" &&
      (toolName === "subagent" ||
        toolName === "subagent_fork" ||
        toolName === "spawn_teammate" ||
        toolName === "task");
    if (isDelegation) {
      let matched = step.toolId ? byParentToolId.get(step.toolId) : undefined;
      if (!matched) {
        const agentId = extractAgentIdFromOutput(step.toolOutput);
        if (agentId) matched = byId.get(agentId);
      }
      if (!matched) {
        const stepLabel = String(
          step.toolInput?.description ?? step.toolInput?.name ?? "",
        ).trim();
        if (stepLabel) {
          matched = subagents.find(
            (s) => !usedSubagentIds.has(s.id) && s.label.trim() === stepLabel,
          );
        }
      }
      if (matched && !usedSubagentIds.has(matched.id)) {
        usedSubagentIds.add(matched.id);
        nodes.push({ kind: "subagent", subagent: matched, sourceStep: step });
        continue;
      }
    }
    nodes.push({ kind: "step", step });
  }

  const unmatched = subagents.filter((s) => !usedSubagentIds.has(s.id));
  if (unmatched.length) {
    const firstWaitIdx = nodes.findIndex(
      (n) => n.kind === "step" && n.step.toolName === "subagent_wait",
    );
    const orphanNodes: UnifiedTimelineNode[] = unmatched.map((subagent) => ({
      kind: "subagent",
      subagent,
    }));
    if (firstWaitIdx >= 0) {
      nodes.splice(firstWaitIdx, 0, ...orphanNodes);
    } else {
      nodes.push(...orphanNodes);
    }
  }
  return nodes;
}

export default function DeepSpaceThinkingPanel({
  content = "",
  isStreaming,
  isQueued = false,
  agentSteps = [],
  timeline = [],
  subagents = [],
  startedAt,
  onOpenFileDiff,
}: {
  content?: string;
  isStreaming: boolean;
  isQueued?: boolean;
  agentSteps?: AgentStep[];
  timeline?: TimelineStep[];
  subagents?: SubagentActivityTimeline[];
  startedAt?: string;
  onOpenFileDiff?: (path: string) => void;
}) {
  const hasRunningSubagent = subagents.some((s) => s.status === "running");
  const [panelOpen, setPanelOpen] = useState(
    () =>
      isStreaming ||
      Boolean(content.trim()) ||
      agentSteps.length > 0 ||
      timeline.length > 0 ||
      hasRunningSubagent,
  );
  const activityPanelId = `deepspace-activity-${useId().replace(/:/g, "")}`;
  const wasStreaming = useRef(isStreaming);
  const [clock, setClock] = useState(() => Date.now());
  const [expandSignal, setExpandSignal] = useState<TimelineExpandSignal>({
    expanded: false,
    seq: 0,
  });

  // Opening a newly active run should remain automatic, but completion must
  // not forcibly collapse the panel or override a user's expand/collapse
  // choice during reconciliation.
  useEffect(() => {
    if (isStreaming || hasRunningSubagent) {
      const timer = window.setTimeout(() => setPanelOpen(true), 0);
      wasStreaming.current = isStreaming;
      return () => window.clearTimeout(timer);
    }
    wasStreaming.current = false;
  }, [hasRunningSubagent, isStreaming]);

  useEffect(() => {
    if ((!isStreaming || isQueued) && !hasRunningSubagent) return;
    const timer = window.setInterval(() => setClock(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [hasRunningSubagent, isQueued, isStreaming]);

  // Providers may emit argument fragments before the function name. The
  // backend labels that transient fragment `pending_tool`; it is not a
  // second execution and must not appear as a failed duplicate row.
  const isHiddenProviderLifecycle = (step: { data?: Record<string, unknown> }) =>
    step.data?.phase === "resolving_provider" || step.data?.phase === "provider_ready";
  const activitySteps = agentSteps.filter(
    (step) =>
      step.type !== "thinking" &&
      step.toolName !== "pending_tool" &&
      !isHiddenProviderLifecycle(step),
  );
  const questionToolIds = new Set(
    timeline
      .filter(
        (step) =>
          step.type === "permission" &&
          step.toolName === "ask_user" &&
          typeof step.data?.question_id === "string",
      )
      .map((step) => step.toolId || step.stepId)
      .filter(Boolean),
  );
  // `ask_user` has one durable tool-result event and one question event. They
  // are the same interaction, not two requests for input. Keep the question
  // as the single visible timeline row; the durable tool record remains in
  // history for diagnostics.
  const orderedTimeline = timeline.filter(
    (step) =>
      step.toolName !== "pending_tool" &&
      !isHiddenProviderLifecycle(step) &&
      !(
        step.toolName === "ask_user" &&
        step.type !== "permission" &&
        questionToolIds.has(step.toolId || step.stepId)
      ),
  );
  const unifiedNodes = buildUnifiedTimelineNodes(orderedTimeline, subagents);
  const syntheticDurationSteps: TimelineStep[] = [
    ...orderedTimeline,
    ...subagents.map((sub) => ({
      id: `dur_${sub.id}`,
      stepId: `dur_${sub.id}`,
      turnIndex: 0,
      phase: "exploring" as const,
      type: "tool_call" as const,
      title: sub.label,
      status: sub.status === "running" ? ("running" as const) : ("completed" as const),
      startedAt: sub.startedAt,
      completedAt: sub.completedAt,
    })),
  ];
  const elapsedMs = timelineDurationMs(
    syntheticDurationSteps,
    clock,
    startedAt,
    isStreaming || hasRunningSubagent,
  );
  const hasNarrativeTimeline = orderedTimeline.some(
    (step) => step.type === "thinking" || step.type === "model_message",
  );
  const activityMetricsLine = (() => {
    let commands = 0;
    let edits = 0;
    let thoughts = 0;
    let reads = 0;
    let tools = 0;
    for (const node of unifiedNodes) {
      if (node.kind === "subagent") continue;
      const step = node.step;
      if (step.type === "thinking") {
        thoughts += 1;
        continue;
      }
      if (step.type === "model_message" || step.type === "permission") {
        continue;
      }
      const name = String(step.toolName ?? "").toLowerCase();
      if (!name || name === "subagent_wait") continue;
      if (
        name === "bash" ||
        name === "powershell" ||
        name.startsWith("terminal_") ||
        name.startsWith("ssh_")
      ) {
        commands += 1;
      } else if (
        name === "write" ||
        name === "edit" ||
        name === "write_file" ||
        name === "edit_file" ||
        name === "patch_file" ||
        name === "delete_file" ||
        name === "create_file" ||
        name === "apply_patch"
      ) {
        edits += 1;
      } else if (
        name === "read" ||
        name === "read_file" ||
        name === "list_files" ||
        name === "find_files" ||
        name === "grep_search" ||
        name === "search_files" ||
        name === "web_fetch" ||
        name === "url_read" ||
        name === "web_search" ||
        name.startsWith("inspect_")
      ) {
        reads += 1;
      } else {
        tools += 1;
      }
    }
    if (thoughts === 0 && content.trim()) {
      thoughts = 1;
    }
    const parts: string[] = [];
    if (commands > 0) parts.push(`${commands} ${commands === 1 ? "command" : "commands"}`);
    if (edits > 0) parts.push(`${edits} ${edits === 1 ? "edit" : "edits"}`);
    if (thoughts > 0) parts.push(`${thoughts} ${thoughts === 1 ? "thought" : "thoughts"}`);
    if (subagents.length > 0) {
      parts.push(`${subagents.length} ${subagents.length === 1 ? "subagent" : "subagents"}`);
    }
    if (reads > 0) parts.push(`${reads} ${reads === 1 ? "read" : "reads"}`);
    if (tools > 0) parts.push(`${tools} ${tools === 1 ? "tool" : "tools"}`);
    return parts.join(", ");
  })();
  const durationLabel = elapsedMs === null ? null : formatElapsed(elapsedMs);
  const isActivelyStreaming = isStreaming || hasRunningSubagent;

  if (
    !content.trim() &&
    activitySteps.length === 0 &&
    unifiedNodes.length === 0 &&
    !isStreaming
  ) {
    return null;
  }

  return (
    <div className="motion-safe:animate-in motion-safe:fade-in mb-4 motion-safe:duration-300 motion-safe:ease-out">
      <div className="flex w-full items-center gap-2 border-b border-white/8">
        <button
          type="button"
          aria-expanded={panelOpen}
          aria-controls={activityPanelId}
          onClick={() => setPanelOpen((open) => !open)}
          className="text-foreground/50 hover:text-foreground/80 flex flex-1 cursor-pointer items-center gap-2 py-2 text-left text-[11px] font-bold tracking-wider uppercase transition-colors duration-200 focus-visible:ring-2 focus-visible:ring-cyan-300/40 focus-visible:outline-none"
        >
          <BrainCircuit size={13} className="text-primary/60 shrink-0" />
          <span>
            {isQueued
              ? "Queued"
              : isActivelyStreaming
                ? "Thinking & activity…"
                : "Thinking & activity"}
          </span>
        </button>
        {unifiedNodes.length > 0 ? (
          <button
            type="button"
            aria-label={
              expandSignal.expanded ? "Collapse all timeline steps" : "Expand all timeline steps"
            }
            onClick={() => {
              const nextExpanded = !expandSignal.expanded;
              if (nextExpanded) setPanelOpen(true);
              setExpandSignal((prev) => ({ expanded: nextExpanded, seq: prev.seq + 1 }));
            }}
            className="text-foreground/45 hover:text-foreground/85 shrink-0 cursor-pointer rounded-md border border-white/10 bg-white/[0.03] px-2 py-0.5 text-[10px] font-medium transition hover:border-white/20 hover:bg-white/[0.07]"
          >
            {expandSignal.expanded ? "Collapse all" : "Expand all"}
          </button>
        ) : null}
        <button
          type="button"
          aria-expanded={panelOpen}
          aria-controls={activityPanelId}
          aria-label={panelOpen ? "Collapse activity timeline" : "Expand activity timeline"}
          onClick={() => setPanelOpen((open) => !open)}
          className="text-foreground/40 hover:text-foreground/75 ml-auto flex shrink-0 cursor-pointer items-center gap-2 py-2 text-[11px] font-bold tracking-normal normal-case transition-colors duration-200 focus-visible:ring-2 focus-visible:ring-cyan-300/40 focus-visible:outline-none"
        >
          {isQueued ? (
            <span className="text-foreground/35">Waiting for your turn to start</span>
          ) : durationLabel ? (
            <span className="text-foreground/35">
              {isActivelyStreaming ? `Working for ${durationLabel}` : `Worked for ${durationLabel}`}
            </span>
          ) : null}
          <ChevronDown
            size={12}
            className={`transition-transform duration-200 ${panelOpen ? "rotate-180" : ""}`}
          />
        </button>
      </div>
      {activityMetricsLine ? (
        <div
          data-testid="thinking-activity-metrics"
          className="text-foreground/45 pt-1.5 pb-1 font-mono text-[11px]"
        >
          {activityMetricsLine}
        </div>
      ) : null}
      <div
        id={activityPanelId}
        aria-hidden={!panelOpen}
        className={`grid transition-[grid-template-rows,opacity] duration-300 ease-out motion-reduce:transition-none ${panelOpen ? "grid-rows-[1fr] opacity-100" : "grid-rows-[0fr] opacity-0"}`}
      >
        <div className="min-h-0 overflow-hidden">
          <div
            className="text-foreground/60 space-y-3 overscroll-contain py-3 text-xs"
            data-thinking-activity="true"
          >
            {/*
              `content` is a legacy, message-wide thinking fallback. Once a
              durable timeline exists, rendering it above the timeline makes
              reloads look like all thought was merged into one block. The
              ordered timeline is authoritative whenever present; retain the
              fallback only for older messages that have no timeline at all.
            */}
            {content.trim() && !hasNarrativeTimeline ? (
              <div className="border-b border-white/8 pb-3" data-testid="deepspace-thinking-stream">
                <div className="text-foreground/45 mb-2 text-[10px] font-semibold tracking-[0.12em] uppercase">
                  Model thinking
                </div>
                <DeepSpaceMarkdownRenderer content={content} streaming={isStreaming} compact />
              </div>
            ) : null}
            {unifiedNodes.length ? (
              <ol className="space-y-3" aria-label="Live agent timeline">
                {unifiedNodes.map((node, index) =>
                  node.kind === "subagent" ? (
                    <SubagentTimelineNode
                      key={`subagent-node-${node.subagent.id}-${index}`}
                      subagent={node.subagent}
                      index={index}
                      isLast={index === unifiedNodes.length - 1}
                      clock={clock}
                      expandSignal={expandSignal}
                    />
                  ) : (
                    <TimelineEntry
                      key={`timeline-${node.step.id}-${index}`}
                      step={node.step}
                      index={index}
                      isLast={index === unifiedNodes.length - 1}
                      onOpenFileDiff={onOpenFileDiff}
                      expandSignal={expandSignal}
                    />
                  ),
                )}
              </ol>
            ) : null}
            {!unifiedNodes.length && activitySteps.length ? (
              <div className="space-y-2" aria-label="Tool and agent activity">
                {activitySteps.map((step, index) => (
                  <ActivityStep
                    key={`activity-${step.id}-${index}`}
                    step={step}
                    onOpenFileDiff={onOpenFileDiff}
                  />
                ))}
              </div>
            ) : null}
            {isQueued &&
            !content.trim() &&
            activitySteps.length === 0 &&
            unifiedNodes.length === 0 ? (
              <div className="text-foreground/45">Your message is saved and waiting to start.</div>
            ) : null}
            {isStreaming &&
            !isQueued &&
            !content.trim() &&
            activitySteps.length === 0 &&
            unifiedNodes.length === 0 ? (
              <div className="text-foreground/45">Waiting for the model and tools…</div>
            ) : null}
            {(() => {
              const changedFilesMap = new Map<
                string,
                { path: string; additions: number; deletions: number }
              >();
              for (const s of orderedTimeline) {
                if (s.status === "failed" || !s.diffStats?.path) continue;
                if (
                  s.diffStats.additions === 0 &&
                  s.diffStats.deletions === 0 &&
                  (!s.diffStats.diffLines || s.diffStats.diffLines.length === 0)
                ) {
                  continue;
                }
                const existing = changedFilesMap.get(s.diffStats.path) ?? {
                  path: s.diffStats.path,
                  additions: 0,
                  deletions: 0,
                };
                existing.additions += s.diffStats.additions;
                existing.deletions += s.diffStats.deletions;
                changedFilesMap.set(s.diffStats.path, existing);
              }
              const changedFilePills = [...changedFilesMap.values()];
              if (!changedFilePills.length) return null;
              return (
                <div
                  className="flex flex-wrap items-center gap-1.5 pt-1"
                  data-testid="deepspace-timeline-changed-files"
                >
                  {changedFilePills.map((file) => (
                    <button
                      key={file.path}
                      type="button"
                      onClick={() => onOpenFileDiff?.(file.path)}
                      disabled={!onOpenFileDiff}
                      title={file.path}
                      className="inline-flex cursor-pointer items-center gap-1.5 rounded-lg border border-white/10 bg-[#141514] px-2.5 py-1 font-mono text-[11px] transition hover:border-cyan-400/30 hover:bg-white/[0.06] disabled:cursor-default"
                    >
                      <span className="text-foreground/90">{fileBasename(file.path)}</span>
                      <span className="text-emerald-400 tabular-nums">+{file.additions}</span>
                      <span className="text-rose-400 tabular-nums">-{file.deletions}</span>
                    </button>
                  ))}
                </div>
              );
            })()}
          </div>
        </div>
      </div>
    </div>
  );
}
