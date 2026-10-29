import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";

import {
  renderStructuredToolInput,
  renderStructuredToolOutput,
} from "../app/dashboard/deepspace/_components/ToolRichPayload";
import { TOOL_LABELS } from "../app/dashboard/deepspace/_lib/constants";

it("labels every native tool the DeepSpace agent can call", () => {
  const nativeTools = [
    "fs_read",
    "fs_write",
    "fs_edit",
    "fs_delete",
    "fs_str_replace",
    "fs_grep",
    "fs_glob",
    "bash",
    "pwsh",
    "subagent",
    "subagent_fork",
    "send_message",
    "interrupt_agent",
    "list_agents",
    "job_list",
    "job_status",
    "job_kill",
    "workflow_run",
    "ralph_run",
    "goal_read",
    "goal_set",
    "skill_load",
    "skill_list",
  ];

  for (const tool of nativeTools) {
    expect(TOOL_LABELS[tool], `missing label for ${tool}`).toBeTruthy();
  }
});

it("shows the workspace path for a file tool call", () => {
  render(
    renderStructuredToolInput("fs_read", { path: "src/app.ts", offset: 1 }) as React.ReactElement,
  );

  expect(screen.getByText("src/app.ts")).toBeInTheDocument();
});

it("shows the command for a shell tool call", () => {
  render(
    renderStructuredToolInput("bash", { command: "pnpm test" }) as React.ReactElement,
  );

  expect(screen.getByText("pnpm test")).toBeInTheDocument();
});

it("shows the delegated task for a subagent call", () => {
  render(
    renderStructuredToolInput("subagent", {
      description: "audit the parser",
      task: "read the parser and report the findings",
    }) as React.ReactElement,
  );

  expect(screen.getByText("audit the parser")).toBeInTheDocument();
  expect(screen.getByText("read the parser and report the findings")).toBeInTheDocument();
});

it("lists the child agents with their state", () => {
  const output = JSON.stringify({
    status: "ok",
    agent_count: 1,
    agents: [
      {
        agent_id: "child-1",
        label: "audit the parser",
        mode: "continuable",
        origin: "subagent",
        status: "idle",
        depth: 1,
        turn_count: 2,
      },
    ],
  });

  render(renderStructuredToolOutput("list_agents", output) as React.ReactElement);

  expect(screen.getByText("Sub-agents (1)")).toBeInTheDocument();
  expect(screen.getByText("audit the parser")).toBeInTheDocument();
  expect(screen.getByText(/depth 1/)).toBeInTheDocument();
});

it("shows the command output and marks a failing command", () => {
  const failing = JSON.stringify({
    status: "failed",
    exit_code: 2,
    output: "boom",
  });

  render(renderStructuredToolOutput("bash", failing) as React.ReactElement);

  expect(screen.getByText("Command failed")).toBeInTheDocument();
  expect(screen.getByText("boom")).toBeInTheDocument();
});

it("shows an empty shell output rather than an empty panel", () => {
  const empty = JSON.stringify({ status: "ok", exit_code: 0, output: "" });

  render(renderStructuredToolOutput("bash", empty) as React.ReactElement);

  expect(screen.getByText("(no output)")).toBeInTheDocument();
});
