"use client";

import { AverQelLoader } from "@/app/components/ui/AverQelLoading";

import {
  AlertTriangle,
  Bot,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  CircleDot,
  Clock3,
  ExternalLink,
  FileCode,
  GitBranch,
  ListTodo,
  Maximize2,
  MessageSquare,
  Power,
  RefreshCw,
  ShieldCheck,
  StopCircle,
  Users,
  X,
} from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { fetchWithAuth } from "@/lib/api";

export type TeamMember = {
  id: string;
  name: string;
  provider: string | null;
  model: string | null;
  tool_filter: string[];
  state: "idle" | "running" | "finished" | string;
  child_id?: string | null;
  result: string | null;
  created_at: string | null;
};

export type TeamTask = {
  id: string;
  title: string;
  body: string;
  status: "pending" | "in_progress" | "blocked" | "completed" | string;
  owner: string | null;
  revision: number;
  depends_on: string[];
  write_scopes: string[];
  ready: boolean;
  blocked_by: string[];
  write_scope_warnings: string[];
  updated_at: string | null;
};

export type TeamMessage = {
  id: string;
  from: string;
  to: string;
  body: string;
  state: "queued" | "delivered" | string;
  created_at: string | null;
};

export type TeamChild = {
  agent_id: string;
  label: string;
  origin: string;
  mode: string;
  status: string;
  depth: number;
  turn_count: number;
  pending_messages: number;
  agent_provider: string | null;
  agent_model: string | null;
};

export type TeamTool = {
  name: string;
  allowed: boolean;
};

export type AgentTeamData = {
  enabled: boolean;
  preset: string;
  team_tools_available: boolean;
  team_tools: TeamTool[];
  max_members: number;
  lead: { name: string; label: string; kind: string };
  members: TeamMember[];
  messages: TeamMessage[];
  tasks: TeamTask[];
  children: TeamChild[];
};

