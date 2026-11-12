"use client";

import { motion, useReducedMotion } from "framer-motion";
import {
  AlertCircle,
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  Circle,
  ListChecks,
  PauseCircle,
} from "lucide-react";
import { useEffect, useId, useMemo, useRef, useState } from "react";

import type { TaskProgress, PersistedTask } from "../_lib/task-progress";
import {
  browserTaskPlanLocale,
  taskPlanCopy,
  type TaskPlanCopy,
} from "../_lib/task-plan-copy";

function taskStatusLabel(
  status: PersistedTask["status"],
  copy: TaskPlanCopy,
): string {
  return copy.status[status] ?? copy.status.pending;
}

function TaskStatusIcon({
  status,
  copy,
  reduceMotion,
  live = true,
}: {
  status: PersistedTask["status"];
  copy: TaskPlanCopy;
  reduceMotion: boolean;
  live?: boolean;
}) {
  if (status === "completed") {
    return (
      <motion.span
        key="completed"
        initial={reduceMotion ? false : { scale: 0.45, opacity: 0 }}
        animate={{ scale: 1, opacity: 1 }}
        transition={
          reduceMotion
            ? { duration: 0.01 }
            : { type: "spring", stiffness: 520, damping: 24 }
        }
        className="inline-flex text-emerald-400"
        aria-label={copy.status.completed}
      >
        <CheckCircle2 size={15} />
      </motion.span>
    );
  }

  if (status === "in_progress") {
    if (!live) {
      return (
        <PauseCircle
          size={15}
          className="text-amber-300"
          aria-label={copy.paused}
        />
      );
    }
    return (
      <span
        className="inline-flex h-4 w-4 items-center justify-center gap-[2px]"
        aria-label={copy.status.in_progress}
      >
        {[0, 1, 2].map((index) => (
          <motion.span
            key={index}
            className="h-1 w-1 rounded-full bg-cyan-300"
            animate={
              reduceMotion
                ? undefined
                : { y: [0, -3, 0], opacity: [0.5, 1, 0.5] }
            }
            transition={
              reduceMotion
                ? undefined
                : {
                    duration: 0.72,
                    repeat: Infinity,
                    ease: "easeInOut",
                    delay: index * 0.12,
                  }
            }
          />
        ))}
      </span>
    );
  }

  if (status === "blocked" || status === "failed") {
    return (
      <AlertCircle
        size={15}
        className="text-amber-300"
        aria-label={taskStatusLabel(status, copy)}
      />
    );
  }

  return (
    <Circle
      size={14}
      className="text-foreground/30"
      aria-label={copy.status.pending}
    />
  );
}

