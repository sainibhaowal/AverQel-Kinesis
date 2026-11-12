import React from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import DeepSpaceQuestionCard, {
  type MCPConnectionPromptOption,
} from "../app/dashboard/deepspace/_components/DeepSpaceQuestionCard";

const option: MCPConnectionPromptOption = {
  entry_id: "entry-local-filesystem",
  name: "Local Filesystem",
  description: "Read files from the selected local location.",
  command: "/usr/bin/python3",
  args: ["-m", "example_mcp_server"],
  permission_notice:
    "The MCP worker runs with its operating-system account permissions. The shell sandbox does not contain this process.",
};

describe("DeepSpace local MCP connection question", () => {
  it("shows the exact launch command and permission boundary before Connect", () => {
    render(
      <DeepSpaceQuestionCard
        question="Connect a local provider?"
        options={[]}
        onSubmit={vi.fn(async () => {})}
        mcpConnectionOptions={[option]}
        onConnectMcp={vi.fn(async () => {})}
        onCancelMcp={vi.fn(async () => {})}
      />,
    );

    expect(screen.getByText("Local Filesystem")).toBeInTheDocument();
    expect(screen.getByText(/"\/usr\/bin\/python3"/)).toBeInTheDocument();
    expect(screen.getByText(/operating-system account permissions/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Connect Local Filesystem" })).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Cancel and continue without MCP" }),
    ).toBeInTheDocument();
  });

  it("connects only the selected approved server", () => {
    const onConnectMcp = vi.fn(async () => {});
    render(
      <DeepSpaceQuestionCard
        question="Connect a local provider?"
        options={[]}
        onSubmit={vi.fn(async () => {})}
        mcpConnectionOptions={[option]}
        onConnectMcp={onConnectMcp}
        onCancelMcp={vi.fn(async () => {})}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Connect Local Filesystem" }));

    expect(onConnectMcp).toHaveBeenCalledWith(option);
  });

  it("cancels MCP setup while keeping the original request resumable", () => {
    const onCancelMcp = vi.fn(async () => {});
    render(
      <DeepSpaceQuestionCard
        question="Connect a local provider?"
        options={[]}
        onSubmit={vi.fn(async () => {})}
        mcpConnectionOptions={[option]}
        onConnectMcp={vi.fn(async () => {})}
        onCancelMcp={onCancelMcp}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Cancel and continue without MCP" }));

    expect(onCancelMcp).toHaveBeenCalledOnce();
  });

  it("offers a separate action that stops the paused task", () => {
    const onStopTask = vi.fn(async () => {});
    render(
      <DeepSpaceQuestionCard
        question="Connect a local provider?"
        options={[]}
        onSubmit={vi.fn(async () => {})}
        mcpConnectionOptions={[option]}
        onConnectMcp={vi.fn(async () => {})}
        onCancelMcp={vi.fn(async () => {})}
        onStopTask={onStopTask}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Stop this task" }));

    expect(onStopTask).toHaveBeenCalledOnce();
    expect(
      screen.getByRole("button", { name: "Cancel and continue without MCP" }),
    ).toBeInTheDocument();
  });
});
