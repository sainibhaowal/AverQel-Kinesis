"use client";

import { memo, useId, useMemo, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import {
  MessageCircleQuestion,
  ChevronRight,
  Edit3,
  ArrowUp,
  LoaderCircle,
  Info,
} from "lucide-react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

export interface ParsedQuestionOption {
  id: string;
  badge: string;
  label: string;
  title: string;
  description: string;
  value: string;
}

export interface ParsedQuestionContent {
  prompt: string;
  options: ParsedQuestionOption[];
  footer: string;
}

/**
 * Parses question text and explicit options into structured prompt, interactive choices,
 * and optional footer notes.
 */
export function parseQuestionContent(
  questionText: string,
  passedOptions: string[] = [],
): ParsedQuestionContent {
  if (passedOptions && passedOptions.length > 0) {
    return {
      prompt: questionText,
      options: passedOptions.map((opt, i) => {
        let title = opt;
        let description = "";
        const m = opt.match(/^([\s\S]+?)(?:\s*—\s*|\s*–\s*|\s*-\s*|\s*:\s*)([\s\S]+)$/);
        if (m) {
          title = m[1].trim();
          description = m[2].trim();
        }
        return {
          id: `opt-${i}`,
          badge: String.fromCharCode(65 + (i % 26)),
          label: opt,
          title,
          description,
          value: opt,
        };
      }),
      footer: "",
    };
  }

  const text = (questionText || "").trim();
  if (!text) return { prompt: "", options: [], footer: "" };

  // Detect embedded choices (e.g. - **(a) ...** or - (a) ... or 1. ... or inline items: " - **(a)")
  const bulletOptionRegex =
    /(?:^|[\n\r]+|\s+[-*•]\s+)(?:\*\*)?\(?([a-zA-Z0-9]{1,2})\)?[\.:\)]\s*(?:\*\*)?\s*/g;

  const matches: Array<{
    index: number;
    fullLength: number;
    badge: string;
    matchStart: number;
  }> = [];

  let m: RegExpExecArray | null;
  while ((m = bulletOptionRegex.exec(text)) !== null) {
    matches.push({
      index: m.index,
      fullLength: m[0].length,
      badge: m[1],
      matchStart: m.index + (m[0].startsWith(" ") ? 1 : 0),
    });
  }

  if (matches.length >= 2) {
    const isAlpha = matches.every((match) => /^[a-zA-Z]$/.test(match.badge));
    const isNumeric = matches.every((match) => /^[0-9]+$/.test(match.badge));

    const validMatches: typeof matches = [];
    let expectedChar = matches[0].badge.toLowerCase().charCodeAt(0);
    let expectedNum = parseInt(matches[0].badge, 10);

    for (let i = 0; i < matches.length; i++) {
      const cur = matches[i];
      if (isAlpha) {
        const code = cur.badge.toLowerCase().charCodeAt(0);
        if (i === 0 || code === expectedChar) {
          validMatches.push(cur);
          expectedChar = code + 1;
        } else {
          break;
        }
      } else if (isNumeric) {
        const num = parseInt(cur.badge, 10);
        if (i === 0 || num === expectedNum) {
          validMatches.push(cur);
          expectedNum = num + 1;
        } else {
          break;
        }
      } else {
        validMatches.push(cur);
      }
    }

    if (validMatches.length >= 2) {
      const prompt = text.substring(0, validMatches[0].matchStart).trim();
      const options: ParsedQuestionOption[] = [];

      for (let i = 0; i < validMatches.length; i++) {
        const cur = validMatches[i];
        const nextStart =
          i + 1 < validMatches.length ? validMatches[i + 1].matchStart : text.length;

        let segment = text.substring(cur.index + cur.fullLength, nextStart).trim();
        let itemFooter = "";

        if (i === validMatches.length - 1) {
          const footerSplit = segment.match(
            /^([\s\S]*?)(\s+(?:One caveat|Please note|Note:|Pick a letter|Let me know|One note)[\s\S]*)$/,
          );
          if (footerSplit) {
            segment = footerSplit[1].trim();
            itemFooter = footerSplit[2].trim();
          }
        }

        segment = segment.replace(/^[-*•]\s*/, "").replace(/[-*•]\s*$/, "").trim();

        let title = segment;
        let description = "";

        const splitMatch = segment.match(
          /^([\s\S]+?)(?:\*\*|\*|\s*—\s*|\s*–\s*|\s*-\s*|\s*:\s*)([\s\S]+)$/,
        );
        if (splitMatch) {
          title = splitMatch[1].replace(/^\*\*|\*\*$/g, "").replace(/\*+/g, "").trim();
          description = splitMatch[2].replace(/^[—–\-\:\s]+/, "").replace(/^\*\*|\*\*$/g, "").trim();
        } else {
          title = segment.replace(/^\*\*|\*\*$/g, "").replace(/\*+/g, "").trim();
        }

        options.push({
          id: `opt-${cur.badge.toUpperCase()}`,
          badge: cur.badge.toUpperCase(),
          label: segment.replace(/\*\*/g, "").trim(),
          title,
          description,
          value: `(${cur.badge.toLowerCase()}) ${title}`,
        });

        if (itemFooter) {
          return { prompt, options, footer: itemFooter };
        }
      }

      return { prompt, options, footer: "" };
    }
  }

  // Also handle standard markdown bullet lines: "Choose:\n- Alpha\n- Beta"
  const lines = text.split(/\r?\n/);
  const bulletLines: string[] = [];
  let startLine = -1;
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i].trim();
    if (/^[-*•]\s+(.+)/.test(line)) {
      if (startLine === -1) startLine = i;
      bulletLines.push(line.replace(/^[-*•]\s+/, ""));
    } else if (bulletLines.length > 0 && line) {
      break;
    }
  }

  if (bulletLines.length >= 2) {
    const prompt = lines.slice(0, startLine).join("\n").trim();
    const footer = lines.slice(startLine + bulletLines.length).join("\n").trim();
    return {
      prompt: prompt || text,
      options: bulletLines.map((opt, i) => {
        let title = opt;
        let description = "";
        const m = opt.match(/^([\s\S]+?)(?:\s*—\s*|\s*–\s*|\s*-\s*|\s*:\s*)([\s\S]+)$/);
        if (m) {
          title = m[1].replace(/^\*\*|\*\*$/g, "").trim();
          description = m[2].replace(/^\*\*|\*\*$/g, "").trim();
        }
        return {
          id: `opt-${i}`,
          badge: String.fromCharCode(65 + (i % 26)),
          label: opt,
          title,
          description,
          value: opt,
        };
      }),
      footer,
    };
  }

  return { prompt: text, options: [], footer: "" };
}

