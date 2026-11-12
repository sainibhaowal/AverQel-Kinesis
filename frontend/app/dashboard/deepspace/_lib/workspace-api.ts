import { fetchWithAuth } from "@/lib/api";

/** One workspace a user may work in. */
export interface Workspace {
  /** The stable UUID identifier the server records. */
  id: string;
  /** The filesystem directory name (slug, derived from title at creation). */
  slug: string;
  /** The display name the menu shows. */
  name: string;
  /** The absolute path of the workspace on the host. */
  path: string;
  /** Optional custom project directory path. */
  custom_path?: string | null;
  /** Whether the workspace is the one every conversation starts in. */
  is_default: boolean;
  /** Whether the registered directory still exists. */
  status: "ok" | "missing-dir";
}

/** One conversation grouped under a project in the sidebar. */
export interface WorkspaceProjectSession {
  id: string;
  title: string;
  updated_at: string;
}

/** One project and the conversations that work in it. */
export interface WorkspaceProject extends Workspace {
  sessions: WorkspaceProjectSession[];
}

/** The workspaces a user holds, and the one a new conversation starts in. */
export interface WorkspaceCatalogue {
  workspaces: Workspace[];
  defaultWorkspace: string;
}

/** The workspace one conversation uses. */
export interface ConversationWorkspace {
  /** The workspace in force. */
  workspace: Workspace | null;
  /** The workspace the conversation recorded, when it recorded one. */
  recorded: string | null;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function asString(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function toWorkspace(value: unknown): Workspace {
  const record = isRecord(value) ? value : {};
  return {
    id: asString(record.id),
    slug: asString(record.slug),
    name: asString(record.name),
    path: asString(record.path),
    custom_path: typeof record.custom_path === "string" ? record.custom_path : null,
    is_default: record.is_default === true,
    status: record.status === "missing-dir" ? "missing-dir" : "ok",
  };
}

/**
 * Parse one project and its grouped conversations.
 *
 * @param value - The server projection.
 * @returns The project.
 */
function toWorkspaceProject(value: unknown): WorkspaceProject {
  const record = isRecord(value) ? value : {};
  const sessions = Array.isArray(record.sessions)
    ? record.sessions.filter(isRecord).map((session) => ({
        id: asString(session.id),
        title: asString(session.title),
        updated_at: asString(session.updated_at),
      }))
    : [];
  return { ...toWorkspace(record), sessions };
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
    if (isRecord(payload)) {
      const error = payload.error;
      if (isRecord(error) && typeof error.message === "string" && error.message.trim()) {
        return error.message.trim();
      }
      if (typeof payload.detail === "string" && payload.detail.trim()) return payload.detail.trim();
    }
  } catch {
    // A gateway can answer with a non-JSON body; the status fallback is used then.
  }
  return null;
}

/**
 * List the workspaces the signed-in user holds.
 *
 * @returns The workspaces and the one a new conversation starts in.
 */
export async function listWorkspaces(): Promise<WorkspaceCatalogue> {
  const response = (await fetchWithAuth("/deepspace/chats/workspaces")) as Response;
  if (!response.ok) {
    const message = await readErrorMessage(response);
    throw new Error(message ?? `The workspaces could not be loaded (HTTP ${response.status}).`);
  }
  const payload: unknown = await response.json();
  const record = isRecord(payload) ? payload : {};
  const workspaces = Array.isArray(record.workspaces) ? record.workspaces.map(toWorkspace) : [];
  return { workspaces, defaultWorkspace: asString(record.default) || "default" };
}

/**
 * List the projects and the conversations grouped under each one.
 *
 * @returns The projects in the user's order.
 */
export async function listWorkspaceProjects(): Promise<WorkspaceProject[]> {
  const response = (await fetchWithAuth("/deepspace/chats/workspaces/projects")) as Response;
  if (!response.ok) {
    const message = await readErrorMessage(response);
    throw new Error(message ?? `The projects could not be loaded (HTTP ${response.status}).`);
  }
  const payload: unknown = await response.json();
  const record = isRecord(payload) ? payload : {};
  return Array.isArray(record.workspaces) ? record.workspaces.map(toWorkspaceProject) : [];
}

/**
 * Rename one workspace without changing its identity.
 *
 * @param workspaceId - The workspace to rename.
 * @param name - The new display name.
 * @returns The renamed workspace.
 */
export async function renameWorkspace(workspaceId: string, name: string): Promise<Workspace> {
  const response = (await fetchWithAuth(
    `/deepspace/chats/workspaces/${encodeURIComponent(workspaceId)}`,
    { method: "PATCH", body: JSON.stringify({ name }) },
  )) as Response;
  if (!response.ok) {
    const message = await readErrorMessage(response);
    throw new Error(message ?? `The workspace could not be renamed (HTTP ${response.status}).`);
  }
  const payload: unknown = await response.json();
  return toWorkspace(payload);
}

/**
 * Move one workspace before another in the user's order.
 *
 * @param workspaceId - The workspace to move.
 * @param beforeId - The workspace it should precede, or null to move it last.
 * @returns The workspaces in their new order.
 */
export async function moveWorkspace(
  workspaceId: string,
  beforeId: string | null,
): Promise<Workspace[]> {
  const response = (await fetchWithAuth(
    `/deepspace/chats/workspaces/${encodeURIComponent(workspaceId)}/order`,
    { method: "PUT", body: JSON.stringify({ before: beforeId }) },
  )) as Response;
  if (!response.ok) {
    const message = await readErrorMessage(response);
    throw new Error(message ?? `The workspace order could not be saved (HTTP ${response.status}).`);
  }
  const payload: unknown = await response.json();
  const record = isRecord(payload) ? payload : {};
  return Array.isArray(record.workspaces) ? record.workspaces.map(toWorkspace) : [];
}

/**
 * Replace the conversation order inside one workspace.
 *
 * @param workspaceId - The workspace whose order changes.
 * @param conversationIds - The conversation identifiers in their new order.
 */
export async function orderWorkspaceSessions(
  workspaceId: string,
  conversationIds: string[],
): Promise<void> {
  const response = (await fetchWithAuth(
    `/deepspace/chats/workspaces/${encodeURIComponent(workspaceId)}/sessions/order`,
    { method: "PUT", body: JSON.stringify({ conversation_ids: conversationIds }) },
  )) as Response;
  if (!response.ok) {
    const message = await readErrorMessage(response);
    throw new Error(
      message ?? `The conversation order could not be saved (HTTP ${response.status}).`,
    );
  }
}

export interface DirectoryEntry {
  name: string;
  path: string;
  hidden: boolean;
}

export interface DirectoryListing {
  path: string;
  home: string;
  crumbs: DirectoryEntry[];
  entries: DirectoryEntry[];
  truncated: boolean;
}

/**
 * List host directories for the workspace directory picker.
 *
 * @param path - Optional directory path to list. If omitted, lists the host user home.
 * @param signal - AbortSignal for cancellation.
 */
export async function browseDirectory(
  path?: string,
  signal?: AbortSignal,
): Promise<DirectoryListing> {
  const url = path
    ? `/deepspace/chats/workspaces/browse?path=${encodeURIComponent(path)}`
    : "/deepspace/chats/workspaces/browse";
  const response = (await fetchWithAuth(url, { signal })) as Response;
  if (!response.ok) {
    const message = await readErrorMessage(response);
    throw new Error(message ?? `Failed to browse directory (HTTP ${response.status}).`);
  }
  const payload: unknown = await response.json();
  const record = isRecord(payload) ? payload : {};
  const entries = Array.isArray(record.entries)
    ? (record.entries as Array<Record<string, unknown>>).map((e) => ({
        name: asString(e.name),
        path: asString(e.path),
        hidden: e.hidden === true,
      }))
    : [];
  const crumbs = Array.isArray(record.crumbs)
    ? (record.crumbs as Array<Record<string, unknown>>).map((c) => ({
        name: asString(c.name),
        path: asString(c.path),
        hidden: c.hidden === true,
      }))
    : [];
  return {
    path: asString(record.path),
    home: asString(record.home),
    crumbs,
    entries,
    truncated: record.truncated === true,
  };
}

/**
 * Create a new directory in the host filesystem.
 *
 * @param parentPath - Parent directory path.
 * @param name - Name of new directory.
 */
export async function createDirectory(parentPath: string, name: string): Promise<string> {
  const response = (await fetchWithAuth("/deepspace/chats/workspaces/create-directory", {
    method: "POST",
    body: JSON.stringify({ path: parentPath, name }),
  })) as Response;
  if (!response.ok) {
    const message = await readErrorMessage(response);
    throw new Error(message ?? `Could not create directory (HTTP ${response.status}).`);
  }
  const payload: unknown = await response.json();
  const record = isRecord(payload) ? payload : {};
  return asString(record.path);
}

/**
 * Create a workspace for the signed-in user.
 *
 * @param name - The name of the new workspace.
 * @param path - Optional local directory path on the machine.
 * @returns The workspace that was created.
 */
export async function createWorkspace(name: string, path?: string): Promise<Workspace> {
  const response = (await fetchWithAuth("/deepspace/chats/workspaces", {
    method: "POST",
    body: JSON.stringify({ name, path }),
  })) as Response;
  if (!response.ok) {
    const message = await readErrorMessage(response);
    throw new Error(message ?? `The workspace could not be created (HTTP ${response.status}).`);
  }
  const payload: unknown = await response.json();
  return toWorkspace(payload);
}

/**
 * Read the workspace one conversation uses.
 *
 * @param chatId - The conversation to read.
 * @returns The workspace in force and the one the conversation recorded.
 */
export async function readConversationWorkspace(chatId: string): Promise<ConversationWorkspace> {
  const response = (await fetchWithAuth(
    `/deepspace/chats/${encodeURIComponent(chatId)}/workspace`,
  )) as Response;
  if (!response.ok) {
    const message = await readErrorMessage(response);
    throw new Error(message ?? `The workspace could not be read (HTTP ${response.status}).`);
  }
  const payload: unknown = await response.json();
  const record = isRecord(payload) ? payload : {};
  return {
    workspace: record.workspace ? toWorkspace(record.workspace) : null,
    recorded: typeof record.recorded === "string" ? record.recorded : null,
  };
}

/**
 * Record the workspace one conversation uses.
 *
 * The server answers with the workspace that was recorded, so the caller
 * reads the full record back to learn the name and the path.
 *
 * @param chatId - The conversation to change.
 * @param workspace - The workspace to record.
 * @returns The workspace that is now in force.
 */
export async function writeConversationWorkspace(
  chatId: string,
  workspace: string,
): Promise<ConversationWorkspace> {
  const response = (await fetchWithAuth(
    `/deepspace/chats/${encodeURIComponent(chatId)}/workspace`,
    { method: "PUT", body: JSON.stringify({ workspace }) },
  )) as Response;
  if (!response.ok) {
    const message = await readErrorMessage(response);
    throw new Error(message ?? `The workspace could not be changed (HTTP ${response.status}).`);
  }
  const payload: unknown = await response.json();
  const record = isRecord(payload) ? payload : {};
  const recorded = typeof record.workspace === "string" ? record.workspace : workspace;
  return { workspace: null, recorded };
}

/**
 * Unlink/remove one named workspace from the DeepSpace registry.
 * Does not delete the actual folder or files on host storage.
 *
 * @param workspaceId - The identifier of the workspace to unlink.
 * @param stopSessions - Whether to close live terminal sessions first.
 */
export async function deleteWorkspace(workspaceId: string, stopSessions = false): Promise<void> {
  const query = stopSessions ? "?stop_sessions=true" : "";
  const response = (await fetchWithAuth(
    `/deepspace/chats/workspaces/${encodeURIComponent(workspaceId)}${query}`,
    {
      method: "DELETE",
    },
  )) as Response;
  // If the server returns 404, the workspace was already unlinked/removed.
  // Treat as idempotent success so the frontend unlinks it cleanly.
  if (!response.ok && response.status !== 404) {
    const message = await readErrorMessage(response);
    throw new Error(message ?? `Could not unlink workspace (HTTP ${response.status}).`);
  }
}