export interface DeepSpaceAgentTeamPanelProps {
  /** The conversation whose durable team is displayed. */
  conversationId: string | null;
  /** Optional callback when user clicks a teammate to view their session transcript. */
  onSelectSession?: (sessionId: string) => void;
  /** Optional callback when user selects a teammate member record. */
  onSelectMember?: (member: TeamMember) => void;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function stringValue(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function nullableString(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function stringList(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string")
    : [];
}

export function parseTeam(value: unknown): AgentTeamData {
  if (!isRecord(value)) throw new Error("The team response was invalid.");
  const lead = isRecord(value.lead) ? value.lead : {};
  const members = Array.isArray(value.members)
    ? value.members.flatMap((item): TeamMember[] => {
        if (!isRecord(item) || typeof item.id !== "string" || typeof item.name !== "string") {
          return [];
        }
        return [
          {
            id: item.id,
            name: item.name,
            provider: nullableString(item.provider),
            model: nullableString(item.model),
            tool_filter: stringList(item.tool_filter),
            state: stringValue(item.state, "idle"),
            child_id: nullableString(item.child_id),
            result: nullableString(item.result),
            created_at: nullableString(item.created_at),
          },
        ];
      })
    : [];
  const tasks = Array.isArray(value.tasks)
    ? value.tasks.flatMap((item): TeamTask[] => {
        if (!isRecord(item) || typeof item.id !== "string" || typeof item.title !== "string") {
          return [];
        }
        return [
          {
            id: item.id,
            title: item.title,
            body: stringValue(item.body),
            status: stringValue(item.status, "pending"),
            owner: nullableString(item.owner),
            revision: typeof item.revision === "number" ? item.revision : 0,
            depends_on: stringList(item.depends_on),
            write_scopes: stringList(item.write_scopes),
            ready: item.ready !== false,
            blocked_by: stringList(item.blocked_by),
            write_scope_warnings: stringList(item.write_scope_warnings),
            updated_at: nullableString(item.updated_at),
          },
        ];
      })
    : [];
  const messages = Array.isArray(value.messages)
    ? value.messages.flatMap((item): TeamMessage[] => {
        if (!isRecord(item) || typeof item.id !== "string" || typeof item.body !== "string") {
          return [];
        }
        return [
          {
            id: item.id,
            from: stringValue(item.from, "lead"),
            to: stringValue(item.to, "team"),
            body: item.body,
            state: stringValue(item.state, "queued"),
            created_at: nullableString(item.created_at),
          },
        ];
      })
    : [];
  const children = Array.isArray(value.children)
    ? value.children.flatMap((item): TeamChild[] => {
        if (!isRecord(item) || typeof item.agent_id !== "string") return [];
        return [
          {
            agent_id: item.agent_id,
            label: stringValue(item.label, "Unnamed child"),
            origin: stringValue(item.origin, "subagent"),
            mode: stringValue(item.mode, "continuable"),
            status: stringValue(item.status, "idle"),
            depth: typeof item.depth === "number" ? item.depth : 0,
            turn_count: typeof item.turn_count === "number" ? item.turn_count : 0,
            pending_messages: typeof item.pending_messages === "number" ? item.pending_messages : 0,
            agent_provider: nullableString(item.agent_provider),
            agent_model: nullableString(item.agent_model),
          },
        ];
      })
    : [];
  return {
    enabled: value.enabled === true,
    preset: stringValue(value.preset, "standard"),
    team_tools_available: value.team_tools_available === true,
    team_tools: Array.isArray(value.team_tools)
      ? value.team_tools.flatMap((item): TeamTool[] => {
          if (!isRecord(item) || typeof item.name !== "string") return [];
          return [{ name: item.name, allowed: item.allowed === true }];
        })
      : [],
    max_members: typeof value.max_members === "number" ? value.max_members : 8,
    lead: {
      name: stringValue(lead.name, "lead"),
      label: stringValue(lead.label, "DeepSpace lead"),
      kind: stringValue(lead.kind, "lead"),
    },
    members,
    messages,
    tasks,
    children,
  };
}

function statusLabel(status: string): string {
  return status.replaceAll("_", " ");
}

function statusClass(status: string): string {
  if (status === "running" || status === "in_progress") {
    return "border-cyan-500/40 bg-cyan-500/15 text-cyan-700 dark:text-cyan-300";
  }
  if (
    status === "completed" ||
    status === "finished" ||
    status === "delivered" ||
    status === "enabled"
  ) {
    return "border-emerald-500/40 bg-emerald-500/15 text-emerald-700 dark:text-emerald-300";
  }
  if (status === "blocked" || status === "failed") {
    return "border-rose-500/40 bg-rose-500/15 text-rose-700 dark:text-rose-300";
  }
  return "border-border-subtle bg-surface-2/80 text-muted-foreground";
}

function formatDate(value: string | null): string {
  if (!value) return "No activity yet";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? "No activity yet" : parsed.toLocaleString();
}

function resolveMessageMeta(message: TeamMessage) {
  let sender = message.from;
  let isTaskAssignment = false;
  const body = message.body || "";

  if (
    body.includes("You are teammate") ||
    body.includes("YOUR TASK") ||
    body.includes("REQUIRED SEQUENCE")
  ) {
    sender = "Lead (Orchestrator)";
    isTaskAssignment = true;
  } else if (sender === "agent" || sender === "lead") {
    if (
      body.includes("Architecture doc analysis") ||
      body.includes("analyst here") ||
      body.startsWith("Analyst here")
    ) {
      sender = "analyst";
    } else if (body.includes("reviewer here") || body.startsWith("Reviewer here")) {
      sender = "reviewer";
    } else {
      sender = "Lead (Orchestrator)";
    }
  }

  const isPeerHandOff = !isTaskAssignment && sender !== "Lead (Orchestrator)";

  return {
    sender,
    isTaskAssignment,
    badgeLabel: isTaskAssignment
      ? "Task Assignment"
      : isPeerHandOff
        ? "Peer Hand-off"
        : "Lead Message",
    badgeClass: isTaskAssignment
      ? "bg-amber-500/15 text-amber-700 dark:text-amber-300 border-amber-500/30"
      : isPeerHandOff
        ? "bg-sky-500/15 text-sky-700 dark:text-sky-300 border-sky-500/30"
        : "bg-primary/15 text-primary border-primary/30",
  };
}

function StatusPill({ status }: { status: string }) {
  const isRunning = status === "running" || status === "in_progress";
  const isComplete =
    status === "completed" ||
    status === "finished" ||
    status === "delivered" ||
    status === "enabled";
  const isProblem = status === "blocked" || status === "failed";
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-[10px] font-semibold tracking-[0.08em] uppercase shadow-2xs ${statusClass(status)}`}
    >
      <span className="relative flex h-1.5 w-1.5">
        {isRunning && (
          <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-cyan-400 opacity-75" />
        )}
        <span
          className={`relative inline-flex h-1.5 w-1.5 rounded-full ${
            isRunning
              ? "bg-cyan-500"
              : isComplete
                ? "bg-emerald-500"
                : isProblem
                  ? "bg-rose-500"
                  : "bg-muted-foreground"
          }`}
        />
      </span>
      {statusLabel(status)}
    </span>
  );
}

export default function DeepSpaceAgentTeamPanel({
  conversationId,
  onSelectSession,
  onSelectMember,
}: DeepSpaceAgentTeamPanelProps) {
  const [team, setTeam] = useState<AgentTeamData | null>(null);
  const [loading, setLoading] = useState(Boolean(conversationId));
  const [refreshing, setRefreshing] = useState(false);
  const [togglingEnabled, setTogglingEnabled] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Expanded task accordions (set of task IDs)
  const [expandedTasks, setExpandedTasks] = useState<Set<string>>(new Set());
  // Active task detail modal
  const [activeDetailTask, setActiveDetailTask] = useState<TeamTask | null>(null);
  // Selected teammate live inspection modal
  const [inspectedMember, setInspectedMember] = useState<TeamMember | null>(null);
  const [interruptingMember, setInterruptingMember] = useState(false);

  const loadTeam = useCallback(
    async (manual = false) => {
      if (!conversationId) {
        setTeam(null);
        setLoading(false);
        return;
      }
      if (!manual) setLoading(true);
      if (manual) setRefreshing(true);
      try {
        const response = (await fetchWithAuth(
          `/deepspace/chats/${encodeURIComponent(conversationId)}/agent-team`,
        )) as Response;
        if (!response.ok) throw new Error(`Unable to load the Agent Team (${response.status}).`);
        setTeam(parseTeam(await response.json()));
        setError(null);
      } catch (cause) {
        setError(cause instanceof Error ? cause.message : "Unable to load the Agent Team.");
      } finally {
        setLoading(false);
        setRefreshing(false);
      }
    },
    [conversationId],
  );

  const handleToggleEnabled = useCallback(async () => {
    if (!conversationId || !team || togglingEnabled) return;
    const nextState = !team.enabled;
    setTogglingEnabled(true);
    // Optimistic update
    setTeam((prev) => (prev ? { ...prev, enabled: nextState } : prev));
    try {
      const response = (await fetchWithAuth(
        `/deepspace/chats/${encodeURIComponent(conversationId)}/agent-team`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ enabled: nextState }),
        },
      )) as Response;
      if (!response.ok) {
        throw new Error(`Failed to toggle team state (${response.status}).`);
      }
      const data = await response.json();
      setTeam(parseTeam(data));
    } catch (cause) {
      // Revert optimistic update
      setTeam((prev) => (prev ? { ...prev, enabled: !nextState } : prev));
      setError(cause instanceof Error ? cause.message : "Failed to toggle team state.");
    } finally {
      setTogglingEnabled(false);
    }
  }, [conversationId, team, togglingEnabled]);

  const handleInterruptMember = useCallback(
    async (member: TeamMember) => {
      if (!conversationId || interruptingMember) return;
      setInterruptingMember(true);
      try {
        const response = (await fetchWithAuth(
          `/deepspace/chats/${encodeURIComponent(conversationId)}/agent-team/interrupt`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ target: member.name }),
          },
        )) as Response;
        if (!response.ok) {
          throw new Error(`Failed to interrupt teammate (${response.status}).`);
        }
        await loadTeam(true);
        setInspectedMember((prev) => (prev ? { ...prev, state: "finished" } : null));
      } catch (cause) {
        setError(cause instanceof Error ? cause.message : "Failed to interrupt teammate.");
      } finally {
        setInterruptingMember(false);
      }
    },
    [conversationId, interruptingMember, loadTeam],
  );

  const toggleTaskAccordion = (taskId: string) => {
    setExpandedTasks((prev) => {
      const next = new Set(prev);
      if (next.has(taskId)) {
        next.delete(taskId);
      } else {
        next.add(taskId);
      }
      return next;
    });
  };

  const handleTeammateClick = (member: TeamMember) => {
    if (onSelectMember) {
      onSelectMember(member);
    }
    if (onSelectSession && member.child_id) {
      onSelectSession(member.child_id);
    }
    setInspectedMember(member);
  };

  useEffect(() => {
    const handle = window.setTimeout(() => void loadTeam(), 0);
    return () => window.clearTimeout(handle);
  }, [conversationId, loadTeam]);

  useEffect(() => {
    if (!conversationId) return;
    const interval = window.setInterval(() => void loadTeam(), 3_000);
    return () => window.clearInterval(interval);
  }, [conversationId, loadTeam]);

  if (!conversationId) {
    return (
      <div className="text-muted-foreground flex h-full items-center justify-center p-8 text-center text-sm">
        Open a conversation to see its Agent Team.
      </div>
    );
  }

  if (loading && !team) {
    return (
      <div className="text-muted-foreground flex h-full items-center justify-center gap-2 text-sm">
        <AverQelLoader size={16} className="animate-spin" /> Loading team state…
      </div>
    );
  }

  if (error && !team) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-3 p-8 text-center">
        <AlertTriangle size={20} className="text-amber-500" />
        <p className="text-muted-foreground text-sm">{error}</p>
        <button
          type="button"
          onClick={() => void loadTeam(true)}
          className="border-border-subtle hover:bg-surface-1 inline-flex items-center gap-2 rounded-lg border px-3 py-2 text-xs font-semibold"
        >
          <RefreshCw size={13} /> Try again
        </button>
      </div>
    );
  }

  if (!team) return null;

  const missingTeamTools = team.team_tools.filter((tool) => !tool.allowed).map((tool) => tool.name);

  return (
    <div
      data-testid="deepspace-agent-team-panel"
      className="bg-surface-0 min-h-0 flex-1 space-y-4 overflow-y-auto p-3 sm:p-5"
    >
      {/* Streamlined Action and Status Bar */}
      <div className="border-border-subtle/80 bg-surface-1/60 flex flex-wrap items-center justify-between gap-3 rounded-2xl border p-3.5 shadow-xs backdrop-blur-xs">
        <div className="flex flex-wrap items-center gap-2.5">
          <span className="text-foreground text-xs font-semibold">Status:</span>
          <StatusPill status={team.enabled ? "enabled" : "disabled"} />
          <div className="bg-surface-2/70 border-border-subtle/50 text-muted-foreground flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-[11px] font-medium">
            <Users size={12} className="text-primary shrink-0" />
            <span>
              • {team.members.length} / {team.max_members} active teammates
            </span>
          </div>
          <span className="bg-surface-2/70 border-border-subtle/50 text-muted-foreground rounded-full border px-2.5 py-0.5 text-[11px]">
            Preset: <strong className="text-foreground font-semibold">{team.preset}</strong>
          </span>
        </div>

        <div className="flex items-center gap-2">
          {/* Fully Enabled/Disabled Toggle Button */}
          <button
            type="button"
            data-testid="agent-team-toggle-button"
            role="switch"
            aria-checked={team.enabled}
            aria-label={team.enabled ? "Disable Agent Teams" : "Enable Agent Teams"}
            disabled={togglingEnabled}
            onClick={() => void handleToggleEnabled()}
            className={`inline-flex items-center gap-2 rounded-xl border px-3.5 py-1.5 text-xs font-semibold shadow-xs transition-all hover:shadow-sm ${
              team.enabled
                ? "border-emerald-500/50 bg-emerald-500/15 text-emerald-700 hover:bg-emerald-500/25 dark:text-emerald-300"
                : "border-border-subtle bg-surface-1 text-muted-foreground hover:bg-surface-2 hover:text-foreground"
            } disabled:opacity-60`}
          >
            {togglingEnabled ? (
              <AverQelLoader size={13} className="animate-spin" />
            ) : (
              <Power size={13} className={team.enabled ? "text-emerald-500" : ""} />
            )}
            <span>{team.enabled ? "Disable Team" : "Enable Team"}</span>
          </button>

          <button
            type="button"
            onClick={() => void loadTeam(true)}
            disabled={refreshing}
            aria-label="Refresh Agent Team"
            className="text-muted-foreground hover:bg-surface-2 hover:text-foreground border-border-subtle bg-surface-1/60 inline-flex items-center gap-1.5 rounded-xl border px-3 py-1.5 text-xs font-semibold shadow-2xs transition-all hover:shadow-xs disabled:opacity-60"
          >
            {refreshing ? <AverQelLoader size={13} /> : <RefreshCw size={13} />}
            <span>Refresh</span>
          </button>
        </div>
      </div>

      {!team.enabled ? (
        <div className="to-surface-0 flex items-start gap-3 rounded-2xl border border-amber-500/40 bg-gradient-to-r from-amber-500/10 via-amber-500/[0.04] p-3.5 shadow-xs">
          <div className="shrink-0 rounded-xl border border-amber-500/30 bg-amber-500/15 p-1.5 text-amber-600 dark:text-amber-400">
            <ShieldCheck size={16} />
          </div>
          <p className="text-xs leading-relaxed text-amber-900/90 dark:text-amber-200/90">
            Agent Teams are currently <strong>disabled</strong> for this conversation. Click the{" "}
            <strong className="underline underline-offset-2">Enable Team</strong> button above to
            activate named teammates and the shared board.
          </p>
        </div>
      ) : null}

      {team.enabled && !team.team_tools_available ? (
        <div className="to-surface-0 flex items-start gap-3 rounded-2xl border border-amber-500/40 bg-gradient-to-r from-amber-500/10 via-amber-500/[0.04] p-3.5 shadow-xs">
          <div className="shrink-0 rounded-xl border border-amber-500/30 bg-amber-500/15 p-1.5 text-amber-600 dark:text-amber-400">
            <ShieldCheck size={16} />
          </div>
          <div className="min-w-0 flex-1 text-xs leading-relaxed text-amber-900/90 dark:text-amber-200/90">
            <p>
              Teams are enabled, but the <strong>{team.preset}</strong> preset does not allow every
              team tool. Add the missing team tools to Custom before asking the lead to spawn or
              coordinate teammates. Missing: <code>{missingTeamTools.join(", ")}</code>
            </p>
            <div className="mt-2 flex flex-wrap items-center gap-1.5">
              {missingTeamTools.map((tool) => (
                <span
                  key={tool}
                  className="rounded-md border border-amber-500/30 bg-amber-500/15 px-2 py-0.5 font-mono text-[10px] font-semibold text-amber-900 shadow-2xs dark:text-amber-200"
                >
                  {tool}
                </span>
              ))}
            </div>
          </div>
        </div>
      ) : null}

      <div className="grid gap-4 md:grid-cols-2">
        {/* Members Roster Section */}
        <section className="border-border-subtle/80 bg-surface-1/40 flex flex-col rounded-2xl border p-4 shadow-xs transition-all hover:shadow-sm">
          <div className="border-border-subtle/60 mb-3 flex items-center justify-between gap-2 border-b pb-2.5">
            <div className="flex items-center gap-2">
              <div className="bg-primary/10 text-primary border-primary/20 rounded-xl border p-1.5 shadow-2xs">
                <Bot size={15} />
              </div>
              <h4 className="text-foreground text-xs font-bold tracking-[0.08em] uppercase">
                Members Roster
              </h4>
            </div>
            <span className="bg-surface-2/80 text-foreground/80 border-border-subtle/60 rounded-full border px-2.5 py-0.5 font-mono text-[11px] font-semibold shadow-2xs">
              {team.members.length}/{team.max_members}
            </span>
          </div>
          <p className="text-muted-foreground mb-3 text-[11px]">
            Preset: <span className="text-foreground font-semibold">{team.preset}</span>
          </p>
          <div className="space-y-2.5">
            {/* Lead Agent Card */}
            <div className="border-primary/30 from-primary/[0.08] via-primary/[0.03] to-surface-0 relative overflow-hidden rounded-xl border bg-gradient-to-r p-3 shadow-xs">
              <div className="flex items-center justify-between gap-2">
                <div className="flex min-w-0 items-center gap-2.5">
                  <div className="bg-primary/15 text-primary border-primary/30 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border text-xs font-bold shadow-2xs">
                    👑
                  </div>
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="text-foreground truncate text-sm font-semibold">
                        {team.lead.label}
                      </span>
                      <span className="bg-primary/15 text-primary rounded px-1.5 py-0.5 text-[9px] font-bold tracking-wider uppercase">
                        Lead
                      </span>
                    </div>
                    <p className="text-muted-foreground truncate text-[11px]">
                      Owns the conversation and coordinates named teammates.
                    </p>
                  </div>
                </div>
                <StatusPill status="running" />
              </div>
            </div>

            {/* Clickable Teammate Cards */}
            {team.members.map((member) => (
              <button
                key={member.id}
                type="button"
                onClick={() => handleTeammateClick(member)}
                aria-label={`View live session of teammate ${member.name}`}
                className="group border-border-subtle bg-surface-0 hover:border-primary/50 focus:ring-primary/40 w-full rounded-xl border p-3 text-left shadow-xs transition-all hover:-translate-y-0.5 hover:shadow-md focus:ring-2 focus:outline-none"
              >
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="flex items-center gap-2">
                    <div className="bg-surface-2 border-border-subtle text-foreground/80 flex h-7 w-7 items-center justify-center rounded-lg border text-[11px] font-bold shadow-2xs">
                      {member.name.slice(0, 2).toUpperCase()}
                    </div>
                    <span className="text-foreground group-hover:text-primary text-sm font-semibold transition-colors">
                      {member.name}
                    </span>
                    {member.child_id ? (
                      <span className="bg-surface-2 text-muted-foreground border-border-subtle/50 rounded-md border px-1.5 py-0.5 font-mono text-[9px]">
                        session:{member.child_id.slice(0, 8)}
                      </span>
                    ) : null}
                  </div>
                  <div className="flex items-center gap-1.5">
                    <StatusPill status={member.state} />
                    <ChevronRight
                      size={14}
                      className="text-muted-foreground group-hover:text-primary transition-transform group-hover:translate-x-0.5"
                    />
                  </div>
                </div>
                <div className="text-muted-foreground mt-2 flex flex-wrap items-center gap-1.5 text-[11px]">
                  <span className="bg-surface-1 border-border-subtle/50 rounded border px-1.5 py-0.5">
                    {member.provider || "Inherited provider"} · {member.model || "Inherited model"}
                  </span>
                  <span className="bg-surface-1 border-border-subtle/50 rounded border px-1.5 py-0.5">
                    {member.tool_filter.length > 0
                      ? `${member.tool_filter.length} restricted tool${member.tool_filter.length === 1 ? "" : "s"}`
                      : "Uses the parent tool allowance"}
                  </span>
                </div>
                {team.tasks
                  .filter((t) => t.owner === member.name)
                  .map((t) => (
                    <div key={t.id} className="mt-2 flex items-center gap-1.5">
                      <span className="bg-primary/10 border-primary/20 text-primary inline-flex max-w-full items-center gap-1 truncate rounded-md border px-2 py-0.5 text-[10px] font-medium">
                        <ListTodo size={11} className="shrink-0" />
                        <span className="truncate">Task: {t.title}</span>
                      </span>
                    </div>
                  ))}
                {member.result ? (
                  <p className="text-foreground/80 border-border-subtle bg-surface-1/40 mt-2 line-clamp-2 rounded border-t p-1.5 pt-1.5 text-xs italic">
                    &quot;{member.result}&quot;
                  </p>
                ) : null}
                <div className="text-muted-foreground border-border-subtle/50 mt-2.5 flex items-center justify-between border-t pt-2 text-[10px]">
                  <span className="text-primary inline-flex items-center gap-1 font-medium group-hover:underline">
                    <ExternalLink size={10} /> Click to open live session
                  </span>
                  <span>{formatDate(member.created_at)}</span>
                </div>
              </button>
            ))}

            {team.members.length === 0 ? (
              <div className="border-border-subtle bg-surface-0/60 flex flex-col items-center justify-center rounded-xl border border-dashed p-5 text-center shadow-2xs">
                <div className="bg-surface-2 text-muted-foreground mb-2 rounded-xl p-2.5">
                  <Bot size={20} />
                </div>
                <p className="text-foreground text-xs font-semibold">No Teammates Spawned</p>
                <p className="text-muted-foreground mt-0.5 max-w-xs text-[11px]">
                  No named teammates yet. The lead creates them with the{" "}
                  <code className="text-primary font-mono font-semibold">spawn_teammate</code> tool.
                </p>
              </div>
            ) : null}
          </div>
        </section>

        {/* Shared Task Board Section */}
        <section className="border-border-subtle/80 bg-surface-1/40 flex flex-col rounded-2xl border p-4 shadow-xs transition-all hover:shadow-sm">
          <div className="border-border-subtle/60 mb-3 flex items-center justify-between gap-2 border-b pb-2.5">
            <div className="flex items-center gap-2">
              <div className="rounded-xl border border-cyan-500/20 bg-cyan-500/10 p-1.5 text-cyan-500 shadow-2xs">
                <ListTodo size={15} />
              </div>
              <h4 className="text-foreground text-xs font-bold tracking-[0.08em] uppercase">
                Shared Tasks Board
              </h4>
            </div>
            <span className="bg-surface-2/80 text-foreground/80 border-border-subtle/60 rounded-full border px-2.5 py-0.5 font-mono text-[11px] font-semibold shadow-2xs">
              {team.tasks.length} task(s)
            </span>
          </div>
          <div className="space-y-2.5">
            {team.tasks.map((task) => {
              const isExpanded = expandedTasks.has(task.id);
              return (
                <article
                  key={task.id}
                  className="border-border-subtle bg-surface-0 hover:border-border-muted rounded-xl border p-3.5 shadow-xs transition-all hover:shadow-sm"
                >
                  <div className="flex items-start justify-between gap-2">
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-2">
                        <h5 className="text-foreground text-sm font-semibold">{task.title}</h5>
                        {/* Dynamic Ready vs Blocked Badge */}
                        {task.status === "pending" ? (
                          task.ready ? (
                            <span className="inline-flex items-center gap-1 rounded-full border border-cyan-500/40 bg-cyan-500/15 px-2 py-0.5 text-[10px] font-semibold text-cyan-700 shadow-2xs dark:text-cyan-300">
                              <CheckCircle2 size={10} /> Ready
                            </span>
                          ) : (
                            <span className="inline-flex items-center gap-1 rounded-full border border-amber-500/40 bg-amber-500/15 px-2 py-0.5 text-[10px] font-semibold text-amber-700 shadow-2xs dark:text-amber-300">
                              <AlertTriangle size={10} /> Blocked{" "}
                              {task.blocked_by.length > 0 ? `(${task.blocked_by.join(", ")})` : ""}
                            </span>
                          )
                        ) : (
                          <StatusPill status={task.status} />
                        )}
                      </div>
                      <p className="text-muted-foreground mt-1 text-[11px]">
                        {task.owner ? `Owner: ${task.owner}` : "Unassigned"} · Revision{" "}
                        {task.revision}
                      </p>
                    </div>

                    <div className="flex shrink-0 items-center gap-1">
                      <button
                        type="button"
                        onClick={() => toggleTaskAccordion(task.id)}
                        aria-label={isExpanded ? "Collapse task details" : "Expand task details"}
                        className="text-muted-foreground hover:bg-surface-2 hover:text-foreground rounded-lg p-1 transition-colors"
                      >
                        {isExpanded ? <ChevronDown size={15} /> : <ChevronRight size={15} />}
                      </button>
                      <button
                        type="button"
                        onClick={() => setActiveDetailTask(task)}
                        aria-label="Open task in full modal view"
                        className="text-muted-foreground hover:bg-surface-2 hover:text-foreground rounded-lg p-1 transition-colors"
                      >
                        <Maximize2 size={13} />
                      </button>
                    </div>
                  </div>

                  {/* Task Instructions / Body with Full Markdown Rendering */}
                  {task.body ? (
                    <div className="mt-2.5">
                      {isExpanded ? (
                        <div className="border-border-subtle bg-surface-1/60 text-foreground/90 prose prose-xs dark:prose-invert max-w-none rounded-lg border p-3 text-xs shadow-2xs">
                          <ReactMarkdown remarkPlugins={[remarkGfm]}>{task.body}</ReactMarkdown>
                        </div>
                      ) : (
                        <p
                          onClick={() => toggleTaskAccordion(task.id)}
                          className="text-foreground/80 hover:text-foreground bg-surface-1/30 line-clamp-2 cursor-pointer rounded p-1.5 text-xs transition-colors"
                        >
                          {task.body}
                        </p>
                      )}
                    </div>
                  ) : null}

                  {/* Dependencies */}
                  {task.depends_on.length > 0 ? (
                    <div className="text-muted-foreground mt-2 flex flex-wrap items-center gap-1.5 text-[11px]">
                      <span className="font-medium">Depends on:</span>
                      {task.depends_on.map((depId) => (
                        <span
                          key={depId}
                          className="bg-surface-2 border-border-subtle/50 rounded border px-1.5 py-0.5 font-mono text-[10px]"
                        >
                          {depId}
                        </span>
                      ))}
                    </div>
                  ) : null}

                  {/* Write Scopes & Collision Overlap Warnings */}
                  <div className="border-border-subtle/50 mt-2.5 flex flex-wrap items-center gap-2 border-t pt-2">
                    {task.write_scopes.length > 0 ? (
                      <span className="bg-surface-2 text-muted-foreground border-border-subtle/50 inline-flex items-center gap-1 rounded-md border px-2 py-0.5 font-mono text-[10px]">
                        <FileCode size={11} className="text-primary" />
                        Write scopes: {task.write_scopes.join(", ")}
                      </span>
                    ) : null}

                    {task.write_scope_warnings.length > 0 ? (
                      <span className="inline-flex items-center gap-1 rounded-md border border-amber-500/40 bg-amber-500/10 px-2 py-0.5 text-[10px] font-medium text-amber-700 dark:text-amber-300">
                        <AlertTriangle size={11} className="shrink-0 text-amber-500" />
                        Overlap warning: {task.write_scope_warnings.join(", ")}
                      </span>
                    ) : null}
                  </div>
                </article>
              );
            })}

            {team.tasks.length === 0 ? (
              <div className="border-border-subtle bg-surface-0/60 flex flex-col items-center justify-center rounded-xl border border-dashed p-5 text-center shadow-2xs">
                <div className="bg-surface-2 text-muted-foreground mb-2 rounded-xl p-2.5">
                  <ListTodo size={20} />
                </div>
                <p className="text-foreground text-xs font-semibold">Task Board Ready</p>
                <p className="text-muted-foreground mt-0.5 max-w-xs text-[11px]">
                  The shared task board is empty. The lead or a teammate can create tasks with{" "}
                  <code className="font-mono font-semibold text-cyan-600 dark:text-cyan-400">
                    team_task_create
                  </code>
                  .
                </p>
              </div>
            ) : null}
          </div>
        </section>

        {/* Delegated Children Section */}
        <section className="border-border-subtle/80 bg-surface-1/40 flex flex-col rounded-2xl border p-4 shadow-xs transition-all hover:shadow-sm md:col-span-2">
          <div className="border-border-subtle/60 mb-3 flex items-center justify-between gap-2 border-b pb-2.5">
            <div className="flex items-center gap-2">
              <div className="rounded-xl border border-indigo-500/20 bg-indigo-500/10 p-1.5 text-indigo-500 shadow-2xs">
                <GitBranch size={15} />
              </div>
              <h4 className="text-foreground text-xs font-bold tracking-[0.08em] uppercase">
                Delegated Children
              </h4>
            </div>
            <span className="bg-surface-2/80 text-foreground/80 border-border-subtle/60 rounded-full border px-2.5 py-0.5 font-mono text-[11px] font-semibold shadow-2xs">
              {team.children.length}
            </span>
          </div>
          {team.children.length > 0 ? (
            <div className="grid gap-2.5 sm:grid-cols-2 lg:grid-cols-3">
              {team.children.map((child) => (
                <div
                  key={child.agent_id}
                  onClick={() => onSelectSession?.(child.agent_id)}
                  className={`border-border-subtle bg-surface-0 rounded-xl border p-3 shadow-xs transition-all hover:shadow-sm ${
                    onSelectSession
                      ? "hover:border-primary/50 hover:bg-surface-1/80 cursor-pointer hover:-translate-y-0.5"
                      : ""
                  }`}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-foreground truncate text-sm font-semibold">
                      {child.label}
                    </span>
                    <StatusPill status={child.status} />
                  </div>
                  <p className="text-muted-foreground mt-1 text-[11px]">
                    {child.origin} · {child.mode} · depth {child.depth}
                  </p>
                  <p className="text-muted-foreground mt-1 text-[11px]">
                    {child.agent_provider || "Inherited provider"} ·{" "}
                    {child.agent_model || "Inherited model"} · {child.turn_count} turn(s)
                  </p>
                  {child.pending_messages > 0 ? (
                    <p className="mt-1.5 inline-flex items-center gap-1 rounded border border-amber-500/20 bg-amber-500/10 px-1.5 py-0.5 text-[10px] font-medium text-amber-700 dark:text-amber-300">
                      {child.pending_messages} queued message(s)
                    </p>
                  ) : null}
                </div>
              ))}
            </div>
          ) : (
            <div className="border-border-subtle bg-surface-0/60 flex flex-col items-center justify-center rounded-xl border border-dashed p-5 text-center shadow-2xs">
              <div className="bg-surface-2 text-muted-foreground mb-2 rounded-xl p-2.5">
                <GitBranch size={20} />
              </div>
              <p className="text-foreground text-xs font-semibold">No Delegated Children</p>
              <p className="text-muted-foreground mt-0.5 text-[11px]">
                No delegated child runs have been recorded for this conversation.
              </p>
            </div>
          )}
        </section>

        {/* Team Activity Section */}
        <section className="border-border-subtle/80 bg-surface-1/40 flex flex-col rounded-2xl border p-4 shadow-xs transition-all hover:shadow-sm md:col-span-2">
          <div className="border-border-subtle/60 mb-3 flex items-center justify-between gap-2 border-b pb-2.5">
            <div className="flex items-center gap-2">
              <div className="rounded-xl border border-emerald-500/20 bg-emerald-500/10 p-1.5 text-emerald-500 shadow-2xs">
                <MessageSquare size={15} />
              </div>
              <h4 className="text-foreground text-xs font-bold tracking-[0.08em] uppercase">
                Team Activity & Messages
              </h4>
            </div>
            <span className="bg-surface-2/80 text-foreground/80 border-border-subtle/60 rounded-full border px-2.5 py-0.5 font-mono text-[11px] font-semibold shadow-2xs">
              {team.messages.length} message(s)
            </span>
          </div>
          {team.messages.length > 0 ? (
            <div className="space-y-2">
              {team.messages.slice(-12).map((message) => {
                const meta = resolveMessageMeta(message);
                return (
                  <div
                    key={message.id}
                    className="border-border-subtle bg-surface-0 rounded-xl border p-3 shadow-xs"
                  >
                    <div className="flex flex-wrap items-center gap-2 text-[11px]">
                      <span className="text-foreground font-semibold">{meta.sender}</span>
                      <span className="text-muted-foreground">→</span>
                      <span className="text-foreground font-semibold">{message.to}</span>
                      <span
                        className={`inline-flex items-center rounded-md border px-1.5 py-0.5 text-[10px] font-medium ${meta.badgeClass}`}
                      >
                        {meta.badgeLabel}
                      </span>
                      <StatusPill status={message.state} />
                      <span className="text-muted-foreground ml-auto">
                        {formatDate(message.created_at)}
                      </span>
                    </div>
                    <div className="border-primary/50 bg-surface-1/40 text-foreground/90 prose prose-xs dark:prose-invert mt-1.5 max-w-none rounded-lg border-l-2 p-2.5 text-xs [overflow-wrap:anywhere] break-words">
                      <ReactMarkdown remarkPlugins={[remarkGfm]}>{message.body}</ReactMarkdown>
                    </div>
                  </div>
                );
              })}
            </div>
          ) : (
            <div className="border-border-subtle bg-surface-0/60 flex flex-col items-center justify-center rounded-xl border border-dashed p-5 text-center shadow-2xs">
              <div className="bg-surface-2 text-muted-foreground mb-2 rounded-xl p-2.5">
                <MessageSquare size={20} />
              </div>
              <p className="text-foreground text-xs font-semibold">No Messages Yet</p>
              <p className="text-muted-foreground mt-0.5 text-[11px]">
                No messages have been exchanged between the lead and teammates yet.
              </p>
            </div>
          )}
        </section>
      </div>

      {/* Footer Info */}
      <div className="border-border-subtle/60 bg-surface-1/40 text-muted-foreground mt-4 flex flex-wrap items-center justify-between gap-x-4 gap-y-2 rounded-xl border px-3.5 py-2 text-[11px]">
        <div className="flex items-center gap-4">
          <span className="inline-flex items-center gap-1.5">
            <span className="relative flex h-2 w-2">
              <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-emerald-400 opacity-75" />
              <span className="relative inline-flex h-2 w-2 rounded-full bg-emerald-500" />
            </span>
            <CheckCircle2 size={12} className="text-emerald-500" /> Durable team state
          </span>
          <span className="inline-flex items-center gap-1.5">
            <Clock3 size={12} /> Refreshes every 3 seconds
          </span>
        </div>
        <span className="text-muted-foreground/70 font-mono text-[10px]">
          AverQel NeoSIS Agent Teams Engine
        </span>
      </div>

      {/* Teammate Live Session Inspector Modal */}
      {inspectedMember ? (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4 backdrop-blur-xs">
          <div className="border-border-subtle bg-surface-0 w-full max-w-lg rounded-2xl border p-5 shadow-2xl">
            <div className="border-border-subtle flex items-center justify-between border-b pb-3">
              <div className="flex items-center gap-2">
                <Bot size={18} className="text-primary" />
                <h3 className="text-foreground text-base font-semibold">{inspectedMember.name}</h3>
                <StatusPill status={inspectedMember.state} />
              </div>
              <button
                type="button"
                onClick={() => setInspectedMember(null)}
                aria-label="Close teammate inspector"
                className="text-muted-foreground hover:bg-surface-2 hover:text-foreground rounded-lg p-1"
              >
                <X size={16} />
              </button>
            </div>

            <div className="mt-4 space-y-3 text-xs">
              <div className="bg-surface-1 space-y-1.5 rounded-lg p-3">
                <div className="flex justify-between">
                  <span className="text-muted-foreground">Provider:</span>
                  <span className="text-foreground font-medium">
                    {inspectedMember.provider || "Inherited"}
                  </span>
                </div>
                <div className="flex justify-between">
                  <span className="text-muted-foreground">Model:</span>
                  <span className="text-foreground font-medium">
                    {inspectedMember.model || "Inherited"}
                  </span>
                </div>
                <div className="flex justify-between">
                  <span className="text-muted-foreground">Session / Child ID:</span>
                  <span className="text-primary font-mono font-medium">
                    {inspectedMember.child_id || "None attached"}
                  </span>
                </div>
                <div className="flex justify-between">
                  <span className="text-muted-foreground">Created At:</span>
                  <span className="text-foreground">{formatDate(inspectedMember.created_at)}</span>
                </div>
              </div>

              {inspectedMember.result ? (
                <div>
                  <h5 className="text-foreground mb-1 font-semibold">Latest Result / Summary:</h5>
                  <div className="bg-surface-1 text-foreground/80 max-h-36 overflow-y-auto rounded-lg p-3">
                    {inspectedMember.result}
                  </div>
                </div>
              ) : null}

              {/* Messages related to this member */}
              <div>
                <h5 className="text-foreground mb-1 font-semibold">Recent Messages:</h5>
                <div className="max-h-40 space-y-1.5 overflow-y-auto">
                  {team.messages
                    .filter((m) => {
                      const meta = resolveMessageMeta(m);
                      return (
                        meta.sender === inspectedMember.name ||
                        m.from === inspectedMember.name ||
                        m.to === inspectedMember.name
                      );
                    })
                    .map((msg) => {
                      const meta = resolveMessageMeta(msg);
                      return (
                        <div
                          key={msg.id}
                          className="border-border-subtle bg-surface-1 rounded border p-2.5 text-[11px]"
                        >
                          <div className="flex flex-wrap items-center justify-between gap-1.5 font-medium">
                            <div className="flex items-center gap-1.5">
                              <span>
                                {meta.sender} → {msg.to}
                              </span>
                              <span
                                className={`py-0.2 inline-flex items-center rounded border px-1.5 text-[9px] font-medium ${meta.badgeClass}`}
                              >
                                {meta.badgeLabel}
                              </span>
                            </div>
                            <span className="text-muted-foreground">
                              {formatDate(msg.created_at)}
                            </span>
                          </div>
                          <div className="text-foreground/90 prose prose-xs dark:prose-invert mt-1.5 max-w-none [overflow-wrap:anywhere] break-words">
                            <ReactMarkdown remarkPlugins={[remarkGfm]}>{msg.body}</ReactMarkdown>
                          </div>
                        </div>
                      );
                    })}
                  {team.messages.filter(
                    (m) => m.from === inspectedMember.name || m.to === inspectedMember.name,
                  ).length === 0 ? (
                    <p className="text-muted-foreground text-[11px] italic">
                      No direct messages recorded for this member.
                    </p>
                  ) : null}
                </div>
              </div>
            </div>

            <div className="border-border-subtle mt-5 flex items-center justify-between border-t pt-3">
              {inspectedMember.state !== "finished" ? (
                <button
                  type="button"
                  disabled={interruptingMember}
                  onClick={() => void handleInterruptMember(inspectedMember)}
                  className="inline-flex items-center gap-1.5 rounded-lg border border-rose-500/40 bg-rose-500/10 px-3 py-1.5 text-xs font-semibold text-rose-700 hover:bg-rose-500/20 disabled:opacity-50 dark:text-rose-300"
                >
                  {interruptingMember ? (
                    <AverQelLoader size={13} className="animate-spin" />
                  ) : (
                    <StopCircle size={13} />
                  )}
                  <span>Interrupt Teammate</span>
                </button>
              ) : (
                <span className="text-muted-foreground text-xs italic">
                  Member turn is finished.
                </span>
              )}

              <div className="flex items-center gap-2">
                {inspectedMember.child_id && onSelectSession ? (
                  <button
                    type="button"
                    onClick={() => {
                      onSelectSession(inspectedMember.child_id!);
                      setInspectedMember(null);
                    }}
                    className="bg-primary text-primary-foreground hover:bg-primary/90 inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-semibold"
                  >
                    <ExternalLink size={13} /> Switch to Session
                  </button>
                ) : null}
                <button
                  type="button"
                  onClick={() => setInspectedMember(null)}
                  className="border-border-subtle hover:bg-surface-1 rounded-lg border px-3 py-1.5 text-xs font-semibold"
                >
                  Close
                </button>
              </div>
            </div>
          </div>
        </div>
      ) : null}

      {/* Task Full Detail & Markdown Modal */}
      {activeDetailTask ? (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4 backdrop-blur-xs">
          <div className="border-border-subtle bg-surface-0 flex max-h-[85vh] w-full max-w-2xl flex-col overflow-hidden rounded-2xl border p-5 shadow-2xl">
            <div className="border-border-subtle flex items-start justify-between border-b pb-3">
              <div>
                <div className="flex flex-wrap items-center gap-2">
                  <h3 className="text-foreground text-base font-semibold">
                    {activeDetailTask.title}
                  </h3>
                  <StatusPill status={activeDetailTask.status} />
                  {activeDetailTask.status === "pending" &&
                    (activeDetailTask.ready ? (
                      <span className="inline-flex items-center gap-1 rounded-full border border-cyan-500/40 bg-cyan-500/15 px-2 py-0.5 text-[10px] font-semibold text-cyan-600 dark:text-cyan-300">
                        <CheckCircle2 size={10} /> Ready
                      </span>
                    ) : (
                      <span className="inline-flex items-center gap-1 rounded-full border border-amber-500/40 bg-amber-500/15 px-2 py-0.5 text-[10px] font-semibold text-amber-600 dark:text-amber-300">
                        <AlertTriangle size={10} /> Blocked{" "}
                        {activeDetailTask.blocked_by.length > 0
                          ? `(${activeDetailTask.blocked_by.join(", ")})`
                          : ""}
                      </span>
                    ))}
                </div>
                <p className="text-muted-foreground mt-1 text-xs">
                  {activeDetailTask.owner ? `Owner: ${activeDetailTask.owner}` : "Unassigned"} ·
                  Revision {activeDetailTask.revision} · Updated{" "}
                  {formatDate(activeDetailTask.updated_at)}
                </p>
              </div>
              <button
                type="button"
                onClick={() => setActiveDetailTask(null)}
                aria-label="Close task details"
                className="text-muted-foreground hover:bg-surface-2 hover:text-foreground rounded-lg p-1"
              >
                <X size={16} />
              </button>
            </div>

            <div className="mt-4 min-h-0 min-w-0 flex-1 space-y-4 overflow-y-auto pr-1">
              {/* Full Markdown Rendering */}
              <div className="min-w-0">
                <h5 className="text-muted-foreground mb-2 text-xs font-semibold tracking-wider uppercase">
                  Task Instructions & Rubrics
                </h5>
                <div className="border-border-subtle bg-surface-1/40 prose prose-sm dark:prose-invert text-foreground/90 max-w-none min-w-0 overflow-x-auto rounded-xl border p-4 [overflow-wrap:anywhere] break-words">
                  <ReactMarkdown remarkPlugins={[remarkGfm]}>
                    {activeDetailTask.body || "_No detailed body provided for this task._"}
                  </ReactMarkdown>
                </div>
              </div>

              {/* Dependencies & Write Scopes in Modal */}
              <div className="grid gap-3 text-xs sm:grid-cols-2">
                <div className="border-border-subtle bg-surface-1 rounded-lg border p-3">
                  <h6 className="text-foreground mb-1 font-semibold">Dependencies:</h6>
                  {activeDetailTask.depends_on.length > 0 ? (
                    <ul className="text-muted-foreground list-inside list-disc space-y-0.5 font-mono">
                      {activeDetailTask.depends_on.map((d) => (
                        <li key={d}>{d}</li>
                      ))}
                    </ul>
                  ) : (
                    <p className="text-muted-foreground italic">None (Root DAG task)</p>
                  )}
                </div>

                <div className="border-border-subtle bg-surface-1 rounded-lg border p-3">
                  <h6 className="text-foreground mb-1 font-semibold">Advisory Write Scopes:</h6>
                  {activeDetailTask.write_scopes.length > 0 ? (
                    <ul className="text-muted-foreground list-inside list-disc space-y-0.5 font-mono">
                      {activeDetailTask.write_scopes.map((s) => (
                        <li key={s}>{s}</li>
                      ))}
                    </ul>
                  ) : (
                    <p className="text-muted-foreground italic">No write scope constraints</p>
                  )}
                </div>
              </div>

              {activeDetailTask.write_scope_warnings.length > 0 ? (
                <div className="flex items-start gap-2 rounded-xl border border-amber-500/40 bg-amber-500/10 p-3 text-xs text-amber-800 dark:text-amber-200">
                  <AlertTriangle size={15} className="mt-0.5 shrink-0 text-amber-500" />
                  <div>
                    <span className="font-semibold">Write Scope Overlap Detected:</span>
                    <p className="mt-0.5">
                      This task writes to folders that overlap with:{" "}
                      {activeDetailTask.write_scope_warnings.join(", ")}
                    </p>
                  </div>
                </div>
              ) : null}
            </div>

            <div className="border-border-subtle mt-4 flex justify-end border-t pt-3">
              <button
                type="button"
                onClick={() => setActiveDetailTask(null)}
                className="bg-primary text-primary-foreground hover:bg-primary/90 rounded-lg px-4 py-2 text-xs font-semibold"
              >
                Done
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}
