"use client";

import {
  AlertTriangle,
  CheckCircle2,
  Clock3,
  FolderTree,
  ListTodo,
  Terminal,
} from "lucide-react";

import DeepSpaceInlineMarkdown from "./DeepSpaceInlineMarkdown";

type TodoItem = {
  content?: string;
  activeForm?: string;
  active_form?: string;
  status?: string;
  priority?: number;
  metadata_json?: Record<string, unknown>;
  metadata?: Record<string, unknown>;
};

type AgentSummary = {
  agent_id?: string;
  label?: string;
  mode?: string;
  origin?: string;
  status?: string;
  depth?: number;
  turn_count?: number;
  answer?: string;
};

function normalizeTodoStatus(status: unknown): string {
  const value = String(status ?? "").toLowerCase();
  if (value === "completed") return "completed";
  if (value === "in_progress") return "in progress";
  if (value === "pending") return "pending";
  return value || "pending";
}

function statusChipClass(status: string): string {
  if (status === "completed") return "border-emerald-500/30 bg-emerald-500/10 text-emerald-300";
  if (status === "in progress") return "border-cyan-500/30 bg-cyan-500/10 text-cyan-300";
  if (status === "pending") return "border-amber-500/30 bg-amber-500/10 text-amber-300";
  return "border-white/10 bg-white/5 text-foreground/50";
}

function panel(title: string, icon: React.ReactNode, body: React.ReactNode) {
  return (
    <div className="space-y-2 rounded-xl border border-white/8 bg-black/20 p-3 shadow-[inset_0_1px_0_rgba(255,255,255,0.03)]">
      <div className="text-foreground/45 flex items-center gap-2 text-[10px] tracking-[0.28em] uppercase">
        {icon}
        <span>{title}</span>
      </div>
      {body}
    </div>
  );
}

function Mono({ children }: { children: React.ReactNode }) {
  return (
    <span className="font-mono text-[11px] break-all text-foreground/70">{children}</span>
  );
}

function parsePayload(toolOutput: string): Record<string, unknown> | null {
  try {
    const parsed = JSON.parse(toolOutput);
    return parsed && typeof parsed === "object" ? (parsed as Record<string, unknown>) : null;
  } catch {
    return null;
  }
}

function renderFileToolInput(toolName: string, toolInput: Record<string, unknown>) {
  const path = String(toolInput.path ?? "").trim();
  if (!path) return null;
  const detail =
    toolName === "fs_grep"
      ? String(toolInput.pattern ?? "")
      : toolName === "fs_glob"
        ? String(toolInput.pattern ?? "")
        : toolName === "fs_str_replace"
          ? String(toolInput.old_string ?? "")
          : "";
  return panel(
    "Workspace file",
    <FolderTree size={12} className="text-primary/70" />,
    <div className="space-y-1">
      <Mono>{path}</Mono>
      {detail ? (
        <div className="text-foreground/40 text-[11px] leading-relaxed">
          <span className="text-foreground/25">Match: </span>
          <Mono>{detail}</Mono>
        </div>
      ) : null}
    </div>,
  );
}

function renderShellToolInput(toolName: string, toolInput: Record<string, unknown>) {
  const command = String(toolInput.command ?? "").trim();
  if (!command) return null;
  return panel(
    toolName === "pwsh" ? "PowerShell" : "Shell",
    <Terminal size={12} className="text-primary/70" />,
    <pre className="custom-scrollbar max-h-[180px] overflow-auto rounded-md border border-white/6 bg-black/40 p-2 font-mono text-[11px] leading-relaxed whitespace-pre-wrap text-foreground/75">
      {command}
    </pre>,
  );
}

function renderAgentToolInput(toolName: string, toolInput: Record<string, unknown>) {
  if (toolName !== "subagent" && toolName !== "subagent_fork") return null;
  const description = String(toolInput.description ?? "").trim();
  const task = String(toolInput.task ?? "").trim();
  return panel(
    "Delegated task",
    <FolderTree size={12} className="text-primary/70" />,
    <div className="space-y-1.5">
      {description ? (
        <div className="text-foreground/70 text-[12px] leading-relaxed">{description}</div>
      ) : null}
      {task ? (
        <pre className="custom-scrollbar max-h-[180px] overflow-auto rounded-md border border-white/6 bg-black/40 p-2 font-mono text-[11px] leading-relaxed whitespace-pre-wrap text-foreground/60">
          {task}
        </pre>
      ) : null}
    </div>,
  );
}

