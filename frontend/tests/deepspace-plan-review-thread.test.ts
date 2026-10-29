import { describe, expect, it } from "vitest";

import {
  deepSpaceThreadReducer,
  initialDeepSpaceThreadState,
} from "../app/dashboard/deepspace/_lib/deepspace-thread";

describe("the DeepSpace plan review in the thread", () => {
  it("turns a submitted plan into a step awaiting the user's decision", () => {
    const started = deepSpaceThreadReducer(initialDeepSpaceThreadState, {
      type: "stream_event",
      event: {
        event: "start",
        data: { conversation_id: "conversation-plan", message_id: "assistant-plan" },
      },
    });

    const state = deepSpaceThreadReducer(started, {
      type: "stream_event",
      event: {
        event: "plan_submitted",
        data: {
          plan_id: "plan-1",
          title: "Ship the release",
          content: "# Plan\n\n1. Build",
          status: "proposed",
          step_id: "step-1",
          tool_id: "call-1",
          turn_index: 0,
        },
      },
    });

    const step = state.messages[0]?.agentSteps?.[0];
    expect(step?.type).toBe("plan_submitted");
    expect(step?.status).toBe("awaiting_approval");
    expect(step?.data?.plan_id).toBe("plan-1");
    expect(step?.data?.title).toBe("Ship the release");
  });

  it("restores the pending plan from saved history", () => {
    const state = deepSpaceThreadReducer(initialDeepSpaceThreadState, {
      type: "load_history",
      conversationId: "conversation-plan",
      messages: [
        {
          id: "assistant-plan",
          role: "assistant",
          content: "The plan is ready for your review.",
          created_at: "2026-10-20T12:00:00.000Z",
          metadata_json: {
            status: "awaiting_plan_review",
            pending_plan_review: {
              plan_id: "plan-2",
              title: "Ship the release",
              content: "# Plan\n\n1. Build",
              status: "proposed",
              step_id: "step-2",
              call_id: "call-2",
              turn_index: 0,
            },
          },
        },
      ],
    });

    const step = state.messages[0]?.agentSteps?.[0];
    expect(step?.type).toBe("plan_submitted");
    expect(step?.status).toBe("awaiting_approval");
    expect(step?.data?.plan_id).toBe("plan-2");
  });
});
