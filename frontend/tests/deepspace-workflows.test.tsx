import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { WorkflowsTab } from "../app/dashboard/deepspace/_components/DeepSpaceSidePanel";
import DeepSpaceWorkflowEditor from "../app/dashboard/deepspace/_components/DeepSpaceWorkflowEditor";

const fetchWithAuthMock = vi.fn();
const realtimeHandler = { current: null as null | ((event: unknown) => void) };

vi.mock("@/lib/api", () => ({
  fetchWithAuth: (...args: unknown[]) => fetchWithAuthMock(...args),
  getRequestTenantId: () => null,
  resolveApiUrl: (endpoint: string) => endpoint,
}));

vi.mock("@/lib/realtime", () => ({
  useRealtimeEvents: (
    handler: (event: unknown) => void,
    resources: string[],
  ) => {
    if (resources.length === 1 && resources[0] === "workflow-runs") {
      realtimeHandler.current = handler;
    }
  },
}));

function jsonResponse(payload: unknown) {
  return { ok: true, status: 200, json: async () => payload };
}

/** The workflow runs one conversation holds. */
function workflowRuns() {
  return [
    {
      run_id: "job-1",
      job_id: "job-1",
      workflow: "file-audit",
      mode: "script",
      status: "running",
      current_phase: "reading",
      started_at: "2026-01-01T00:00:00.000Z",
      completed_at: null,
      agents_spawned: 1,
      events: [
        { sequence: 1, type: "workflow/phase", payload: { title: "reading" } },
        { sequence: 2, type: "workflow/log", payload: { message: "started" } },
      ],
    },
  ];
}