export default function DeepSpaceTaskPlanDrawer({
  progress,
  isStreaming,
}: {
  progress: TaskProgress;
  isStreaming: boolean;
}) {
  const reduceMotion = useReducedMotion() ?? false;
  const [expanded, setExpanded] = useState(false);
  const [copy, setCopy] = useState<TaskPlanCopy>(() => taskPlanCopy(browserTaskPlanLocale()));
  const listId = `deepspace-task-plan-${useId().replace(/:/g, "")}`;
  const previousPlanKey = useRef<string | null>(null);
  const planKey = useMemo(
    () =>
      progress.tasks
        .map((task) => `${task.id}:${task.content}`)
        .join("|")
        .slice(0, 8_000),
    [progress.tasks],
  );

  useEffect(() => {
    if (isStreaming && previousPlanKey.current !== planKey) setExpanded(true);
    previousPlanKey.current = planKey;
  }, [isStreaming, planKey]);

  const total = progress.tasks.length;
  const completed = Math.min(Math.max(progress.completed, 0), total);
  const activeTask = progress.tasks.find(
    (task) => task.status === "in_progress",
  );
  const needsAttention = progress.tasks.some(
    (task) => task.status === "blocked" || task.status === "failed",
  );
  const allComplete = total > 0 && completed === total;
  const headerStatus = needsAttention
    ? copy.needsAttention
    : allComplete
      ? progress.verified
        ? isStreaming
          ? copy.verifiedPreparing
          : copy.complete
        : isStreaming
          ? copy.verifyingCompletion
          : copy.awaitingVerification
      : activeTask
        ? isStreaming
          ? activeTask.active_form || activeTask.content
          : copy.pausedRetry
        : isStreaming
          ? copy.workingThroughPlan
          : copy.planIncomplete;
  const progressPercent = total ? Math.round((completed / total) * 100) : 0;

  return (
    <motion.section
      aria-label={copy.title}
      data-testid="deepspace-task-plan-drawer"
      initial={
        reduceMotion ? { opacity: 0 } : { opacity: 0, y: 8, scale: 0.99 }
      }
      animate={{ opacity: 1, y: 0, scale: 1 }}
      exit={reduceMotion ? { opacity: 0 } : { opacity: 0, y: 5, scale: 0.99 }}
      transition={
        reduceMotion
          ? { duration: 0.01 }
          : { duration: 0.22, ease: [0.22, 1, 0.36, 1] }
      }
      className="mx-auto mb-2 w-full max-w-[min(100%,74rem)] overflow-hidden rounded-xl border border-cyan-300/15 bg-slate-50/95 shadow-[0_10px_32px_-24px_rgba(34,211,238,0.55)] backdrop-blur-xl dark:bg-[#0b1411]/95"
    >
      <button
        type="button"
        aria-expanded={expanded}
        aria-controls={listId}
        onClick={() => setExpanded((value) => !value)}
        className="flex min-h-10 w-full items-center gap-2.5 px-3 py-2 text-left transition-colors hover:bg-cyan-300/[0.06] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-cyan-300/60 sm:px-3.5"
      >
        <ListChecks
          size={15}
          className="shrink-0 text-cyan-700 dark:text-cyan-200"
          aria-hidden="true"
        />
        <span className="shrink-0 text-[10px] font-bold tracking-[0.12em] text-slate-700 uppercase dark:text-foreground/75">
          {copy.title}
        </span>
        <span className="shrink-0 rounded-full border border-cyan-600/15 bg-cyan-500/[0.07] px-2 py-0.5 text-[10px] font-semibold tabular-nums text-cyan-800 dark:border-cyan-300/15 dark:text-cyan-100">
          {completed}/{total}
        </span>
        {!expanded && activeTask ? (
          <span
            data-testid="deepspace-active-task-summary"
            className="inline-flex min-w-0 shrink items-center gap-1.5 text-cyan-800 dark:text-cyan-100"
          >
            <TaskStatusIcon
              status={activeTask.status}
              copy={copy}
              reduceMotion={reduceMotion}
              live={isStreaming}
            />
            <span className="shrink-0 text-[9px] font-semibold tracking-wide uppercase">
              {isStreaming ? copy.active : copy.paused}
            </span>
          </span>
        ) : null}
        <span
          className={`min-w-0 flex-1 truncate text-[11px] ${needsAttention ? "text-amber-700 dark:text-amber-200" : allComplete && progress.verified && !isStreaming ? "text-emerald-700 dark:text-emerald-300" : "text-slate-500 dark:text-foreground/55"}`}
          aria-live="polite"
        >
          {headerStatus}
        </span>
        {expanded ? (
          <ChevronDown
            size={15}
            className="shrink-0 text-slate-400 dark:text-foreground/45"
            aria-hidden="true"
          />
        ) : (
          <ChevronUp
            size={15}
            className="shrink-0 text-slate-400 dark:text-foreground/45"
            aria-hidden="true"
          />
        )}
      </button>

      <div id={listId} aria-hidden={!expanded} className="overflow-hidden">
        <motion.div
          initial={false}
          animate={{
            height: expanded ? "auto" : 0,
            opacity: expanded ? 1 : 0,
          }}
          transition={
            reduceMotion
              ? { duration: 0.01 }
              : {
                  height: { duration: 0.28, ease: [0.22, 1, 0.36, 1] },
                  opacity: { duration: 0.18 },
                }
          }
          className="overflow-hidden"
        >
          <div className="border-t border-cyan-300/10 px-3 pt-2.5 pb-3 sm:px-3.5">
            <div
              className="mb-2 h-1 overflow-hidden rounded-full bg-slate-200 dark:bg-white/[0.07]"
              role="progressbar"
              aria-label={copy.completionAria}
              aria-valuemin={0}
              aria-valuemax={total}
              aria-valuenow={completed}
            >
              <motion.div
                className="h-full rounded-full bg-gradient-to-r from-cyan-500 to-emerald-400 dark:from-cyan-300 dark:to-emerald-300"
                initial={false}
                animate={{ width: `${progressPercent}%` }}
                transition={
                  reduceMotion
                    ? { duration: 0.01 }
                    : { duration: 0.42, ease: "easeOut" }
                }
              />
            </div>
            <ol
              className="max-h-56 space-y-1 overflow-y-auto pr-1"
              aria-label={copy.plannedTasksAria}
            >
              {progress.tasks.map((task, index) => (
                <motion.li
                  key={task.id || `${task.content}-${index}`}
                  layout={!reduceMotion}
                  transition={
                    reduceMotion
                      ? { duration: 0.01 }
                      : {
                          layout: {
                            duration: 0.28,
                            ease: [0.22, 1, 0.36, 1],
                          },
                        }
                  }
                  className={`flex items-start gap-2.5 rounded-lg px-2 py-2 text-[11px] transition-colors duration-300 ${task.status === "in_progress" && isStreaming ? "bg-cyan-500/[0.07] dark:bg-cyan-300/[0.06]" : ""}`}
                >
                  <span className="mt-px grid h-4 w-4 shrink-0 place-items-center">
                    <TaskStatusIcon
                      status={task.status}
                      copy={copy}
                      reduceMotion={reduceMotion}
                      live={isStreaming}
                    />
                  </span>
                  <span className="min-w-0 flex-1">
                    <span
                      className={`block leading-relaxed ${task.status === "completed" ? "text-slate-400 line-through decoration-emerald-500/40 dark:text-foreground/40" : "text-slate-700 dark:text-foreground/80"}`}
                    >
                      {task.content}
                    </span>
                    {task.status === "in_progress" && isStreaming && task.active_form ? (
                      <span className="mt-0.5 block truncate text-[10px] text-cyan-700 dark:text-cyan-100/65">
                        {task.active_form}
                      </span>
                    ) : null}
                  </span>
                  <span
                    className={`shrink-0 pt-0.5 text-[9px] font-semibold tracking-wide uppercase ${task.status === "completed" ? "text-emerald-700 dark:text-emerald-300" : task.status === "in_progress" && isStreaming ? "text-cyan-700 dark:text-cyan-200" : task.status === "in_progress" || task.status === "blocked" || task.status === "failed" ? "text-amber-700 dark:text-amber-200" : "text-slate-400 dark:text-foreground/35"}`}
                  >
                    {task.status === "in_progress" && !isStreaming
                      ? copy.paused
                      : taskStatusLabel(task.status, copy)}
                  </span>
                </motion.li>
              ))}
            </ol>
            {allComplete && isStreaming ? (
              <div
                className="mt-2 flex items-center gap-2 border-t border-cyan-300/10 pt-2 text-[10px] text-cyan-800 dark:text-cyan-100/65"
                role="status"
              >
                <span
                  className="inline-flex h-4 w-4 items-center justify-center gap-[2px]"
                  aria-hidden="true"
                >
                  {[0, 1, 2].map((index) => (
                    <motion.span
                      key={index}
                      className="h-1 w-1 rounded-full bg-cyan-500 dark:bg-cyan-300"
                      animate={
                        reduceMotion
                          ? undefined
                          : { y: [0, -3, 0], opacity: [0.5, 1, 0.5] }
                      }
                      transition={
                        reduceMotion
                          ? undefined
                          : {
                              duration: 0.72,
                              repeat: Infinity,
                              ease: "easeInOut",
                              delay: index * 0.12,
                            }
                      }
                    />
                  ))}
                </span>
                {progress.verified
                  ? copy.verifiedPreparingFinal
                  : copy.verifyingEveryTask}
              </div>
            ) : null}
          </div>
        </motion.div>
      </div>
    </motion.section>
  );
}
