import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import DeepSpaceComposer from "../app/dashboard/deepspace/_components/DeepSpaceComposer";

const fetchWithAuth = vi.hoisted(() => vi.fn());

vi.mock("@/lib/api", () => ({
  fetchWithAuth,
}));

describe("the DeepSpace sandbox mode control", () => {
  it("shows the conversation's mode in the selector", () => {
    render(
      <DeepSpaceComposer
        conversationId="conversation-1"
        query=""
        isStreaming={false}
        sandboxMode="read-only"
        onSandboxModeChange={vi.fn()}
        onQueryChange={vi.fn()}
        onSubmit={vi.fn()}
        onStop={vi.fn()}
      />,
    );

    const selector = screen.getByLabelText("Sandbox mode") as HTMLSelectElement;
    expect(selector.value).toBe("read-only");
    expect(screen.getByRole("option", { name: "Workspace" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Full access" })).toBeInTheDocument();
  });

  it("reports the mode the user chose", () => {
    const onSandboxModeChange = vi.fn();
    render(
      <DeepSpaceComposer
        conversationId="conversation-1"
        query=""
        isStreaming={false}
        sandboxMode="workspace-write"
        onSandboxModeChange={onSandboxModeChange}
        onQueryChange={vi.fn()}
        onSubmit={vi.fn()}
        onStop={vi.fn()}
      />,
    );

    fireEvent.change(screen.getByLabelText("Sandbox mode"), { target: { value: "read-only" } });

    expect(onSandboxModeChange).toHaveBeenCalledWith("read-only");
  });

  it("offers the plan mode and the agent tree next to the mode selector", () => {
    render(
      <DeepSpaceComposer
        conversationId="conversation-1"
        query=""
        isStreaming={false}
        planModeActive={false}
        onPlanModeChange={vi.fn()}
        agentTreeOpen={false}
        onAgentTreeToggle={vi.fn()}
        onQueryChange={vi.fn()}
        onSubmit={vi.fn()}
        onStop={vi.fn()}
      />,
    );

    expect(screen.getByRole("button", { name: "Enter plan mode" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Show the agent tree" })).toBeInTheDocument();
  });
});
