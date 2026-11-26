"use client";

import { AverQelLoader } from "@/app/components/ui/AverQelLoading";

import { memo, useId, useMemo, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { ArrowUp, Check, ChevronRight, Edit3, Info, MessageCircleQuestion } from "lucide-react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import type { StructuredUserQuestion } from "../_lib/deepspace-thread";

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

        segment = segment
          .replace(/^[-*•]\s*/, "")
          .replace(/[-*•]\s*$/, "")
          .trim();

        let title = segment;
        let description = "";

        const splitMatch = segment.match(
          /^([\s\S]+?)(?:\*\*|\*|\s*—\s*|\s*–\s*|\s*-\s*|\s*:\s*)([\s\S]+)$/,
        );
        if (splitMatch) {
          title = splitMatch[1]
            .replace(/^\*\*|\*\*$/g, "")
            .replace(/\*+/g, "")
            .trim();
          description = splitMatch[2]
            .replace(/^[—–\-\:\s]+/, "")
            .replace(/^\*\*|\*\*$/g, "")
            .trim();
        } else {
          title = segment
            .replace(/^\*\*|\*\*$/g, "")
            .replace(/\*+/g, "")
            .trim();
        }

        options.push({
          id: `opt-${cur.badge.toUpperCase()}`,
          badge: cur.badge.toUpperCase(),
          label: segment.replace(/\*\*/g, "").trim(),
          title,
          description,
          value: `(${cur.badge.toLowerCase()}) ${title}`,
        });

        if (i === validMatches.length - 1 && itemFooter) {
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
    const footer = lines
      .slice(startLine + bulletLines.length)
      .join("\n")
      .trim();
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
    <p className="deepspace-question-prompt text-[14.5px] leading-relaxed font-normal tracking-[-0.01em] text-slate-900 sm:text-[15px] dark:text-slate-100">
      {children}
    </p>
  ),
  strong: ({ children }) => (
    <strong className="font-semibold text-slate-950 dark:bg-gradient-to-r dark:from-cyan-200 dark:via-white dark:to-cyan-100 dark:bg-clip-text dark:text-transparent dark:drop-shadow-[0_0_8px_rgba(6,182,212,0.25)]">
      {children}
    </strong>
  ),
  code: ({ children }) => (
    <code className="rounded-md border border-teal-600/25 bg-teal-50 px-1.5 py-0.5 font-mono text-[13px] text-teal-900 dark:border-cyan-400/25 dark:bg-cyan-950/70 dark:text-cyan-200 dark:shadow-[0_0_8px_rgba(6,182,212,0.15)]">
      {children}
    </code>
  ),
  ul: ({ children }) => (
    <ul className="my-2 list-disc space-y-1.5 pl-4 text-slate-700 marker:text-teal-600 dark:text-slate-200 dark:marker:text-cyan-400">
      {children}
    </ul>
  ),
  ol: ({ children }) => (
    <ol className="my-2 list-decimal space-y-1.5 pl-4 font-mono text-sm text-slate-700 marker:text-teal-600 dark:text-slate-200 dark:marker:text-cyan-400">
      {children}
    </ol>
  ),
  li: ({ children }) => <li className="text-[14px] leading-relaxed">{children}</li>,
  a: ({ href, children }) => (
    <a
      href={href}
      target="_blank"
      rel="noreferrer noopener"
      className="text-teal-700 underline decoration-teal-600/40 transition-colors hover:text-teal-900 dark:text-cyan-300 dark:decoration-cyan-400/40 dark:hover:text-cyan-200 dark:hover:decoration-cyan-300"
    >
      {children}
    </a>
  ),
};

const inlineMarkdownComponents: Components = {
  p: ({ children }) => <>{children}</>,
  strong: ({ children }) => (
    <strong className="font-semibold text-slate-900 dark:text-slate-100">{children}</strong>
  ),
  code: ({ children }) => (
    <code className="rounded border border-teal-600/25 bg-teal-50 px-1 py-0.5 font-mono text-[11.5px] text-teal-900 dark:border-cyan-400/20 dark:bg-cyan-950/50 dark:text-cyan-200">
      {children}
    </code>
  ),
};

