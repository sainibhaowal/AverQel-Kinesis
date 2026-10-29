import { fetchWithAuth } from "@/lib/api";

/** The conversation fields the fork route returns to the workspace. */
export interface ForkedConversation {
  id: string;
  title: string;
  content_html?: string | null;
  updated_at: string;
}

/** The optional boundary used when creating a conversation fork. */
export interface ConversationForkOptions {
  messageId?: string;
  turn?: number;
  title?: string;
  copyFiles?: boolean;
}

/**
 * Create a conversation fork at a durable message or turn boundary.
 *
 * @param conversationId - The source conversation.
 * @param options - The boundary and copy settings.
 * @returns The newly created conversation.
 * @throws Error When the API rejects the fork.
 */
export async function forkConversation(
  conversationId: string,
  options: ConversationForkOptions = {},
): Promise<ForkedConversation> {
  const body = {
    ...(options.messageId ? { message_id: options.messageId } : {}),
    ...(options.turn !== undefined ? { turn: options.turn } : {}),
    ...(options.title ? { title: options.title } : {}),
    ...(options.copyFiles !== undefined ? { copy_files: options.copyFiles } : {}),
  };
  const response = (await fetchWithAuth(
    `/deepspace/chats/${encodeURIComponent(conversationId)}/fork`,
    { method: "POST", body: JSON.stringify(body) },
  )) as Response;
  if (!response.ok) {
    throw new Error(`The conversation fork could not be created (HTTP ${response.status}).`);
  }
  return (await response.json()) as ForkedConversation;
}
