"use client";

import { memo, useMemo, useState, useRef, useEffect, useCallback, type CSSProperties, type PointerEvent as ReactPointerEvent } from "react";
import {
  Clock,
  Search,
  RefreshCw,
  ChevronDown,
  ChevronRight,
  CheckCircle2,
  AlertCircle,
  Copy,
  Check,
  X,
  Filter,
} from "lucide-react";
import { fetchWithAuth } from "@/lib/api";

export interface TimeRange {
  startMs: number;
  endMs: number;
}

export interface TrajectorySpan {
  id: string;
  lane: "input" | "model" | "tools";
  kind: "system" | "user" | "context" | "message" | "tool" | "subtool";
  label: string;
  started_at_ms: number;
  duration_ms: number;
  ttft_ms?: number;
  decoding_ms?: number;
  status: string;
  arguments?: Record<string, unknown>;
  error?: string;
  record_id?: string;
}

export interface TrajectoryRecord {
  id: string;
  turn: number;
  step?: number;
  kind: "system" | "user" | "context" | "message" | "tool" | "subtool";
  label: "SYSTEM" | "USER" | "CONTEXT" | "ASSISTANT" | "TOOL" | "SUB";
  timestamp: string;
  started_at_ms: number;
  duration_ms: number;
  title: string;
  content?: string;
  arguments?: Record<string, unknown>;
  result?: unknown;
  status: string;
  assistant_metrics?: {
    tokens?: number;
    ttft_ms?: number;
    decoding_ms?: number;
    input_tokens?: number;
    output_tokens?: number;
    think_tokens?: number;
  };
}

export interface TrajectoryStep {
  step: number;
  started_at?: string | null;
  ended_at?: string | null;
  duration_ms?: number | null;
  tool_count?: number;
  tool_duration_ms?: number | null;
  tools?: Array<{
    tool_name: string;
    status: string;
    duration_ms?: number | null;
    wall_ms?: number | null;
    called_at?: string | null;
    completed_at?: string | null;
    arguments?: Record<string, unknown>;
    error?: string | null;
  }>;
}

export interface TrajectoryTurn {
  turn: number;
  started_at?: string | null;
  ended_at?: string | null;
  duration_ms?: number | null;
  steps?: TrajectoryStep[];
  spans?: TrajectorySpan[];
  records?: TrajectoryRecord[];
  user?: {
    content?: string;
    created_at?: string;
  };
  assistant?: {
    content?: string;
    created_at?: string;
    tokens?: number;
    citations?: unknown;
    model_name?: string;
  };
}

export interface TrajectoryPayload {
  turns: TrajectoryTurn[];
  totals?: {
    turn_count?: number;
    step_count?: number;
    tool_count?: number;
    duration_ms?: number;
  };
}

function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "—";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  const sec = ms / 1000;
  return `${sec < 10 ? sec.toFixed(2) : sec.toFixed(1)}s`;
}

function formatClockTime(isoOrTimestamp: string | number | null | undefined): string {
  if (!isoOrTimestamp) return "";
  try {
    const d = new Date(isoOrTimestamp);
    if (isNaN(d.getTime())) return "";
    return d.toLocaleTimeString(undefined, {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      fractionalSecondDigits: 3,
    });
  } catch {
    return "";
  }
}

/**
 * Interactive 3-lane Gantt timeline: Input, Model, Tools.
 * Supports drag-to-highlight time range, dual boundary handles, and outside-dimming.
 */
interface TimelineProps {
  turns: TrajectoryTurn[];
  actualDuration: boolean;
  timeRange: TimeRange | null;
  onTimeRangeChange: (range: TimeRange | null) => void;
  selectedRecordId: string | null;
  onSelectRecord: (recordId: string) => void;
}

