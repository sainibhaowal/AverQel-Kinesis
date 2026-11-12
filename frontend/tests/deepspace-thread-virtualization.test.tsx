import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import DeepSpaceThread from "../app/dashboard/deepspace/_components/DeepSpaceThread";

describe("DeepSpaceThread approval styling", () => {
  it("keeps approval details and actions readable in both themes", () => {
    render(
      <DeepSpaceThread
        messages={[
          {
            id: "approval-request",
            role: "assistant",
            content: "",
            rawContent: "",
            createdAt: new Date().toISOString(),
            status: "ready",
            agentSteps: [
              {
                id: "approval-step",
                type: "permission_request",
                status: "awaiting_approval",
                startedAt: new Date().toISOString(),
                data: {
                  approval_id: "approval-1",
                  tool_name: "bash",
                  tool_input: { command: "find /home -type f" },
                  risk_level: "critical",
                  message: "bash wants to run this command.",
                  warning: "Review the command and working directory before approving.",
                },
              },
            ],
          },
        ]}
        emptyPrompts={[]}
        onPromptSelect={() => {}}
        onInsertLatestAnswer={() => {}}
        onResolveApproval={async () => {}}
      />,
    );

    const approvalHeading = screen.getByText("Approval required");
    expect(approvalHeading).toHaveClass("text-slate-950", "dark:text-slate-100");
    expect(approvalHeading.closest(".rounded-2xl")).toHaveClass(
      "bg-slate-100",
      "border-l-amber-500",
      "dark:bg-amber-950/35",
      "dark:border-l-amber-300",
    );
    expect(
      screen.getByText("Review the command and working directory before approving."),
    ).toHaveClass("bg-white/90", "text-slate-800", "dark:bg-amber-400/10", "dark:text-amber-100");
    expect(screen.getByRole("button", { name: "Approve" })).toHaveClass(
      "deepspace-approval-approve",
      "bg-emerald-700",
      "text-white",
      "dark:bg-emerald-700",
    );
    expect(screen.getByRole("button", { name: "Deny" })).toHaveClass(
      "text-slate-800",
      "dark:text-slate-100",
    );
    const commandPreview = Array.from(document.querySelectorAll("pre")).find((element) =>
      element.classList.contains("border-slate-300"),
    );
    expect(commandPreview).toHaveClass("text-slate-900", "dark:text-slate-100");
  });
});

