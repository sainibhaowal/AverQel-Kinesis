"use client";

import { AverQelLoader } from "@/app/components/ui/AverQelLoading";

interface StreamingTypingIndicatorProps {
  phase?: "searching" | "grounding" | "answering";
}

function labelForPhase(phase?: StreamingTypingIndicatorProps["phase"]) {
  switch (phase) {
    case "searching":
      return "Searching evidence";
    case "grounding":
      return "Grounding answer";
    case "answering":
    default:
      return "Streaming response";
  }
}

function helperForPhase(phase?: StreamingTypingIndicatorProps["phase"]) {
  switch (phase) {
    case "searching":
      return "Scanning the workspace for grounded signals.";
    case "grounding":
      return "Cross-checking evidence and assembling the final path.";
    case "answering":
    default:
      return "Writing the response as validated content arrives.";
  }
}

const PHASE_ACCENT = {
  searching: "from-primary/16 to-primary/8 border-primary/18 text-primary",
  grounding:
    "from-emerald-500/16 to-primary/8 border-emerald-500/18 text-emerald-800 dark:text-emerald-50/86",
  answering:
    "from-violet-500/14 to-primary/8 border-violet-500/18 text-violet-800 dark:text-violet-50/86",
} as const;

function PhaseIcon({ phase }: { phase: StreamingTypingIndicatorProps["phase"] }) {
  const color =
    phase === "searching"
      ? "text-primary"
      : phase === "grounding"
        ? "text-emerald-500"
        : "text-violet-500";
  return <AverQelLoader size={20} className={color} />;
}

export default function StreamingTypingIndicator({
  phase = "answering",
}: StreamingTypingIndicatorProps) {
  return (
    <div className={`rounded-[1.35rem] border bg-gradient-to-r px-4 py-3.5 ${PHASE_ACCENT[phase]}`}>
      <div className="theme-chip inline-flex items-center gap-2 rounded-full px-3 py-1.5 text-[10px] font-bold tracking-[0.22em] uppercase">
        <PhaseIcon phase={phase} />
        {labelForPhase(phase)}
      </div>
      <p className="text-foreground/66 mt-2.5 text-sm leading-6">{helperForPhase(phase)}</p>
    </div>
  );
}
