import { fetchWithAuth, getRequestTenantId, resolveApiUrl } from "@/lib/api";

/** A file or directory inside a conversation workspace. */
export interface WorkspaceEntry {
  name: string;
  path: string;
  kind: "file" | "directory";
  size_bytes: number;
}

/** The text content and metadata of one workspace file. */
export interface WorkspaceFile {
  path: string;
  content: string;
  truncated: boolean;
  language: string | null;
  size_bytes?: number;
  mime_type?: string;
  is_binary?: boolean;
  preview_url?: string | null;
  preview_error?: string | null;
  archive_entries?:
    | {
        name: string;
        directory: boolean;
        compressed_size: number;
        size: number;
      }[]
    | null;
}

/** The shells a terminal session can run under. */
export type TerminalShell = "bash" | "pwsh" | "cmd";

/** A shell the current host can actually launch. */
export interface TerminalShellOption {
  id: TerminalShell;
  label: string;
}

/** A live terminal session owned by a conversation. */
export interface TerminalSession {
  session_id: string;
  shell: TerminalShell;
  pty: boolean;
  cwd: string;
  created_at: string;
  idle_seconds: number;
}

/** The buffered output of a terminal session. */
export interface TerminalOutput {
  output: string;
  truncated: boolean;
  dropped_bytes: number;
  alive: boolean;
}

/** The session the server created for a new terminal. */
export interface OpenedTerminal {
  session_id: string;
  shell: TerminalShell;
  pty: boolean;
  cwd: string;
  output: string;
}

/** The number of bytes a write to a terminal session accepted. */
export interface TerminalWriteResult {
  written_bytes: number;
}

