import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import DeepSpaceThinkingPanel from "../app/dashboard/deepspace/_components/DeepSpaceThinkingPanel";
import {
  deepSpaceThreadReducer,
  initialDeepSpaceThreadState,
  normalizeHistorySubagentTimelines,
} from "../app/dashboard/deepspace/_lib/deepspace-thread";
import {
  parseTimestampMs,
  type SubagentActivityTimeline,
  type TimelineStep,
} from "../app/dashboard/deepspace/_lib/deepspace-stream";

describe("DeepSpaceThinkingPanel inline orchestration tree", () => {
  it("renders sibling subagent accordions inline inside Thinking & activity and expands steps independently", () => {
    const parentTimeline: TimelineStep[] = [
      {
        id: "parent-1",
        stepId: "parent-1",
        turnIndex: 1,
        phase: "completed",
        type: "tool_call",
        title: "Delegating To Sub-Agent",
        status: "completed",
        startedAt: "2026-11-25T12:00:00Z",
        completedAt: "2026-11-25T12:00:05Z",
        toolName: "subagent",
        toolId: "call-alpha",
      },
    ];

    const subagents: SubagentActivityTimeline[] = [
      {
        id: "child-alpha",
        parentToolId: "call-alpha",
        label: "Code Scout",
        mode: "one-shot",
        mission: "Inspect backend modules",
        status: "completed",
        rounds: 2,
        toolCount: 1,
        summary: "Found 2 route handlers.",
        startedAt: "2026-11-25T12:00:00Z",
        completedAt: "2026-11-25T12:00:04Z",
        steps: [
          {
            id: "alpha-step-1",
            stepId: "alpha-step-1",
            turnIndex: 1,
            phase: "completed",
            type: "tool_call",
            title: "Ran read",
            status: "completed",
            startedAt: "2026-11-25T12:00:01Z",
            completedAt: "2026-11-25T12:00:02Z",
            toolName: "read",
            toolInput: { path: "backend/app/main.py" },
            toolOutput: "ok",
            success: true,
          },
        ],
      },
      {
        id: "child-beta",
        label: "Test Auditor",
        mode: "one-shot",
        mission: "Verify unit test coverage",
        status: "running",
        rounds: 1,
        toolCount: 1,
        startedAt: "2026-11-25T12:00:01Z",
        steps: [
          {
            id: "beta-step-1",
            stepId: "beta-step-1",
            turnIndex: 1,
            phase: "exploring",
            type: "tool_call",
            title: "Running grep",
            status: "running",
            startedAt: "2026-11-25T12:00:02Z",
            toolName: "grep",
          },
        ],
      },
    ];

    render(
      <DeepSpaceThinkingPanel
        content="Parent reasoning"
        isStreaming={true}
        timeline={parentTimeline}
        subagents={subagents}
      />,
    );

    // No separate top horizontal tablist; both subagent nodes live directly in Thinking & activity.
    expect(screen.queryByRole("tablist")).toBeNull();
    expect(screen.getByTestId("subagent-node-child-alpha")).toBeTruthy();
    expect(screen.getByTestId("subagent-node-child-beta")).toBeTruthy();
    expect(screen.getByTestId("thinking-activity-metrics").textContent).toContain("2 subagents");

    // Completed subagent starts collapsed with compact mission; running subagent auto-expands.
    const alphaTrigger = screen.getByTestId("subagent-trigger-child-alpha");
    const betaTrigger = screen.getByTestId("subagent-trigger-child-beta");
    expect(alphaTrigger.getAttribute("aria-expanded")).toBe("false");
    expect(betaTrigger.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getAllByText(/Inspect backend modules/i).length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText(/Running grep/i)).toBeTruthy();

    // Expanding Code Scout inline reveals its nested steps and outcome.
    fireEvent.click(alphaTrigger);
    expect(alphaTrigger.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByTestId("subagent-tab-panel-child-alpha")).toBeTruthy();
    expect(screen.getByText("Found 2 route handlers.")).toBeTruthy();

    // Collapsing the outer Thinking & activity header collapses the body cleanly.
    const collapseBtn = screen.getByRole("button", { name: /Collapse activity timeline/i });
    fireEvent.click(collapseBtn);
    expect(collapseBtn.getAttribute("aria-expanded")).toBe("false");
  });

  it("parses naive SQLite UTC timestamps without adding local timezone offset", () => {
    expect(parseTimestampMs("2026-11-25 15:25:16")).toBe(Date.UTC(2026, 10, 25, 15, 25, 16));
    expect(parseTimestampMs("2026-11-25T15:25:16.304731+00:00")).toBe(
      Date.UTC(2026, 10, 25, 15, 25, 16, 304),
    );
    expect(Number.isNaN(parseTimestampMs(""))).toBe(true);
  });

  it("renders inline child internal thought, model message, and Markdown outcome", () => {
    const subagents: SubagentActivityTimeline[] = [
      {
        id: "child-reasoner",
        label: "Deep Auditor",
        mode: "one-shot",
        mission: "Audit config",
        status: "completed",
        rounds: 2,
        toolCount: 1,
        startedAt: "2026-11-25T17:00:00.000Z",
        completedAt: "2026-11-25T17:00:08.000Z",
        summary: "### **OpenAI GPT-6.2**\n\n- Config verified.",
        steps: [
          {
            id: "think-1",
            stepId: "child_child-reasoner_think_r1",
            turnIndex: 1,
            phase: "thinking",
            type: "thinking",
            title: "Internal Thought",
            status: "completed",
            startedAt: "2026-11-25T17:00:01.000Z",
            completedAt: "2026-11-25T17:00:02.000Z",
            details: "Analyzing package dependencies.",
          },
          {
            id: "msg-1",
            stepId: "child_child-reasoner_msg_r1",
            turnIndex: 1,
            phase: "thinking",
            type: "model_message",
            title: "Model message",
            status: "completed",
            startedAt: "2026-11-25T17:00:03.000Z",
            completedAt: "2026-11-25T17:00:03.000Z",
            details: "Reading pyproject.toml next.",
          },
          {
            id: "tool-1",
            stepId: "child_child-reasoner_call_1",
            turnIndex: 1,
            phase: "exploring",
            type: "tool_call",
            title: "Ran read",
            status: "completed",
            startedAt: "2026-11-25T17:00:04.000Z",
            completedAt: "2026-11-25T17:00:06.000Z",
            toolName: "read",
            toolInput: { path: "pyproject.toml" },
            success: true,
          },
        ],
      },
    ];

    render(
      <DeepSpaceThinkingPanel
        content="Parent reasoning"
        isStreaming={false}
        timeline={[]}
        subagents={subagents}
      />,
    );

    const trigger = screen.getByTestId("subagent-trigger-child-reasoner");
    fireEvent.click(trigger);

    expect(screen.getByTestId("thinking-activity-metrics").textContent).toContain("1 subagent");
    expect(screen.getAllByText(/Internal thought/i).length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText(/Model message/i).length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText(/pyproject\.toml/).length).toBeGreaterThanOrEqual(1);
    const outcomeEl = screen.getByTestId("subagent-outcome-child-reasoner");
    expect(outcomeEl.textContent).toContain("OpenAI GPT-6.2");
    expect(outcomeEl.textContent).not.toContain("###");
    expect(outcomeEl.textContent).not.toContain("**");
  });

  it("preserves child thinking, model_message, details, and toolCount when hydrating persisted timelines", () => {
    const hydrated = normalizeHistorySubagentTimelines(
      [
        {
          id: "child-persisted",
          label: "Auditor",
          status: "completed",
          rounds: 2,
          toolCount: 3,
          steps: [
            {
              id: "t1",
              stepId: "child_1_think_r1",
              type: "thinking",
              phase: "thinking",
              title: "Internal Thought",
              details: "Checking web results.",
              status: "completed",
            },
            {
              id: "m1",
              stepId: "child_1_msg_r1",
              type: "model_message",
              phase: "thinking",
              title: "Model message",
              details: "Fetching article next.",
              status: "completed",
            },
            {
              id: "c1",
              stepId: "child_1_call_1",
              type: "tool_call",
              title: "Ran web_fetch",
              toolName: "web_fetch",
              toolOutput: "{\"title\": \"AI News\"}",
              status: "completed",
              success: true,
            },
          ],
        },
      ],
      "ready",
      "2026-11-25T17:05:00.000Z",
    );

    expect(hydrated).toHaveLength(1);
    const tl = hydrated![0]!;
    expect(tl.toolCount).toBe(1);
    expect(tl.steps[0]?.type).toBe("thinking");
    expect(tl.steps[0]?.details).toBe("Checking web results.");
    expect(tl.steps[1]?.type).toBe("model_message");
    expect(tl.steps[1]?.details).toBe("Fetching article next.");
    expect(tl.steps[2]?.type).toBe("tool_call");
    expect(tl.steps[2]?.toolOutput).toBe("{\"title\": \"AI News\"}");
  });

  it("renders TerminalBlock for bash steps and clickable changed-file pills inside Timeline", () => {
    const onOpenFileDiff = vi.fn();
    const timeline: TimelineStep[] = [
      {
        id: "cmd-1",
        stepId: "cmd-1",
        turnIndex: 1,
        phase: "completed",
        type: "tool_call",
        title: "Ran command",
        toolName: "bash",
        toolInput: { command: "npm run test" },
        toolOutput: JSON.stringify({ status: "ok", exit_code: 0, output: "All 4 tests passed" }),
        status: "completed",
        startedAt: "2026-11-25T17:00:00.000Z",
        completedAt: "2026-11-25T17:00:01.000Z",
      },
      {
        id: "write-1",
        stepId: "write-1",
        turnIndex: 1,
        phase: "completed",
        type: "tool_call",
        title: "Wrote",
        toolName: "write",
        toolInput: { path: "src/todo-app.ts" },
        diffStats: {
          path: "src/todo-app.ts",
          additions: 78,
          deletions: 9,
          diffLines: [{ type: "added", text: "export const app = 1;" }],
        },
        status: "completed",
        startedAt: "2026-11-25T17:00:01.000Z",
        completedAt: "2026-11-25T17:00:02.000Z",
      },
    ];

    render(
      <DeepSpaceThinkingPanel
        content=""
        timeline={timeline}
        isStreaming={false}
        onOpenFileDiff={onOpenFileDiff}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /Ran command/i }));
    expect(screen.getByTestId("deepspace-activity-terminal")).toBeInTheDocument();
    expect(screen.getAllByText(/npm run test/).length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("exit 0")).toBeInTheDocument();
    expect(screen.getByText("All 4 tests passed")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Copy terminal output/i })).toBeInTheDocument();

    const changedFilesBar = screen.getByTestId("deepspace-timeline-changed-files");
    expect(changedFilesBar).toHaveTextContent("todo-app.ts");
    expect(changedFilesBar).toHaveTextContent("+78");
    expect(changedFilesBar).toHaveTextContent("-9");

    const filePillBtn = changedFilesBar.querySelector("button");
    expect(filePillBtn).not.toBeNull();
    fireEvent.click(filePillBtn!);
    expect(onOpenFileDiff).toHaveBeenCalledWith("src/todo-app.ts");
  });

  it("streams live green/red diff counts in real time as tool_delta chunks arrive", () => {
    let state = deepSpaceThreadReducer(initialDeepSpaceThreadState, {
      type: "submit_query",
      query: "Update composer",
    });

    state = deepSpaceThreadReducer(state, {
      type: "stream_event",
      event: {
        event: "tool_delta",
        data: {
          tool_name: "write",
          tool_id: "call_live_1",
          step_id: "step_live_1",
          turn_index: 1,
          text: '{"path":"composer.tsx","content":"line 1\\n',
        },
      },
    });
    const afterChunk1 = state.messages.find((m) => m.role === "assistant")?.timeline?.[0];
    expect(afterChunk1?.toolInput?.path).toBe("composer.tsx");
    expect(afterChunk1?.diffStats?.additions).toBe(2);

    state = deepSpaceThreadReducer(state, {
      type: "stream_event",
      event: {
        event: "tool_delta",
        data: {
          tool_name: "write",
          tool_id: "call_live_1",
          step_id: "step_live_1",
          turn_index: 1,
          text: 'line 2\\nline 3"}',
        },
      },
    });
    const afterChunk2 = state.messages.find((m) => m.role === "assistant")?.timeline?.[0];
    expect(afterChunk2?.diffStats?.additions).toBe(3);
    expect(afterChunk2?.diffStats?.deletions).toBe(0);
  });

  it("reconciles tool_delta + permission_granted + tool_result without duplicate live running rows and closes open steps on finish/fail", () => {
    let state = deepSpaceThreadReducer(initialDeepSpaceThreadState, {
      type: "submit_query",
      query: "Check python version",
    });

    state = deepSpaceThreadReducer(state, {
      type: "stream_events",
      events: [
        {
          event: "tool_delta",
          data: {
            tool_name: "bash",
            tool_id: "tool_0",
            step_id: "tool_stream_1_0_tool_0",
            turn_index: 1,
            text: '{"command":"python3 --version"}',
          },
        },
        {
          event: "permission_request",
          data: {
            tool_name: "bash",
            tool_id: "call-bash-1",
            step_id: "step-perm-1",
            tool_input: { command: "python3 --version" },
            approval_id: "esc-1",
            turn_index: 1,
          },
        },
        {
          event: "permission_granted",
          data: {
            tool_name: "bash",
            tool_id: "call-bash-1",
            step_id: "step-perm-1",
            approval_id: "esc-1",
            turn_index: 1,
          },
        },
        {
          event: "tool_start",
          data: {
            tool_name: "bash",
            tool_id: "call-bash-1",
            step_id: "step-tool-1",
            tool_input: { command: "python3 --version" },
            turn_index: 1,
          },
        },
        {
          event: "tool_result",
          data: {
            tool_name: "bash",
            tool_id: "call-bash-1",
            step_id: "step-tool-1",
            tool_input: { command: "python3 --version" },
            output: JSON.stringify({ status: "ok", exit_code: 0, output: "Python 3.12.0" }),
            success: true,
            turn_index: 1,
          },
        },
      ],
    });

    state = deepSpaceThreadReducer(state, { type: "stream_finished" });

    const assistant = state.messages.find((m) => m.role === "assistant");
    const timeline = assistant?.timeline ?? [];
    expect(timeline.some((s) => s.status === "running" || s.status === "awaiting_approval")).toBe(
      false,
    );
    expect(timeline.filter((s) => s.type === "tool_call")).toHaveLength(1);

    render(<DeepSpaceThinkingPanel content="" timeline={timeline} isStreaming={false} />);
    expect(screen.queryByText(/^live$/i)).toBeNull();
    expect(screen.queryByText(/● running/i)).toBeNull();
    expect(screen.queryByText(/^running$/i)).toBeNull();
    expect(screen.getByTestId("thinking-activity-metrics").textContent).toContain("1 command");
    expect(screen.getAllByTestId("deepspace-activity-terminal")).toHaveLength(1);
    const bashTrigger = screen.getByRole("button", { name: /Ran command/i });
    expect(bashTrigger.getAttribute("aria-expanded")).toBe("false");

    // Expand all opens every step end to end without clamping terminal height or showing running.
    const expandAllBtn = screen.getByRole("button", { name: /Expand all timeline steps/i });
    fireEvent.click(expandAllBtn);
    expect(bashTrigger.getAttribute("aria-expanded")).toBe("true");
    const requestDetailsButtons = screen.getAllByRole("button", { name: /Request details/i });
    expect(requestDetailsButtons.every((btn) => btn.getAttribute("aria-expanded") === "true")).toBe(
      true,
    );
    const terminalEl = screen.getByTestId("deepspace-activity-terminal");
    expect(terminalEl.querySelector("pre")?.className).not.toContain("max-h-56");
    expect(screen.getByText("exit 0")).toBeInTheDocument();
    expect(screen.getByText("Python 3.12.0")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Collapse all timeline steps/i })).toBeInTheDocument();

    // Also verify stream_failed marks any lingering running timeline step as failed.
    let failedState = deepSpaceThreadReducer(initialDeepSpaceThreadState, {
      type: "submit_query",
      query: "Run failing task",
    });
    failedState = deepSpaceThreadReducer(failedState, {
      type: "stream_event",
      event: {
        event: "tool_start",
        data: {
          tool_name: "bash",
          tool_id: "call-fail-1",
          step_id: "step-fail-1",
          tool_input: { command: "false" },
          turn_index: 1,
        },
      },
    });
    failedState = deepSpaceThreadReducer(failedState, {
      type: "stream_failed",
      error: { code: "TIMEOUT", message: "Timed out" },
    });
    const failedStep = failedState.messages.find((m) => m.role === "assistant")?.timeline?.[0];
    expect(failedStep?.status).toBe("failed");

    // Failed write payload {"status":"error",...} must be marked failed with no fake +61 -0 diff or pill.
    let errWriteState = deepSpaceThreadReducer(initialDeepSpaceThreadState, {
      type: "submit_query",
      query: "Write assignment",
    });
    errWriteState = deepSpaceThreadReducer(errWriteState, {
      type: "stream_events",
      events: [
        {
          event: "tool_delta",
          data: {
            tool_name: "write",
            tool_id: "call_err_write",
            step_id: "step_err_write",
            turn_index: 1,
            text: '{"path":"assignment.txt","content":"line 1\\nline 2"}',
          },
        },
        {
          event: "tool_result",
          data: {
            tool_name: "write",
            tool_id: "call_err_write",
            step_id: "step_err_write",
            tool_input: { path: "assignment.txt", content: "line 1\nline 2" },
            output: JSON.stringify({
              status: "error",
              error_code: "EXISTS",
              message: "The file already exists; use 'overwrite' or 'append'.",
            }),
            success: true,
            turn_index: 1,
          },
        },
      ],
    });
    const errStep = errWriteState.messages.find((m) => m.role === "assistant")?.timeline?.[0];
    expect(errStep?.status).toBe("failed");
    expect(errStep?.diffStats).toBeUndefined();
  });
});