const questionMarkdownComponents: Components = {
  p: ({ children }) => (
    <p className="deepspace-question-prompt text-[14.5px] sm:text-[15px] leading-relaxed text-slate-100 font-normal tracking-[-0.01em]">
      {children}
    </p>
  ),
  strong: ({ children }) => (
    <strong className="font-semibold text-white bg-gradient-to-r from-cyan-200 via-white to-cyan-100 bg-clip-text text-transparent drop-shadow-[0_0_8px_rgba(6,182,212,0.25)]">
      {children}
    </strong>
  ),
  code: ({ children }) => (
    <code className="rounded-md border border-cyan-400/25 bg-cyan-950/70 px-1.5 py-0.5 font-mono text-[13px] text-cyan-200 shadow-[0_0_8px_rgba(6,182,212,0.15)]">
      {children}
    </code>
  ),
  ul: ({ children }) => (
    <ul className="my-2 space-y-1.5 pl-4 text-slate-200 list-disc marker:text-cyan-400">
      {children}
    </ul>
  ),
  ol: ({ children }) => (
    <ol className="my-2 space-y-1.5 pl-4 text-slate-200 list-decimal marker:text-cyan-400 font-mono text-sm">
      {children}
    </ol>
  ),
  li: ({ children }) => <li className="text-[14px] leading-relaxed">{children}</li>,
  a: ({ href, children }) => (
    <a
      href={href}
      target="_blank"
      rel="noreferrer noopener"
      className="text-cyan-300 underline decoration-cyan-400/40 hover:text-cyan-200 hover:decoration-cyan-300 transition-colors"
    >
      {children}
    </a>
  ),
};