/** Whether a terminal session was closed. */
export interface TerminalCloseResult {
  closed: boolean;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function asString(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function asNumber(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

function asShell(value: unknown): TerminalShell {
  return value === "pwsh" || value === "cmd" ? value : "bash";
}

function asShellOptions(value: unknown): TerminalShellOption[] {
  if (!Array.isArray(value)) return [];
  return value
    .filter((item): item is TerminalShell => item === "bash" || item === "pwsh" || item === "cmd")
    .map((id) => ({ id, label: id }));
}

/** The endpoint prefix of one conversation's workspace and terminal routes. */
function chatBase(chatId: string): string {
  return `/deepspace/chats/${encodeURIComponent(chatId)}`;
}

/** Append the query parameters that carry a value to an endpoint. */
function withQuery(path: string, params: Record<string, string | number | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === "") continue;
    search.set(key, String(value));
  }
  const query = search.toString();
  return query ? `${path}?${query}` : path;
}

/**
 * Read the message the server sent with a failed response.
 *
 * @param response - The failed response whose body carries the message.
 * @returns The server message, or null when the body carries none.
 */
async function readErrorMessage(response: Response): Promise<string | null> {
  try {
    const payload: unknown = await response.json();
    if (!isRecord(payload)) return null;
    const error = payload.error;
    if (isRecord(error) && typeof error.message === "string" && error.message.trim()) {
      return error.message.trim();
    }
    if (typeof payload.detail === "string" && payload.detail.trim()) return payload.detail.trim();
    if (typeof payload.message === "string" && payload.message.trim())
      return payload.message.trim();
  } catch {
    // A gateway can answer with a non-JSON body; the status fallback is used then.
  }
  return null;
}

/**
 * Send one request and read its JSON body.
 *
 * @param path - The endpoint, relative to the API root.
 * @param init - The request options, when the call is not a plain GET.
 * @returns The parsed response body.
 * @throws Error When the response is not ok; the message is the server's own when it sent one.
 */
async function requestJson(path: string, init?: RequestInit): Promise<unknown> {
  const response = (await fetchWithAuth(path, init)) as Response;
  if (!response.ok) {
    const message = await readErrorMessage(response);
    throw new Error(message ?? `The request failed (HTTP ${response.status}).`);
  }
  return (await response.json()) as unknown;
}

function toWorkspaceEntry(value: unknown): WorkspaceEntry {
  const record = isRecord(value) ? value : {};
  return {
    name: asString(record.name),
    path: asString(record.path),
    kind: record.kind === "directory" ? "directory" : "file",
    size_bytes: asNumber(record.size_bytes),
  };
}

function toTerminalSession(value: unknown): TerminalSession {
  const record = isRecord(value) ? value : {};
  return {
    session_id: asString(record.session_id),
    shell: asShell(record.shell),
    pty: record.pty === true,
    cwd: asString(record.cwd),
    created_at: asString(record.created_at),
    idle_seconds: asNumber(record.idle_seconds),
  };
}

export interface WorkspaceTreeResult {
  entries: WorkspaceEntry[];
  root?: string;
}

/**
 * List the entries and root path of one workspace directory.
 *
 * @param chatId - The conversation whose workspace is read.
 * @param path - The directory relative to the workspace root; the root when omitted.
 * @returns The directory entries and root path.
 */
export async function listWorkspaceTree(
  chatId: string,
  path?: string,
): Promise<WorkspaceTreeResult> {
  const payload = await requestJson(
    withQuery(`${chatBase(chatId)}/workspace/tree`, { path: path || undefined }),
  );
  const entries = isRecord(payload) ? payload.entries : undefined;
  const root = isRecord(payload) && typeof payload.root === "string" ? payload.root : undefined;
  return {
    entries: Array.isArray(entries) ? entries.map(toWorkspaceEntry) : [],
    root,
  };
}

/**
 * List the entries of one workspace directory.
 *
 * @param chatId - The conversation whose workspace is read.
 * @param path - The directory relative to the workspace root; the root when omitted.
 * @returns The directory entries, empty when the server sent none.
 */
export async function listWorkspaceEntries(
  chatId: string,
  path?: string,
): Promise<WorkspaceEntry[]> {
  const result = await listWorkspaceTree(chatId, path);
  return result.entries;
}

/**
 * Read the text content of one workspace file.
 *
 * @param chatId - The conversation whose workspace is read.
 * @param path - The file path relative to the workspace root.
 * @returns The file content and its metadata.
 */
export async function readWorkspaceFile(chatId: string, path: string): Promise<WorkspaceFile> {
  const payload = await requestJson(withQuery(`${chatBase(chatId)}/workspace/file`, { path }));
  const record = isRecord(payload) ? payload : {};
  return {
    path: asString(record.path),
    content: asString(record.content),
    truncated: record.truncated === true,
    language: typeof record.language === "string" ? record.language : null,
    size_bytes: typeof record.size_bytes === "number" ? record.size_bytes : undefined,
    mime_type: typeof record.mime_type === "string" ? record.mime_type : undefined,
    is_binary: record.is_binary === true,
    archive_entries: Array.isArray(record.archive_entries)
      ? record.archive_entries.flatMap((entry) => {
          if (!isRecord(entry) || typeof entry.name !== "string") return [];
          return [
            {
              name: entry.name,
              directory: entry.directory === true,
              compressed_size: asNumber(entry.compressed_size),
              size: asNumber(entry.size),
            },
          ];
        })
      : null,
  };
}

function needsRawWorkspacePreview(file: WorkspaceFile): boolean {
  const mimeType = (file.mime_type ?? "").toLowerCase();
  const extension = file.path.split(".").pop()?.toLowerCase() ?? "";
  const richBinaryExtensions = new Set([
    "doc",
    "docx",
    "docm",
    "dot",
    "dotx",
    "dotm",
    "odt",
    "ott",
    "odm",
    "oth",
    "rtf",
    "ppt",
    "pptx",
    "pptm",
    "pot",
    "potx",
    "potm",
    "pps",
    "ppsx",
    "ppsm",
    "odp",
    "otp",
    "xls",
    "xlsx",
    "xlsm",
    "xlt",
    "xltx",
    "xltm",
    "xlm",
    "xla",
    "xlw",
    "xlsb",
    "ods",
    "ots",
    "odg",
    "otg",
    "odc",
    "otc",
    "odf",
    "otf",
    "odi",
    "oti",
    "pdf",
    "zip",
    "7z",
    "rar",
  ]);
  const isBrowserImage =
    mimeType.startsWith("image/") &&
    !mimeType.startsWith("image/x-portable-") &&
    mimeType !== "image/x-portable-pixmap";
  return (
    richBinaryExtensions.has(extension) ||
    mimeType === "application/pdf" ||
    isBrowserImage ||
    mimeType.startsWith("video/") ||
    mimeType.startsWith("audio/") ||
    mimeType.includes("spreadsheetml") ||
    mimeType.includes("wordprocessingml") ||
    mimeType.includes("presentationml") ||
    mimeType.includes("opendocument.spreadsheet") ||
    mimeType.includes("opendocument.text") ||
    mimeType.includes("opendocument.presentation") ||
    mimeType.includes("msword") ||
    mimeType.includes("ms-powerpoint") ||
    mimeType.includes("ms-excel") ||
    mimeType === "application/rtf" ||
    mimeType === "application/zip" ||
    mimeType === "application/x-7z-compressed" ||
    mimeType === "application/x-rar-compressed"
  );
}

/** Read a workspace file and attach an authenticated raw payload for rich previews. */
export async function readWorkspaceFileForPreview(
  chatId: string,
  path: string,
): Promise<WorkspaceFile> {
  const file = await readWorkspaceFile(chatId, path);
  if (!needsRawWorkspacePreview(file)) return file;

  try {
    const blob = await fetchWorkspaceFileBlobUrl(chatId, path, file.mime_type);
    return {
      ...file,
      mime_type: blob.contentType || file.mime_type,
      preview_url: blob.objectUrl,
    };
  } catch (cause) {
    return {
      ...file,
      preview_error:
        cause instanceof Error ? cause.message : "The raw file preview could not be loaded.",
    };
  }
}

/** Build the authenticated API path for raw workspace file content or download. */
export function workspaceFileRawPath(chatId: string, path: string, download = false): string {
  return withQuery(`${chatBase(chatId)}/workspace/file/raw`, {
    path,
    download: download ? "true" : undefined,
  });
}

/** Fetch a workspace file via authenticated request and return an object URL for previewing. */
export async function fetchWorkspaceFileBlobUrl(
  chatId: string,
  path: string,
  mimeTypeHint?: string,
): Promise<{ objectUrl: string; contentType: string; sizeBytes: number }> {
  const response = await fetchWithAuth(workspaceFileRawPath(chatId, path, false), {
    timeoutMs: 30_000,
  });
  if (!response.ok) {
    throw new Error(`Request failed with status ${response.status}`);
  }
  const rawBlob = await response.blob();
  const contentType =
    response.headers.get("Content-Type")?.split(";", 1)[0]?.trim() ||
    rawBlob.type ||
    mimeTypeHint ||
    "application/octet-stream";
  const typedBlob =
    rawBlob.type === contentType ? rawBlob : new Blob([rawBlob], { type: contentType });
  return {
    objectUrl: URL.createObjectURL(typedBlob),
    contentType,
    sizeBytes: typedBlob.size,
  };
}

/** Trigger an authenticated browser download for any workspace file. */
export async function downloadWorkspaceFile(
  chatId: string,
  path: string,
  filename?: string,
): Promise<void> {
  const response = await fetchWithAuth(workspaceFileRawPath(chatId, path, true), {
    timeoutMs: 60_000,
  });
  if (!response.ok) {
    throw new Error(`Download failed with status ${response.status}`);
  }
  const blob = await response.blob();
  const objectUrl = URL.createObjectURL(blob);
  try {
    const anchor = document.createElement("a");
    anchor.href = objectUrl;
    anchor.download = filename || path.split(/[\\/]/).filter(Boolean).at(-1) || "workspace-file";
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
  } finally {
    URL.revokeObjectURL(objectUrl);
  }
}

/**
 * List the live terminal sessions of a conversation.
 *
 * @param chatId - The conversation whose sessions are listed.
 * @returns The live sessions, empty when the server sent none.
 */
export async function listTerminals(chatId: string): Promise<TerminalSession[]> {
  const payload = await requestJson(`${chatBase(chatId)}/terminals`);
  const sessions = isRecord(payload) ? payload.sessions : undefined;
  return Array.isArray(sessions) ? sessions.map(toTerminalSession) : [];
}

/**
 * List the shells installed on the host running the terminal service.
 *
 * @param chatId - The conversation whose terminal capability is queried.
 * @returns The available shells and the host's preferred default.
 */
export async function listTerminalShells(chatId: string): Promise<{
  shells: TerminalShellOption[];
  defaultShell: TerminalShell | null;
}> {
  const payload = await requestJson(`${chatBase(chatId)}/terminals/shells`);
  const record = isRecord(payload) ? payload : {};
  const shells = asShellOptions(record.shells);
  const defaultValue = record.default_shell;
  return {
    shells,
    defaultShell:
      defaultValue === "bash" || defaultValue === "pwsh" || defaultValue === "cmd"
        ? defaultValue
        : (shells[0]?.id ?? null),
  };
}

/**
 * Open a new terminal session for a conversation.
 *
 * @param chatId - The conversation that owns the session.
 * @param shell - The shell to run; the server default when omitted.
 * @param cwd - The working directory to start in; the workspace root when omitted.
 * @returns The created session and the output it produced so far.
 */
export async function openTerminal(
  chatId: string,
  shell?: TerminalShell,
  cwd?: string,
): Promise<OpenedTerminal> {
  const body: Record<string, string> = {};
  if (shell) body.shell = shell;
  if (cwd) body.cwd = cwd;
  const payload = await requestJson(`${chatBase(chatId)}/terminals`, {
    method: "POST",
    body: JSON.stringify(body),
  });
  const record = isRecord(payload) ? payload : {};
  return {
    session_id: asString(record.session_id),
    shell: asShell(record.shell),
    pty: record.pty === true,
    cwd: asString(record.cwd),
    output: asString(record.output),
  };
}

/**
 * Build the authenticated WebSocket URL for one terminal stream.
 *
 * @param chatId - The conversation that owns the session.
 * @param sessionId - The terminal session to stream.
 * @returns A WebSocket URL carrying the same access context as API requests.
 */
export function terminalWebSocketUrl(chatId: string, sessionId: string): string {
  const endpoint = resolveApiUrl(
    `${chatBase(chatId)}/terminals/${encodeURIComponent(sessionId)}/stream`,
  );
  const url = new URL(endpoint, window.location.href);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  const token = localStorage.getItem("averqel_token");
  const tenantId = getRequestTenantId(token);
  if (token) url.searchParams.set("token", token);
  if (tenantId) url.searchParams.set("tenant_id", tenantId);
  return url.toString();
}

/**
 * Read the buffered output of one terminal session.
 *
 * @param chatId - The conversation that owns the session.
 * @param sessionId - The terminal session to read.
 * @param maxBytes - The largest output to return; the server default when omitted.
 * @returns The buffered output and whether the session is still alive.
 */
export async function readTerminal(
  chatId: string,
  sessionId: string,
  maxBytes?: number,
): Promise<TerminalOutput> {
  const payload = await requestJson(
    withQuery(`${chatBase(chatId)}/terminals/${encodeURIComponent(sessionId)}`, {
      max_bytes: maxBytes,
    }),
  );
  const record = isRecord(payload) ? payload : {};
  return {
    output: asString(record.output),
    truncated: record.truncated === true,
    dropped_bytes: asNumber(record.dropped_bytes),
    alive: record.alive === true,
  };
}

/**
 * Write keystrokes to one terminal session.
 *
 * @param chatId - The conversation that owns the session.
 * @param sessionId - The terminal session to write to.
 * @param data - The keystrokes to write, exactly as the user typed them.
 * @returns The number of bytes the session accepted.
 */
export async function writeTerminal(
  chatId: string,
  sessionId: string,
  data: string,
): Promise<TerminalWriteResult> {
  const payload = await requestJson(
    `${chatBase(chatId)}/terminals/${encodeURIComponent(sessionId)}`,
    {
      method: "POST",
      body: JSON.stringify({ data }),
    },
  );
  const record = isRecord(payload) ? payload : {};
  return { written_bytes: asNumber(record.written_bytes) };
}

/**
 * Close one terminal session.
 *
 * @param chatId - The conversation that owns the session.
 * @param sessionId - The terminal session to close.
 * @returns Whether the session was closed.
 */
export async function closeTerminal(
  chatId: string,
  sessionId: string,
): Promise<TerminalCloseResult> {
  const payload = await requestJson(
    `${chatBase(chatId)}/terminals/${encodeURIComponent(sessionId)}`,
    { method: "DELETE" },
  );
  const record = isRecord(payload) ? payload : {};
  return { closed: record.closed === true };
}
