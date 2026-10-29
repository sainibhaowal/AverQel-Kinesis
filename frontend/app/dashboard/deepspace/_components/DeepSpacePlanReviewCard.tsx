"use client";

import { useState } from "react";
import { motion } from "framer-motion";
import { Check, ClipboardList, MessageSquareText, X } from "lucide-react";

export interface DeepSpacePlanReviewCardProps {
  /** The plan's title. */
  title: string;
  /** The plan's Markdown body. */
  content: string;
  /** The plan's identity. */
  planId: string;
  /** Whether the card is busy with a decision. */
  busy?: boolean;
  /** Approve the plan and start the work. */
  onApprove: (planId: string) => Promise<void> | void;
  /** Ask for changes, optionally with the user's feedback. */
  onRequestChanges: (planId: string, feedback: string) => Promise<void> | void;
  /** Dismiss the card without deciding. */
  onDismiss?: (planId: string) => void;
}

/**
 * The plan review card.
 *
 * A plan the agent proposed is shown here with its body and the two
 * decisions the user can make: approve it, which starts the work, or ask
 * for changes, which sends the plan back to the agent.
 */
export default function DeepSpacePlanReviewCard({
  title,
  content,
  planId,
  busy = false,
  onApprove,
  onRequestChanges,
  onDismiss,
}: DeepSpacePlanReviewCardProps) {
  const [feedbackOpen, setFeedbackOpen] = useState(false);
  const [feedback, setFeedback] = useState("");
  const [error, setError] = useState<string | null>(null);

  const run = async (action: () => Promise<void> | void) => {
    if (busy) return;
    setError(null);
    try {
      await action();
    } catch {
      setError("The plan decision could not be sent. Please try again.");
    }
  };

  const requestChanges = () =>
    run(async () => {
      await onRequestChanges(planId, feedback.trim());
      setFeedback("");
      setFeedbackOpen(false);
    });

  return (
    <motion.section
      initial={{ opacity: 0, y: 14, scale: 0.985 }}
      animate={{ opacity: 1, y: 0, scale: 1 }}
      transition={{ duration: 0.24, ease: "easeOut" }}
      aria-label="Plan for review"
      className="deepspace-plan-card relative mt-5 w-full max-w-4xl overflow-hidden rounded-2xl border border-white/10 bg-[linear-gradient(145deg,rgba(16,25,27,.96),rgba(12,19,21,.98))] p-5 shadow-[0_8px_24px_-18px_rgba(0,0,0,.75)] sm:p-6"
    >
      <div className="relative flex items-start gap-3">
        <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border border-emerald-200/15 bg-emerald-300/[0.07] text-emerald-100/85">
          <ClipboardList size={17} />
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="deepspace-plan-title text-[10px] font-semibold tracking-[0.12em] text-emerald-100/70 uppercase">
              Plan for review
            </span>
            <span className="deepspace-plan-status rounded-full border border-emerald-200/15 bg-emerald-200/[0.06] px-2 py-0.5 text-[10px] font-medium text-emerald-100/65">
              Awaiting your decision
            </span>
          </div>
          <h3 className="deepspace-plan-name mt-2 text-[15px] leading-6 font-semibold text-white/95">
            {title || "Proposed plan"}
          </h3>
        </div>
      </div>

      <div className="deepspace-plan-body relative mt-4 max-h-72 overflow-auto rounded-xl border border-white/10 bg-white/[0.025] px-4 py-3 text-sm leading-6 whitespace-pre-wrap text-white/80">
        {content}
      </div>

      <p className="deepspace-plan-hint mt-3 text-xs leading-5 text-white/55">
        Approve the plan to start the work, or ask for changes and the agent will revise it.
      </p>

      {feedbackOpen ? (
        <div className="deepspace-plan-feedback relative mt-3">
          <textarea
            value={feedback}
            onChange={(event) => setFeedback(event.target.value)}
            rows={3}
            placeholder="What should change in the plan?"
            aria-label="Feedback for the plan"
            className="w-full resize-y rounded-xl border border-white/10 bg-white/[0.03] px-3 py-2 text-sm text-white/85 placeholder:text-white/35 focus-visible:ring-2 focus-visible:ring-emerald-200/60 focus-visible:outline-none"
          />
          <div className="mt-2 flex flex-wrap gap-2">
            <button
              type="button"
              disabled={busy}
              onClick={() => void requestChanges()}
              className="inline-flex items-center gap-1.5 rounded-lg border border-white/12 bg-white/[0.06] px-3 py-1.5 text-xs font-medium text-white/85 transition-colors hover:bg-white/[0.1] focus-visible:ring-2 focus-visible:ring-emerald-200/60 focus-visible:outline-none disabled:cursor-wait disabled:opacity-50"
            >
              <MessageSquareText size={13} /> Send feedback
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() => {
                setFeedbackOpen(false);
                setFeedback("");
              }}
              className="rounded-lg px-3 py-1.5 text-xs font-medium text-white/60 transition-colors hover:text-white/85 focus-visible:ring-2 focus-visible:ring-emerald-200/60 focus-visible:outline-none disabled:cursor-wait disabled:opacity-50"
            >
              Cancel
            </button>
          </div>
        </div>
      ) : null}

      {error ? (
        <p role="alert" className="mt-3 text-xs text-red-300">
          {error}
        </p>
      ) : null}

      <div className="mt-4 flex flex-wrap gap-2">
        <button
          type="button"
          disabled={busy}
          onClick={() => void run(() => onApprove(planId))}
          className="inline-flex items-center gap-1.5 rounded-lg border border-emerald-200/25 bg-emerald-300/[0.1] px-3 py-1.5 text-xs font-semibold text-emerald-100 transition-colors hover:bg-emerald-300/[0.16] focus-visible:ring-2 focus-visible:ring-emerald-200/60 focus-visible:outline-none disabled:cursor-wait disabled:opacity-50"
        >
          <Check size={13} /> Approve plan
        </button>
        <button
          type="button"
          disabled={busy}
          onClick={() => setFeedbackOpen((open) => !open)}
          className="inline-flex items-center gap-1.5 rounded-lg border border-white/12 bg-white/[0.05] px-3 py-1.5 text-xs font-medium text-white/80 transition-colors hover:bg-white/[0.1] focus-visible:ring-2 focus-visible:ring-emerald-200/60 focus-visible:outline-none disabled:cursor-wait disabled:opacity-50"
        >
          <MessageSquareText size={13} /> Request changes
        </button>
        {onDismiss ? (
          <button
            type="button"
            disabled={busy}
            onClick={() => onDismiss(planId)}
            aria-label="Dismiss the plan card"
            className="rounded-lg px-3 py-1.5 text-xs font-medium text-white/50 transition-colors hover:text-white/80 focus-visible:ring-2 focus-visible:ring-emerald-200/60 focus-visible:outline-none disabled:cursor-wait disabled:opacity-50"
          >
            <X size={13} />
          </button>
        ) : null}
      </div>
    </motion.section>
  );
}