const inlineMarkdownComponents: Components = {
  p: ({ children }) => <>{children}</>,
  strong: ({ children }) => <strong className="font-semibold text-slate-100">{children}</strong>,
  code: ({ children }) => (
    <code className="rounded border border-cyan-400/20 bg-cyan-950/50 px-1 py-0.5 font-mono text-[11.5px] text-cyan-200">
      {children}
    </code>
  ),
};

export interface DeepSpaceQuestionCardProps {
  question: string;
  options: string[];
  onSubmit: (answer: string) => Promise<void>;
}

export const DeepSpaceQuestionCard = memo(function DeepSpaceQuestionCard({
  question,
  options,
  onSubmit,
}: DeepSpaceQuestionCardProps) {
  const parsed = useMemo(
    () => parseQuestionContent(question, options),
    [question, options],
  );

  const [customAnswer, setCustomAnswer] = useState("");
  const [otherOpen, setOtherOpen] = useState(parsed.options.length === 0);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const customAnswerId = useId();

  const submit = async (answer: string) => {
    const normalized = answer.trim();
    if (!normalized || submitting) return;
    setSubmitting(true);
    setSubmitError(null);
    try {
      await onSubmit(normalized);
    } catch {
      setSubmitError("The answer could not be sent. Please try again.");
      setSubmitting(false);
    }
  };

  return (
    <motion.section
      initial={{ opacity: 0, y: 14, scale: 0.985 }}
      animate={{ opacity: 1, y: 0, scale: 1 }}
      transition={{ duration: 0.24, ease: "easeOut" }}
      aria-label="DeepSpace question"
      className="deepspace-question-card relative mt-5 w-full max-w-4xl overflow-hidden rounded-2xl border border-cyan-500/25 bg-[linear-gradient(155deg,rgba(15,23,32,0.98),rgba(10,16,23,0.99))] p-5 shadow-[0_16px_48px_-16px_rgba(0,0,0,0.85),0_0_32px_-8px_rgba(6,182,212,0.12)] backdrop-blur-xl sm:p-6"
    >
      {/* Top accent glow line and ambient light */}
      <div className="pointer-events-none absolute top-0 left-0 right-0 h-[1.5px] bg-gradient-to-r from-transparent via-cyan-400/60 to-transparent" />
      <div className="pointer-events-none absolute -top-24 left-1/2 h-36 w-96 -translate-x-1/2 rounded-full bg-cyan-500/10 blur-3xl" />

      {/* Header with jewel icon and live status badge */}
      <div className="relative flex items-start gap-3.5">
        <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl border border-cyan-400/30 bg-gradient-to-br from-cyan-400/20 via-cyan-500/10 to-blue-600/20 text-cyan-200 shadow-[0_0_16px_-2px_rgba(6,182,212,0.35)]">
          <MessageCircleQuestion size={19} />
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="deepspace-question-title text-[11px] font-bold tracking-[0.14em] text-cyan-300 uppercase font-mono">
              Your input is needed
            </span>
            <span className="deepspace-question-status flex items-center gap-1.5 rounded-full border border-cyan-400/30 bg-cyan-950/60 px-2.5 py-0.5 text-[11px] font-medium text-cyan-200 shadow-[0_0_10px_rgba(6,182,212,0.15)]">
              <span className="relative flex h-2 w-2">
                <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-cyan-400 opacity-75" />
                <span className="relative inline-flex h-2 w-2 rounded-full bg-cyan-400 shadow-[0_0_6px_#22d3ee]" />
              </span>
              Waiting for you
            </span>
          </div>

          {/* Formatted Markdown Question Prompt */}
          <div className="mt-3">
            <ReactMarkdown
              remarkPlugins={[remarkGfm]}
              components={questionMarkdownComponents}
            >
              {parsed.prompt}
            </ReactMarkdown>
          </div>

          <p className="deepspace-question-hint mt-2 text-xs leading-5 text-slate-400/80">
            Choose an answer to send it and continue the task.
          </p>
        </div>
      </div>

      {/* Interactive Options Cards */}
      {parsed.options.length > 0 ? (
        <div className="deepspace-question-options relative mt-4 space-y-2.5">
          {parsed.options.map((option, index) => (
            <motion.button
              key={option.id + "-" + index}
              type="button"
              disabled={submitting}
              onClick={() => void submit(option.value)}
              whileTap={{ scale: 0.995 }}
              aria-label={index === 0 ? "Recommended: " + option.value : "Choose: " + option.value}
              className="deepspace-question-option group relative flex min-h-[56px] w-full items-start gap-3.5 rounded-xl border border-white/10 bg-white/[0.03] p-3.5 text-left transition-all duration-200 hover:border-cyan-400/40 hover:bg-gradient-to-r hover:from-cyan-500/[0.08] hover:to-blue-500/[0.03] hover:shadow-[0_0_24px_-6px_rgba(6,182,212,0.2)] focus-visible:z-10 focus-visible:ring-2 focus-visible:ring-cyan-400/60 focus-visible:outline-none focus-visible:ring-inset disabled:cursor-wait disabled:opacity-50"
            >
              <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg border border-cyan-400/25 bg-cyan-500/10 font-mono text-xs font-bold text-cyan-300 group-hover:border-cyan-400/50 group-hover:bg-cyan-500/20 group-hover:text-cyan-100 transition-all mt-0.5 shadow-sm">
                {option.badge}
              </span>
              <span className="min-w-0 flex-1">
                {index === 0 ? (
                  <span className="deepspace-question-recommended mb-1.5 inline-flex items-center gap-1 rounded-md border border-cyan-400/30 bg-cyan-400/10 px-2 py-0.5 text-[9px] font-bold tracking-[0.08em] text-cyan-200 uppercase shadow-[0_0_8px_rgba(6,182,212,0.15)]">
                    Recommended
                  </span>
                ) : null}
                <span className="deepspace-question-option-text block text-sm font-semibold text-slate-100 group-hover:text-white transition-colors break-words leading-snug">
                  {option.title}
                </span>
                {option.description ? (
                  <span className="mt-1 block text-xs leading-relaxed text-slate-400 group-hover:text-slate-300 transition-colors break-words">
                    <ReactMarkdown components={inlineMarkdownComponents}>
                      {option.description}
                    </ReactMarkdown>
                  </span>
                ) : null}
              </span>
              <ChevronRight
                size={16}
                className="deepspace-question-option-arrow shrink-0 text-cyan-400/40 transition-transform group-hover:translate-x-1 group-hover:text-cyan-300 mt-1"
              />
            </motion.button>
          ))}

          {/* Toggle button for custom answer */}
          <button
            type="button"
            disabled={submitting}
            onClick={() => setOtherOpen((open) => !open)}
            aria-expanded={otherOpen}
            aria-controls={customAnswerId}
            aria-label={otherOpen ? "Hide custom answer" : "Write a different answer"}
            className="deepspace-question-other deepspace-question-option group flex min-h-[52px] w-full items-center gap-3 rounded-xl border border-dashed border-white/10 bg-transparent p-3 text-left transition-colors hover:border-cyan-400/30 hover:bg-white/[0.04] disabled:opacity-50"
          >
            <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg border border-white/10 bg-white/[0.03] text-cyan-300 group-hover:border-cyan-400/30 group-hover:text-cyan-200 transition-colors">
              <Edit3 size={15} className="deepspace-question-other-icon" />
            </div>
            <span className="min-w-0 flex-1">
              <span className="deepspace-question-other-label block text-xs font-semibold text-slate-200 group-hover:text-cyan-100 transition-colors">
                {otherOpen ? "Hide custom answer" : "Write a different answer"}
              </span>
              <span className="deepspace-question-other-copy mt-0.5 block text-[11px] text-slate-400">
                Use your own words instead of a suggestion
              </span>
            </span>
            <ChevronRight
              size={15}
              className={`deepspace-question-other-chevron shrink-0 text-slate-400 transition-transform ${otherOpen ? "rotate-90" : ""}`}
            />
          </button>
        </div>
      ) : null}

      {/* Optional Caveat / Note Callout */}
      {parsed.footer ? (
        <div className="mt-3.5 flex items-start gap-2.5 rounded-xl border border-cyan-400/20 bg-cyan-950/30 p-3.5 text-xs text-cyan-100/90 leading-relaxed shadow-sm">
          <Info size={15} className="mt-0.5 shrink-0 text-cyan-400" />
          <div className="flex-1 space-y-1">
            <ReactMarkdown
              remarkPlugins={[remarkGfm]}
              components={questionMarkdownComponents}
            >
              {parsed.footer}
            </ReactMarkdown>
          </div>
        </div>
      ) : null}

      {/* Custom Answer Form */}
      <div className={parsed.options.length > 0 ? "relative mt-3" : "relative mt-3"}>
        {parsed.options.length === 0 ? (
          <div className="mb-2 flex items-center justify-between">
            <p className="deepspace-question-custom-label text-xs font-semibold text-slate-300">
              Your answer
            </p>
            <span className="text-[11px] text-slate-500">
              Press Enter ↵ to send • Shift+Enter for new line
            </span>
          </div>
        ) : null}
        <AnimatePresence initial={false}>
          {otherOpen ? (
            <motion.form
              initial={{ opacity: 0, height: 0, y: -5 }}
              animate={{ opacity: 1, height: "auto", y: 0 }}
              exit={{ opacity: 0, height: 0, y: -5 }}
              transition={{ duration: 0.18, ease: "easeOut" }}
              id={customAnswerId}
              onSubmit={(event) => {
                event.preventDefault();
                void submit(customAnswer);
              }}
              className="mt-2.5 flex items-end gap-2.5 overflow-hidden"
            >
              <textarea
                value={customAnswer}
                onChange={(event) => setCustomAnswer(event.target.value.slice(0, 4000))}
                onKeyDown={(event) => {
                  if (event.key === "Enter" && !event.shiftKey) {
                    event.preventDefault();
                    void submit(customAnswer);
                  }
                }}
                autoFocus={parsed.options.length === 0 || otherOpen}
                aria-label="Write your answer"
                rows={3}
                disabled={submitting}
                placeholder="Write your answer…"
                className="deepspace-question-input min-h-[80px] min-w-0 flex-1 resize-y rounded-xl border border-white/12 bg-black/40 px-3.5 py-3 text-sm leading-relaxed text-slate-100 transition-all outline-none placeholder:text-slate-500 focus:border-cyan-400/50 focus:bg-black/60 focus:ring-2 focus:ring-cyan-400/20"
              />
              <button
                type="submit"
                disabled={!customAnswer.trim() || submitting}
                aria-label="Send answer"
                title="Send answer and continue the task"
                className="deepspace-question-submit flex h-11 w-11 shrink-0 items-center justify-center rounded-xl border border-cyan-400/30 bg-gradient-to-r from-cyan-500 to-blue-600 text-white shadow-[0_0_16px_rgba(6,182,212,0.35)] transition-all hover:from-cyan-400 hover:to-blue-500 hover:shadow-[0_0_24px_rgba(6,182,212,0.5)] focus-visible:ring-2 focus-visible:ring-cyan-400 focus-visible:outline-none disabled:opacity-40 disabled:cursor-not-allowed disabled:shadow-none"
              >
                {submitting ? (
                  <LoaderCircle size={16} className="animate-spin" />
                ) : (
                  <ArrowUp size={16} />
                )}
              </button>
            </motion.form>
          ) : null}
        </AnimatePresence>
      </div>

      {submitting ? (
        <div
          role="status"
          className="deepspace-question-sending mt-3 flex items-center gap-2 text-xs font-medium text-cyan-300 animate-pulse"
        >
          <LoaderCircle size={13} className="animate-spin text-cyan-400" />
          Sending answer and resuming the task…
        </div>
      ) : null}

      {submitError ? (
        <p role="alert" className="mt-3 text-xs text-rose-300">
          {submitError}
        </p>
      ) : null}
    </motion.section>
  );
});

export default DeepSpaceQuestionCard;
