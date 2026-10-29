"use client";

import { useEffect, useMemo, useState } from "react";
import { motion } from "framer-motion";
import { ChevronRight, GitBranch, RefreshCw } from "lucide-react";
import { fetchWithAuth } from "@/lib/api";

export interface DeepSpaceAgentTreeNode {
  agentId: string;
  parentAgentId: string | null;
  label: string;
  mode: string;
  origin: string;
  status: string;
  depth: number;
  turnCount: number;
  children: DeepSpaceAgentTreeNode[];
}

export interface DeepSpaceAgentTreeProps {
  /** The conversation whose delegation tree is shown. */
  conversationId: string | null;
  /** Whether the panel is open. */
  open: boolean;
}

/**
 * Build the delegation tree from the flat projection rows.
 *
 * The projection returns one row per child agent with its parent, so the
 * tree is assembled here rather than in the API.
 */
export function buildAgentTree(
  rows: Array<Record<string, unknown>>,
): DeepSpaceAgentTreeNode[] {
  const nodes = new Map<string, DeepSpaceAgentTreeNode>();
  const order: string[] = [];
  for (const row of rows) {
    const agentId = String(row.agent_id ?? "").trim();
    if (!agentId) continue;
    nodes.set(agentId, {
      agentId,
      parentAgentId: row.parent_agent_id ? String(row.parent_agent_id) : null,
      label: String(row.label ?? ""),
      mode: String(row.mode ?? ""),
      origin: String(row.origin ?? ""),
      status: String(row.status ?? ""),
      depth: Number(row.depth ?? 0) || 0,
      turnCount: Number(row.turn_count ?? 0) || 0,
      children: [],
    });
    order.push(agentId);
  }
  const roots: DeepSpaceAgentTreeNode[] = [];
  for (const agentId of order) {
    const node = nodes.get(agentId);
    if (!node) continue;
    const parent = node.parentAgentId ? nodes.get(node.parentAgentId) : undefined;
    if (parent) {
      parent.children.push(node);
    } else {
      roots.push(node);
    }
  }
  return roots;
}

/**
 * The agent tree panel.
 *
 * It shows the child agents of a conversation and the shape of their
 * delegation, so a user can see which work ran in parallel and which agent
 * started it.
 */
export default function DeepSpaceAgentTree({
  conversationId,
  open,
}: DeepSpaceAgentTreeProps) {
  const [rows, setRows] = useState<Array<Record<string, unknown>> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const [refreshToken, setRefreshToken] = useState(0);

  useEffect(() => {
    if (!open || !conversationId) return;
    let cancelled = false;
    void (async () => {
      try {
        const response = (await fetchWithAuth(
          `/deepspace/chats/${conversationId}/projection`,
        )) as Response;
        if (cancelled) return;
        if (!response.ok) {
          setError("The agent tree could not be loaded.");
          return;
        }
        const payload = (await response.json()) as {
          delegation_tree?: Array<Record<string, unknown>>;
        };
        if (cancelled) return;
        setRows(Array.isArray(payload.delegation_tree) ? payload.delegation_tree : []);
        setError(null);
      } catch {
        if (!cancelled) setError("The agent tree could not be loaded.");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [open, conversationId, refreshToken]);

  const tree = useMemo(() => buildAgentTree(rows ?? []), [rows]);
  const loading = open && rows === null && error === null;

  if (!open) return null;

  const toggle = (agentId: string) => {
    setCollapsed((current) => {
      const next = new Set(current);
      if (next.has(agentId)) {
        next.delete(agentId);
      } else {
        next.add(agentId);
      }
      return next;
    });
  };

  const renderNode = (node: DeepSpaceAgentTreeNode, depth: number) => {
    const isCollapsed = collapsed.has(node.agentId);
    return (
      <li key={node.agentId} className="deepspace-agent-tree-node">
        <div
          className="flex items-center gap-2 rounded-lg border border-white/8 bg-white/[0.025] px-2.5 py-2"
          style={{ marginLeft: depth * 14 }}
        >
          {node.children.length > 0 ? (
            <button
              type="button"
              onClick={() => toggle(node.agentId)}
              aria-label={isCollapsed ? "Expand the agent" : "Collapse the agent"}
              className="rounded p-0.5 text-white/50 transition-colors hover:text-white focus-visible:ring-2 focus-visible:ring-cyan-200/60 focus-visible:outline-none"
            >
              <ChevronRight
                size={13}
                className={`transition-transform ${isCollapsed ? "" : "rotate-90"}`}
              />
            </button>
          ) : (
            <span className="w-4" />
          )}
          <span className="min-w-0 flex-1 truncate text-xs font-medium text-white/85">
            {node.label || "Delegated agent"}
          </span>
          <span className="deepspace-agent-tree-mode rounded-md border border-white/10 bg-white/[0.05] px-1.5 py-0.5 text-[9px] font-semibold tracking-[0.08em] text-white/55 uppercase">
            {node.mode || node.origin || "agent"}
          </span>
          <span
            className={`deepspace-agent-tree-status rounded-md px-1.5 py-0.5 text-[9px] font-semibold tracking-[0.08em] uppercase ${
              node.status === "completed" || node.status === "idle"
                ? "bg-emerald-300/10 text-emerald-200/80"
                : node.status === "failed" || node.status === "error"
                  ? "bg-red-300/10 text-red-200/80"
                  : "bg-cyan-300/10 text-cyan-200/80"
            }`}
          >
            {node.status || "running"}
          </span>
        </div>
        {isCollapsed ? null : (
          <ul className="mt-1 space-y-1">
            {node.children.map((child) => renderNode(child, depth + 1))}
          </ul>
        )}
      </li>
    );
  };

  return (
    <motion.section
      initial={{ opacity: 0, y: -6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.2, ease: "easeOut" }}
      aria-label="Agent tree"
      className="deepspace-agent-tree mb-4 w-full max-w-4xl rounded-2xl border border-white/10 bg-[rgba(14,22,24,.9)] p-4"
    >
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2 text-white/80">
          <GitBranch size={15} className="text-cyan-200/80" />
          <span className="text-xs font-semibold tracking-[0.1em] text-white/70 uppercase">
            Agent tree
          </span>
          <span className="text-[10px] text-white/45">
            {tree.length === 0
              ? "No delegated agents yet"
              : `${countAgents(tree)} agent${countAgents(tree) === 1 ? "" : "s"}`}
          </span>
        </div>
        <button
          type="button"
          onClick={() => {
            setRows(null);
            setError(null);
            setRefreshToken((token) => token + 1);
          }}
          aria-label="Refresh the agent tree"
          className="rounded p-1 text-white/50 transition-colors hover:text-white focus-visible:ring-2 focus-visible:ring-cyan-200/60 focus-visible:outline-none"
        >
          <RefreshCw size={13} className={loading ? "animate-spin" : ""} />
        </button>
      </div>
      {error ? (
        <p role="alert" className="mt-2 text-xs text-red-300">
          {error}
        </p>
      ) : null}
      {tree.length === 0 && !loading && !error ? (
        <p className="mt-2 text-xs text-white/50">
          This conversation has not delegated any work to a child agent.
        </p>
      ) : (
        <ul className="mt-3 space-y-1">{tree.map((node) => renderNode(node, 0))}</ul>
      )}
    </motion.section>
  );
}

function countAgents(nodes: DeepSpaceAgentTreeNode[]): number {
  return nodes.reduce((total, node) => total + 1 + countAgents(node.children), 0);
}