describe("DeepSpaceThread virtualization", () => {
  it("shows measured output speed and TTFT beside the response actions", () => {
    render(
      <DeepSpaceThread
        messages={[
          {
            id: "measured-response",
            role: "assistant",
            content: "Measured answer",
            rawContent: "Measured answer",
            createdAt: new Date().toISOString(),
            status: "ready",
            metrics: {
              providerOutputTokens: 51,
              providerGenerationDurationMs: 1200,
              tokensPerSecond: 42.5,
              timeToFirstTokenMs: 1190,
              providerTimeToFirstOutputMs: 480,
            },
          },
        ]}
        emptyPrompts={[]}
        onPromptSelect={() => {}}
        onInsertLatestAnswer={() => {}}
      />,
    );

    const performance = screen.getByRole("group", { name: "Response performance" });
    expect(performance).toHaveTextContent("42.5 tok/s");
    expect(performance).toHaveTextContent("TTFT 1.19s");
    expect(performance).toHaveTextContent("Provider 480ms");
    expect(screen.getByLabelText(/provider-reported output tokens/i)).toBeInTheDocument();
  });

  it("shows speed as unavailable when the provider does not report exact usage", () => {
    render(
      <DeepSpaceThread
        messages={[
          {
            id: "unavailable-speed-response",
            role: "assistant",
            content: "Answer",
            rawContent: "Answer",
            createdAt: new Date().toISOString(),
            status: "ready",
            metrics: {
              providerOutputTokens: null,
              providerGenerationDurationMs: null,
              tokensPerSecond: null,
              tokensPerSecondUnavailableReason: "usage_missing_for_generation_round",
              timeToFirstTokenMs: 472,
            },
          },
        ]}
        emptyPrompts={[]}
        onPromptSelect={() => {}}
        onInsertLatestAnswer={() => {}}
      />,
    );

    const performance = screen.getByRole("group", { name: "Response performance" });
    expect(performance).toHaveTextContent("Tok/s —");
    expect(performance).toHaveTextContent("TTFT 472ms");
    expect(
      screen.getByLabelText(
        /at least one generation round did not report exact output-token usage/,
      ),
    ).toBeInTheDocument();
  });

  it("renders a bounded window from a long mission when scroll metrics are provided", () => {
    const messages = Array.from({ length: 80 }, (_, index) => ({
      id: `msg-${index + 1}`,
      role: "assistant" as const,
      content: `Message ${index + 1}`,
      rawContent: `Message ${index + 1}`,
      createdAt: new Date().toISOString(),
      status: "ready" as const,
    }));

    render(
      <DeepSpaceThread
        messages={messages}
        emptyPrompts={[]}
        scrollMetrics={{
          scrollTop: 7000,
          viewportHeight: 900,
        }}
        onPromptSelect={() => {}}
        onInsertLatestAnswer={() => {}}
      />,
    );

    expect(screen.queryByText(/^Message 1$/)).not.toBeInTheDocument();
    expect(screen.getByText(/^Message 31$/)).toBeInTheDocument();
    expect(screen.queryByText(/^Message 80$/)).not.toBeInTheDocument();
  });

  it("falls back safely to the tail of the thread when scroll metrics are past the end", () => {
    const messages = Array.from({ length: 32 }, (_, index) => ({
      id: `msg-${index + 1}`,
      role: "assistant" as const,
      content: `Message ${index + 1}`,
      rawContent: `Message ${index + 1}`,
      createdAt: new Date().toISOString(),
      status: "ready" as const,
    }));

    render(
      <DeepSpaceThread
        messages={messages}
        emptyPrompts={[]}
        scrollMetrics={{
          scrollTop: 100000,
          viewportHeight: 900,
        }}
        onPromptSelect={() => {}}
        onInsertLatestAnswer={() => {}}
      />,
    );

    expect(screen.getByText(/^Message 32$/)).toBeInTheDocument();
    expect(screen.queryByText(/^Message 1$/)).not.toBeInTheDocument();
  });

  it("keeps the complete thread in a stable layout while an answer streams", () => {
    const messages = Array.from({ length: 80 }, (_, index) => ({
      id: `stream-msg-${index + 1}`,
      role: "assistant" as const,
      content: `Message ${index + 1}`,
      rawContent: `Message ${index + 1}`,
      createdAt: new Date().toISOString(),
      status: (index === 79 ? "streaming" : "ready") as "streaming" | "ready",
    }));

    render(
      <DeepSpaceThread
        messages={messages}
        emptyPrompts={[]}
        scrollMetrics={{ scrollTop: 7000, viewportHeight: 900 }}
        onPromptSelect={() => {}}
        onInsertLatestAnswer={() => {}}
      />,
    );

    expect(screen.getByText(/^Message 1$/)).toBeInTheDocument();
    expect(screen.getByText(/^Message 80$/)).toBeInTheDocument();
  });

  it("keeps agent timelines in the normal flow while scrolling", () => {
    const messages = Array.from({ length: 80 }, (_, index) => ({
      id: `agent-msg-${index + 1}`,
      role: "assistant" as const,
      content: `Agent message ${index + 1}`,
      rawContent: `Agent message ${index + 1}`,
      createdAt: new Date().toISOString(),
      status: "ready" as const,
      agentSteps: [
        {
          id: `step-${index + 1}`,
          type: "thinking" as const,
          status: "completed" as const,
          startedAt: new Date().toISOString(),
          data: { message: "Observed" },
        },
      ],
    }));

    render(
      <DeepSpaceThread
        messages={messages}
        emptyPrompts={[]}
        scrollMetrics={{ scrollTop: 7000, viewportHeight: 900 }}
        onPromptSelect={() => {}}
        onInsertLatestAnswer={() => {}}
      />,
    );

    expect(screen.getByText(/^Agent message 1$/)).toBeInTheDocument();
    expect(screen.getByText(/^Agent message 80$/)).toBeInTheDocument();
  });

  it("hides the response indicator as soon as any streamed progress begins", () => {
    const { rerender } = render(
      <DeepSpaceThread
        messages={[
          {
            id: "pending-assistant",
            role: "assistant" as const,
            content: "",
            rawContent: "",
            createdAt: new Date().toISOString(),
            status: "streaming" as const,
            agentSteps: [],
            timeline: [],
          },
        ]}
        emptyPrompts={[]}
        onPromptSelect={() => {}}
        onInsertLatestAnswer={() => {}}
      />,
    );

    expect(
      screen.getByRole("status", { name: "AverQel is preparing a response" }),
    ).toBeInTheDocument();

    rerender(
      <DeepSpaceThread
        messages={[
          {
            id: "pending-assistant",
            role: "assistant" as const,
            content: "Hello",
            rawContent: "Hello",
            createdAt: new Date().toISOString(),
            status: "streaming" as const,
          },
        ]}
        emptyPrompts={[]}
        onPromptSelect={() => {}}
        onInsertLatestAnswer={() => {}}
      />,
    );

    expect(
      screen.queryByRole("status", { name: "AverQel is preparing a response" }),
    ).not.toBeInTheDocument();

    rerender(
      <DeepSpaceThread
        messages={[
          {
            id: "pending-assistant",
            role: "assistant" as const,
            content: "",
            rawContent: "",
            createdAt: new Date().toISOString(),
            status: "streaming" as const,
            timeline: [
              {
                id: "thinking-1",
                stepId: "thinking-1",
                turnIndex: 1,
                phase: "thinking",
                type: "thinking" as const,
                title: "Internal Thought",
                status: "running" as const,
                startedAt: new Date().toISOString(),
                details: "Working…",
              },
            ],
          },
        ]}
        emptyPrompts={[]}
        onPromptSelect={() => {}}
        onInsertLatestAnswer={() => {}}
      />,
    );

    expect(
      screen.queryByRole("status", { name: "AverQel is preparing a response" }),
    ).not.toBeInTheDocument();
  });
});