describe("the workflow panel", () => {
  beforeEach(() => {
    fetchWithAuthMock.mockReset();
    realtimeHandler.current = null;
    fetchWithAuthMock.mockImplementation(async (url: string) => {
      if (url === "/deepspace/chats/conv-1/workflow-runs?limit=50") {
        return jsonResponse(workflowRuns());
      }
      return jsonResponse([]);
    });
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("appends a live event to the run without waiting for another poll", async () => {
    render(<WorkflowsTab conversationId="conv-1" />);
    expect(await screen.findByText("file-audit")).toBeInTheDocument();

    fireEvent.click(screen.getByText("file-audit"));
    expect(screen.getByText("started")).toBeInTheDocument();

    const callsBefore = fetchWithAuthMock.mock.calls.length;

    await act(async () => {
      realtimeHandler.current?.({
        resource: "workflow-runs",
        type: "workflow-runs",
        data: {
          run_id: "job-1",
          sequence: 3,
          event_type: "workflow/log",
          payload: { message: "agent finished" },
          agents_spawned: 2,
        },
        occurred_at: "2026-01-01T00:00:02.000Z",
      });
    });

    expect(await screen.findByText("agent finished")).toBeInTheDocument();
    expect(fetchWithAuthMock.mock.calls.length).toBe(callsBefore);
  });

  it("shows the live agent count and the run's mode", async () => {
    render(<WorkflowsTab conversationId="conv-1" />);
    expect(await screen.findByText("file-audit")).toBeInTheDocument();

    fireEvent.click(screen.getByText("file-audit"));

    await act(async () => {
      realtimeHandler.current?.({
        resource: "workflow-runs",
        type: "workflow-runs",
        data: { run_id: "job-1", agents_spawned: 4 },
      });
    });

    await waitFor(() => {
      expect(screen.getByText("4")).toBeInTheDocument();
    });
    expect(screen.getByText("script")).toBeInTheDocument();
  });

  it("keeps a live event when a later poll returns fewer events", async () => {
    let call = 0;
    fetchWithAuthMock.mockImplementation(async (url: string) => {
      if (url === "/deepspace/chats/conv-1/workflow-runs?limit=50") {
        call += 1;
        return jsonResponse(call === 1 ? workflowRuns() : [
          {
            ...workflowRuns()[0],
            events: [{ sequence: 1, type: "workflow/phase", payload: { title: "reading" } }],
          },
        ]);
      }
      return jsonResponse([]);
    });

    render(<WorkflowsTab conversationId="conv-1" />);
    expect(await screen.findByText("file-audit")).toBeInTheDocument();
    fireEvent.click(screen.getByText("file-audit"));

    await act(async () => {
      realtimeHandler.current?.({
        resource: "workflow-runs",
        type: "workflow-runs",
        data: {
          run_id: "job-1",
          sequence: 5,
          event_type: "workflow/log",
          payload: { message: "kept alive" },
        },
      });
    });

    expect(await screen.findByText("kept alive")).toBeInTheDocument();
  });
});

describe("the workflow editor", () => {
  beforeEach(() => {
    fetchWithAuthMock.mockReset();
    fetchWithAuthMock.mockImplementation(async (url: string, init?: RequestInit) => {
      if (url === "/deepspace/chats/conv-1/workspace/workflows") {
        return jsonResponse({
          workflows: [
            {
              name: "file-audit",
              path: ".averqel/workflows/file-audit.json",
              valid: true,
              step_count: 2,
              description: "",
            },
          ],
        });
      }
      if (url === "/deepspace/chats/conv-1/workspace/workflows/file-audit") {
        if (init?.method === "DELETE") return jsonResponse({ name: "file-audit", deleted: true });
        return jsonResponse({
          name: "file-audit",
          path: ".averqel/workflows/file-audit.json",
          document: {
            max_concurrency: 2,
            steps: [
              { id: "read", task: "Read every file." },
              { id: "judge", task: "Judge the findings.", depends_on: ["read"] },
            ],
          },
          content: "{}",
        });
      }
      return jsonResponse({});
    });
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("lists the installed workflows and opens one", async () => {
    render(<DeepSpaceWorkflowEditor conversationId="conv-1" />);

    expect(await screen.findByText("file-audit")).toBeInTheDocument();
    expect(screen.getByText("2 steps")).toBeInTheDocument();

    fireEvent.click(screen.getByText("file-audit"));

    const editor = await screen.findByLabelText("Workflow document");
    expect((editor as HTMLTextAreaElement).value).toContain("Read every file.");
    expect((editor as HTMLTextAreaElement).value).toContain("depends_on");
  });

  it("saves the edited document back to the workspace", async () => {
    render(<DeepSpaceWorkflowEditor conversationId="conv-1" />);
    expect(await screen.findByText("file-audit")).toBeInTheDocument();

    fireEvent.click(screen.getByText("file-audit"));
    const editor = await screen.findByLabelText("Workflow document");
    fireEvent.change(editor, {
      target: {
        value: JSON.stringify({ steps: [{ id: "only", task: "One step." }] }, null, 2),
      },
    });

    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(
        fetchWithAuthMock.mock.calls.some(
          ([url, init]) =>
            url === "/deepspace/chats/conv-1/workspace/workflows/file-audit" &&
            init?.method === "PUT" &&
            String(init.body).includes("One step."),
        ),
      ).toBe(true);
    });
    expect(await screen.findByText("Saved file-audit.")).toBeInTheDocument();
  });

  it("refuses to save a document that is not valid JSON", async () => {
    render(<DeepSpaceWorkflowEditor conversationId="conv-1" />);
    expect(await screen.findByText("file-audit")).toBeInTheDocument();

    fireEvent.click(screen.getByText("file-audit"));
    const editor = await screen.findByLabelText("Workflow document");
    fireEvent.change(editor, { target: { value: "{ not json" } });

    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(
      fetchWithAuthMock.mock.calls.some(([, init]) => init?.method === "PUT"),
    ).toBe(false);
  });

  it("deletes an installed workflow", async () => {
    render(<DeepSpaceWorkflowEditor conversationId="conv-1" />);
    expect(await screen.findByText("file-audit")).toBeInTheDocument();

    fireEvent.click(screen.getByText("file-audit"));
    await screen.findByLabelText("Workflow document");

    fireEvent.click(screen.getByRole("button", { name: "Delete file-audit" }));

    await waitFor(() => {
      expect(
        fetchWithAuthMock.mock.calls.some(
          ([url, init]) =>
            url === "/deepspace/chats/conv-1/workspace/workflows/file-audit" &&
            init?.method === "DELETE",
        ),
      ).toBe(true);
    });
    expect(await screen.findByText("Deleted file-audit.")).toBeInTheDocument();
  });

  it("offers a new workflow that is not installed yet", async () => {
    render(<DeepSpaceWorkflowEditor conversationId="conv-1" />);
    expect(await screen.findByText("file-audit")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "New" }));

    const editor = await screen.findByLabelText("Workflow document");
    expect((editor as HTMLTextAreaElement).value).toContain("step-1");
    expect(screen.queryByRole("button", { name: "Delete new-workflow" })).not.toBeInTheDocument();
  });
});