function renderAgentListOutput(payload: Record<string, unknown>) {
  const agents = Array.isArray(payload.agents) ? (payload.agents as AgentSummary[]) : [];
  if (agents.length === 0) {
    return panel(
      "Sub-agents",
      <FolderTree size={12} className="text-primary/70" />,
      <div className="text-foreground/45 text-[11px]">No child agents yet.</div>,
    );
  }
  return panel(
    `Sub-agents (${agents.length})`,
    <FolderTree size={12} className="text-primary/70" />,
    <div className="space-y-1.5">
      {agents.map((agent, index) => (
        <div
          key={`${agent.agent_id ?? index}`}
          className="rounded-lg border border-white/6 bg-white/[0.02] px-3 py-2"
        >
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0 flex-1">
              <div className="text-foreground/80 text-[12px] leading-relaxed">
                {agent.label || "Unnamed agent"}
              </div>
              <div className="text-foreground/35 mt-0.5 text-[10px] tracking-wide uppercase">
                {agent.mode ?? "one-shot"} · depth {agent.depth ?? 1} · turns {agent.turn_count ?? 0}
              </div>
            </div>
            <span
              className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[9px] font-bold tracking-[0.18em] uppercase ${statusChipClass(
                agent.status === "idle" || agent.status === "completed"
                  ? "completed"
                  : String(agent.status ?? "pending"),
              )}`}
            >
              {agent.status === "idle" || agent.status === "completed" ? (
                <CheckCircle2 size={10} />
              ) : (
                <Clock3 size={10} />
              )}
              {agent.status ?? "pending"}
            </span>
          </div>
        </div>
      ))}
    </div>,
  );
}

export function renderStructuredToolInput(toolName: string, toolInput: Record<string, unknown>) {
  if (toolName === "todo_write") {
    const todos = Array.isArray(toolInput.todos) ? (toolInput.todos as TodoItem[]) : [];
    const title = toolInput.title ? String(toolInput.title) : "Task Ledger";

    return (
      <div className="space-y-2 rounded-xl border border-white/8 bg-black/20 p-3 shadow-[inset_0_1px_0_rgba(255,255,255,0.03)]">
        <div className="text-foreground/45 flex items-center gap-2 text-[10px] tracking-[0.28em] uppercase">
          <ListTodo size={12} className="text-primary/70" />
          <span>{title}</span>
        </div>
        <div className="space-y-2">
          {todos.map((todo, idx) => {
            const content = String(todo.content ?? "").trim() || "Untitled task";
            const activeForm = String(todo.activeForm ?? todo.active_form ?? "").trim();
            const status = normalizeTodoStatus(todo.status);
            return (
              <div
                key={`${content}-${idx}`}
                className="rounded-lg border border-white/6 bg-white/[0.02] px-3 py-2.5"
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0 flex-1">
                    <div className="prose prose-invert text-foreground/80 max-w-none text-[12px] leading-relaxed">
                      <DeepSpaceInlineMarkdown content={content} />
                    </div>
                    {activeForm && activeForm !== content ? (
                      <div className="text-foreground/45 mt-1 text-[11px] leading-relaxed">
                        <span className="text-foreground/25">Active form: </span>
                        <DeepSpaceInlineMarkdown content={activeForm} />
                      </div>
                    ) : null}
                  </div>
                  <span
                    className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[9px] font-bold tracking-[0.18em] uppercase ${statusChipClass(status)}`}
                  >
                    {status === "completed" ? <CheckCircle2 size={10} /> : <Clock3 size={10} />}
                    {status}
                  </span>
                </div>
              </div>
            );
          })}
        </div>
        {todos.length === 0 ? (
          <div className="rounded-lg border border-amber-500/20 bg-amber-500/10 px-3 py-2 text-[11px] text-amber-200/80">
            <AlertTriangle size={12} className="mr-1 inline-block translate-y-[-1px]" />
            No todo items were provided yet.
          </div>
        ) : null}
      </div>
    );
  }

  if (toolName.startsWith("fs_")) {
    return renderFileToolInput(toolName, toolInput);
  }

  if (toolName === "bash" || toolName === "pwsh") {
    return renderShellToolInput(toolName, toolInput);
  }

  if (toolName === "subagent" || toolName === "subagent_fork") {
    return renderAgentToolInput(toolName, toolInput);
  }

  return null;
}

export function renderStructuredToolOutput(toolName: string, toolOutput: string) {
  const trimmed = toolOutput.trim();
  if (!trimmed) {
    return null;
  }

  if (toolName === "todo_write") {
    return (
      <div className="space-y-2 rounded-xl border border-white/8 bg-black/20 p-3">
        <div className="text-foreground/45 flex items-center gap-2 text-[10px] tracking-[0.28em] uppercase">
          <ListTodo size={12} className="text-primary/70" />
          <span>Ledger update</span>
        </div>
        <div className="prose prose-invert text-foreground/70 max-w-none text-[11px] leading-relaxed">
          <DeepSpaceInlineMarkdown content={trimmed} />
        </div>
      </div>
    );
  }

  if (toolName === "list_agents") {
    const payload = parsePayload(trimmed);
    if (payload) {
      return renderAgentListOutput(payload);
    }
  }

  if (toolName === "bash" || toolName === "pwsh") {
    const payload = parsePayload(trimmed);
    const output = payload ? String(payload.output ?? "") : trimmed;
    const exitCode = payload ? payload.exit_code : undefined;
    const status = payload ? String(payload.status ?? "") : "";
    const failed = status === "failed" || (typeof exitCode === "number" && exitCode !== 0);
    return panel(
      failed ? "Command failed" : "Command output",
      <Terminal size={12} className="text-primary/70" />,
      <pre
        className={`custom-scrollbar max-h-[250px] overflow-auto rounded-md border p-2 font-mono text-[11px] leading-relaxed whitespace-pre-wrap ${
          failed
            ? "border-amber-500/20 bg-amber-500/5 text-amber-200/80"
            : "border-white/6 bg-black/40 text-foreground/70"
        }`}
      >
        {output || "(no output)"}
      </pre>,
    );
  }

  return (
    <div className="prose prose-invert text-foreground/45 custom-scrollbar max-h-[250px] max-w-none overflow-x-auto overflow-y-auto rounded border border-white/5 bg-black/25 p-2 text-[11px] leading-relaxed">
      <DeepSpaceInlineMarkdown content={trimmed} />
    </div>
  );
}