export interface DeepSpaceQuestionCardProps {
  question: string;
  options: string[];
  questions?: StructuredUserQuestion[];
  onSubmit: (answer: string) => Promise<void>;
  mcpConnectionOptions?: MCPConnectionPromptOption[];
  onConnectMcp?: (option: MCPConnectionPromptOption) => Promise<void>;
  onCancelMcp?: () => Promise<void>;
  onStopTask?: () => Promise<void>;
  className?: string;
}

export interface MCPConnectionPromptOption {
  entry_id: string;
  name: string;
  description?: string;
  command: string;
  args: string[];
  permission_notice: string;
}

export const DeepSpaceQuestionCard = memo(function DeepSpaceQuestionCard({
  question,
  options,
  questions,
  onSubmit,
  mcpConnectionOptions = [],
  onConnectMcp,
  onCancelMcp,
  onStopTask,
  className,
}: DeepSpaceQuestionCardProps) {
  const structuredQuestions = useMemo(
    () => (questions && questions.length > 0 ? questions : []),
    [questions],
  );
  const singleStructured =
    structuredQuestions.length === 1 && !structuredQuestions[0]?.multi_select
      ? structuredQuestions[0]
      : null;

  const parsed = useMemo(() => {
    if (singleStructured) {
      return {
        prompt: singleStructured.question,
        options: (singleStructured.options ?? []).map((opt, i) => ({
          id: `${singleStructured.id}-opt-${i}`,
          badge: String.fromCharCode(65 + (i % 26)),
          label: opt.label,
          title: opt.label,
          description: opt.description ?? "",
          value: opt.label,
        })),
        footer: "",
      };
    }
    return parseQuestionContent(question, options);
  }, [singleStructured, question, options]);

  const usesMultiFlow =
    mcpConnectionOptions.length === 0 &&
    structuredQuestions.length > 0 &&
    (structuredQuestions.length > 1 || Boolean(structuredQuestions[0]?.multi_select));

  const [selectedByQuestion, setSelectedByQuestion] = useState<Record<string, string[]>>({});
  const [customByQuestion, setCustomByQuestion] = useState<Record<string, string>>({});
  const [customAnswer, setCustomAnswer] = useState("");
  const [otherOpen, setOtherOpen] = useState(() => !usesMultiFlow && parsed.options.length === 0);
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

  const toggleStructuredChoice = (q: StructuredUserQuestion, label: string) => {
    setSelectedByQuestion((prev) => {
      const current = prev[q.id] ?? [];
      if (q.multi_select) {
        const exists = current.includes(label);
        return {
          ...prev,
          [q.id]: exists ? current.filter((item) => item !== label) : [...current, label],
        };
      }
      return { ...prev, [q.id]: [label] };
    });
  };

  const canSubmitMulti = useMemo(() => {
    if (!usesMultiFlow) return false;
    return structuredQuestions.some((q) => {
      const sel = selectedByQuestion[q.id] ?? [];
      const custom = (customByQuestion[q.id] ?? "").trim();
      return sel.length > 0 || custom.length > 0;
    });
  }, [usesMultiFlow, structuredQuestions, selectedByQuestion, customByQuestion]);

  const submitMulti = () => {
    if (!canSubmitMulti || submitting) return;
    const lines = structuredQuestions
      .map((q) => {
        const sel = selectedByQuestion[q.id] ?? [];
        const custom = (customByQuestion[q.id] ?? "").trim();
        const combined = [...sel, ...(custom ? [custom] : [])].join(", ");
        if (!combined) return "";
        if (structuredQuestions.length === 1) return combined;
        return `- ${q.header || q.question}: ${combined}`;
      })
      .filter(Boolean);
    if (lines.length === 0) return;
    void submit(lines.join("\n"));
  };

  const runMcpAction = async (
    action: (() => Promise<void>) | undefined,
    failureMessage = "The MCP connection was not verified. Check the message and try again.",
  ) => {
    if (!action || submitting) return;
    setSubmitting(true);
    setSubmitError(null);
    try {
      await action();
    } catch {
      setSubmitError(failureMessage);
      setSubmitting(false);
    }
  };

  return (
    <motion.section
      initial={{ opacity: 0, y: 14, scale: 0.985 }}
      animate={{ opacity: 1, y: 0, scale: 1 }}
      transition={{ duration: 0.24, ease: "easeOut" }}
      aria-label="DeepSpace question"
      className={
        className ??
        "deepspace-question-card relative mt-5 w-full max-w-4xl overflow-hidden rounded-2xl border border-cyan-500/25 bg-[linear-gradient(155deg,rgba(15,23,32,0.98),rgba(10,16,23,0.99))] p-5 shadow-[0_16px_48px_-16px_rgba(0,0,0,0.85),0_0_32px_-8px_rgba(6,182,212,0.12)] backdrop-blur-xl sm:p-6"
      }
    >
      {/* Top accent glow line and ambient light */}
      <div className="pointer-events-none absolute top-0 right-0 left-0 h-[1.5px] bg-gradient-to-r from-transparent via-cyan-400/60 to-transparent" />
      <div className="pointer-events-none absolute -top-24 left-1/2 h-36 w-96 -translate-x-1/2 rounded-full bg-cyan-500/10 blur-3xl" />

      {/* Header with jewel icon and live status badge */}
      <div className="relative flex items-start gap-3.5">
        <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl border border-teal-600/25 bg-teal-50 text-teal-700 shadow-xs dark:border-cyan-400/30 dark:bg-gradient-to-br dark:from-cyan-400/20 dark:via-cyan-500/10 dark:to-blue-600/20 dark:text-cyan-200 dark:shadow-[0_0_16px_-2px_rgba(6,182,212,0.35)]">
          <MessageCircleQuestion size={19} />
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="deepspace-question-title font-mono text-[11px] font-bold tracking-[0.14em] text-teal-800 uppercase dark:text-cyan-300">
              Your input is needed
            </span>
            {singleStructured?.header ? (
              <span className="rounded-md border border-slate-200 bg-slate-100 px-2 py-0.5 text-[10.5px] font-medium text-slate-700 dark:border-white/10 dark:bg-white/[0.04] dark:text-slate-300">
                {singleStructured.header}
              </span>
            ) : null}
            <span className="deepspace-question-status flex items-center gap-1.5 rounded-full border border-cyan-400/30 bg-cyan-950/60 px-2.5 py-0.5 text-[11px] font-medium text-cyan-200 shadow-[0_0_10px_rgba(6,182,212,0.15)]">
              <span className="relative flex h-2 w-2">
                <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-cyan-400 opacity-75" />
                <span className="relative inline-flex h-2 w-2 rounded-full bg-cyan-400 shadow-[0_0_6px_#22d3ee]" />
              </span>
              Waiting for you
            </span>
          </div>

          {!usesMultiFlow ? (
            <>
              <div className="mt-3">
                <ReactMarkdown remarkPlugins={[remarkGfm]} components={questionMarkdownComponents}>
                  {parsed.prompt}
                </ReactMarkdown>
              </div>
              <p className="deepspace-question-hint mt-2 text-xs leading-5 text-slate-400/80">
                {parsed.options.length > 0
                  ? "Choose an option below or write your own answer to continue."
                  : "Enter your reply below to continue the task."}
              </p>
            </>
          ) : (
            <p className="deepspace-question-hint mt-2 text-xs leading-5 text-slate-400/80">
              Select your preferences below, then send to continue.
            </p>
          )}
        </div>
      </div>

      {mcpConnectionOptions.length > 0 ? (
        <div className="deepspace-mcp-connect-options relative mt-4 space-y-3">
          {mcpConnectionOptions.map((option) => (
            <section
              key={option.entry_id}
              className="rounded-xl border border-slate-200 bg-slate-50/90 p-4 dark:border-white/10 dark:bg-white/[0.03]"
            >
              <h3 className="text-sm font-semibold text-slate-900 dark:text-slate-100">
                {option.name}
              </h3>
              {option.description ? (
                <p className="mt-1 text-xs leading-5 text-slate-600 dark:text-slate-300/70">
                  {option.description}
                </p>
              ) : null}
              <div className="mt-3 text-[11px] text-slate-600 dark:text-slate-300/70">
                Exact process arguments
              </div>
              <pre className="mt-1 max-h-32 overflow-auto rounded-lg border border-slate-200 bg-white p-2 text-[10px] leading-4 break-all whitespace-pre-wrap text-slate-800 dark:border-white/10 dark:bg-black/25 dark:text-cyan-100/80">
                {JSON.stringify([option.command, ...option.args], null, 2)}
              </pre>
              <p className="mt-2 rounded-lg border border-amber-500/30 bg-amber-50 px-3 py-2 text-xs leading-5 text-amber-900 dark:border-amber-300/20 dark:bg-amber-300/[0.06] dark:text-amber-100/80">
                {option.permission_notice}
              </p>
              <button
                type="button"
                disabled={submitting}
                onClick={() => void runMcpAction(() => onConnectMcp?.(option) ?? Promise.resolve())}
                className="mt-3 inline-flex min-h-10 items-center justify-center rounded-lg border border-emerald-600/30 bg-emerald-600/12 px-4 py-2 text-xs font-semibold text-emerald-800 transition hover:bg-emerald-600/20 disabled:cursor-wait disabled:opacity-50 dark:border-transparent dark:bg-emerald-400/15 dark:text-emerald-200 dark:hover:bg-emerald-400/25"
              >
                {submitting ? "Starting and checking…" : `Connect ${option.name}`}
              </button>
            </section>
          ))}
          <button
            type="button"
            disabled={submitting}
            onClick={() => void runMcpAction(onCancelMcp)}
            className="inline-flex min-h-10 items-center justify-center rounded-lg border border-slate-300 bg-white px-4 py-2 text-xs font-semibold text-slate-700 transition hover:bg-slate-100 disabled:cursor-wait disabled:opacity-50 dark:border-white/10 dark:bg-transparent dark:text-white/70 dark:hover:bg-white/10"
          >
            Cancel and continue without MCP
          </button>
          {onStopTask ? (
            <button
              type="button"
              disabled={submitting}
              onClick={() =>
                void runMcpAction(onStopTask, "DeepSpace could not stop this task. Try again.")
              }
              className="ml-2 inline-flex min-h-10 items-center justify-center rounded-lg border border-rose-500/35 bg-rose-50 px-4 py-2 text-xs font-semibold text-rose-800 transition hover:bg-rose-100 disabled:cursor-wait disabled:opacity-50 dark:border-rose-400/25 dark:bg-transparent dark:text-rose-200 dark:hover:bg-rose-400/10"
            >
              Stop this task
            </button>
          ) : null}
        </div>
      ) : null}

      {/* Multi-question / Multi-select Flow */}
      {usesMultiFlow ? (
        <div className="relative mt-4 space-y-4">
          {structuredQuestions.map((q, qIndex) => {
            const selected = selectedByQuestion[q.id] ?? [];
            const opts = q.options ?? [];
            return (
              <div
                key={q.id}
                className="rounded-xl border border-slate-200 bg-slate-50/75 p-3.5 sm:p-4 dark:border-white/10 dark:bg-white/[0.02]"
              >
                <div className="mb-2 flex flex-wrap items-center gap-2">
                  {q.header ? (
                    <span className="rounded border border-teal-600/25 bg-teal-50 px-2 py-0.5 text-[10px] font-semibold tracking-wider text-teal-800 uppercase dark:border-cyan-400/25 dark:bg-cyan-500/10 dark:text-cyan-200">
                      {q.header}
                    </span>
                  ) : null}
                  {structuredQuestions.length > 1 ? (
                    <span className="text-[10.5px] font-medium text-slate-600 dark:text-slate-400">
                      Question {qIndex + 1} of {structuredQuestions.length}
                    </span>
                  ) : null}
                  {q.multi_select ? (
                    <span className="text-[10.5px] text-slate-600 dark:text-slate-400">
                      Select one or more
                    </span>
                  ) : null}
                </div>
                <div className="mb-3">
                  <ReactMarkdown
                    remarkPlugins={[remarkGfm]}
                    components={questionMarkdownComponents}
                  >
                    {q.question}
                  </ReactMarkdown>
                </div>
                {opts.length > 0 ? (
                  <div className="space-y-2">
                    {opts.map((opt, idx) => {
                      const isSelected = selected.includes(opt.label);
                      return (
                        <button
                          key={`${q.id}-${opt.label}-${idx}`}
                          type="button"
                          disabled={submitting}
                          onClick={() => toggleStructuredChoice(q, opt.label)}
                          aria-pressed={isSelected}
                          className={`group flex w-full items-start gap-3 rounded-xl border p-3 text-left transition ${
                            isSelected
                              ? "border-teal-600 bg-teal-50/90 dark:border-cyan-400/60 dark:bg-cyan-500/[0.12]"
                              : "border-slate-200 bg-white hover:border-teal-500/50 hover:bg-teal-50/50 dark:border-white/10 dark:bg-white/[0.025] dark:hover:border-cyan-400/35 dark:hover:bg-white/[0.04]"
                          }`}
                        >
                          <span
                            className={`mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-md border font-mono text-[11px] font-bold transition ${
                              isSelected
                                ? "border-teal-600 bg-teal-600 text-white dark:border-cyan-300 dark:bg-cyan-400 dark:text-slate-950"
                                : "border-teal-600/25 bg-teal-50 text-teal-800 dark:border-cyan-400/25 dark:bg-cyan-500/10 dark:text-cyan-300"
                            }`}
                          >
                            {isSelected ? (
                              <Check size={13} />
                            ) : (
                              String.fromCharCode(65 + (idx % 26))
                            )}
                          </span>
                          <span className="min-w-0 flex-1">
                            <span className="block text-sm font-semibold text-slate-900 dark:text-slate-100">
                              {opt.label}
                            </span>
                            {opt.description ? (
                              <span className="mt-0.5 block text-xs leading-relaxed text-slate-600 dark:text-slate-400">
                                {opt.description}
                              </span>
                            ) : null}
                          </span>
                        </button>
                      );
                    })}
                  </div>
                ) : null}
                <input
                  type="text"
                  value={customByQuestion[q.id] ?? ""}
                  onChange={(e) =>
                    setCustomByQuestion((prev) => ({
                      ...prev,
                      [q.id]: e.target.value.slice(0, 1000),
                    }))
                  }
                  placeholder={opts.length > 0 ? "Or add a custom note…" : "Write your answer…"}
                  disabled={submitting}
                  className="mt-2.5 w-full rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs text-slate-900 placeholder:text-slate-400 focus:border-teal-600 focus:outline-none dark:border-white/10 dark:bg-black/35 dark:text-slate-100 dark:placeholder:text-slate-500 dark:focus:border-cyan-400/50"
                />
              </div>
            );
          })}
          <div className="flex justify-end">
            <button
              type="button"
              disabled={!canSubmitMulti || submitting}
              onClick={submitMulti}
              className="inline-flex items-center gap-2 rounded-xl border border-cyan-400/35 bg-gradient-to-r from-cyan-500 to-blue-600 px-4 py-2 text-xs font-semibold text-white shadow-[0_0_16px_rgba(6,182,212,0.3)] transition hover:from-cyan-400 hover:to-blue-500 disabled:cursor-not-allowed disabled:opacity-40"
            >
              {submitting ? (
                <AverQelLoader size={14} className="animate-spin" />
              ) : (
                <ArrowUp size={14} />
              )}
              Send answers
            </button>
          </div>
        </div>
      ) : null}

      {/* Single-select Interactive Option Cards */}
      {!usesMultiFlow && mcpConnectionOptions.length === 0 && parsed.options.length > 0 ? (
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
              <span className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-lg border border-teal-600/25 bg-teal-50 font-mono text-xs font-bold text-teal-800 shadow-sm transition-all group-hover:border-teal-600/50 group-hover:bg-teal-100 dark:border-cyan-400/25 dark:bg-cyan-500/10 dark:text-cyan-300 dark:group-hover:border-cyan-400/50 dark:group-hover:bg-cyan-500/20 dark:group-hover:text-cyan-100">
                {option.badge}
              </span>
              <span className="min-w-0 flex-1">
                {index === 0 ? (
                  <span className="deepspace-question-recommended mb-1.5 inline-flex items-center gap-1 rounded-md border border-teal-600/30 bg-teal-50 px-2 py-0.5 text-[9px] font-bold tracking-[0.08em] text-teal-800 uppercase dark:border-cyan-400/30 dark:bg-cyan-400/10 dark:text-cyan-200 dark:shadow-[0_0_8px_rgba(6,182,212,0.15)]">
                    Recommended
                  </span>
                ) : null}
                <span className="deepspace-question-option-text block text-sm leading-snug font-semibold break-words text-slate-900 transition-colors dark:text-slate-100 dark:group-hover:text-white">
                  {option.title}
                </span>
                {option.description ? (
                  <span className="mt-1 block text-xs leading-relaxed break-words text-slate-600 transition-colors dark:text-slate-400 dark:group-hover:text-slate-300">
                    <ReactMarkdown components={inlineMarkdownComponents}>
                      {option.description}
                    </ReactMarkdown>
                  </span>
                ) : null}
              </span>
              <ChevronRight
                size={16}
                className="deepspace-question-option-arrow mt-1 shrink-0 text-cyan-400/40 transition-transform group-hover:translate-x-1 group-hover:text-cyan-300"
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
            <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg border border-white/10 bg-white/[0.03] text-cyan-300 transition-colors group-hover:border-cyan-400/30 group-hover:text-cyan-200">
              <Edit3 size={15} className="deepspace-question-other-icon" />
            </div>
            <span className="min-w-0 flex-1">
              <span className="deepspace-question-other-label block text-xs font-semibold text-slate-200 transition-colors group-hover:text-cyan-100">
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
      {!usesMultiFlow && parsed.footer ? (
        <div className="mt-3.5 flex items-start gap-2.5 rounded-xl border border-teal-600/25 bg-teal-50/85 p-3.5 text-xs leading-relaxed text-teal-950 shadow-sm dark:border-cyan-400/20 dark:bg-cyan-950/30 dark:text-cyan-100/90">
          <Info size={15} className="mt-0.5 shrink-0 text-teal-700 dark:text-cyan-400" />
          <div className="flex-1 space-y-1">
            <ReactMarkdown remarkPlugins={[remarkGfm]} components={questionMarkdownComponents}>
              {parsed.footer}
            </ReactMarkdown>
          </div>
        </div>
      ) : null}

      {/* Custom Answer Form */}
      {!usesMultiFlow ? (
        <div className="relative mt-3">
          {mcpConnectionOptions.length === 0 && parsed.options.length === 0 ? (
            <div className="mb-2 flex items-center justify-between">
              <p className="deepspace-question-custom-label text-xs font-semibold text-slate-700 dark:text-slate-300">
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
                  className="deepspace-question-submit flex h-11 w-11 shrink-0 items-center justify-center rounded-xl border border-cyan-400/30 bg-gradient-to-r from-cyan-500 to-blue-600 text-white shadow-[0_0_16px_rgba(6,182,212,0.35)] transition-all hover:from-cyan-400 hover:to-blue-500 hover:shadow-[0_0_24px_rgba(6,182,212,0.5)] focus-visible:ring-2 focus-visible:ring-cyan-400 focus-visible:outline-none disabled:cursor-not-allowed disabled:opacity-40 disabled:shadow-none"
                >
                  {submitting ? (
                    <AverQelLoader size={16} className="animate-spin" />
                  ) : (
                    <ArrowUp size={16} />
                  )}
                </button>
              </motion.form>
            ) : null}
          </AnimatePresence>
        </div>
      ) : null}

      {submitting ? (
        <div
          role="status"
          className="deepspace-question-sending mt-3 flex animate-pulse items-center gap-2 text-xs font-medium text-cyan-300"
        >
          <AverQelLoader size={13} className="animate-spin text-cyan-400" />
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