function DeepSpaceTimeline({
  turns,
  actualDuration,
  timeRange,
  onTimeRangeChange,
  selectedRecordId,
  onSelectRecord,
}: TimelineProps) {
  const trackRef = useRef<HTMLDivElement | null>(null);
  const dragRef = useRef<{
    pointerId: number;
    anchorClientX: number;
    anchorMs: number;
  } | null>(null);

  const [draftRange, setDraftRange] = useState<TimeRange | null>(null);
  const [hoverPosition, setHoverPosition] = useState<{ fraction: number } | null>(null);
  const [hoveredSpan, setHoveredSpan] = useState<{
    span: TrajectorySpan;
    x: number;
    y: number;
  } | null>(null);

  // Flatten all spans across all turns
  const allSpans = useMemo(() => {
    return turns.flatMap((turn) =>
      (turn.spans || []).map((span) => ({
        ...span,
        turnNumber: turn.turn,
      }))
    );
  }, [turns]);

  // Determine full domain time range
  const { domainStart, domainEnd, totalDuration } = useMemo(() => {
    if (!allSpans.length) {
      return { domainStart: 0, domainEnd: 1, totalDuration: 1 };
    }
    let minStart = Infinity;
    let maxEnd = -Infinity;
    for (const span of allSpans) {
      const start = span.started_at_ms || 0;
      const end = start + Math.max(1, span.duration_ms || 1);
      if (start < minStart) minStart = start;
      if (end > maxEnd) maxEnd = end;
    }
    if (!isFinite(minStart)) minStart = 0;
    if (!isFinite(maxEnd) || maxEnd <= minStart) maxEnd = minStart + 1000;
    return {
      domainStart: minStart,
      domainEnd: maxEnd,
      totalDuration: Math.max(1, maxEnd - minStart),
    };
  }, [allSpans]);

  // Turn boundary divider lines
  const turnBoundaries = useMemo(() => {
    return turns.map((turn) => {
      const firstSpan = (turn.spans || [])[0];
      const startMs = firstSpan?.started_at_ms ?? domainStart;
      const pct = Math.max(0, Math.min(100, ((startMs - domainStart) / totalDuration) * 100));
      return { turn: turn.turn, pct };
    });
  }, [turns, domainStart, totalDuration]);

  // Active highlighted range is draftRange (while dragging) or committed timeRange
  const activeRange = draftRange ?? timeRange;

  const fractionFromClientX = useCallback(
    (clientX: number): number => {
      if (!trackRef.current) return 0;
      const rect = trackRef.current.getBoundingClientRect();
      return Math.max(0, Math.min(1, (clientX - rect.left) / Math.max(1, rect.width)));
    },
    []
  );

  const onPointerDown = useCallback(
    (e: ReactPointerEvent<HTMLDivElement>) => {
      if (e.button !== 0) return;
      const fraction = fractionFromClientX(e.clientX);
      const pointMs = domainStart + fraction * totalDuration;

      dragRef.current = {
        pointerId: e.pointerId,
        anchorClientX: e.clientX,
        anchorMs: pointMs,
      };

      try {
        e.currentTarget.setPointerCapture(e.pointerId);
      } catch {}

      setDraftRange({ startMs: pointMs, endMs: pointMs });
    },
    [domainStart, totalDuration, fractionFromClientX]
  );

  const onPointerMove = useCallback(
    (e: ReactPointerEvent<HTMLDivElement>) => {
      const fraction = fractionFromClientX(e.clientX);
      setHoverPosition({ fraction });

      if (!dragRef.current || dragRef.current.pointerId !== e.pointerId) return;

      const currentMs = domainStart + fraction * totalDuration;
      const start = Math.min(dragRef.current.anchorMs, currentMs);
      const end = Math.max(dragRef.current.anchorMs, currentMs);
      setDraftRange({ startMs: start, endMs: end });
    },
    [domainStart, totalDuration, fractionFromClientX]
  );

  const onPointerUp = useCallback(
    (e: ReactPointerEvent<HTMLDivElement>) => {
      const drag = dragRef.current;
      if (!drag || drag.pointerId !== e.pointerId) return;

      try {
        e.currentTarget.releasePointerCapture(e.pointerId);
      } catch {}

      const deltaX = Math.abs(e.clientX - drag.anchorClientX);
      dragRef.current = null;

      if (deltaX < 4) {
        // Simple click without drag
        // If clicking on an empty area, clear the active range
        if (draftRange && draftRange.endMs - draftRange.startMs < 5) {
          // Keep existing selection or clear if already selected
          setDraftRange(null);
          return;
        }
      }

      if (draftRange && draftRange.endMs - draftRange.startMs >= 5) {
        onTimeRangeChange({
          startMs: draftRange.startMs,
          endMs: draftRange.endMs,
        });
      }
      setDraftRange(null);
    },
    [draftRange, onTimeRangeChange]
  );

  const onPointerCancel = useCallback(() => {
    dragRef.current = null;
    setDraftRange(null);
    setHoverPosition(null);
  }, []);

  // Keyboard shortcut: Escape to reset selection
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape" && timeRange !== null) {
        onTimeRangeChange(null);
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [timeRange, onTimeRangeChange]);

  if (!allSpans.length) {
    return (
      <div className="relative flex h-[58px] w-full items-center justify-center border-b border-border-subtle bg-surface-1/40 text-[11px] text-muted-foreground">
        No timing telemetry spans recorded yet.
      </div>
    );
  }

  // Calculate range geometry percentages
  let selectionLeftPct = 0;
  let selectionWidthPct = 0;
  if (activeRange) {
    const sStart = Math.max(domainStart, Math.min(domainEnd, activeRange.startMs));
    const sEnd = Math.max(domainStart, Math.min(domainEnd, activeRange.endMs));
    selectionLeftPct = Math.max(0, Math.min(100, ((sStart - domainStart) / totalDuration) * 100));
    const rightPct = Math.max(0, Math.min(100, ((sEnd - domainStart) / totalDuration) * 100));
    selectionWidthPct = Math.max(0.1, rightPct - selectionLeftPct);
  }

  return (
    <div className="relative select-none border-b border-border-subtle bg-surface-1/50 dark:bg-neutral-900/40">
      <div className="grid grid-cols-[48px_minmax(0,1fr)] h-[58px] overflow-hidden">
        {/* Left Lane Labels */}
        <div className="relative border-r border-border-subtle/60 text-[10px] font-semibold tracking-wider text-muted-foreground">
          <span className="absolute top-[8px] right-2 leading-none uppercase">Input</span>
          <span className="absolute top-[23px] right-2 leading-none uppercase">Model</span>
          <span className="absolute top-[38px] right-2 leading-none uppercase">Tools</span>
        </div>

        {/* Track with drag-to-highlight support */}
        <div
          ref={trackRef}
          role="region"
          aria-label="Trajectory Gantt timeline. Drag to select time range, double-click to reset."
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
          onPointerCancel={onPointerCancel}
          onDoubleClick={(e) => {
            e.preventDefault();
            onTimeRangeChange(null);
          }}
          className="relative h-full overflow-hidden cursor-crosshair touch-none"
        >
          {/* Turn Boundaries */}
          {turnBoundaries.map((b, boundaryIndex) => (
            <div
              key={`turn-boundary-${b.turn}-${boundaryIndex}`}
              className="absolute top-0 bottom-0 pointer-events-none border-l border-dashed border-border-subtle/80 z-0"
              style={{ left: `${b.pct}%` }}
            >
              <span className="absolute top-0.5 left-1 text-[8px] font-bold text-muted-foreground/70 tracking-widest uppercase">
                T{b.turn}
              </span>
            </div>
          ))}

          {/* Lane Horizontal Background Guides */}
          <div className="absolute top-[8px] left-0 right-0 h-[10px] bg-primary/5 pointer-events-none" />
          <div className="absolute top-[23px] left-0 right-0 h-[10px] bg-indigo-500/5 pointer-events-none" />
          <div className="absolute top-[38px] left-0 right-0 h-[10px] bg-amber-500/5 pointer-events-none" />

          {/* Spans */}
          {allSpans.map((span, idx) => {
            let topPx = 8;
            if (span.lane === "model") topPx = 23;
            else if (span.lane === "tools") topPx = 38;

            let leftPct = 0;
            let widthPct = 0;

            if (actualDuration) {
              const startOffset = Math.max(0, span.started_at_ms - domainStart);
              leftPct = (startOffset / totalDuration) * 100;
              widthPct = Math.max(0.6, ((Math.max(1, span.duration_ms) / totalDuration) * 100));
            } else {
              // Equal width mode
              const totalItems = allSpans.length;
              const slotWidth = 100 / Math.max(1, totalItems);
              leftPct = idx * slotWidth;
              widthPct = Math.max(0.5, slotWidth * 0.9);
            }

            // Check if span intersects the active selection range
            const spanStart = span.started_at_ms;
            const spanEnd = span.started_at_ms + Math.max(1, span.duration_ms);
            const isIntersecting =
              activeRange === null ||
              (spanStart <= activeRange.endMs && spanEnd >= activeRange.startMs);

            // Styling by kind
            let bgClass = "bg-neutral-500";
            if (span.kind === "user") bgClass = "bg-emerald-500 dark:bg-emerald-400";
            else if (span.kind === "context") bgClass = "bg-cyan-500 dark:bg-cyan-400";
            else if (span.kind === "system") bgClass = "bg-slate-400 dark:bg-slate-500";
            else if (span.kind === "message") bgClass = "bg-indigo-500 dark:bg-indigo-400";
            else if (span.kind === "tool" || span.kind === "subtool") {
              bgClass = span.status === "failed" ? "bg-rose-500" : "bg-amber-500 dark:bg-amber-400";
            }

            const isSelected = selectedRecordId === span.id || selectedRecordId === `rec-${span.id}`;

            return (
              <div
                key={span.id}
                role="button"
                tabIndex={0}
                aria-label={`${span.kind}: ${span.label}`}
                onClick={(e) => {
                  e.stopPropagation();
                  onSelectRecord(span.id);
                }}
                onMouseEnter={(e) => {
                  const rect = e.currentTarget.getBoundingClientRect();
                  setHoveredSpan({
                    span,
                    x: rect.left + rect.width / 2,
                    y: rect.bottom + 4,
                  });
                }}
                onMouseLeave={() => setHoveredSpan(null)}
                className={`absolute h-[10px] rounded-[2px] transition-all cursor-pointer z-10 ${bgClass} ${
                  isSelected
                    ? "ring-2 ring-primary ring-offset-1 z-30 brightness-125"
                    : isIntersecting
                    ? "opacity-95 hover:brightness-125 hover:z-20"
                    : "opacity-20 hover:opacity-70"
                }`}
                style={{
                  top: `${topPx}px`,
                  left: `${leftPct}%`,
                  width: `${widthPct}%`,
                  minWidth: "4px",
                }}
              />
            );
          })}

          {/* Active Highlight Selection Window & Dimmed Outside Masks */}
          {activeRange && (
            <>
              {/* Dimmed Left Mask */}
              <div
                className="absolute top-0 bottom-0 left-0 bg-neutral-950/65 dark:bg-black/75 pointer-events-none z-15 transition-opacity"
                style={{ width: `${selectionLeftPct}%` }}
              />

              {/* Dimmed Right Mask */}
              <div
                className="absolute top-0 bottom-0 right-0 bg-neutral-950/65 dark:bg-black/75 pointer-events-none z-15 transition-opacity"
                style={{ left: `${selectionLeftPct + selectionWidthPct}%` }}
              />

              {/* Center Translucent Highlight Region */}
              <div
                className="absolute top-0 bottom-0 bg-blue-500/15 pointer-events-none z-15 backdrop-brightness-110"
                style={{
                  left: `${selectionLeftPct}%`,
                  width: `${selectionWidthPct}%`,
                }}
              />

              {/* Left Edge Handle Line (matches Neosis visual) */}
              <div
                className="absolute top-0 bottom-0 w-[2.5px] bg-blue-400 dark:bg-blue-400 pointer-events-none z-25 shadow-[0_0_8px_rgba(96,165,250,0.9)]"
                style={{ left: `${selectionLeftPct}%` }}
              />

              {/* Right Edge Handle Line (matches Neosis visual) */}
              <div
                className="absolute top-0 bottom-0 w-[2.5px] bg-blue-400 dark:bg-blue-400 pointer-events-none z-25 shadow-[0_0_8px_rgba(96,165,250,0.9)]"
                style={{ left: `calc(${selectionLeftPct + selectionWidthPct}% - 2.5px)` }}
              />
            </>
          )}

          {/* Hover Cursor Line */}
          {hoverPosition && !activeRange && (
            <div
              className="absolute top-0 bottom-0 w-[1px] bg-blue-400/80 pointer-events-none z-20"
              style={{ left: `${hoverPosition.fraction * 100}%` }}
            />
          )}
        </div>
      </div>

      {/* Hover Floating Tooltip */}
      {hoveredSpan && (
        <div
          className="pointer-events-none fixed z-50 rounded-md border border-border-subtle bg-surface-0/95 px-2.5 py-1.5 shadow-xl backdrop-blur text-[11px] text-foreground dark:bg-neutral-900/95"
          style={{
            left: `${Math.min(window.innerWidth - 240, Math.max(12, hoveredSpan.x - 100))}px`,
            top: `${hoveredSpan.y}px`,
          }}
        >
          <div className="flex items-center gap-1.5 font-bold">
            <span className="uppercase text-[9px] tracking-wider text-muted-foreground">
              {hoveredSpan.span.kind}
            </span>
            <span>{hoveredSpan.span.label}</span>
          </div>
          <div className="mt-0.5 text-[10px] text-muted-foreground">
            {formatClockTime(hoveredSpan.span.started_at_ms)} · {formatDuration(hoveredSpan.span.duration_ms)}
          </div>
          {hoveredSpan.span.ttft_ms !== undefined && hoveredSpan.span.decoding_ms !== undefined && (
            <div className="mt-1 flex items-center gap-2 text-[10px] border-t border-border-subtle pt-1 text-primary">
              <span>TTFT: {hoveredSpan.span.ttft_ms}ms</span>
              <span>·</span>
              <span>Decoding: {hoveredSpan.span.decoding_ms}ms</span>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/**
 * Complete Native DeepSpace Trajectory View.
 */
interface DeepSpaceTrajectoryViewProps {
  conversationId: string | null;
}

export default function DeepSpaceTrajectoryView({
  conversationId,
}: DeepSpaceTrajectoryViewProps) {
  const [data, setData] = useState<TrajectoryPayload | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const [actualDuration, setActualDuration] = useState<boolean>(true);
  const [timeRange, setTimeRange] = useState<TimeRange | null>(null);
  const [allTurnsCollapsed, setAllTurnsCollapsed] = useState<boolean>(false);
  const [allCallsCollapsed, setAllCallsCollapsed] = useState<boolean>(false);
  const [searchQuery, setSearchQuery] = useState<string>("");
  const [selectedRecordId, setSelectedRecordId] = useState<string | null>(null);
  const [expandedRecordIds, setExpandedRecordIds] = useState<Set<string>>(new Set());
  const [collapsedTurnNumbers, setCollapsedTurnNumbers] = useState<Set<number>>(new Set());
  const [copiedId, setCopiedId] = useState<string | null>(null);

  const fetchTrajectory = useCallback(async () => {
    if (!conversationId) return;
    setLoading(true);
    setError(null);
    try {
      const response = await fetchWithAuth(
        `/deepspace/chats/${encodeURIComponent(conversationId)}/trajectory?max_turns=50`,
        { method: "GET" }
      );
      if (!response.ok) {
        throw new Error(`Failed to load trajectory (${response.status})`);
      }
      const json = await response.json();
      setData(json);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load trajectory data.");
    } finally {
      setLoading(false);
    }
  }, [conversationId]);

  useEffect(() => {
    const timer = window.setTimeout(() => void fetchTrajectory(), 0);
    return () => window.clearTimeout(timer);
  }, [fetchTrajectory]);

  // Expand / collapse all turns
  const toggleAllTurns = () => {
    if (!data?.turns) return;
    if (allTurnsCollapsed) {
      setCollapsedTurnNumbers(new Set());
      setAllTurnsCollapsed(false);
    } else {
      setCollapsedTurnNumbers(new Set(data.turns.map((t) => t.turn)));
      setAllTurnsCollapsed(true);
    }
  };

  // Expand / collapse all tool calls
  const toggleAllCalls = () => {
    if (allCallsCollapsed) {
      setExpandedRecordIds(new Set());
      setAllCallsCollapsed(false);
    } else {
      const allRecordIds = (data?.turns || []).flatMap((t) =>
        (t.records || []).map((r) => r.id)
      );
      setExpandedRecordIds(new Set(allRecordIds));
      setAllCallsCollapsed(true);
    }
  };

  const toggleRecordExpansion = (id: string) => {
    setExpandedRecordIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const toggleTurnCollapse = (turnNum: number) => {
    setCollapsedTurnNumbers((prev) => {
      const next = new Set(prev);
      if (next.has(turnNum)) next.delete(turnNum);
      else next.add(turnNum);
      return next;
    });
  };

  const copyToClipboard = async (text: string, id: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopiedId(id);
      setTimeout(() => setCopiedId(null), 2000);
    } catch {}
  };

  // Filter records by BOTH search query and time range intersection
  const query = searchQuery.trim().toLowerCase();
  const filteredTurns = useMemo(() => {
    if (!data?.turns) return [];

    return data.turns
      .map((turn) => {
        const matchingRecords = (turn.records || []).filter((r) => {
          // 1. Time range intersection check
          if (timeRange !== null) {
            const recStart = r.started_at_ms;
            const recEnd = r.started_at_ms + Math.max(1, r.duration_ms);
            const intersects = recStart <= timeRange.endMs && recEnd >= timeRange.startMs;
            if (!intersects) return false;
          }

          // 2. Query search check
          if (query) {
            const titleMatch = r.title.toLowerCase().includes(query);
            const labelMatch = r.label.toLowerCase().includes(query);
            const contentMatch = (r.content || "").toLowerCase().includes(query);
            const argsMatch = JSON.stringify(r.arguments || {}).toLowerCase().includes(query);
            if (!titleMatch && !labelMatch && !contentMatch && !argsMatch) {
              return false;
            }
          }

          return true;
        });

        return {
          ...turn,
          records: matchingRecords,
        };
      })
      .filter((turn) => turn.records && turn.records.length > 0);
  }, [data, query, timeRange]);

  const totalMatchingRecords = useMemo(() => {
    return filteredTurns.reduce((acc, t) => acc + (t.records?.length || 0), 0);
  }, [filteredTurns]);

  const totalToolCount = data?.totals?.tool_count ?? (data?.turns || []).reduce(
    (acc, t) => acc + (t.steps || []).reduce((sAcc, s) => sAcc + (s.tools?.length || 0), 0),
    0
  );

  return (
    <div className="flex h-full min-h-0 w-full flex-col bg-surface-0 font-sans text-foreground dark:bg-neutral-950">
      {/* 1. Chrome-Network-style Overview Timeline with drag-to-highlight */}
      <DeepSpaceTimeline
        turns={data?.turns || []}
        actualDuration={actualDuration}
        timeRange={timeRange}
        onTimeRangeChange={setTimeRange}
        selectedRecordId={selectedRecordId}
        onSelectRecord={(recId) => {
          setSelectedRecordId(recId);
          toggleRecordExpansion(recId);
        }}
      />

      {/* 2. Trajectory Toolbar */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border-subtle bg-surface-1/70 px-3 py-1.5 text-xs backdrop-blur dark:bg-neutral-900/70">
        <div className="flex items-center gap-1.5">
          {/* Duration mode toggle */}
          <button
            type="button"
            onClick={() => setActualDuration(!actualDuration)}
            aria-pressed={actualDuration}
            title={actualDuration ? "Switch to Equal Width Spans" : "Switch to Actual Duration Spans"}
            className={`inline-flex items-center gap-1.5 rounded-md border px-2 py-1 text-[11px] font-semibold transition ${
              actualDuration
                ? "border-primary/50 bg-primary/10 text-primary shadow-xs"
                : "border-border-subtle bg-surface-2/60 text-muted-foreground hover:bg-surface-2 hover:text-foreground"
            }`}
          >
            <Clock size={12} className="shrink-0" />
            <span>Duration</span>
          </button>

          {/* Expand/Collapse Turns */}
          <button
            type="button"
            onClick={toggleAllTurns}
            className="inline-flex items-center gap-1 rounded-md border border-border-subtle bg-surface-2/60 px-2 py-1 text-[11px] font-semibold text-muted-foreground hover:bg-surface-2 hover:text-foreground transition"
          >
            <span className="font-mono text-[10px]">{allTurnsCollapsed ? "⊞" : "⊟"}</span>
            <span>Turns</span>
          </button>

          {/* Expand/Collapse Calls */}
          <button
            type="button"
            onClick={toggleAllCalls}
            className="inline-flex items-center gap-1 rounded-md border border-border-subtle bg-surface-2/60 px-2 py-1 text-[11px] font-semibold text-muted-foreground hover:bg-surface-2 hover:text-foreground transition"
          >
            <span className="font-mono text-[10px]">{allCallsCollapsed ? "⊞" : "⊟"}</span>
            <span>Calls</span>
          </button>

          {/* Refresh button */}
          <button
            type="button"
            onClick={fetchTrajectory}
            disabled={loading}
            aria-label="Refresh trajectory"
            className="rounded-md border border-border-subtle bg-surface-2/60 p-1 text-muted-foreground hover:bg-surface-2 hover:text-foreground transition"
          >
            <RefreshCw size={12} className={loading ? "animate-spin text-primary" : ""} />
          </button>

          {/* Active Time Selection Filter Chip */}
          {timeRange !== null && (
            <div className="inline-flex items-center gap-1.5 rounded-md border border-blue-400/40 bg-blue-500/10 px-2 py-0.5 text-[11px] font-medium text-blue-400 shadow-xs">
              <Filter size={11} className="shrink-0" />
              <span>
                Selection: {formatDuration(timeRange.endMs - timeRange.startMs)} ({totalMatchingRecords} items)
              </span>
              <button
                type="button"
                onClick={() => setTimeRange(null)}
                aria-label="Clear time range filter"
                title="Clear time filter (or double-click timeline / press Esc)"
                className="hover:bg-blue-400/20 rounded p-0.5 text-blue-400 transition"
              >
                <X size={11} />
              </button>
            </div>
          )}

          {/* Summary Badges */}
          <div className="hidden sm:flex items-center gap-2 pl-2 text-[10px] text-muted-foreground border-l border-border-subtle/80">
            <span>Turns: <strong className="text-foreground">{data?.turns?.length || 0}</strong></span>
            <span>·</span>
            <span>Tools: <strong className="text-foreground">{totalToolCount}</strong></span>
            {data?.totals?.duration_ms ? (
              <>
                <span>·</span>
                <span>Total: <strong className="text-foreground">{formatDuration(data.totals.duration_ms)}</strong></span>
              </>
            ) : null}
          </div>
        </div>

        {/* Live Search */}
        <div className="relative flex items-center">
          <Search size={12} className="absolute left-2.5 text-muted-foreground pointer-events-none" />
          <input
            type="search"
            placeholder="Search trajectory..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            className="h-6 w-36 sm:w-52 rounded-md border border-border-subtle bg-surface-0 pl-7 pr-2 text-[11px] text-foreground placeholder:text-muted-foreground focus:border-primary focus:outline-none focus:ring-1 focus:ring-primary/40 dark:bg-neutral-900"
          />
        </div>
      </div>

      {/* 3. Event Ledger Body */}
      <div className="custom-scrollbar min-h-0 flex-1 overflow-y-auto p-3">
        {loading && !data && (
          <div className="flex h-48 items-center justify-center gap-2 text-xs text-muted-foreground">
            <RefreshCw size={14} className="animate-spin text-primary" />
            <span>Loading trajectory events...</span>
          </div>
        )}

        {error && (
          <div className="my-4 rounded-lg border border-rose-300/20 bg-rose-500/10 p-3 text-xs text-rose-200">
            {error}
          </div>
        )}

        {!loading && !data?.turns?.length && (
          <div className="flex h-48 flex-col items-center justify-center gap-1 text-center">
            <Clock size={24} className="text-muted-foreground/40" />
            <p className="text-xs font-semibold text-foreground">No Trajectory Recorded</p>
            <p className="text-[11px] text-muted-foreground">
              Send a prompt in DeepSpace to inspect real-time turns, tools, and execution spans.
            </p>
          </div>
        )}

        {/* Empty state when time range filters out everything */}
        {!loading && data?.turns?.length && filteredTurns.length === 0 && (
          <div className="flex h-48 flex-col items-center justify-center gap-2 text-center">
            <Filter size={20} className="text-muted-foreground/50" />
            <p className="text-xs font-semibold text-foreground">No Events In Selected Time Range</p>
            <p className="text-[11px] text-muted-foreground">
              Drag across a different region of the timeline, or clear the selection.
            </p>
            <button
              type="button"
              onClick={() => {
                setTimeRange(null);
                setSearchQuery("");
              }}
              className="mt-1 inline-flex items-center gap-1.5 rounded-md border border-border-subtle bg-surface-2 px-2.5 py-1 text-[11px] font-semibold text-foreground hover:bg-surface-1 transition"
            >
              Reset Filters
            </button>
          </div>
        )}

        {filteredTurns.map((turn) => {
          const isTurnCollapsed = collapsedTurnNumbers.has(turn.turn);
          const records = turn.records || [];

          return (
            <div key={turn.turn} className="mb-4">
              {/* Sticky Turn Header Bar with Metric Headers */}
              <div className="sticky top-0 z-10 flex items-center justify-between rounded-lg border border-border-subtle bg-surface-2/95 px-3 py-1.5 backdrop-blur shadow-xs dark:bg-neutral-900/95">
                <button
                  type="button"
                  onClick={() => toggleTurnCollapse(turn.turn)}
                  className="flex items-center gap-2 font-semibold text-xs text-foreground hover:text-primary transition"
                >
                  {isTurnCollapsed ? <ChevronRight size={14} /> : <ChevronDown size={14} />}
                  <span>Turn {turn.turn}</span>
                  <span className="rounded-full bg-surface-1 px-1.5 py-0.2 font-mono text-[10px] text-muted-foreground border border-border-subtle">
                    {formatDuration(turn.duration_ms)}
                  </span>
                </button>

                {/* Right Column Metric Labels */}
                <div className="hidden sm:flex items-center gap-6 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
                  <span className="w-12 text-right">Input</span>
                  <span className="w-12 text-right">Output</span>
                  <span className="w-12 text-right">Think</span>
                  <span className="w-16 text-right">Time</span>
                </div>
              </div>

              {/* Turn Records List with Connecting Thread Line */}
              {!isTurnCollapsed && (
                <div className="relative mt-2 pl-4 ml-3 border-l-2 border-border-subtle/70 space-y-2">
                  {records.map((record, rIdx) => {
                    const isExpanded = expandedRecordIds.has(record.id);
                    const isTool = record.kind === "tool" || record.kind === "subtool";

                    // Badge Color Mapping matching Neosis
                    let badgeBg = "bg-slate-500/15 text-slate-300 border-slate-500/30";
                    if (record.kind === "user") {
                      badgeBg = "bg-emerald-500/15 text-emerald-400 border-emerald-500/30";
                    } else if (record.kind === "context") {
                      badgeBg = "bg-cyan-500/15 text-cyan-400 border-cyan-500/30";
                    } else if (record.kind === "message") {
                      badgeBg = "bg-indigo-500/15 text-indigo-400 border-indigo-500/30";
                    } else if (isTool) {
                      badgeBg = "bg-amber-500/15 text-amber-400 border-amber-500/30";
                    }

                    return (
                      <div
                        key={record.id}
                        id={record.id}
                        className={`group relative rounded-lg border transition ${
                          selectedRecordId === record.id
                            ? "border-primary/80 bg-primary/5 shadow-xs"
                            : "border-border-subtle/80 bg-surface-1/40 hover:bg-surface-1/70 dark:bg-neutral-900/40 hover:border-border-subtle"
                        }`}
                      >
                        {/* Connecting Pin on the Thread Line */}
                        <div
                          className={`absolute -left-[21px] top-3 h-2.5 w-2.5 rounded-full border-2 border-surface-0 transition ${
                            record.status === "failed"
                              ? "bg-rose-500"
                              : record.kind === "user"
                              ? "bg-emerald-500"
                              : record.kind === "message"
                              ? "bg-indigo-500"
                              : isTool
                              ? "bg-amber-500"
                              : "bg-slate-400"
                          }`}
                        />

                        {/* Cell Row */}
                        <div
                          role="button"
                          tabIndex={0}
                          onClick={() => toggleRecordExpansion(record.id)}
                          className="flex items-center justify-between gap-3 px-3 py-2 cursor-pointer"
                        >
                          <div className="flex items-center gap-2.5 min-w-0">
                            {/* Index */}
                            <span className="font-mono text-[10px] text-muted-foreground/70 shrink-0">
                              #{rIdx + 1}
                            </span>

                            {/* Kind Tag Chip */}
                            <span
                              className={`shrink-0 inline-flex items-center justify-center rounded-[4px] border px-1.5 py-0.5 text-[9px] font-bold tracking-wider ${badgeBg}`}
                            >
                              {record.label}
                            </span>

                            {/* Title / Description */}
                            <span className="text-[11px] font-semibold text-foreground truncate">
                              {record.title}
                            </span>

                            {/* Content Snippet */}
                            {record.content && (
                              <span className="hidden md:inline text-[10px] text-muted-foreground truncate max-w-sm">
                                — {record.content}
                              </span>
                            )}
                          </div>

                          {/* Right Metrics & Time */}
                          <div className="flex items-center gap-6 shrink-0 font-mono text-[11px] text-muted-foreground">
                            {record.assistant_metrics ? (
                              <>
                                <span className="w-12 text-right">
                                  {record.assistant_metrics.input_tokens ?? "—"}
                                </span>
                                <span className="w-12 text-right">
                                  {record.assistant_metrics.output_tokens ?? record.assistant_metrics.tokens ?? "—"}
                                </span>
                                <span className="w-12 text-right">
                                  {record.assistant_metrics.think_tokens ?? "—"}
                                </span>
                              </>
                            ) : (
                              <>
                                <span className="w-12 text-right text-muted-foreground/40">—</span>
                                <span className="w-12 text-right text-muted-foreground/40">—</span>
                                <span className="w-12 text-right text-muted-foreground/40">—</span>
                              </>
                            )}

                            <span className="w-16 text-right font-semibold text-foreground">
                              {formatDuration(record.duration_ms)}
                            </span>

                            <div className="text-muted-foreground/70">
                              {isExpanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                            </div>
                          </div>
                        </div>

                        {/* Inline Expandable Drawer for Details/JSON */}
                        {isExpanded && (
                          <div className="border-t border-border-subtle/80 bg-surface-0/60 p-3 text-xs dark:bg-neutral-950/60">
                            <div className="flex items-center justify-between pb-2 mb-2 border-b border-border-subtle/50 text-[10px] text-muted-foreground">
                              <div className="flex items-center gap-3">
                                <span>Clock: <strong className="text-foreground">{record.timestamp}</strong></span>
                                <span>Status: <strong className={record.status === "failed" ? "text-rose-400" : "text-emerald-400"}>{record.status}</strong></span>
                              </div>
                              <button
                                type="button"
                                onClick={(e) => {
                                  e.stopPropagation();
                                  copyToClipboard(
                                    JSON.stringify({ ...record }, null, 2),
                                    record.id
                                  );
                                }}
                                className="inline-flex items-center gap-1 rounded bg-surface-2 px-1.5 py-0.5 text-[10px] hover:text-foreground transition"
                              >
                                {copiedId === record.id ? (
                                  <>
                                    <Check size={10} className="text-emerald-400" />
                                    <span>Copied</span>
                                  </>
                                ) : (
                                  <>
                                    <Copy size={10} />
                                    <span>Copy JSON</span>
                                  </>
                                )}
                              </button>
                            </div>

                            {/* Full text content if user/assistant */}
                            {record.content && (
                              <div className="mb-2">
                                <span className="text-[10px] font-bold uppercase tracking-wider text-muted-foreground">Content:</span>
                                <div className="mt-1 max-h-48 overflow-y-auto whitespace-pre-wrap rounded bg-surface-1 p-2 font-mono text-[11px] text-foreground dark:bg-neutral-900 border border-border-subtle">
                                  {record.content}
                                </div>
                              </div>
                            )}

                            {/* Arguments */}
                            {record.arguments && Object.keys(record.arguments).length > 0 && (
                              <div className="mb-2">
                                <span className="text-[10px] font-bold uppercase tracking-wider text-muted-foreground">Parameters:</span>
                                <pre className="mt-1 max-h-48 overflow-y-auto rounded bg-surface-1 p-2 font-mono text-[10px] text-foreground dark:bg-neutral-900 border border-border-subtle">
                                  {JSON.stringify(record.arguments, null, 2)}
                                </pre>
                              </div>
                            )}

                            {/* Result */}
                            {record.result !== undefined && (
                              <div>
                                <span className="text-[10px] font-bold uppercase tracking-wider text-muted-foreground">Result:</span>
                                <pre className="mt-1 max-h-48 overflow-y-auto rounded bg-surface-1 p-2 font-mono text-[10px] text-foreground dark:bg-neutral-900 border border-border-subtle">
                                  {typeof record.result === "string" ? record.result : JSON.stringify(record.result, null, 2)}
                                </pre>
                              </div>
                            )}
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
