import { fireEvent, render, screen, within } from "@testing-library/react";
import type { ComponentProps } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import DeepSpaceSidePanel from "../app/dashboard/deepspace/_components/DeepSpaceSidePanel";
import DeepSpaceThread from "../app/dashboard/deepspace/_components/DeepSpaceThread";
import {
  deepSpaceThreadReducer,
  initialDeepSpaceThreadState,
} from "../app/dashboard/deepspace/_lib/deepspace-thread";
import type { DeepSpaceMessage } from "../app/dashboard/deepspace/_lib/deepspace-stream";
import { BUILTIN_PANEL_MODULES } from "../app/dashboard/deepspace/_lib/builtin-modules";
import { ModuleRegistryProvider } from "../app/dashboard/deepspace/_lib/module-registry";

const fetchWithAuthMock = vi.fn();

vi.mock("@/lib/api", () => ({
  fetchWithAuth: (...args: unknown[]) => fetchWithAuthMock(...args),
  getRequestTenantId: () => null,
  resolveApiUrl: (endpoint: string) => endpoint,
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn() }),
  usePathname: () => "/dashboard/deepspace",
}));

vi.mock("@xterm/xterm", () => ({
  Terminal: class {
    open() {}
    loadAddon() {}
    focus() {}
    dispose() {}
    write() {}
    onData() {
      return { dispose() {} };
    }
    onResize() {
      return { dispose() {} };
    }
  },
}));

vi.mock("@xterm/addon-fit", () => ({
  FitAddon: class {
    fit() {}
    dispose() {}
  },
}));

function jsonResponse(payload: unknown) {
  return { ok: true, status: 200, json: async () => payload };
}

function renderPanel(props: ComponentProps<typeof DeepSpaceSidePanel>) {
  return render(
    <ModuleRegistryProvider initialModules={BUILTIN_PANEL_MODULES}>
      <DeepSpaceSidePanel initialTab="files" {...props} />
    </ModuleRegistryProvider>,
  );
}

