/**
 * The model a conversation runs on.
 *
 * A conversation either names the model it runs on, or it lets DeepSpace
 * choose the model that fits the work. The choice is per conversation, and
 * the deployment decides the default.
 */

import { fetchWithAuth } from "@/lib/api";

/** A model the tenant may run a conversation on. */
export interface SelectableModel {
  provider_config_id: string;
  provider_type: string;
  provider_name: string;
  model_name: string;
  context_window: number | null;
  supports_images: boolean;
  supports_tools: boolean;
}

/** The conversation's model choice. */
export interface ConversationModelSelection {
  mode: "auto" | "manual";
  effective_model: string | null;
  effective_provider_config_id: string | null;
  recorded_model: string | null;
  recorded_provider_config_id: string | null;
  deployment_default: "auto" | "manual";
}

/**
 * Read every model the tenant may use.
 *
 * @returns The models, grouped by provider by the caller.
 */
export async function listSelectableModels(): Promise<SelectableModel[]> {
  const response = (await fetchWithAuth("/deepspace/chats/models")) as Response;
  if (!response.ok) {
    throw new Error("The available models could not be loaded.");
  }
  const payload = (await response.json()) as { models?: SelectableModel[] };
  return payload.models ?? [];
}

/**
 * Read the model a conversation runs on.
 *
 * @param conversationId The conversation to read.
 * @returns The conversation's model choice.
 */
export async function readConversationModel(
  conversationId: string,
): Promise<ConversationModelSelection> {
  const response = (await fetchWithAuth(
    `/deepspace/chats/${conversationId}/model`,
  )) as Response;
  if (!response.ok) {
    throw new Error("The conversation's model could not be read.");
  }
  const payload = (await response.json()) as Partial<ConversationModelSelection>;
  if (payload.mode !== "auto" && payload.mode !== "manual") {
    throw new Error("The conversation's model choice is not readable.");
  }
  return payload as ConversationModelSelection;
}

/**
 * Let DeepSpace choose the model for a conversation.
 *
 * @param conversationId The conversation to change.
 * @returns The conversation's model choice.
 */
export async function letDeepSpaceChoose(
  conversationId: string,
): Promise<ConversationModelSelection> {
  const response = (await fetchWithAuth(`/deepspace/chats/${conversationId}/model`, {
    method: "PUT",
    body: JSON.stringify({ mode: "auto" }),
  })) as Response;
  if (!response.ok) {
    throw new Error("The model choice could not be changed.");
  }
  return (await response.json()) as ConversationModelSelection;
}

/**
 * Run a conversation on one named model.
 *
 * @param conversationId The conversation to change.
 * @param providerConfigId The provider the model belongs to.
 * @param modelName The model's name.
 * @returns The conversation's model choice.
 */
export async function chooseModel(
  conversationId: string,
  providerConfigId: string,
  modelName: string,
): Promise<ConversationModelSelection> {
  const response = (await fetchWithAuth(`/deepspace/chats/${conversationId}/model`, {
    method: "PUT",
    body: JSON.stringify({
      provider_config_id: providerConfigId,
      model_name: modelName,
    }),
  })) as Response;
  if (!response.ok) {
    throw new Error("The model choice could not be changed.");
  }
  return (await response.json()) as ConversationModelSelection;
}
