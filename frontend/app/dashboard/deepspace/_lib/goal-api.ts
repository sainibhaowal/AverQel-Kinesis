import { fetchWithAuth } from "@/lib/api";

/** The lifecycle phase a goal is in. */
export type GoalPhase = "active" | "paused" | "blocked" | "complete";

/** Whether the continuation driver may start another round for a goal. */
export type GoalActivation = "armed" | "disarmed";

/** One goal a conversation holds. */
export interface ConversationGoal {
  goal_id: string;
  name: string;
  content?: string | null;
  phase: GoalPhase;
  revision: number;
  rounds_started: number;
  max_goal_rounds: number;
  blocked_reason?: Record<string, unknown> | null;
  note?: string | null;
  activation?: GoalActivation;
  created_at?: string | null;
  updated_at?: string | null;
}

/** The result of one `/goal` command. */
export interface GoalCommandResult {
  success: boolean;
  message: string;
  error_code: string | null;
  data: {
    action?: string;
    goal?: ConversationGoal | null;
    goals?: ConversationGoal[];
    next_commands?: string[];
    goal_id?: string;
  };
}

/**
 * Read the goals a conversation holds.
 *
 * @param conversationId - The conversation to read.
 * @returns Every goal the conversation holds.
 * @throws Error When the API rejects the read.
 */
export async function fetchConversationGoals(conversationId: string): Promise<ConversationGoal[]> {
  const response = (await fetchWithAuth(
    `/deepspace/chats/${encodeURIComponent(conversationId)}/goals`,
    { method: "GET" },
  )) as Response;
  if (!response.ok) {
    throw new Error(`The goals could not be loaded (HTTP ${response.status}).`);
  }
  const payload = await response.json();
  return Array.isArray(payload) ? (payload as ConversationGoal[]) : [];
}

/**
 * Carry out one `/goal` command against a conversation's goals.
 *
 * The command never spends a model turn. Its mutations are ordinary durable
 * goal changes, so the goal bar and the model read the same state afterwards.
 *
 * @param conversationId - The conversation the goals belong to.
 * @param input - The text after the `/goal` command name.
 * @param goalName - The goal a control word acts on. The slash command acts
 *   on the conversation's current goal; an interface that shows several
 *   goals names the one the person chose.
 * @param attachments - Optional list of attachment objects (from the composer)
 *   to submit alongside the goal objective. Only used for `create` and `edit`.
 * @returns What the command did.
 * @throws Error When the API rejects the command.
 */
export async function runGoalCommand(
  conversationId: string,
  input: string,
  goalName?: string,
  attachments?: Array<Record<string, unknown>>,
): Promise<GoalCommandResult> {
  const body: { input: string; goal_name?: string; attachments?: Array<Record<string, unknown>> } =
    { input };
  if (goalName) {
    body.goal_name = goalName;
  }
  if (attachments && attachments.length > 0) {
    body.attachments = attachments;
  }
  const response = (await fetchWithAuth(
    `/deepspace/chats/${encodeURIComponent(conversationId)}/goals/command`,
    { method: "POST", body: JSON.stringify(body) },
  )) as Response;
  if (!response.ok) {
    throw new Error(`The goal command failed (HTTP ${response.status}).`);
  }
  return (await response.json()) as GoalCommandResult;
}