describe("DeepSpace workspace attachment wrap chips and universal preview", () => {
  beforeEach(() => {
    fetchWithAuthMock.mockReset();
    Object.defineProperty(URL, "createObjectURL", {
      configurable: true,
      writable: true,
      value: vi.fn(() => "blob:workspace-file"),
    });
    Object.defineProperty(URL, "revokeObjectURL", {
      configurable: true,
      writable: true,
      value: vi.fn(),
    });
    if (!Element.prototype.scrollIntoView) {
      Element.prototype.scrollIntoView = vi.fn();
    }
  });

  it("captures workspace_files from SSE stream events and rehydrates them from history", () => {
    let state = deepSpaceThreadReducer(initialDeepSpaceThreadState, {
      type: "submit_query",
      query: "Create wallpaper files",
      clientRequestId: "req-1",
    });

    state = deepSpaceThreadReducer(state, {
      type: "stream_event",
      event: {
        event: "workspace_files",
        data: {
          files: [
            {
              path: "ai_news_wallpaper.svg",
              name: "ai_news_wallpaper.svg",
              extension: "svg",
              mime_type: "image/svg+xml",
              size_bytes: 1405,
              change_type: "created",
            },
            {
              path: "wallpaper_ai_news.ppm",
              name: "wallpaper_ai_news.ppm",
              extension: "ppm",
              mime_type: "image/x-portable-pixmap",
              size_bytes: 25_165_824,
              change_type: "created",
            },
          ],
        },
      },
    });

    const streamedAssistant = state.messages.find((m) => m.role === "assistant");
    expect(streamedAssistant?.workspaceFiles).toHaveLength(2);
    expect(streamedAssistant?.workspaceFiles?.[1]).toMatchObject({
      path: "wallpaper_ai_news.ppm",
      extension: "ppm",
      mime_type: "image/x-portable-pixmap",
    });

    const hydrated = deepSpaceThreadReducer(initialDeepSpaceThreadState, {
      type: "load_history",
      conversationId: "conv-1",
      messages: [
        {
          id: "msg-1",
          role: "assistant",
          content: "Created your files.",
          created_at: "2026-11-25T22:00:00Z",
          metadata_json: {
            status: "ready",
            workspace_files: [
              {
                path: "reports/summary.pdf",
                name: "summary.pdf",
                extension: "pdf",
                mime_type: "application/pdf",
                size_bytes: 245_760,
                change_type: "presented",
              },
            ],
          },
        },
      ],
    });

    expect(hydrated.messages[0]?.workspaceFiles).toEqual([
      {
        path: "reports/summary.pdf",
        name: "summary.pdf",
        extension: "pdf",
        mime_type: "application/pdf",
        size_bytes: 245_760,
        change_type: "presented",
      },
    ]);
  });

  it("renders compact wrap chips for non-code and binary workspace files and opens them in the sidebar", () => {
    const onOpenWorkspaceFile = vi.fn();
    const message: DeepSpaceMessage = {
      id: "assistant-1",
      role: "assistant",
      content: "Files created in workspace.",
      rawContent: "Files created in workspace.",
      createdAt: "2026-11-25T22:00:00Z",
      status: "ready",
      workspaceFiles: [
        {
          path: "ai_news_wallpaper.svg",
          name: "ai_news_wallpaper.svg",
          extension: "svg",
          mime_type: "image/svg+xml",
          size_bytes: 1405,
        },
        {
          path: "ai_news_wallpaper.html",
          name: "ai_news_wallpaper.html",
          extension: "html",
          mime_type: "text/html",
          size_bytes: 1834,
        },
        {
          path: "wallpaper_ai_news.ppm",
          name: "wallpaper_ai_news.ppm",
          extension: "ppm",
          mime_type: "image/x-portable-pixmap",
          size_bytes: 25_165_824,
        },
        {
          path: "docs/offer.pdf",
          name: "offer.pdf",
          extension: "pdf",
          mime_type: "application/pdf",
          size_bytes: 245_760,
        },
      ],
    };

    render(
      <DeepSpaceThread
        messages={[message]}
        conversationId="conv-1"
        emptyPrompts={[]}
        onPromptSelect={vi.fn()}
        onInsertLatestAnswer={vi.fn()}
        onOpenWorkspaceFile={onOpenWorkspaceFile}
      />,
    );

    const chipsContainer = screen.getByTestId("deepspace-workspace-file-chips");
    expect(chipsContainer).toHaveClass("flex-wrap");
    expect(chipsContainer).toHaveTextContent("ai_news_wallpaper.svg");
    expect(chipsContainer).toHaveTextContent("SVG · 1.4 KB");
    expect(chipsContainer).toHaveTextContent("ai_news_wallpaper.html");
    expect(chipsContainer).toHaveTextContent("HTML · 1.8 KB");
    expect(chipsContainer).toHaveTextContent("wallpaper_ai_news.ppm");
    expect(chipsContainer).toHaveTextContent("PPM · 24 MB");
    expect(chipsContainer).toHaveTextContent("offer.pdf");
    expect(chipsContainer).toHaveTextContent("PDF · 240 KB");

    fireEvent.click(screen.getByRole("button", { name: /offer\.pdf/i }));
    expect(onOpenWorkspaceFile).toHaveBeenCalledWith("docs/offer.pdf");
  });

  it("previews HTML, SVG, PDF, and opaque binary workspace files in the right side panel", async () => {
    fetchWithAuthMock.mockImplementation(async (url: string) => {
      if (url.startsWith("/deepspace/chats/conv-1/workspace/tree")) {
        return jsonResponse({ entries: [] });
      }
      if (url.startsWith("/deepspace/chats/conv-1/workspace/file/raw")) {
        const type = url.includes(".ppm") ? "image/x-portable-pixmap" : "application/pdf";
        return new Response(new Uint8Array([0x25, 0x50, 0x44, 0x46]), {
          status: 200,
          headers: { "Content-Type": type },
        });
      }
      if (url.includes("ai_news_wallpaper.html")) {
        return jsonResponse({
          path: "ai_news_wallpaper.html",
          content: "<!doctype html><html><body><h1>AI Wallpaper</h1></body></html>",
          truncated: false,
          language: "html",
          size_bytes: 1834,
          mime_type: "text/html",
          is_binary: false,
        });
      }
      if (url.includes("wallpaper_ai_news.ppm")) {
        return jsonResponse({
          path: "wallpaper_ai_news.ppm",
          content: "",
          truncated: false,
          language: null,
          size_bytes: 25_165_824,
          mime_type: "image/x-portable-pixmap",
          is_binary: true,
        });
      }
      if (url.includes("offer.pdf")) {
        return jsonResponse({
          path: "docs/offer.pdf",
          content: "",
          truncated: false,
          language: null,
          size_bytes: 245_760,
          mime_type: "application/pdf",
          is_binary: true,
        });
      }
      return jsonResponse({
        workspace: { id: "default", name: "Default", path: "/home/u/Kinesis" },
      });
    });

    const { rerender } = renderPanel({
      conversationId: "conv-1",
      open: true,
      onClose: vi.fn(),
      requestedFileDiff: {
        path: "ai_news_wallpaper.html",
        seq: 1,
        preferDiff: false,
      },
    });

    const htmlPreview = await screen.findByTestId("deepspace-side-panel-file-preview");
    expect(htmlPreview.querySelector("iframe")).toHaveAttribute(
      "srcDoc",
      expect.stringContaining("AI Wallpaper"),
    );

    rerender(
      <ModuleRegistryProvider initialModules={BUILTIN_PANEL_MODULES}>
        <DeepSpaceSidePanel
          initialTab="files"
          conversationId="conv-1"
          open={true}
          onClose={vi.fn()}
          requestedFileDiff={{
            path: "wallpaper_ai_news.ppm",
            seq: 2,
            preferDiff: false,
          }}
        />
      </ModuleRegistryProvider>,
    );

    expect(await screen.findByText(/cannot be rendered inline/i)).toBeInTheDocument();
    expect(screen.getAllByText(/24 MB/i).length).toBeGreaterThanOrEqual(1);
    expect(screen.getByRole("button", { name: /^Download$/i })).toBeInTheDocument();

    rerender(
      <ModuleRegistryProvider initialModules={BUILTIN_PANEL_MODULES}>
        <DeepSpaceSidePanel
          initialTab="files"
          conversationId="conv-1"
          open={true}
          onClose={vi.fn()}
          requestedFileDiff={{
            path: "docs/offer.pdf",
            seq: 3,
            preferDiff: false,
          }}
        />
      </ModuleRegistryProvider>,
    );

    const pdfIframe = await screen.findByTitle("PDF preview");
    expect(pdfIframe).toHaveAttribute("src", "blob:workspace-file");
  });

  it("extracts wrap chips from historical messages, tool outputs, and artifacts across all formats", () => {
    const onOpenWorkspaceFile = vi.fn();
    const onOpenArtifact = vi.fn();

    const historicalMessage: DeepSpaceMessage = {
      id: "hist-1",
      role: "assistant",
      content: [
        "Files Created",
        "1. `ai_news_wallpaper.svg` (1,405 bytes)",
        "2. `ai_news_wallpaper.html` (1,834 bytes)",
        "3. `wallpaper_ai_news.ppm` (24 MB)",
        "4. Ignore casual `nonexistent.txt` and `https://example.com`.",
        "Open `/home/AverQel/Kinesis/ai_news_wallpaper.svg` in any browser.",
      ].join("\n"),
      rawContent: "Files Created",
      createdAt: "2026-11-25T21:54:00Z",
      status: "ready",
      timeline: [
        {
          id: "st-1",
          stepId: "st-1",
          turnIndex: 1,
          phase: "modifying",
          type: "tool_output",
          title: "Write report",
          status: "completed",
          startedAt: "2026-11-25T21:54:00Z",
          toolName: "fs_write",
          toolInput: { path: "/home/AverQel/Kinesis/reports/summary.docx" },
        },
        {
          id: "st-2",
          stepId: "st-2",
          turnIndex: 1,
          phase: "modifying",
          type: "tool_output",
          title: "Export chart",
          status: "completed",
          startedAt: "2026-11-25T21:54:01Z",
          toolName: "bash",
          toolOutput: JSON.stringify({ output_path: "charts/trend.png" }),
        },
      ],
      artifacts: [
        {
          id: "art-audio",
          kind: "audio",
          title: "briefing.mp3",
          url: "/deepspace/artifacts/briefing.mp3",
          content_type: "audio/mpeg",
          size_bytes: 524_288,
          status: "ready",
        },
      ],
    };

    render(
      <DeepSpaceThread
        messages={[historicalMessage]}
        conversationId="conv-1"
        emptyPrompts={[]}
        onPromptSelect={vi.fn()}
        onInsertLatestAnswer={vi.fn()}
        onOpenWorkspaceFile={onOpenWorkspaceFile}
        onOpenArtifact={onOpenArtifact}
        workspaceRoot="/home/AverQel/Kinesis"
        workspaceEntries={[
          {
            name: "ai_news_wallpaper.svg",
            path: "ai_news_wallpaper.svg",
            kind: "file",
            size_bytes: 1405,
          },
          {
            name: "ai_news_wallpaper.html",
            path: "ai_news_wallpaper.html",
            kind: "file",
            size_bytes: 1834,
          },
          {
            name: "wallpaper_ai_news.ppm",
            path: "wallpaper_ai_news.ppm",
            kind: "file",
            size_bytes: 24_884_296,
          },
        ]}
      />,
    );

    const chipsContainer = screen.getByTestId("deepspace-workspace-file-chips");
    expect(chipsContainer).toHaveTextContent("ai_news_wallpaper.svg");
    expect(chipsContainer).toHaveTextContent("SVG · 1.4 KB");
    expect(chipsContainer).toHaveTextContent("ai_news_wallpaper.html");
    expect(chipsContainer).toHaveTextContent("HTML · 1.8 KB");
    expect(chipsContainer).toHaveTextContent("wallpaper_ai_news.ppm");
    expect(chipsContainer).toHaveTextContent("PPM · 24 MB");
    expect(chipsContainer).toHaveTextContent("summary.docx");
    expect(chipsContainer).toHaveTextContent("trend.png");
    expect(chipsContainer).toHaveTextContent("briefing.mp3");
    expect(chipsContainer).toHaveTextContent("MP3 · 512 KB");
    expect(chipsContainer).not.toHaveTextContent("nonexistent.txt");

    fireEvent.click(
      within(chipsContainer).getByRole("button", { name: /ai_news_wallpaper\.svg/i }),
    );
    expect(onOpenWorkspaceFile).toHaveBeenCalledWith("ai_news_wallpaper.svg");

    fireEvent.click(within(chipsContainer).getByRole("button", { name: /summary\.docx/i }));
    expect(onOpenWorkspaceFile).toHaveBeenCalledWith("reports/summary.docx");

    fireEvent.click(within(chipsContainer).getByRole("button", { name: /briefing\.mp3/i }));
    expect(onOpenArtifact).toHaveBeenCalledWith(
      expect.objectContaining({ id: "art-audio", title: "briefing.mp3" }),
    );
  });
});
