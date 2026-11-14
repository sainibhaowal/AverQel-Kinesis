import { fetchWithAuth } from "@/lib/api";

/** One workflow document installed in a workspace. */
export interface WorkflowSummary {
  name: string;
  path: string;
  valid: boolean;
  step_count: number;
  description: string;
}

/** One workflow document read in full. */
export interface WorkflowDocument {
  name: string;
  path: string;
  document: Record<string, unknown>;
  content: string;
}

/** The endpoint prefix of one conversation's workspace routes. */
function chatBase(chatId: string): string {
  return `/deepspace/chats/${encodeURIComponent(chatId)}`;
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
    if (typeof payload.message === "string" && payload.message.trim()) return payload.message.trim();
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

/**
 * List the workflow documents one workspace holds.
 *
 * @param chatId - The conversation whose workspace is read.
 * @returns The installed workflow documents.
 */
export async function listWorkflows(chatId: string): Promise<WorkflowSummary[]> {
  const payload = await requestJson(`${chatBase(chatId)}/workspace/workflows`);
  const workflows = isRecord(payload) ? payload.workflows : undefined;
  if (!Array.isArray(workflows)) return [];
  return workflows.flatMap((item): WorkflowSummary[] => {
    if (!isRecord(item)) return [];
    return [
      {
        name: asString(item.name),
        path: asString(item.path),
        valid: item.valid === true,
        step_count: asNumber(item.step_count),
        description: asString(item.description),
      },
    ];
  });
}

/**
 * Read one workflow document in full.
 *
 * @param chatId - The conversation whose workspace is read.
 * @param name - The workflow document's name.
 * @returns The document and its rendered source.
 */
export async function readWorkflow(chatId: string, name: string): Promise<WorkflowDocument> {
  const payload = await requestJson(
    `${chatBase(chatId)}/workspace/workflows/${encodeURIComponent(name)}`,
  );
  const record = isRecord(payload) ? payload : {};
  return {
    name: asString(record.name),
    path: asString(record.path),
    document: isRecord(record.document) ? record.document : {},
    content: asString(record.content),
  };
}

/**
 * Create or replace one workflow document.
 *
 * @param chatId - The conversation whose workspace is written.
 * @param name - The workflow document's name.
 * @param document - The document to store.
 * @returns The stored document's name and step count.
 */
export async function writeWorkflow(
  chatId: string,
  name: string,
  document: Record<string, unknown>,
): Promise<{ name: string; step_count: number }> {
  const payload = await requestJson(
    `${chatBase(chatId)}/workspace/workflows/${encodeURIComponent(name)}`,
    { method: "PUT", body: JSON.stringify({ document }) },
  );
  const record = isRecord(payload) ? payload : {};
  return { name: asString(record.name), step_count: asNumber(record.step_count) };
}

/**
 * Delete one workflow document.
 *
 * @param chatId - The conversation whose workspace is written.
 * @param name - The workflow document's name.
 */
export async function deleteWorkflow(chatId: string, name: string): Promise<void> {
  await requestJson(`${chatBase(chatId)}/workspace/workflows/${encodeURIComponent(name)}`, {
    method: "DELETE",
  });
}
