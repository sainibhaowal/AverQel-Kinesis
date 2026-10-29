"use client";

import { useEffect, useState } from "react";
import { Flag, ChevronRight, Loader2 } from "lucide-react";
import { fetchWithAuth } from "@/lib/api";

type GoalPhase = "active" | "paused" | "blocked" | "complete";

interface Goal {
  goal_id: string;
  name: string;
  phase: GoalPhase;
  revision: number;
  rounds_started: number;
  max_goal_rounds: number;
  blocked_reason: Record<string, unknown>;
  note: string | null;
  created_at: string | null;
  updated_at: string | null;
}

interface DeepSpaceGoalBarProps {
  conversationId: string | null;
  /** When true the bar is visible even if no goals exist. */
  alwaysShow?: boolean;
}

const PHASE_COLOR: Record<GoalPhase, string> = {
  active: "text-cyan-600 dark:text-cyan-400",
  paused: "text-amber-600 dark:text-amber-400",
  blocked: "text-red-600 dark:text-red-400",
  complete: "text-emerald-600 dark:text-emerald-400",
};

const PHASE_BG: Record<GoalPhase, string> = {
  active: "bg-cyan-500/10 dark:bg-cyan-900/20",
  paused: "bg-amber-500/10 dark:bg-amber-900/20",
  blocked: "bg-red-500/10 dark:bg-red-900/20",
  complete: "bg-emerald-500/10 dark:bg-emerald-900/20",
};

export const EMPTY_CONVERSATION_ID = "00000000-0000-0000-0000-000000000000";

export function isRealConversationId(conversationId: string | null): conversationId is string {
  return Boolean(conversationId && conversationId !== EMPTY_CONVERSATION_ID);
}

export function DeepSpaceGoalBar({
  conversationId,
  alwaysShow = false,
}: DeepSpaceGoalBarProps) {
  const [goals, setGoals] = useState<Goal[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!isRealConversationId(conversationId)) {
      setGoals([]);
      setLoading(false);
      return;
    }
    const url = `/deepspace/chats/${encodeURIComponent(conversationId)}/goals`;
    let cancelled = false;

    async function load(isBackground = false) {
      if (!isBackground) {
        setLoading(true);
        setError(null);
      }
      try {
        const response = await fetchWithAuth(url, { method: "GET" });
        if (!response.ok) throw new Error("Failed to load goals");
        const data = await response.json();
        if (!cancelled) {
          setGoals(Array.isArray(data) ? data : Array.isArray(data?.goals) ? data.goals : []);
          setError(null);
        }
      } catch (err) {
        if (!cancelled) {
          if (!isBackground) {
            setError(err instanceof Error ? err.message : "Unknown error");
            setGoals([]);
          }
        }
      } finally {
        if (!cancelled && !isBackground) {
          setLoading(false);
        }
      }
    }

    void load(false);
    const refreshTimer = window.setInterval(() => {
      if (document.visibilityState === "visible") {
        void load(true);
      }
    }, 4000);

    return () => {
      cancelled = true;
      window.clearInterval(refreshTimer);
    };
  }, [conversationId]);

  const activeGoals = goals.filter((g) => g.phase !== "complete");
  if (!alwaysShow && activeGoals.length === 0) return null;

  return (
    <div
      className="mx-auto w-full max-w-[min(100%,74rem)] px-3 sm:px-5 border-b border-white/10"
      role="region"
      aria-label="Conversation goals"
    >
      <div className="flex items-center gap-2 px-3 py-2">
        <Flag size={14} className="text-foreground/50 shrink-0" aria-hidden="true" />
        <span className="text-[11px] font-medium text-foreground/60">Goals</span>
        {loading ? (
          <Loader2 size={12} className="animate-spin text-foreground/40" />
        ) : error ? (
          <span className="text-[11px] text-red-500">Error loading goals</span>
        ) : (
          <>
            <span className="text-[11px] text-foreground/30 mx-1">
              {activeGoals.length}
            </span>
            <ChevronRight size={12} className="text-foreground/20" />
          </>
        )}
      </div>
      {activeGoals.length > 0 && (
        <div className="flex flex-wrap gap-2 px-3 pb-2">
          {activeGoals.map((goal) => (
            <div
              key={goal.goal_id}
              className={`flex items-center gap-1.5 px-2.5 py-1 rounded-full text-[11px] ${
                PHASE_BG[goal.phase]
              } ${PHASE_COLOR[goal.phase]}`}
              role="listitem"
            >
              <span className="font-medium truncate max-w-[180px]">{goal.name}</span>
              <span
                className="flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] bg-white/20 dark:bg-black/20"
              >
                {goal.rounds_started}/{goal.max_goal_rounds}
              </span>
              {goal.phase === "blocked" && goal.blocked_reason && (
                <span className="flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] bg-red-500/20 text-red-400">
                  {String(goal.blocked_reason?.reason ?? "blocked")}
                </span>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export default DeepSpaceGoalBar;
