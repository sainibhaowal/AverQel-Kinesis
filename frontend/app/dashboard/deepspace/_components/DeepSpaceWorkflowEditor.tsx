"use client";

import { AverQelLoader } from "@/app/components/ui/AverQelLoading";

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  FileCode,
  GitBranch,
  Plus,
  RefreshCw,
  Save,
  Trash2,
} from "lucide-react";

import {
  deleteWorkflow,
  listWorkflows,
  readWorkflow,
  writeWorkflow,
} from "../_lib/workflow-editor-api";
import type { WorkflowSummary } from "../_lib/workflow-editor-api";

/** The name a new workflow starts with. */
const NEW_WORKFLOW_NAME = "new-workflow";

/** The document a new workflow starts from. */
function emptyDocument(): Record<string, unknown> {
  return {
    max_concurrency: 2,
    steps: [{ id: "step-1", task: "" }],
  };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

/**
 * Render one workflow document as the JSON the editor shows and saves.
 *
 * @param document - The document to render.
 * @returns The rendered JSON.
 */
function renderDocument(document: Record<string, unknown>): string {
  try {
    return JSON.stringify(document, null, 2);
  } catch {
    return "{}";
  }
}

/**
 * Read one edited document back from the editor's text.
 *
 * @param source - The text the editor holds.
 * @returns The parsed document, or the message that explains why it failed.
 */
function parseDocument(source: string): { document?: Record<string, unknown>; error?: string } {
  const trimmed = source.trim();
  if (!trimmed) return { error: "A workflow document cannot be empty." };
  let parsed: unknown;
  try {
    parsed = JSON.parse(trimmed);
  } catch (cause) {
    return {
      error: cause instanceof Error ? cause.message : "That is not valid JSON.",
    };
  }
  if (!isRecord(parsed)) return { error: "A workflow document must be a JSON object." };
  return { document: parsed };
}

/**
 * The editor for the named workflows one workspace holds.
 *
 * A named workflow is a JSON document the workspace stores, so the editor
 * reads and writes the same document the workflow runner reads.
 */
export default function DeepSpaceWorkflowEditor({
  conversationId,
}: {
  conversationId: string | null;
}) {
  const [workflows, setWorkflows] = useState<WorkflowSummary[]>([]);
  const [selectedName, setSelectedName] = useState<string | null>(null);
  const [source, setSource] = useState("");
  const [loadedConversationId, setLoadedConversationId] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  // The list on screen belongs to one conversation, so the panel is still
  // loading whenever the open conversation is not the one that was read.
  const loading = loadedConversationId !== conversationId;

  const refresh = useCallback(async () => {
    if (!conversationId) return;
    try {
      const listed = await listWorkflows(conversationId);
      setWorkflows(listed);
      setError(null);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Workflows could not be loaded.");
    } finally {
      setLoadedConversationId(conversationId);
    }
  }, [conversationId]);

  // The list belongs to one conversation, so the panel reads the new
  // conversation's workflows when the conversation changes.
  /* eslint-disable react-hooks/set-state-in-effect */
  useEffect(() => {
    if (!conversationId) return;
    void refresh();
  }, [conversationId, refresh]);
  /* eslint-enable react-hooks/set-state-in-effect */

  const openWorkflow = useCallback(
    async (name: string, options?: { keepNotice?: boolean }) => {
      if (!conversationId) return;
      try {
        const document = await readWorkflow(conversationId, name);
        setSelectedName(name);
        setSource(renderDocument(document.document));
        setError(null);
        if (!options?.keepNotice) setNotice(null);
      } catch (cause) {
        setError(
          cause instanceof Error ? cause.message : "That workflow could not be opened.",
        );
      }
    },
    [conversationId],
  );

  const startNew = useCallback(() => {
    setSelectedName(NEW_WORKFLOW_NAME);
    setSource(renderDocument(emptyDocument()));
    setError(null);
    setNotice(null);
  }, []);

  const save = useCallback(async () => {
    if (!conversationId || !selectedName) return;
    const parsed = parseDocument(source);
    if (!parsed.document) {
      setError(parsed.error ?? "That workflow could not be saved.");
      return;
    }
    setSaving(true);
    try {
      await writeWorkflow(conversationId, selectedName, parsed.document);
      setError(null);
      setNotice(`Saved ${selectedName}.`);
      await refresh();
      await openWorkflow(selectedName, { keepNotice: true });
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "That workflow could not be saved.");
    } finally {
      setSaving(false);
    }
  }, [conversationId, selectedName, source, refresh, openWorkflow]);

  const remove = useCallback(async () => {
    if (!conversationId || !selectedName || selectedName === NEW_WORKFLOW_NAME) return;
    setSaving(true);
    try {
      await deleteWorkflow(conversationId, selectedName);
      setError(null);
      setNotice(`Deleted ${selectedName}.`);
      setSelectedName(null);
      setSource("");
      await refresh();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "That workflow could not be deleted.");
    } finally {
      setSaving(false);
    }
  }, [conversationId, selectedName, refresh]);

  const stepCount = useMemo(() => {
    const parsed = parseDocument(source);
    const steps = parsed.document?.steps;
    return Array.isArray(steps) ? steps.length : 0;
  }, [source]);

  if (!conversationId) {
    return (
      <div className="text-muted-foreground flex h-full min-h-0 items-center justify-center p-4 text-[11px]">
        Open a conversation to edit its workflows.
      </div>
    );
  }

  return (
    <div className="bg-surface-0 text-foreground flex h-full min-h-0 flex-col font-sans dark:bg-neutral-950">
      <div className="border-border-subtle bg-surface-1/70 flex flex-wrap items-center justify-between gap-2 border-b px-3 py-2 text-xs backdrop-blur dark:bg-neutral-900/70">
        <div className="flex items-center gap-2">
          <div className="text-foreground flex items-center gap-1.5 text-[11px] font-semibold">
            <FileCode size={13} className="text-primary" />
            <span>Workflow Editor</span>
          </div>
          <span className="text-muted-foreground text-[10px]">({workflows.length} workflows)</span>
        </div>
        <div className="flex items-center gap-1.5">
          <button
            type="button"
            onClick={startNew}
            className="border-border-subtle bg-surface-2/60 text-muted-foreground hover:bg-surface-2 hover:text-foreground inline-flex items-center gap-1 rounded-md border px-2 py-1 text-[10px] transition"
          >
            <Plus size={11} />
            <span>New</span>
          </button>
          <button
            type="button"
            onClick={() => void refresh()}
            disabled={loading}
            aria-label="Refresh workflows"
            title="Refresh workflows"
            className="border-border-subtle bg-surface-2/60 text-muted-foreground hover:bg-surface-2 hover:text-foreground rounded-md border p-1 transition"
          >
            {loading ? (
              <AverQelLoader size={12} className="text-primary" />
            ) : (
              <RefreshCw size={12} />
            )}
          </button>
        </div>
      </div>

      {error && (
        <div
          role="alert"
          className="m-3 rounded-lg border border-rose-300/15 bg-rose-500/10 p-2.5 text-xs text-rose-200"
        >
          {error}
        </div>
      )}
      {notice && !error && (
        <div
          role="status"
          className="m-3 rounded-lg border border-emerald-300/15 bg-emerald-500/10 p-2.5 text-xs text-emerald-200"
        >
          {notice}
        </div>
      )}

      <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto p-3 md:flex-row">
        <div className="border-border-subtle/80 bg-surface-1/40 w-full shrink-0 rounded-lg border p-2 md:w-56">
          <div className="text-muted-foreground mb-2 px-1 text-[9px] font-semibold uppercase">
            Installed workflows
          </div>
          {workflows.length === 0 ? (
            <p className="text-muted-foreground px-1 py-2 text-[11px]">
              No workflows installed yet. Create one to fan work out across agents.
            </p>
          ) : (
            <div className="space-y-1">
              {workflows.map((workflow) => (
                <button
                  key={workflow.name}
                  type="button"
                  onClick={() => void openWorkflow(workflow.name)}
                  className={`flex w-full items-center gap-2 rounded-md border px-2 py-1.5 text-left transition ${
                    selectedName === workflow.name
                      ? "border-primary/40 bg-primary/10 text-foreground"
                      : "border-transparent text-muted-foreground hover:bg-surface-2/60 hover:text-foreground"
                  }`}
                >
                  <GitBranch size={12} className="shrink-0" />
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-[11px] font-semibold">
                      {workflow.name}
                    </span>
                    <span className="text-muted-foreground block truncate text-[10px]">
                      {workflow.step_count} step{workflow.step_count === 1 ? "" : "s"}
                      {workflow.valid ? "" : " · invalid"}
                    </span>
                  </span>
                </button>
              ))}
            </div>
          )}
        </div>

        <div className="flex min-h-0 min-w-0 flex-1 flex-col">
          {selectedName ? (
            <>
              <div className="border-border-subtle/80 bg-surface-1/40 mb-2 flex flex-wrap items-center gap-2 rounded-lg border p-2">
                <label className="text-muted-foreground text-[10px]" htmlFor="workflow-name">
                  Name
                </label>
                <input
                  id="workflow-name"
                  value={selectedName}
                  onChange={(event) => setSelectedName(event.target.value)}
                  spellCheck={false}
                  className="border-border-subtle bg-surface-0 text-foreground min-w-0 flex-1 rounded-md border px-2 py-1 font-mono text-[11px] outline-none focus:border-primary/50"
                />
                <span className="text-muted-foreground text-[10px]">{stepCount} steps</span>
                <button
                  type="button"
                  onClick={() => void save()}
                  disabled={saving}
                  className="border-primary/40 bg-primary/15 text-primary inline-flex items-center gap-1 rounded-md border px-2 py-1 text-[10px] font-semibold transition disabled:opacity-50"
                >
                  {saving ? (
                    <AverQelLoader size={11} className="animate-spin" />
                  ) : (
                    <Save size={11} />
                  )}
                  <span>Save</span>
                </button>
                {selectedName !== NEW_WORKFLOW_NAME && (
                  <button
                    type="button"
                    onClick={() => void remove()}
                    disabled={saving}
                    aria-label={`Delete ${selectedName}`}
                    title="Delete workflow"
                    className="border-rose-500/30 text-rose-300 hover:bg-rose-500/10 inline-flex items-center gap-1 rounded-md border px-2 py-1 text-[10px] transition disabled:opacity-50"
                  >
                    <Trash2 size={11} />
                  </button>
                )}
              </div>
              <textarea
                value={source}
                onChange={(event) => setSource(event.target.value)}
                spellCheck={false}
                aria-label="Workflow document"
                className="border-border-subtle bg-surface-0 text-foreground min-h-[280px] flex-1 resize-y rounded-lg border p-3 font-mono text-[11px] leading-relaxed outline-none focus:border-primary/50"
              />
              <p className="text-muted-foreground mt-2 flex items-start gap-1.5 text-[10px]">
                <AlertTriangle size={11} className="mt-0.5 shrink-0" />
                <span>
                  A workflow document lists the steps and the dependencies between them. Each
                  step runs as its own agent. Save to write the document to{" "}
                  <code className="font-mono">.averqel/workflows/</code> in this conversation&rsquo;s
                  workspace.
                </span>
              </p>
            </>
          ) : (
            <div className="text-muted-foreground flex h-full min-h-[240px] flex-col items-center justify-center gap-2 text-center">
              {loading ? (
                <AverQelLoader size={18} className="text-primary animate-spin" />
              ) : (
                <GitBranch size={22} className="text-muted-foreground/40" />
              )}
              <p className="text-foreground text-xs font-semibold">No workflow open</p>
              <p className="text-muted-foreground max-w-xs text-[11px]">
                Pick an installed workflow to edit it, or create a new one. A workflow fans
                work out across several agents.
              </p>
              {!loading && (
                <button
                  type="button"
                  onClick={startNew}
                  className="border-primary/40 bg-primary/15 text-primary mt-1 inline-flex items-center gap-1 rounded-md border px-3 py-1.5 text-[11px] font-semibold transition"
                >
                  <Plus size={12} />
                  <span>New workflow</span>
                </button>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
