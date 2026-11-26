import type {
  AgentStep,
  DeepSpaceMediaArtifact,
  DeepSpaceMessage,
  DeepSpaceWorkspaceFile,
  TimelineStep,
} from "./deepspace-stream";
import type { WorkspaceEntry } from "./workspace-panel-api";

export interface WrapChipItem extends DeepSpaceWorkspaceFile {
  artifact?: DeepSpaceMediaArtifact;
}

const FILE_WRITE_TOOLS = new Set([
  "write",
  "edit",
  "write_file",
  "edit_file",
  "fs_write",
  "fs_edit",
  "fs_str_replace",
  "create_file",
  "write_document",
  "append_file",
  "save_file",
]);

const SINGLE_PATH_KEYS = [
  "path",
  "file_path",
  "target_file",
  "output_path",
  "destination",
] as const;
const LIST_PATH_KEYS = [
  "paths",
  "output_paths",
  "files",
  "created_files",
  "workspace_files",
] as const;
const BACKTICK_PATH_RE = /`([^`\r\n]{2,260})`/g;
const MARKDOWN_LINK_RE = /\[[^\]]{1,120}\]\(([^)\r\n\s]{2,260})\)/g;
const EXTENSION_CANDIDATE_RE = /\.[A-Za-z0-9]{1,10}$/;

function stripFileScheme(raw: string): string {
  return raw.replace(/^file:\/\//i, "").trim();
}

/** Normalize a workspace path candidate relative to `workspaceRoot` when matched. */
export function normalizeWorkspaceFilePath(rawPath: string, workspaceRoot?: string | null): string {
  let cleaned = stripFileScheme(rawPath).replace(/\\/g, "/").replace(/^\.\//, "");
  cleaned = cleaned.replace(/[),;:'"]+$/, "");
  if (!cleaned) return "";
  if (workspaceRoot) {
    const normalizedRoot = stripFileScheme(workspaceRoot).replace(/\\/g, "/").replace(/\/+$/, "");
    if (normalizedRoot && cleaned.startsWith(`${normalizedRoot}/`)) {
      cleaned = cleaned.slice(normalizedRoot.length + 1);
    }
  }
  return cleaned;
}

function basenameOf(pathValue: string): string {
  return pathValue.split("/").filter(Boolean).at(-1) ?? pathValue;
}

function extensionOf(name: string): string {
  const dot = name.lastIndexOf(".");
  return dot > 0 ? name.slice(dot + 1).toLowerCase() : "";
}

function isWebOrAnchorTarget(candidate: string): boolean {
  return (
    /^(https?:|mailto:|data:|blob:|javascript:)/i.test(candidate) ||
    candidate.startsWith("#") ||
    candidate.includes("://")
  );
}

function buildEntryLookup(
  entries: WorkspaceEntry[] | undefined,
  workspaceRoot?: string | null,
): {
  byPath: Map<string, WorkspaceEntry>;
  byBasename: Map<string, WorkspaceEntry | null>;
} {
  const byPath = new Map<string, WorkspaceEntry>();
  const byBasename = new Map<string, WorkspaceEntry | null>();
  for (const entry of entries ?? []) {
    if (entry.kind !== "file") continue;
    const rel = normalizeWorkspaceFilePath(entry.path, workspaceRoot);
    if (!rel) continue;
    byPath.set(rel.toLowerCase(), { ...entry, path: rel });
    const base = basenameOf(rel).toLowerCase();
    if (!byBasename.has(base)) {
      byBasename.set(base, { ...entry, path: rel });
    } else {
      // Mark ambiguous duplicate basenames across different directories.
      byBasename.set(base, null);
    }
  }
  return { byPath, byBasename };
}

function addStringsFromRecord(
  record: Record<string, unknown>,
  target: string[],
  singleKeys: readonly string[] = SINGLE_PATH_KEYS,
  listKeys: readonly string[] = LIST_PATH_KEYS,
) {
  for (const key of singleKeys) {
    const val = record[key];
    if (typeof val === "string" && val.trim()) {
      target.push(val.trim());
    }
  }
  for (const key of listKeys) {
    const list = record[key];
    if (!Array.isArray(list)) continue;
    for (const item of list) {
      if (typeof item === "string" && item.trim()) {
        target.push(item.trim());
      } else if (
        item &&
        typeof item === "object" &&
        typeof (item as { path?: unknown }).path === "string"
      ) {
        target.push((item as { path: string }).path.trim());
      }
    }
  }
}

function extractStepPaths(steps: Array<AgentStep | TimelineStep>): string[] {
  const extracted: string[] = [];
  for (const step of steps) {
    if (step.status === "failed") continue;
    if (step.diffStats?.path) {
      extracted.push(step.diffStats.path);
    }
    const toolName = (step.toolName ?? "").trim().toLowerCase();
    const input = step.toolInput;
    if (input && typeof input === "object") {
      if (toolName === "present" || FILE_WRITE_TOOLS.has(toolName)) {
        addStringsFromRecord(input, extracted);
      }
    }
    const rawOutput = step.toolOutput?.trim();
    if (rawOutput && rawOutput.startsWith("{")) {
      try {
        const parsed = JSON.parse(rawOutput) as unknown;
        if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
          addStringsFromRecord(parsed as Record<string, unknown>, extracted);
        }
      } catch {
        // Non-JSON tool output is ignored cleanly.
      }
    }
  }
  return extracted;
}

function extractMarkdownCandidatePaths(content: string): string[] {
  if (!content) return [];
  const candidates: string[] = [];
  for (const regex of [BACKTICK_PATH_RE, MARKDOWN_LINK_RE]) {
    regex.lastIndex = 0;
    let match: RegExpExecArray | null;
    while ((match = regex.exec(content)) !== null) {
      const raw = (match[1] ?? "").trim();
      if (!raw || raw.includes(" ") || isWebOrAnchorTarget(raw)) continue;
      if (!EXTENSION_CANDIDATE_RE.test(raw)) continue;
      candidates.push(raw);
    }
  }
  return candidates;
}

function artifactFilename(artifact: DeepSpaceMediaArtifact): string {
  const cleanTitle = artifact.title.trim() || `${artifact.kind}-${artifact.id}`;
  if (extensionOf(cleanTitle)) return cleanTitle;
  const subType = artifact.content_type.split("/")[1]?.split(";")[0]?.trim() ?? "";
  if (subType === "svg+xml") return `${cleanTitle}.svg`;
  if (subType === "plain") return `${cleanTitle}.txt`;
  if (subType && /^[a-z0-9]{2,6}$/i.test(subType)) return `${cleanTitle}.${subType}`;
  return cleanTitle;
}

/**
 * Resolve all workspace file & artifact wrap chips for an assistant message.
 * Combines backend `workspaceFiles`, tool inputs/outputs, confirmed workspace
 * references from markdown content (for historical messages), and ready artifacts.
 */
export function collectMessageWrapFiles({
  message,
  workspaceEntries,
  workspaceRoot,
}: {
  message: DeepSpaceMessage;
  workspaceEntries?: WorkspaceEntry[];
  workspaceRoot?: string | null;
}): WrapChipItem[] {
  const { byPath, byBasename } = buildEntryLookup(workspaceEntries, workspaceRoot);
  const normalizedRoot = workspaceRoot
    ? stripFileScheme(workspaceRoot).replace(/\\/g, "/").replace(/\/+$/, "")
    : "";

  const seenPaths = new Set<string>();
  const seenNames = new Set<string>();
  const items: WrapChipItem[] = [];

  const pushFile = (entry: WrapChipItem) => {
    const normPath = normalizeWorkspaceFilePath(entry.path, workspaceRoot);
    if (!normPath) return;
    const key = normPath.toLowerCase();
    if (seenPaths.has(key)) return;
    const matched =
      byPath.get(key) ?? (!normPath.includes("/") ? (byBasename.get(key) ?? undefined) : undefined);
    const resolvedPath = matched?.path ?? normPath;
    if (seenPaths.has(resolvedPath.toLowerCase())) return;
    seenPaths.add(key);
    seenPaths.add(resolvedPath.toLowerCase());
    const resolvedName = entry.name || basenameOf(resolvedPath);
    seenNames.add(resolvedName.toLowerCase());
    items.push({
      ...entry,
      path: resolvedPath,
      name: resolvedName,
      extension: entry.extension || extensionOf(resolvedName),
      size_bytes: entry.size_bytes > 0 ? entry.size_bytes : (matched?.size_bytes ?? 0),
    });
  };

  for (const file of message.workspaceFiles ?? []) {
    pushFile(file);
  }

  const steps: Array<AgentStep | TimelineStep> = [
    ...(message.agentSteps ?? []),
    ...(message.timeline ?? []),
  ];
  for (const rawPath of extractStepPaths(steps)) {
    const normPath = normalizeWorkspaceFilePath(rawPath, workspaceRoot);
    if (!normPath) continue;
    const name = basenameOf(normPath);
    pushFile({
      path: normPath,
      name,
      extension: extensionOf(name),
      mime_type: "application/octet-stream",
      size_bytes: 0,
    });
  }

  for (const rawCandidate of extractMarkdownCandidatePaths(message.content)) {
    const norm = normalizeWorkspaceFilePath(rawCandidate, workspaceRoot);
    if (!norm) continue;
    const exactMatch = byPath.get(norm.toLowerCase());
    const baseMatch = !norm.includes("/")
      ? (byBasename.get(norm.toLowerCase()) ?? undefined)
      : undefined;
    const insideExplicitRoot = Boolean(
      normalizedRoot &&
      stripFileScheme(rawCandidate).replace(/\\/g, "/").startsWith(`${normalizedRoot}/`),
    );
    const matched = exactMatch ?? baseMatch;
    if (!matched && !insideExplicitRoot) continue;
    const resolvedPath = matched?.path ?? norm;
    const name = basenameOf(resolvedPath);
    pushFile({
      path: resolvedPath,
      name,
      extension: extensionOf(name),
      mime_type: "application/octet-stream",
      size_bytes: matched?.size_bytes ?? 0,
    });
  }

  for (const artifact of message.artifacts ?? []) {
    if (artifact.status !== "ready") continue;
    const name = artifactFilename(artifact);
    if (seenNames.has(name.toLowerCase())) continue;
    seenNames.add(name.toLowerCase());
    items.push({
      path: artifact.url,
      name,
      extension: extensionOf(name),
      mime_type: artifact.content_type || "application/octet-stream",
      size_bytes: artifact.size_bytes ?? 0,
      artifact,
    });
  }

  return items;
}
