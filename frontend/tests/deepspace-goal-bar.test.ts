import { describe, expect, it } from "vitest";

import {
  EMPTY_CONVERSATION_ID,
  isRealConversationId,
} from "../app/dashboard/deepspace/_components/DeepSpaceGoalBar";

describe("DeepSpace goal conversation guard", () => {
  it("does not load goals for the empty conversation placeholder", () => {
    expect(isRealConversationId(null)).toBe(false);
    expect(isRealConversationId(EMPTY_CONVERSATION_ID)).toBe(false);
  });

  it("loads goals for a persisted conversation id", () => {
    expect(isRealConversationId("11111111-1111-4111-8111-111111111111")).toBe(true);
  });
});
