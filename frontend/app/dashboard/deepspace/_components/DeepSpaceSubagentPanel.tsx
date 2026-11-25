"use client";

import { memo, useEffect, useId, useRef, useState } from "react";
import { ChevronDown } from "lucide-react";

import { parseTimestampMs, type SubagentActivityTimeline } from "../_lib/deepspace-stream";
import { BotIcon, formatElapsed, TimelineEntry } from "./DeepSpaceThinkingPanel";

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

function subagentMetricsSummary(subagent: SubagentActivityTimeline): string {
  const parts: string[] = [];
  if (subagent.mode) {
    parts.push(subagent.mode);
  }
  const rounds = subagent.rounds || 0;
  if (rounds > 0) {
    parts.push(`${rounds} round${rounds === 1 ? "" : "s"}`);
  }
  const tools = subagent.toolCount || subagent.steps.length;
  parts.push(`${tools} tool${tools === 1 ? "" : "s"}`);
  if (subagent.status !== "running") {
    parts.push(subagent.status);
  }
  return parts.join(" · ");
}

const SubagentCard = memo(function SubagentCard({
  subagent,
  isParentStreaming,
}: {
  subagent: SubagentActivityTimeline;
  isParentStreaming: boolean;
}) {
  const isRunning = subagent.status === "running";
  const [panelOpen, setPanelOpen] = useState(() => isRunning);
  const bodyId = `deepspace-subagent-${useId().replace(/:/g, "")}`;
  const wasRunning = useRef(isRunning);
  const [clock, setClock] = useState(() => Date.now());

  // Automatically expand a newly started child stream, but never override the
  // user's manual expand/collapse state once running or completed.
  useEffect(() => {
    if (isRunning && !wasRunning.current) {
      const timer = window.setTimeout(() => setPanelOpen(true), 0);
      wasRunning.current = true;
      return () => window.clearTimeout(timer);
    }
    wasRunning.current = isRunning;
  }, [isRunning]);

  useEffect(() => {
    if (!isRunning) return;
    const timer = window.setInterval(() => setClock(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [isRunning]);

  const durationMs = subagentDurationMs(subagent, clock);
  const durationLabel = durationMs !== null ? formatElapsed(durationMs) : null;
  const metricsLine = subagentMetricsSummary(subagent);
  const statusDotClass =
    subagent.status === "running"
      ? "bg-cyan-400 animate-pulse"
      : subagent.status === "failed"
        ? "bg-red-400"
        : subagent.status === "cancelled"
          ? "bg-amber-400"
          : "bg-emerald-400/80";

  return (
    <div
      data-testid={`subagent-panel-${subagent.id}`}
      className="motion-safe:animate-in motion-safe:fade-in mb-3 motion-safe:duration-300 motion-safe:ease-out"
    >
      <button
        type="button"
        aria-expanded={panelOpen}
        aria-controls={bodyId}
        onClick={() => setPanelOpen((open) => !open)}
        className="text-foreground/50 hover:text-foreground/80 flex w-full cursor-pointer items-center gap-2 border-b border-white/8 py-2 text-left text-[11px] font-bold tracking-wider uppercase transition-colors duration-200 focus-visible:ring-2 focus-visible:ring-cyan-300/40 focus-visible:outline-none"
      >
        <BotIcon size={13} className="shrink-0 text-cyan-300/75" />
        <span className={`inline-block h-1.5 w-1.5 shrink-0 rounded-full ${statusDotClass}`} />
        <span className="truncate">
          {`Subagent · ${subagent.label}`}
          {isRunning ? "…" : ""}
        </span>
        <ChevronDown
          size={12}
          className={`text-foreground/40 shrink-0 transition-transform duration-200 ${panelOpen ? "rotate-180" : ""}`}
        />
        {durationLabel ? (
          <span className="text-foreground/35 ml-auto shrink-0 tracking-normal normal-case">
            {isRunning && isParentStreaming
              ? `Working for ${durationLabel}`
              : isRunning
                ? `Working for ${durationLabel}`
                : `Worked for ${durationLabel}`}
          </span>
        ) : null}
      </button>
      <div
        data-testid={`subagent-metrics-${subagent.id}`}
        className="text-foreground/45 pt-1.5 pb-1 font-mono text-[11px]"
      >
        {metricsLine}
      </div>
      <div
        id={bodyId}
        aria-hidden={!panelOpen}
        className={`grid transition-[grid-template-rows,opacity] duration-300 ease-out motion-reduce:transition-none ${panelOpen ? "grid-rows-[1fr] opacity-100" : "grid-rows-[0fr] opacity-0"}`}
      >
        <div className="min-h-0 overflow-hidden">
          <div
            className="text-foreground/60 space-y-3 overscroll-contain py-3 text-xs"
            data-subagent-activity="true"
          >
            {subagent.mission ? (
              <div className="border-b border-white/8 pb-2.5">
                <div className="text-foreground/45 mb-1 text-[10px] font-semibold tracking-[0.12em] uppercase">
                  Delegated mission
                </div>
                <div className="text-foreground/70 text-xs leading-relaxed">
                  {subagent.mission}
                </div>
              </div>
            ) : null}
            {subagent.steps.length ? (
              <ol
                className="space-y-3"
                aria-label={`Subagent ${subagent.label} timeline`}
              >
                {subagent.steps.map((step, index) => (
                  <TimelineEntry
                    key={`subagent-${subagent.id}-${step.id}-${index}`}
                    step={step}
                    index={index}
                    isLast={index === subagent.steps.length - 1}
                  />
                ))}
              </ol>
            ) : isRunning ? (
              <div className="text-foreground/45">Subagent is starting tool execution…</div>
            ) : (
              <div className="text-foreground/45">
                Subagent completed without issuing additional child tool calls.
              </div>
            )}
            {subagent.summary && !isRunning ? (
              <div className="border-t border-white/8 pt-2.5">
                <div className="text-foreground/45 mb-1 text-[10px] font-semibold tracking-[0.12em] uppercase">
                  Subagent outcome
                </div>
                <div className="text-foreground/75 text-xs leading-relaxed whitespace-pre-wrap">
                  {subagent.summary}
                </div>
              </div>
            ) : null}
          </div>
        </div>
      </div>
    </div>
  );
});

export default function DeepSpaceSubagentPanel({
  subagents = [],
  isParentStreaming,
}: {
  subagents?: SubagentActivityTimeline[];
  isParentStreaming: boolean;
}) {
  if (!subagents.length) return null;
  return (
    <div className="mb-3 space-y-1" data-testid="deepspace-subagent-panels">
      {subagents.map((subagent) => (
        <SubagentCard
          key={subagent.id}
          subagent={subagent}
          isParentStreaming={isParentStreaming}
        />
      ))}
    </div>
  );
}
