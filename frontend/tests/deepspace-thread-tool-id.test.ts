import { describe, expect, it } from "vitest";

import {
  deepSpaceThreadReducer,
  initialDeepSpaceThreadState,
} from "../app/dashboard/deepspace/_lib/deepspace-thread";

describe("deepSpaceThreadReducer tool streaming", () => {
  it("does not duplicate a persisted assistant when a stream reuses its durable ID", () => {
    const withHistory = deepSpaceThreadReducer(initialDeepSpaceThreadState, {
      type: "load_history",
      conversationId: "conv-regeneration",
      messages: [
        {
          id: "assistant-1",
          role: "assistant",
          content: "Saved response",
          created_at: new Date().toISOString(),
          metadata_json: {},
        },
      ],
    });
    const submitted = deepSpaceThreadReducer(withHistory, {
      type: "submit_query",
      query: "Regenerate this response",
    });

    const started = deepSpaceThreadReducer(submitted, {
      type: "stream_event",
      event: {
        event: "start",
        data: { message_id: "assistant-1", started_at: new Date().toISOString() },
      },
    });

    expect(started.messages).toHaveLength(2);
    expect(started.messages[0]?.id).toBe("assistant-1");
    expect(started.messages[1]?.role).toBe("user");
    expect(new Set(started.messages.map((message) => message.id)).size).toBe(
      started.messages.length,
    );
    expect(started.activeAssistantId).toBe("assistant-1");
    expect(started.messages[0]?.status).toBe("streaming");
  });

  it("keeps a durable streaming turn active while history reconnects", () => {
    const reloaded = deepSpaceThreadReducer(initialDeepSpaceThreadState, {
      type: "load_history",
      conversationId: "conv-reconnect",
      messages: [
        {
          id: "assistant-running",
          role: "assistant",
          content: "",
          created_at: new Date().toISOString(),
          metadata_json: {
            status: "streaming",
            runtime_active: true,
            client_request_id: "request-running",
          },
        },
      ],
    });

    expect(reloaded.isStreaming).toBe(true);
    expect(reloaded.activeAssistantId).toBe("assistant-running");
    expect(reloaded.messages[0]?.status).toBe("streaming");
  });

  it("rehydrates the model context limit after a completed response", () => {
    const reloaded = deepSpaceThreadReducer(initialDeepSpaceThreadState, {
      type: "load_history",
      conversationId: "conv-context",
      messages: [
        {
          id: "assistant-context",
          role: "assistant",
          content: "Answer",
          created_at: new Date().toISOString(),
          metadata_json: {
            model_name: "provider-model",
            context_limit: 128000,
            context_limit_source: "live_model",
            context_used_tokens: 4773,
          },
        },
      ],
    });

    expect(reloaded.lastContextLimit).toBe(128000);
    expect(reloaded.messages[0]?.metrics?.contextLimit).toBe(128000);
  });

  it("rehydrates an MCP approval card after history reload", () => {
    const reloaded = deepSpaceThreadReducer(initialDeepSpaceThreadState, {
      type: "load_history",
      conversationId: "conv-approval",
      messages: [
        {
          id: "assistant-approval",
          role: "assistant",
          content:
            "DeepSpace is waiting for your approval before running Google Gmail search_threads.",
          created_at: new Date().toISOString(),
          metadata_json: {
            status: "awaiting_approval",
            pending_approval: {
              approval_id: "approval-1",
              call_id: "call-1",
              step_id: "step-1",
              tool_name: "mcp_gmail_search_threads",
              mcp_server: "Google Gmail",
              mcp_tool: "search_threads",
              tool_input: { pageSize: 5 },
              risk_level: "read",
              permission_level: "human",
              message: "Google Gmail wants to run search_threads.",
              warning: "Review the target and inputs before allowing this action.",
            },
          },
        },
      ],
    });

    expect(
      reloaded.messages[0]?.agentSteps?.some(
        (step) =>
          step.type === "permission_request" &&
          step.status === "awaiting_approval" &&
          step.data?.approval_id === "approval-1" &&
          step.data?.warning === "Review the target and inputs before allowing this action.",
      ),
    ).toBe(true);
  });

  it("keeps streamed thinking visible after completion and history reload", () => {
    let state = deepSpaceThreadReducer(initialDeepSpaceThreadState, {
      type: "stream_event",
      event: {
        event: "start",
        data: {
          message_id: "assistant-thinking",
          conversation_id: "conv-thinking",
          started_at: new Date().toISOString(),
        },
      },
    });

    state = deepSpaceThreadReducer(state, {
      type: "stream_event",
      event: {
        event: "thinking",
        data: { text: "First thought." },
      },
    });
    state = deepSpaceThreadReducer(state, {
      type: "stream_event",
      event: {
        event: "done",
        data: {},
      },
    });

    expect(state.messages[0]?.thinkingContent).toBe("First thought.");

    const reloaded = deepSpaceThreadReducer(initialDeepSpaceThreadState, {
      type: "load_history",
      conversationId: "conv-thinking",
      messages: [
        {
          id: "assistant-thinking",
          role: "assistant",
          content: "Answer",
          created_at: new Date().toISOString(),
          metadata_json: { thinking: { content: "First thought." } },
        },
      ],
    });

    expect(reloaded.messages[0]?.thinkingContent).toBe("First thought.");
  });

  it("matches tool results by tool id when the same tool runs multiple times", () => {
    let state = deepSpaceThreadReducer(initialDeepSpaceThreadState, {
      type: "stream_event",
      event: {
        event: "start",
        data: {
          message_id: "assistant-1",
          conversation_id: "conv-1",
          started_at: new Date().toISOString(),
        },
      },
    });

    state = deepSpaceThreadReducer(state, {
      type: "stream_event",
      event: {
        event: "tool_start",
        data: {
          step_id: "step-1",
          tool_id: "call-1",
          tool_name: "web_search",
          tool_input: { query: "alpha" },
          started_at: new Date().toISOString(),
        },
      },
    });

    state = deepSpaceThreadReducer(state, {
      type: "stream_event",
      event: {
        event: "tool_start",
        data: {
          step_id: "step-1",
          tool_id: "call-2",
          tool_name: "web_search",
          tool_input: { query: "beta" },
          started_at: new Date().toISOString(),
        },
      },
    });

    state = deepSpaceThreadReducer(state, {
      type: "stream_event",
      event: {
        event: "tool_result",
        data: {
          step_id: "step-1",
          tool_id: "call-1",
          tool_name: "web_search",
          tool_input: { query: "alpha" },
          output: "alpha result",
          success: true,
          duration_ms: 25,
          completed_at: new Date().toISOString(),
        },
      },
    });

    const stepsAfterFirstResult = state.messages[0]?.agentSteps ?? [];
    expect(stepsAfterFirstResult).toHaveLength(2);
    expect(stepsAfterFirstResult[0]?.tool_id).toBe("call-1");
    expect(stepsAfterFirstResult[0]?.status).toBe("completed");
    expect(stepsAfterFirstResult[0]?.toolOutput).toBe("alpha result");
    expect(stepsAfterFirstResult[1]?.tool_id).toBe("call-2");
    expect(stepsAfterFirstResult[1]?.status).toBe("running");

    state = deepSpaceThreadReducer(state, {
      type: "stream_event",
      event: {
        event: "tool_result",
        data: {
          step_id: "step-1",
          tool_id: "call-2",
          tool_name: "web_search",
          tool_input: { query: "beta" },
          output: "beta result",
          success: true,
          duration_ms: 30,
          completed_at: new Date().toISOString(),
        },
      },
    });

    const finalSteps = state.messages[0]?.agentSteps ?? [];
    expect(finalSteps[1]?.tool_id).toBe("call-2");
    expect(finalSteps[1]?.status).toBe("completed");
    expect(finalSteps[1]?.toolOutput).toBe("beta result");
  });

  it("preserves tool arguments and closes replayed thinking after reload", () => {
    const reloaded = deepSpaceThreadReducer(initialDeepSpaceThreadState, {
      type: "load_history",
      conversationId: "conv-replay",
      messages: [
        {
          id: "assistant-replay",
          role: "assistant",
          content: "Done",
          created_at: new Date().toISOString(),
          metadata_json: {
            status: "ready",
            client_request_id: "request-replay",
            timeline_events: [
              {
                event: "thinking",
                data: { text: "I will inspect the mailbox." },
              },
              {
                event: "tool_start",
                data: {
                  step_id: "step-1",
                  tool_id: "call-1",
                  tool_name: "search_threads",
                  tool_input: { query: "in:inbox", pageSize: 5 },
                },
              },
              {
                event: "tool_result",
                data: {
                  step_id: "step-1",
                  tool_id: "call-1",
                  tool_name: "search_threads",
                  output: "result",
                  success: true,
                },
              },
            ],
          },
        },
      ],
    });

    const timeline = reloaded.messages[0]?.timeline ?? [];
    expect(timeline.find((step) => step.type === "tool_call")?.toolInput).toEqual({
      query: "in:inbox",
      pageSize: 5,
    });
    expect(timeline.find((step) => step.type === "thinking")?.status).toBe("completed");
  });

  it("appends tool_delta output to the running tool pane", () => {
    let state = deepSpaceThreadReducer(initialDeepSpaceThreadState, {
      type: "stream_event",
      event: {
        event: "start",
        data: {
          message_id: "assistant-1",
          conversation_id: "conv-1",
          started_at: new Date().toISOString(),
        },
      },
    });

    state = deepSpaceThreadReducer(state, {
      type: "stream_event",
      event: {
        event: "tool_start",
        data: {
          step_id: "step-2",
          tool_id: "bash-1",
          tool_name: "bash",
          tool_input: { command: "echo hi" },
          started_at: new Date().toISOString(),
        },
      },
    });

    state = deepSpaceThreadReducer(state, {
      type: "stream_event",
      event: {
        event: "tool_delta",
        data: {
          step_id: "step-2",
          tool_id: "bash-1",
          tool_name: "bash",
          tool_input: { command: "echo hi" },
          text: "hi\n",
        },
      },
    });

    const steps = state.messages[0]?.agentSteps ?? [];
    expect(steps[0]?.status).toBe("running");
    expect(steps[0]?.toolOutput).toBe("hi\n");
  });

  it("preserves streamed tool output when the final tool_result arrives", () => {
    let state = deepSpaceThreadReducer(initialDeepSpaceThreadState, {
      type: "stream_event",
      event: {
        event: "start",
        data: {
          message_id: "assistant-1",
          conversation_id: "conv-1",
          started_at: new Date().toISOString(),
        },
      },
    });

    state = deepSpaceThreadReducer(state, {
      type: "stream_event",
      event: {
        event: "tool_start",
        data: {
          step_id: "step-3",
          tool_id: "sync-1",
          tool_name: "sync_connector",
          tool_input: { integration_slug: "github" },
          started_at: new Date().toISOString(),
        },
      },
    });

    state = deepSpaceThreadReducer(state, {
      type: "stream_event",
      event: {
        event: "tool_delta",
        data: {
          step_id: "step-3",
          tool_id: "sync-1",
          tool_name: "sync_connector",
          tool_input: { integration_slug: "github" },
          stream: "system",
          text: "Fetching source data from github.\n",
        },
      },
    });

    state = deepSpaceThreadReducer(state, {
      type: "stream_event",
      event: {
        event: "tool_result",
        data: {
          step_id: "step-3",
          tool_id: "sync-1",
          tool_name: "sync_connector",
          tool_input: { integration_slug: "github" },
          output: "Sync completed.",
          success: true,
          duration_ms: 125,
          completed_at: new Date().toISOString(),
        },
      },
    });

    const steps = state.messages[0]?.agentSteps ?? [];
    expect(steps[0]?.status).toBe("completed");
    expect(steps[0]?.toolOutput).toContain("[system] Fetching source data from github.");
    expect(steps[0]?.toolOutput).toContain("Sync completed.");
  });

  it("merges agent lifecycle phases into a single tool step card", () => {
    let state = deepSpaceThreadReducer(initialDeepSpaceThreadState, {
      type: "stream_event",
      event: {
        event: "start",
        data: {
          message_id: "assistant-3",
          conversation_id: "conv-3",
          started_at: new Date().toISOString(),
        },
      },
    });

    state = deepSpaceThreadReducer(state, {
      type: "stream_event",
      event: {
        event: "tool_start",
        data: {
          step_id: "step-4",
          tool_id: "call-4",
          tool_name: "bash",
          tool_input: { command: "echo hi" },
          started_at: new Date().toISOString(),
        },
      },
    });

    state = deepSpaceThreadReducer(state, {
      type: "stream_event",
      event: {
        event: "tool_result",
        data: {
          step_id: "step-4",
          tool_id: "call-4",
          tool_name: "bash",
          tool_input: { command: "echo hi" },
          output: "hello\n",
          success: true,
          duration_ms: 20,
          completed_at: new Date().toISOString(),
        },
      },
    });

    state = deepSpaceThreadReducer(state, {
      type: "stream_event",
      event: {
        event: "agent_testing",
        data: {
          step_id: "step-4",
          tool_id: "call-4",
          tool_name: "bash",
          output: "Background tests passed.",
          success: true,
          started_at: new Date().toISOString(),
          completed_at: new Date().toISOString(),
        },
      },
    });

    const testingSteps = state.messages[0]?.agentSteps ?? [];
    expect(testingSteps).toHaveLength(1);
    expect(testingSteps[0]?.type).toBe("agent_testing");
    expect(testingSteps[0]?.tool_id).toBe("call-4");
    expect(testingSteps[0]?.status).toBe("completed");
    expect(testingSteps[0]?.toolOutput).toContain("hello\n");
    expect(testingSteps[0]?.toolOutput).toContain("Background tests passed.");

    state = deepSpaceThreadReducer(state, {
      type: "stream_event",
      event: {
        event: "agent_verifying",
        data: {
          step_id: "step-4",
          tool_id: "call-4",
          tool_name: "bash",
          output: "Verification confirmed.",
          success: true,
          started_at: new Date().toISOString(),
          completed_at: new Date().toISOString(),
        },
      },
    });

    const verifyingSteps = state.messages[0]?.agentSteps ?? [];
    expect(verifyingSteps).toHaveLength(1);
    expect(verifyingSteps[0]?.type).toBe("agent_verifying");
    expect(verifyingSteps[0]?.status).toBe("completed");
    expect(verifyingSteps[0]?.toolOutput).toContain("Background tests passed.");
    expect(verifyingSteps[0]?.toolOutput).toContain("Verification confirmed.");

    state = deepSpaceThreadReducer(state, {
      type: "stream_event",
      event: {
        event: "agent_self_correct",
        data: {
          step_id: "step-4",
          tool_id: "call-4",
          tool_name: "bash",
          output: "Self-correction triggered.",
          success: false,
          started_at: new Date().toISOString(),
          completed_at: new Date().toISOString(),
        },
      },
    });

    const selfCorrectSteps = state.messages[0]?.agentSteps ?? [];
    expect(selfCorrectSteps).toHaveLength(1);
    expect(selfCorrectSteps[0]?.type).toBe("agent_self_correct");
    expect(selfCorrectSteps[0]?.status).toBe("failed");
    expect(selfCorrectSteps[0]?.toolOutput).toContain("Self-correction triggered.");
  });

  it("keeps only approval and real execution events in a tool card", () => {
    let state = deepSpaceThreadReducer(initialDeepSpaceThreadState, {
      type: "stream_event",
      event: {
        event: "start",
        data: {
          message_id: "assistant-merge",
          conversation_id: "conv-merge",
          started_at: new Date().toISOString(),
        },
      },
    });

    state = deepSpaceThreadReducer(state, {
      type: "stream_event",
      event: {
        event: "permission_request",
        data: {
          step_id: "step-merge",
          tool_id: "call-merge",
          tool_name: "web_search",
          tool_input: { query: "alpha" },
          permission_level: "approval",
          tier: 2,
          started_at: new Date().toISOString(),
        },
      },
    });

    state = deepSpaceThreadReducer(state, {
      type: "stream_event",
      event: {
        event: "tool_start",
        data: {
          step_id: "step-merge",
          tool_id: "call-merge",
          tool_name: "web_search",
          tool_input: { query: "alpha" },
          permission_level: "approved",
          started_at: new Date().toISOString(),
        },
      },
    });

    state = deepSpaceThreadReducer(state, {
      type: "stream_event",
      event: {
        event: "tool_delta",
        data: {
          step_id: "step-merge",
          tool_id: "call-merge",
          tool_name: "web_search",
          tool_input: { query: "alpha" },
          stream: "stdout",
          text: "searching\n",
        },
      },
    });

    state = deepSpaceThreadReducer(state, {
      type: "stream_event",
      event: {
        event: "tool_result",
        data: {
          step_id: "step-merge",
          tool_id: "call-merge",
          tool_name: "web_search",
          tool_input: { query: "alpha" },
          output: "alpha result",
          success: true,
          duration_ms: 31,
          completed_at: new Date().toISOString(),
        },
      },
    });

    state = deepSpaceThreadReducer(state, {
      type: "stream_event",
      event: {
        event: "observing",
        data: {
          step_id: "step-merge",
          tool_id: "call-merge",
          tool_name: "web_search",
          tool_input: { query: "alpha" },
          summary: "Observation confirmed.",
          success: true,
          observed_at: new Date().toISOString(),
        },
      },
    });

    const steps = state.messages[0]?.agentSteps ?? [];
    expect(steps).toHaveLength(1);
    expect(steps[0]?.status).toBe("completed");
    expect(steps[0]?.type).toBe("tool_result");
    expect(steps[0]?.toolOutput).toContain("searching");
    expect(steps[0]?.toolOutput).toContain("alpha result");
    expect(steps[0]?.toolOutput).not.toContain("Observation confirmed.");
  });

  it("does not rehydrate synthetic observation history as a tool result", () => {
    const state = deepSpaceThreadReducer(initialDeepSpaceThreadState, {
      type: "load_history",
      conversationId: "conv-history",
      messages: [
        {
          id: "assistant-history",
          role: "assistant",
          content: "done",
          created_at: new Date().toISOString(),
          metadata_json: {
            agent_steps: [
              {
                id: "history-step-1",
                type: "permission_request",
                status: "awaiting_approval",
                step_id: "history-step",
                tool_id: "history-tool",
                toolName: "web_search",
                toolInput: { query: "history" },
                permissionLevel: "approval",
                startedAt: new Date().toISOString(),
                data: {
                  step_id: "history-step",
                  tool_id: "history-tool",
                  tool_name: "web_search",
                  tool_input: { query: "history" },
                },
              },
              {
                id: "history-step-2",
                type: "tool_start",
                status: "running",
                step_id: "history-step",
                tool_id: "history-tool",
                toolName: "web_search",
                toolInput: { query: "history" },
                permissionLevel: "approved",
                startedAt: new Date().toISOString(),
                data: {
                  step_id: "history-step",
                  tool_id: "history-tool",
                  tool_name: "web_search",
                  tool_input: { query: "history" },
                },
              },
              {
                id: "history-step-3",
                type: "tool_result",
                status: "completed",
                step_id: "history-step",
                tool_id: "history-tool",
                toolName: "web_search",
                toolInput: { query: "history" },
                toolOutput: "history result",
                success: true,
                startedAt: new Date().toISOString(),
                completedAt: new Date().toISOString(),
                data: {
                  step_id: "history-step",
                  tool_id: "history-tool",
                  tool_name: "web_search",
                  tool_input: { query: "history" },
                  output: "history result",
                },
              },
              {
                id: "history-step-4",
                type: "observing",
                status: "completed",
                step_id: "history-step",
                tool_id: "history-tool",
                toolName: "web_search",
                toolInput: { query: "history" },
                toolOutput: "Confirmed from history.",
                success: true,
                startedAt: new Date().toISOString(),
                completedAt: new Date().toISOString(),
                data: {
                  step_id: "history-step",
                  tool_id: "history-tool",
                  tool_name: "web_search",
                  tool_input: { query: "history" },
                  summary: "Confirmed from history.",
                },
              },
            ],
          },
        },
      ],
    });

    const steps = state.messages[0]?.agentSteps ?? [];
    expect(steps).toHaveLength(1);
    expect(steps[0]?.type).toBe("tool_result");
    expect(steps[0]?.status).toBe("completed");
    expect(steps[0]?.toolOutput).toContain("history result");
    expect(steps[0]?.toolOutput).not.toContain("Confirmed from history.");
  });

  it("streams live tool_progress updates onto matching subagent timeline steps and records subagent_settled", () => {
    const submitted = deepSpaceThreadReducer(initialDeepSpaceThreadState, {
      type: "submit_query",
      query: "Run parallel subagents",
    });

    const started = deepSpaceThreadReducer(submitted, {
      type: "stream_event",
      event: {
        event: "start",
        data: { message_id: "assistant-subagents", started_at: new Date().toISOString() },
      },
    });

    const withToolStart = deepSpaceThreadReducer(started, {
      type: "stream_event",
      event: {
        event: "tool_start",
        data: {
          tool_id: "call-sub-1",
          step_id: "step-sub-1",
          tool_name: "subagent",
          tool_input: { description: "Researcher", task: "Check docs" },
          turn_index: 1,
        },
      },
    });

    const withProgress1 = deepSpaceThreadReducer(withToolStart, {
      type: "stream_event",
      event: {
        event: "tool_progress",
        data: {
          tool_id: "call-sub-1",
          tool_name: "subagent",
          message: "Round 1: running read, glob in parallel",
          turn_index: 1,
        },
      },
    });

    const withProgress2 = deepSpaceThreadReducer(withProgress1, {
      type: "stream_event",
      event: {
        event: "tool_progress",
        data: {
          tool_id: "call-sub-1",
          tool_name: "subagent",
          message: "Round 1: read (done, 12ms)",
          turn_index: 1,
        },
      },
    });

    const settled = deepSpaceThreadReducer(withProgress2, {
      type: "stream_event",
      event: {
        event: "subagent_settled",
        data: {
          agents: [{ agent_id: "child-1", label: "Researcher", status: "completed" }],
          turn_index: 1,
        },
      },
    });

    const withDoubleGetAgent = deepSpaceThreadReducer(
      deepSpaceThreadReducer(settled, {
        type: "stream_event",
        event: {
          event: "tool_start",
          data: {
            tool_id: "call-get-1",
            step_id: "step-get-1",
            tool_name: "get_agent",
            tool_input: { agent_id: "child-1", wait_timeout_seconds: 60 },
            turn_index: 2,
          },
        },
      }),
      {
        type: "stream_event",
        event: {
          event: "tool_start",
          data: {
            tool_id: "call-get-2",
            step_id: "step-get-2",
            tool_name: "get_agent",
            tool_input: { agent_id: "child-2", wait_timeout_seconds: 60 },
            turn_index: 2,
          },
        },
      },
    );

    const assistant = withDoubleGetAgent.messages.find((m) => m.id === "assistant-subagents");
    const timeline = assistant?.timeline ?? [];
    const subagentStep = timeline.find((step) => step.toolId === "call-sub-1");
    expect(subagentStep?.status).toBe("running");
    expect(subagentStep?.details).toContain("Round 1: running read, glob in parallel");
    expect(subagentStep?.details).toContain("Round 1: read (done, 12ms)");
    expect(timeline.some((step) => step.title === "Collected responses from Researcher")).toBe(true);
    expect(timeline.filter((step) => step.stepId === "wait_subagents_r2")).toHaveLength(1);
  });

  it("routes subagent_activity into isolated subagentTimelines without mixing into parent timeline", () => {
    const started = deepSpaceThreadReducer(initialDeepSpaceThreadState, {
      type: "stream_event",
      event: {
        event: "start",
        data: { message_id: "assistant-iso", started_at: new Date().toISOString() },
      },
    });

    const parentTool = deepSpaceThreadReducer(started, {
      type: "stream_event",
      event: {
        event: "tool_start",
        data: {
          tool_id: "parent-call-1",
          step_id: "parent-step-1",
          tool_name: "subagent",
          turn_index: 1,
        },
      },
    });

    const afterChild1Start = deepSpaceThreadReducer(parentTool, {
      type: "stream_event",
      event: {
        event: "subagent_activity",
        data: {
          kind: "start",
          subagent_id: "child-1",
          parent_tool_id: "parent-call-1",
          label: "Code Scout",
          mode: "one-shot",
          mission: "Inspect backend modules",
          started_at: "2026-11-25T12:00:00Z",
        },
      },
    });

    const afterChild1Tool = deepSpaceThreadReducer(afterChild1Start, {
      type: "stream_event",
      event: {
        event: "subagent_activity",
        data: {
          kind: "tool_start",
          subagent_id: "child-1",
          label: "Code Scout",
          round: 1,
          step_id: "child_1_r1_0",
          tool_id: "call_c1_0",
          tool_name: "read",
          tool_input: { path: "app/main.py" },
          started_at: "2026-11-25T12:00:01Z",
        },
      },
    });

    const afterChild2Start = deepSpaceThreadReducer(afterChild1Tool, {
      type: "stream_event",
      event: {
        event: "subagent_activity",
        data: {
          kind: "start",
          subagent_id: "child-2",
          label: "Docs Checker",
          mode: "one-shot",
          started_at: "2026-11-25T12:00:01Z",
        },
      },
    });

    const afterChild2ToolResult = deepSpaceThreadReducer(afterChild2Start, {
      type: "stream_event",
      event: {
        event: "subagent_activity",
        data: {
          kind: "tool_result",
          subagent_id: "child-2",
          label: "Docs Checker",
          round: 1,
          step_id: "child_2_r1_0",
          tool_id: "call_c2_0",
          tool_name: "grep",
          status: "completed",
          duration_ms: 14,
          output_preview: "3 matches",
          completed_at: "2026-11-25T12:00:02Z",
        },
      },
    });

    const afterChild1End = deepSpaceThreadReducer(afterChild2ToolResult, {
      type: "stream_event",
      event: {
        event: "subagent_activity",
        data: {
          kind: "end",
          subagent_id: "child-1",
          status: "completed",
          rounds: 1,
          summary: "Inspected app/main.py.",
          completed_at: "2026-11-25T12:00:03Z",
        },
      },
    });

    const assistant = afterChild1End.messages.find((m) => m.id === "assistant-iso");
    const parentTimeline = assistant?.timeline ?? [];
    expect(parentTimeline.some((s) => s.toolName === "read")).toBe(false);
    expect(parentTimeline.some((s) => s.toolName === "grep")).toBe(false);

    const subs = assistant?.subagentTimelines ?? [];
    expect(subs).toHaveLength(2);
    expect(subs[0]?.id).toBe("child-1");
    expect(subs[0]?.status).toBe("completed");
    expect(subs[0]?.steps).toHaveLength(1);
    expect(subs[0]?.steps[0]?.toolName).toBe("read");
    expect(subs[0]?.steps[0]?.status).toBe("completed");
    expect(subs[0]?.summary).toBe("Inspected app/main.py.");

    expect(subs[1]?.id).toBe("child-2");
    expect(subs[1]?.status).toBe("running");
    expect(subs[1]?.steps).toHaveLength(1);
    expect(subs[1]?.steps[0]?.toolName).toBe("grep");
    expect(subs[1]?.steps[0]?.toolOutput).toBe("3 matches");
  });

  it("rehydrates metadata subagent_timelines and closes stale running state on completed messages", () => {
    const reloaded = deepSpaceThreadReducer(initialDeepSpaceThreadState, {
      type: "load_history",
      conversationId: "conv-sub-reload",
      messages: [
        {
          id: "assistant-sub-reload",
          role: "assistant",
          content: "Final synthesis.",
          created_at: "2026-11-25T12:05:00Z",
          metadata_json: {
            status: "completed",
            subagent_timelines: [
              {
                id: "child-hist-1",
                label: "Audit Worker",
                mode: "one-shot",
                status: "running",
                rounds: 2,
                toolCount: 1,
                startedAt: "2026-11-25T12:04:00Z",
                steps: [
                  {
                    id: "step-hist-1",
                    stepId: "step-hist-1",
                    turnIndex: 1,
                    title: "Running read",
                    status: "running",
                    startedAt: "2026-11-25T12:04:05Z",
                    toolName: "read",
                  },
                ],
              },
            ],
          },
        },
      ],
    });

    const msg = reloaded.messages[0];
    expect(msg?.subagentTimelines).toHaveLength(1);
    expect(msg?.subagentTimelines?.[0]?.status).toBe("completed");
    expect(msg?.subagentTimelines?.[0]?.steps[0]?.status).toBe("completed");
  });
});
