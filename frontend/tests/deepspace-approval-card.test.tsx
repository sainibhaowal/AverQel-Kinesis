import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import DeepSpaceApprovalCard from "../app/dashboard/deepspace/_components/DeepSpaceApprovalCard";
import {
  deepSpaceThreadReducer,
  findPendingApprovalPrompt,
  findPendingUserQuestionPrompt,
  initialDeepSpaceThreadState,
} from "../app/dashboard/deepspace/_lib/deepspace-thread";

describe("DeepSpaceApprovalCard", () => {
  it("renders a clean minimal grant summary and expandable details without warning duplication", async () => {
    const onResolve = vi.fn().mockResolvedValue(undefined);
    render(
      <DeepSpaceApprovalCard
        approvalId="appr-1"
        toolName="bash"
        data={{
          message: "bash wants to run.",
          warning:
            "This command may change or remove workspace data. Review it before allowing it. Command: set +e printf 'gpu' nvidia-smi",
          tool_name: "bash",
          risk_level: "critical",
          tool_input: {
            command: "set +e\nprintf 'gpu'\nnvidia-smi",
            timeout_seconds: 20,
          },
        }}
        onResolve={onResolve}
      />,
    );

    expect(screen.getByRole("region", { name: "Approval required" })).toBeInTheDocument();
    expect(screen.getByText("this grants")).toBeInTheDocument();
    expect(
      screen.getByText(
        "This command may change or remove workspace data. Review it before allowing it.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Command: set \+e/)).not.toBeInTheDocument();
    expect(screen.getByText("20s timeout")).toBeInTheDocument();
    expect(screen.getAllByText(/nvidia-smi/).length).toBeGreaterThanOrEqual(1);

    // Details toggle is collapsed by default and expands on click
    expect(screen.queryByText(/"timeout_seconds": 20/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Details/i }));
    expect(screen.getByText(/"timeout_seconds": 20/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /^Approve$/i }));
    await waitFor(() => {
      expect(onResolve).toHaveBeenCalledWith("appr-1", "approved");
    });
  });

  it("calls onResolve with denied when Deny is clicked", async () => {
    const onResolve = vi.fn().mockResolvedValue(undefined);
    render(
      <DeepSpaceApprovalCard
        approvalId="appr-2"
        toolName="mcp_tool"
        data={{
          mcp_server: "github",
          mcp_tool: "create_issue",
          risk_level: "protected",
          tool_input: { title: "Bug" },
        }}
        onResolve={onResolve}
      />,
    );

    expect(screen.getByText("github · create_issue")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Deny/i }));
    await waitFor(() => {
      expect(onResolve).toHaveBeenCalledWith("appr-2", "denied");
    });
  });

  it("offers This session Full Access escalation and Approve once actions when required_mode is danger-full-access", async () => {
    const onResolve = vi.fn().mockResolvedValue(undefined);
    render(
      <DeepSpaceApprovalCard
        approvalId="escalate_call-1"
        toolName="bash"
        data={{
          tool_name: "bash",
          required_mode: "danger-full-access",
          risk_level: "full-access",
          message: "bash wants to run outside the workspace boundary.",
          tool_input: { command: "systemctl status docker" },
        }}
        onResolve={onResolve}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /This session/i }));
    await waitFor(() => {
      expect(onResolve).toHaveBeenCalledWith("escalate_call-1", "approved", {
        enableFullAccessSession: true,
      });
    });

    fireEvent.click(screen.getByRole("button", { name: /Approve once/i }));
    await waitFor(() => {
      expect(onResolve).toHaveBeenCalledWith("escalate_call-1", "approved");
    });
  });

  it("extracts latest pending approval and pending question prompts for composer docking", () => {
    const approval = findPendingApprovalPrompt([
      {
        id: "m1",
        role: "assistant",
        content: "",
        rawContent: "",
        createdAt: "2026-11-25T16:00:00Z",
        status: "streaming",
        agentSteps: [
          {
            id: "step-appr-1",
            type: "permission_request",
            toolName: "bash",
            toolId: "call-1",
            status: "awaiting_approval",
            startedAt: "2026-11-25T16:00:01Z",
            data: { approval_id: "appr-dock-1", tool_name: "bash", risk_level: "critical" },
          },
        ],
      },
    ]);
    expect(approval?.approvalId).toBe("appr-dock-1");
    expect(approval?.toolName).toBe("bash");

    const question = findPendingUserQuestionPrompt([
      {
        id: "m2",
        role: "assistant",
        content: "",
        rawContent: "",
        createdAt: "2026-11-25T16:00:00Z",
        status: "streaming",
        agentSteps: [
          {
            id: "step-q-1",
            type: "ask_user_question",
            toolName: "ask_user_question",
            toolId: "call-q-1",
            status: "awaiting_approval",
            startedAt: "2026-11-25T16:00:02Z",
            data: {
              question_id: "q-dock-1",
              message: "Choose output path",
              options: ["docs/", "reports/"],
            },
          },
        ],
      },
    ]);
    expect(question?.questionId).toBe("q-dock-1");
    expect(question?.question).toBe("Choose output path");
    expect(question?.options).toEqual(["docs/", "reports/"]);
  });

  it("ignores older turn approvals after a newer user turn and clears stale 404 approvals", () => {
    const staleHistoryState = deepSpaceThreadReducer(initialDeepSpaceThreadState, {
      type: "load_history",
      conversationId: "conv-1",
      messages: [
        {
          id: "u1",
          role: "user",
          content: "First request",
          created_at: "2026-11-25T17:56:00Z",
          metadata_json: {},
        },
        {
          id: "a1",
          role: "assistant",
          content: "",
          created_at: "2026-11-25T17:56:42Z",
          metadata_json: {
            status: "blocked",
            agent_steps: [
              {
                id: "step-old",
                type: "permission_request",
                tool_name: "bash",
                status: "awaiting_approval",
                data: { approval_id: "escalate_old-1" },
              },
            ],
          },
        },
        {
          id: "u2",
          role: "user",
          content: "Build TypeScript TODO app",
          created_at: "2026-11-25T18:09:13Z",
          metadata_json: {},
        },
        {
          id: "a2",
          role: "assistant",
          content: "Done.",
          created_at: "2026-11-25T18:09:44Z",
          metadata_json: { status: "ready" },
        },
      ],
    });
    expect(findPendingApprovalPrompt(staleHistoryState.messages)).toBeNull();

    // Even if an active trailing message holds a stale approval, 404 cleanup dismisses it.
    const activeStaleState = deepSpaceThreadReducer(initialDeepSpaceThreadState, {
      type: "load_history",
      conversationId: "conv-1",
      messages: [
        {
          id: "a-awaiting",
          role: "assistant",
          content: "",
          created_at: "2026-11-25T18:10:00Z",
          metadata_json: {
            status: "awaiting_approval",
            pending_approval: {
              approval_id: "escalate_expired",
              tool_name: "bash",
            },
          },
        },
      ],
    });
    expect(findPendingApprovalPrompt(activeStaleState.messages)?.approvalId).toBe(
      "escalate_expired",
    );
    const clearedState = deepSpaceThreadReducer(activeStaleState, {
      type: "clear_stale_approval",
      approvalId: "escalate_expired",
    });
    expect(findPendingApprovalPrompt(clearedState.messages)).toBeNull();
  });

  it("renders high-contrast emerald Approve action instead of white-on-white button styling", () => {
    render(
      <DeepSpaceApprovalCard
        approvalId="appr-contrast"
        toolName="bash"
        data={{
          message: "bash needs approval",
          risk_level: "full-access",
          required_mode: "danger-full-access",
          tool_input: { command: "echo hi" },
        }}
        onResolve={vi.fn().mockResolvedValue(undefined)}
      />,
    );
    const approveBtn = screen.getByRole("button", { name: /Approve once/i });
    expect(approveBtn.className).toContain("bg-emerald-600");
    expect(approveBtn.className).not.toContain("dark:bg-white");
  });
});
