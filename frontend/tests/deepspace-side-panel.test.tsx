import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ComponentProps } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import DeepSpaceSidePanel, {
  DeliverablesTab,
  TrajectoryTab,
  WorkflowsTab,
} from "../app/dashboard/deepspace/_components/DeepSpaceSidePanel";
import { ModuleRegistryProvider } from "../app/dashboard/deepspace/_lib/module-registry";
import { BUILTIN_PANEL_MODULES } from "../app/dashboard/deepspace/_lib/builtin-modules";

const fetchWithAuthMock = vi.fn();

class TestWebSocket {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  readyState = TestWebSocket.CONNECTING;
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string | ArrayBuffer }) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: (() => void) | null = null;
  readonly sent: string[] = [];

  constructor(readonly url: string) {
    queueMicrotask(() => {
      this.readyState = TestWebSocket.OPEN;
      this.onopen?.();
      this.onmessage?.({
        data: JSON.stringify({
          type: "ready",
          shell: "bash",
          pty: true,
          cwd: "/home/ravi/DeepSpace/Project",
          alive: true,
        }),
      });
    });
  }

  send(data: string) {
    this.sent.push(data);
  }

  close() {
    this.readyState = 3;
    this.onclose?.();
  }
}

vi.mock("@/lib/api", () => ({
  fetchWithAuth: (...args: unknown[]) => fetchWithAuthMock(...args),
  getRequestTenantId: () => null,
  resolveApiUrl: (endpoint: string) => endpoint,
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

/** Render the panel inside the registry its page provides. */
function renderPanel(props: ComponentProps<typeof DeepSpaceSidePanel>) {
  return render(
    <ModuleRegistryProvider initialModules={BUILTIN_PANEL_MODULES}>
      <DeepSpaceSidePanel initialTab="files" {...props} />
    </ModuleRegistryProvider>,
  );
}

/** The calls that read the output of the first terminal session. */
function outputReads(): unknown[][] {
  return fetchWithAuthMock.mock.calls.filter(([url]) =>
    String(url).startsWith("/deepspace/chats/conv-1/terminals/session-1"),
  );
}

describe("DeepSpaceSidePanel", () => {
  beforeEach(() => {
    fetchWithAuthMock.mockReset();
    fetchWithAuthMock.mockImplementation(async (url: string, init?: RequestInit) => {
      if (url.startsWith("/deepspace/chats/conv-1/workspace/tree")) {
        return jsonResponse({
          entries: [
            { name: "src", path: "src", kind: "directory", size_bytes: 0 },
            { name: "README.md", path: "README.md", kind: "file", size_bytes: 42 },
          ],
        });
      }
      if (url.startsWith("/deepspace/chats/conv-1/workspace/file")) {
        return jsonResponse({
          path: "README.md",
          content: "# DeepSpace workspace",
          truncated: false,
          language: "markdown",
        });
      }
      if (url === "/host/open-in-app/apps") {
        return jsonResponse([{ id: "vscode", name: "Visual Studio Code", category: "editor" }]);
      }
      if (url === "/host/open-in-app/open" && init?.method === "POST") {
        return jsonResponse({ success: true });
      }
      if (url === "/deepspace/chats/conv-1/terminals" && init?.method === "POST") {
        return jsonResponse({
          session_id: "session-2",
          shell: "bash",
          pty: true,
          cwd: "/home/ravi/DeepSpace/Project",
          output: "$ ",
        });
      }
      if (url === "/deepspace/chats/conv-1/terminals/shells") {
        return jsonResponse({ shells: ["bash"], default_shell: "bash" });
      }
      if (url === "/deepspace/chats/conv-1/terminals") {
        return jsonResponse({
          sessions: [
            {
              session_id: "session-1",
              shell: "bash",
              pty: true,
              cwd: "/home/ravi/DeepSpace/Project",
              created_at: "2026-01-01T00:00:00.000Z",
              idle_seconds: 4,
            },
          ],
        });
      }
      if (url === "/deepspace/chats/conv-1/terminals/session-1" && init?.method === "POST") {
        return jsonResponse({ written_bytes: 11 });
      }
      if (url === "/deepspace/chats/conv-1/terminals/session-1" && init?.method === "DELETE") {
        return jsonResponse({ closed: true });
      }
      if (url.startsWith("/deepspace/chats/conv-1/terminals/session-1")) {
        return jsonResponse({
          output: "$ npm run dev",
          truncated: false,
          dropped_bytes: 0,
          alive: true,
        });
      }
      return jsonResponse({});
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders nothing while the panel is closed", () => {
    const { container } = renderPanel({
      conversationId: "conv-1",
      open: false,
      onClose: vi.fn(),
    });

    expect(container).toBeEmptyDOMElement();
    expect(fetchWithAuthMock).not.toHaveBeenCalled();
  });

  it("lists the workspace files and shows the content of a clicked file", async () => {
    renderPanel({ conversationId: "conv-1", open: true, onClose: vi.fn() });

    expect(await screen.findByRole("button", { name: "README.md" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "src" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "README.md" }));

    expect(await screen.findByText("# DeepSpace workspace")).toBeInTheDocument();
    const reviewTab = screen.getByRole("tab", { name: "Review · README.md" });
    expect(reviewTab).toHaveAttribute("aria-selected", "true");
    const wrapButton = screen.getByRole("button", { name: "Toggle line wrap" });
    expect(wrapButton).toHaveAttribute("aria-pressed", "false");
    fireEvent.click(wrapButton);
    expect(wrapButton).toHaveAttribute("aria-pressed", "true");
    expect(
      fetchWithAuthMock.mock.calls.some(
        ([url]) => url === "/deepspace/chats/conv-1/workspace/file?path=README.md",
      ),
    ).toBe(true);
  });

  it("lists the terminal sessions and attaches a real interactive stream", async () => {
    const sockets: TestWebSocket[] = [];
    vi.stubGlobal(
      "WebSocket",
      class extends TestWebSocket {
        constructor(url: string) {
          super(url);
          sockets.push(this);
        }
      },
    );
    vi.stubGlobal(
      "ResizeObserver",
      class {
        observe() {}
        disconnect() {}
      },
    );
    renderPanel({
      conversationId: "conv-1",
      open: true,
      onClose: vi.fn(),
      initialTab: "terminal",
    });

    fireEvent.click(await screen.findByRole("button", { name: "Open a new terminal" }));
    expect(await screen.findByRole("menuitem", { name: "New bash terminal" })).toBeInTheDocument();
    expect(
      await screen.findByRole("textbox", { name: "Interactive terminal" }),
    ).toBeInTheDocument();
    expect(await screen.findByText("Connected · bash · PTY")).toBeInTheDocument();
    expect(screen.getByTitle("/home/ravi/DeepSpace/Project")).toBeInTheDocument();
    expect(sockets).toHaveLength(1);
    expect(sockets[0]?.url).toContain("/deepspace/chats/conv-1/terminals/session-1/stream");
    expect(outputReads()).toHaveLength(0);
  });

  it("keeps terminal tabs and the add button on one row and creates from the shell menu", async () => {
    const sockets: TestWebSocket[] = [];
    vi.stubGlobal(
      "WebSocket",
      class extends TestWebSocket {
        constructor(url: string) {
          super(url);
          sockets.push(this);
        }
      },
    );
    vi.stubGlobal(
      "ResizeObserver",
      class {
        observe() {}
        disconnect() {}
      },
    );
    renderPanel({
      conversationId: "conv-1",
      open: true,
      onClose: vi.fn(),
      initialTab: "terminal",
    });

    const tablist = await screen.findByRole("tablist", { name: "Terminal sessions" });
    const addTerminal = await screen.findByRole("button", { name: "Open a new terminal" });
    expect(tablist.parentElement).toContainElement(addTerminal);
    await waitFor(() =>
      expect(within(tablist).getByRole("tab", { name: "bash" })).toBeInTheDocument(),
    );

    fireEvent.click(addTerminal);
    fireEvent.click(await screen.findByRole("menuitem", { name: "New bash terminal" }));

    await waitFor(() => {
      expect(
        fetchWithAuthMock.mock.calls.some(
          ([url, init]) => url === "/deepspace/chats/conv-1/terminals" && init?.method === "POST",
        ),
      ).toBe(true);
    });
    await waitFor(() =>
      expect(within(tablist).getAllByRole("tab", { name: "bash" })).toHaveLength(2),
    );
    expect(tablist.parentElement).toContainElement(addTerminal);
    expect(screen.queryByRole("button", { name: "Terminal shell" })).not.toBeInTheDocument();
    expect(sockets.some((socket) => socket.url.includes("/terminals/session-2/stream"))).toBe(true);
  });

  it("keeps the new-terminal menu inside the viewport at the panel edge", async () => {
    const bounds = vi
      .spyOn(HTMLElement.prototype, "getBoundingClientRect")
      .mockImplementation(function (this: HTMLElement) {
        if (this.getAttribute("aria-label") === "Open a new terminal") {
          return new DOMRect(360, 700, 28, 28);
        }
        if (this.getAttribute("role") === "menu") return new DOMRect(0, 0, 160, 96);
        return new DOMRect(0, 0, 0, 0);
      });
    const originalWidth = Object.getOwnPropertyDescriptor(window, "innerWidth");
    const originalHeight = Object.getOwnPropertyDescriptor(window, "innerHeight");
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 400 });
    Object.defineProperty(window, "innerHeight", { configurable: true, value: 800 });
    renderPanel({
      conversationId: "conv-1",
      open: true,
      onClose: vi.fn(),
      initialTab: "terminal",
    });
    const trigger = await screen.findByRole("button", { name: "Open a new terminal" });
    await waitFor(() => expect(trigger).toBeEnabled());

    fireEvent.click(trigger);

    const menu = await screen.findByRole("menu", { name: "Choose a terminal shell" });
    await waitFor(() =>
      expect(menu).toHaveStyle({ position: "fixed", left: "228px", top: "600px" }),
    );
    expect(menu.parentElement).toBe(document.body);
    bounds.mockRestore();
    if (originalWidth) Object.defineProperty(window, "innerWidth", originalWidth);
    else Reflect.deleteProperty(window, "innerWidth");
    if (originalHeight) Object.defineProperty(window, "innerHeight", originalHeight);
    else Reflect.deleteProperty(window, "innerHeight");
  });

  it("refuses a non-http URL and frames an allowed loopback URL", async () => {
    renderPanel({
      conversationId: "conv-1",
      open: true,
      onClose: vi.fn(),
      initialTab: "browser",
    });

    const urlInput = screen.getByRole("textbox", { name: "Browser URL" });
    fireEvent.change(urlInput, { target: { value: "javascript:alert(1)" } });
    fireEvent.click(screen.getByRole("button", { name: "Go" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Only http:// and https:// URLs can be opened.",
    );
    expect(screen.queryByTitle("Browser preview")).not.toBeInTheDocument();

    fireEvent.change(urlInput, { target: { value: "http://localhost:3000" } });
    fireEvent.click(screen.getByRole("button", { name: "Go" }));

    const frame = await screen.findByTitle("Browser preview");
    expect(frame).toHaveAttribute("src", "http://localhost:3000/");
    const sandbox = frame.getAttribute("sandbox") ?? "";
    expect(sandbox).toContain("allow-scripts");
    expect(sandbox).not.toContain("allow-same-origin");
  });

  it("keeps the pages it visited so the user can go back, forward, and reload", async () => {
    renderPanel({
      conversationId: "conv-1",
      open: true,
      onClose: vi.fn(),
      initialTab: "browser",
    });

    const urlInput = screen.getByRole("textbox", { name: "Browser URL" });
    fireEvent.change(urlInput, { target: { value: "http://localhost:3000" } });
    fireEvent.click(screen.getByRole("button", { name: "Go" }));

    const frame = await screen.findByTitle("Browser preview");
    expect(frame).toHaveAttribute("src", "http://localhost:3000/");
    expect(screen.getByRole("button", { name: "Go back" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Go forward" })).toBeDisabled();

    fireEvent.change(urlInput, { target: { value: "http://localhost:4000" } });
    fireEvent.click(screen.getByRole("button", { name: "Go" }));

    await waitFor(() => {
      expect(screen.getByTitle("Browser preview")).toHaveAttribute("src", "http://localhost:4000/");
    });
    expect(screen.getByRole("button", { name: "Go back" })).toBeEnabled();

    fireEvent.click(screen.getByRole("button", { name: "Go back" }));
    await waitFor(() => {
      expect(screen.getByTitle("Browser preview")).toHaveAttribute("src", "http://localhost:3000/");
    });
    expect(screen.getByRole("button", { name: "Go forward" })).toBeEnabled();

    fireEvent.click(screen.getByRole("button", { name: "Go forward" }));
    await waitFor(() => {
      expect(screen.getByTitle("Browser preview")).toHaveAttribute("src", "http://localhost:4000/");
    });

    const before = screen.getByTitle("Browser preview");
    fireEvent.click(screen.getByRole("button", { name: "Reload the page" }));
    await waitFor(() => {
      expect(screen.getByTitle("Browser preview")).not.toBe(before);
    });
    expect(screen.getByTitle("Browser preview")).toHaveAttribute("src", "http://localhost:4000/");
  });

  it("shows the browser's own empty state before a page is opened", () => {
    renderPanel({
      conversationId: "conv-1",
      open: true,
      onClose: vi.fn(),
      initialTab: "browser",
    });

    expect(screen.getByText("Enter an HTTP(S) address to start browsing")).toBeInTheDocument();
  });

  it("maps a screenshot click to page coordinates and drives the page controls", async () => {
    const page = {
      session_id: "session-1",
      url: "http://localhost:3000/",
      title: "Example",
      screenshot_base64: "aGVsbG8=",
      can_go_back: false,
      can_go_forward: false,
      viewport_width: 1000,
      viewport_height: 500,
    };
    fetchWithAuthMock.mockImplementation(async (url: string) => {
      if (url === "/deepspace/chats/conv-1/browser/session") return jsonResponse(page);
      return jsonResponse(page);
    });

    renderPanel({
      conversationId: "conv-1",
      open: true,
      onClose: vi.fn(),
      initialTab: "browser",
    });

    const surface = await screen.findByRole("button", { name: "Page preview" });
    const image = screen.getByRole("img", { name: "The page Example" });
    vi.spyOn(image, "getBoundingClientRect").mockReturnValue({
      left: 0,
      top: 0,
      right: 1000,
      bottom: 400,
      width: 1000,
      height: 400,
      x: 0,
      y: 0,
      toJSON: () => ({}),
    } as DOMRect);

    // The image covers 800 by 400 pixels, centred in the 1000 by 400 element.
    fireEvent.click(surface, { clientX: 300, clientY: 100, detail: 1 });

    await waitFor(() => {
      expect(fetchWithAuthMock).toHaveBeenCalledWith(
        "/deepspace/chats/conv-1/browser/click",
        expect.objectContaining({
          method: "POST",
          body: JSON.stringify({ x: 250, y: 125 }),
        }),
      );
    });

    // A tall element letterboxes the page vertically, so the click is
    // measured from the top of the document area, not the element.
    vi.spyOn(image, "getBoundingClientRect").mockReturnValue({
      left: 0,
      top: 0,
      right: 1000,
      bottom: 600,
      width: 1000,
      height: 600,
      x: 0,
      y: 0,
      toJSON: () => ({}),
    } as DOMRect);
    fireEvent.click(surface, { clientX: 300, clientY: 150, detail: 1 });

    await waitFor(() => {
      expect(fetchWithAuthMock).toHaveBeenCalledWith(
        "/deepspace/chats/conv-1/browser/click",
        expect.objectContaining({
          method: "POST",
          body: JSON.stringify({ x: 300, y: 100 }),
        }),
      );
    });

    const input = screen.getByRole("textbox", { name: "Text to type into the page" });
    fireEvent.change(input, { target: { value: "hello" } });
    fireEvent.click(screen.getByRole("button", { name: "Type" }));

    await waitFor(() => {
      expect(fetchWithAuthMock).toHaveBeenCalledWith(
        "/deepspace/chats/conv-1/browser/type",
        expect.objectContaining({
          method: "POST",
          body: JSON.stringify({ text: "hello" }),
        }),
      );
    });

    fireEvent.click(screen.getByRole("button", { name: "Press Enter" }));

    await waitFor(() => {
      expect(fetchWithAuthMock).toHaveBeenCalledWith(
        "/deepspace/chats/conv-1/browser/key",
        expect.objectContaining({
          method: "POST",
          body: JSON.stringify({ key: "Enter", modifiers: [] }),
        }),
      );
    });

    fireEvent.click(await screen.findByRole("button", { name: "Scroll down" }));

    await waitFor(() => {
      expect(fetchWithAuthMock).toHaveBeenCalledWith(
        "/deepspace/chats/conv-1/browser/scroll",
        expect.objectContaining({
          method: "POST",
          body: JSON.stringify({ delta_x: 0, delta_y: 600 }),
        }),
      );
    });
  });

  it("expands a folder inline and lists the entries it fetched", async () => {
    fetchWithAuthMock.mockImplementation(async (url: string) => {
      if (url === "/deepspace/chats/conv-1/workspace/tree?path=src") {
        return jsonResponse({
          entries: [{ name: "app.ts", path: "src/app.ts", kind: "file", size_bytes: 120 }],
        });
      }
      if (url === "/deepspace/chats/conv-1/workspace/tree") {
        return jsonResponse({
          entries: [
            { name: "src", path: "src", kind: "directory", size_bytes: 0 },
            { name: "README.md", path: "README.md", kind: "file", size_bytes: 42 },
          ],
        });
      }
      return jsonResponse({});
    });

    renderPanel({ conversationId: "conv-1", open: true, onClose: vi.fn() });

    const folder = await screen.findByRole("button", { name: "src" });
    expect(folder).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(folder);

    expect(await screen.findByRole("button", { name: "app.ts" })).toBeInTheDocument();
    expect(folder).toHaveAttribute("aria-expanded", "true");
    expect(
      fetchWithAuthMock.mock.calls.filter(
        ([url]) => url === "/deepspace/chats/conv-1/workspace/tree?path=src",
      ),
    ).toHaveLength(1);
  });

  it("blocks an external host until the user allows it and resets that choice on a new URL", async () => {
    renderPanel({
      conversationId: "conv-1",
      open: true,
      onClose: vi.fn(),
      initialTab: "browser",
    });

    const urlInput = screen.getByRole("textbox", { name: "Browser URL" });
    fireEvent.change(urlInput, { target: { value: "https://example.com" } });
    fireEvent.click(screen.getByRole("button", { name: "Go" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("outside this app");
    expect(screen.queryByTitle("Browser preview")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("checkbox", { name: "Allow external sites" }));
    fireEvent.click(screen.getByRole("button", { name: "Go" }));

    const frame = await screen.findByTitle("Browser preview");
    expect(frame).toHaveAttribute("src", "https://example.com/");

    fireEvent.change(urlInput, { target: { value: "https://other.example" } });
    fireEvent.click(screen.getByRole("button", { name: "Go" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("outside this app");
  });

  it("does not poll a terminal session for output", async () => {
    fetchWithAuthMock.mockImplementation(async (url: string) => {
      if (url === "/deepspace/chats/conv-1/terminals") {
        return jsonResponse({
          sessions: [
            {
              session_id: "session-1",
              shell: "bash",
              pty: true,
              created_at: "2026-01-01T00:00:00.000Z",
              idle_seconds: 0,
            },
          ],
        });
      }
      if (url.startsWith("/deepspace/chats/conv-1/terminals/session-1")) {
        return jsonResponse({
          output: "$ exit",
          truncated: false,
          dropped_bytes: 0,
          alive: false,
        });
      }
      return jsonResponse({});
    });

    renderPanel({
      conversationId: "conv-1",
      open: true,
      onClose: vi.fn(),
      initialTab: "terminal",
    });

    await screen.findByRole("textbox", { name: "Interactive terminal" });

    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 2_000));
    });

    expect(outputReads()).toHaveLength(0);
  });

  it("closes the live terminal stream when the panel closes", async () => {
    const sockets: TestWebSocket[] = [];
    vi.stubGlobal(
      "WebSocket",
      class extends TestWebSocket {
        constructor(url: string) {
          super(url);
          sockets.push(this);
        }
      },
    );
    vi.stubGlobal(
      "ResizeObserver",
      class {
        observe() {}
        disconnect() {}
      },
    );
    const view = renderPanel({
      conversationId: "conv-1",
      open: true,
      onClose: vi.fn(),
      initialTab: "terminal",
    });

    await screen.findByRole("textbox", { name: "Interactive terminal" });

    view.rerender(
      <ModuleRegistryProvider initialModules={BUILTIN_PANEL_MODULES}>
        <DeepSpaceSidePanel
          conversationId="conv-1"
          open={false}
          onClose={vi.fn()}
          initialTab="terminal"
        />
      </ModuleRegistryProvider>,
    );

    await waitFor(() => expect(sockets[0]?.readyState).toBe(3));
  });

  it("marks the active tab and shows the matching panel", async () => {
    renderPanel({ conversationId: "conv-1", open: true, onClose: vi.fn() });

    const filesTab = screen.getByRole("tab", { name: "Files" });
    expect(filesTab).toHaveAttribute("aria-selected", "true");
    const filesTabId = filesTab.getAttribute("id");
    expect(filesTabId).toBeTruthy();
    expect(screen.getByRole("tabpanel")).toHaveAttribute("aria-labelledby", filesTabId);

    fireEvent.click(screen.getByRole("button", { name: "Open a workspace tab" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Browser" }));

    const browserTab = screen.getByRole("tab", { name: "Browser" });
    expect(browserTab).toHaveAttribute("aria-selected", "true");
    const browserTabId = browserTab.getAttribute("id");
    expect(browserTabId).toBeTruthy();
    expect(screen.getByRole("tabpanel")).toHaveAttribute("aria-labelledby", browserTabId);
  });

  it("closes a tab when its close (X) button is clicked", async () => {
    renderPanel({ conversationId: "conv-1", open: true, onClose: vi.fn() });

    fireEvent.click(screen.getByRole("button", { name: "Open a workspace tab" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Browser" }));
    const browserTab = screen.getByRole("tab", { name: "Browser" });
    const closeButton = browserTab
      .closest("div")
      ?.querySelector('button[aria-label="Close Browser tab"]');
    expect(closeButton).toBeInTheDocument();

    fireEvent.click(closeButton!);

    expect(screen.queryByRole("tab", { name: "Browser" })).not.toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Files" })).toHaveAttribute("aria-selected", "true");
  });

  it("activates the last remaining tab when the active tab is closed", async () => {
    renderPanel({ conversationId: "conv-1", open: true, onClose: vi.fn() });

    const filesTab = screen.getByRole("tab", { name: "Files" });
    const filesClose = filesTab
      .closest("div")
      ?.querySelector('button[aria-label="Close Files tab"]');
    expect(filesClose).toBeInTheDocument();

    fireEvent.click(filesClose!);

    expect(screen.queryByRole("tab", { name: "Files" })).not.toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Start" })).toHaveAttribute("aria-selected", "true");
  });

  it("opens the selected workspace surface from the tab picker", async () => {
    renderPanel({ conversationId: "conv-1", open: true, onClose: vi.fn() });

    expect(screen.getAllByRole("tab")).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "Open a workspace tab" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Terminal" }));
    expect(screen.getByRole("tab", { name: "bash" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getAllByRole("tab")).toHaveLength(2);
  });

  it("opens a fresh bash terminal tab each time the plus menu chooses Terminal", async () => {
    renderPanel({ conversationId: "conv-1", open: true, onClose: vi.fn() });

    const picker = screen.getByRole("button", { name: "Open a workspace tab" });
    fireEvent.click(picker);
    fireEvent.click(screen.getByRole("menuitem", { name: "Terminal" }));
    const countWithTerminal = screen.getAllByRole("tab").length;
    fireEvent.click(picker);
    fireEvent.click(screen.getByRole("menuitem", { name: "Terminal" }));
    expect(screen.getAllByRole("tab")).toHaveLength(countWithTerminal + 1);
    expect(screen.getAllByRole("tab", { name: "bash" })).toHaveLength(2);
    expect(screen.getAllByRole("tab", { name: "bash" })[1]).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });

  it("keeps the selected tab content visible after the last tab in a split pane closes", async () => {
    render(
      <ModuleRegistryProvider initialModules={BUILTIN_PANEL_MODULES}>
        <DeepSpaceSidePanel conversationId="conv-1" open onClose={vi.fn()} />
      </ModuleRegistryProvider>,
    );

    fireEvent.click(screen.getByRole("button", { name: "Split active tab" }));
    fireEvent.click(screen.getByRole("button", { name: "Close Start · 2 tab" }));

    expect(screen.getAllByRole("tab", { name: "Start" })).toHaveLength(1);
    expect(screen.getByRole("tab", { name: "Start" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByText("Choose a workspace surface to begin.")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Open a workspace tab" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Browser" }));

    const browserTab = screen.getByRole("tab", { name: "Browser" });
    expect(browserTab).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("textbox", { name: "Browser URL" })).toBeInTheDocument();
    expect(screen.getByText("Enter an HTTP(S) address to start browsing")).toBeInTheDocument();
    expect(screen.getAllByRole("tab", { name: "Start" })).toHaveLength(1);
  });

  it("opens on a clean Start screen by default", () => {
    render(
      <ModuleRegistryProvider initialModules={BUILTIN_PANEL_MODULES}>
        <DeepSpaceSidePanel conversationId="conv-1" open onClose={vi.fn()} />
      </ModuleRegistryProvider>,
    );

    expect(screen.getByRole("tab", { name: "Start" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getAllByRole("tab")).toHaveLength(1);
    expect(screen.getByText("Choose a workspace surface to begin.")).toBeInTheDocument();
  });

  it("starts a new terminal session when opened directly from a start card", async () => {
    renderPanel({
      conversationId: "conv-1",
      open: true,
      onClose: vi.fn(),
      tab: "terminal",
      startTerminal: true,
    });

    await waitFor(() => {
      expect(
        fetchWithAuthMock.mock.calls.some(
          ([url, init]) => url === "/deepspace/chats/conv-1/terminals" && init?.method === "POST",
        ),
      ).toBe(true);
    });
    const newSessionCalls = () =>
      fetchWithAuthMock.mock.calls.filter(
        ([url, init]) => url === "/deepspace/chats/conv-1/terminals" && init?.method === "POST",
      ).length;
    expect(newSessionCalls()).toBe(1);

    fireEvent.click(screen.getByRole("button", { name: "Close Terminal tab" }));
    fireEvent.click(screen.getByRole("button", { name: "Open a workspace tab" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Terminal" }));
    await waitFor(() =>
      expect(screen.getByRole("tab", { name: "bash" })).toHaveAttribute("aria-selected", "true"),
    );
    await waitFor(() => expect(newSessionCalls()).toBe(2));
  });

  it("launches only an application returned by the verified host catalog", async () => {
    renderPanel({
      conversationId: "conv-1",
      workspace: {
        id: "workspace-1",
        slug: "project",
        name: "Project",
        path: "/workspaces/project",
        is_default: true,
        status: "ok",
      },
      open: true,
      onClose: vi.fn(),
    });

    fireEvent.click(await screen.findByRole("button", { name: "README.md" }));
    fireEvent.click(
      await screen.findByRole("button", { name: "Open workspace with an application" }),
    );
    fireEvent.click(await screen.findByRole("menuitem", { name: /Visual Studio Code/ }));

    expect(fetchWithAuthMock).toHaveBeenCalledWith(
      "/host/open-in-app/open",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ app_id: "vscode", path: "/workspaces/project" }),
      }),
    );
    await waitFor(() => {
      expect(screen.queryByRole("menu", { name: "Open workspace with" })).not.toBeInTheDocument();
    });
  });

  it("loads workflow, trajectory, and deliverable projections through the authenticated API", async () => {
    fetchWithAuthMock.mockImplementation(async (url: string) => {
      if (url === "/deepspace/chats/conv-1/workflow-runs?limit=50") {
        return jsonResponse([
          {
            run_id: 12,
            workflow: "release-check",
            status: "completed",
            started_at: "2026-01-01T00:00:00.000Z",
            completed_at: "2026-01-01T00:00:01.000Z",
          },
        ]);
      }
      if (url === "/deepspace/chats/conv-1/trajectory?max_turns=50") {
        return jsonResponse({
          turns: [
            {
              turn: 1,
              duration_ms: 42,
              steps: [
                {
                  step: 1,
                  duration_ms: 40,
                  tools: [{ tool_name: "fs_read", status: "completed", duration_ms: 8 }],
                },
              ],
              records: [
                {
                  id: "rec-fs-read",
                  turn: 1,
                  step: 1,
                  kind: "tool",
                  label: "TOOL",
                  timestamp: "00:00:00.000",
                  started_at_ms: 0,
                  duration_ms: 8,
                  title: "fs_read",
                  content: "Executed fs_read in 8ms",
                  status: "completed",
                },
              ],
            },
          ],
        });
      }
      if (url === "/deepspace/chats/conv-1/deliverables?limit=100") {
        return jsonResponse({
          changed_files: [{ path: "README.md", write_count: 2 }],
          declared_deliverables: [{ path: "docs/release-notes.md" }],
        });
      }
      return jsonResponse({});
    });

    const workflows = render(<WorkflowsTab conversationId="conv-1" />);

    expect(await screen.findByText("release-check")).toBeInTheDocument();
    expect(fetchWithAuthMock).toHaveBeenCalledWith(
      "/deepspace/chats/conv-1/workflow-runs?limit=50",
      { method: "GET" },
    );

    workflows.unmount();
    render(<TrajectoryTab conversationId="conv-1" />);
    expect(await screen.findByText("Turn 1")).toBeInTheDocument();
    expect(screen.getAllByText(/fs_read/).length).toBeGreaterThan(0);

    cleanup();
    render(<DeliverablesTab conversationId="conv-1" />);
    expect(await screen.findByText("README.md")).toBeInTheDocument();
    expect(screen.getByText("release-notes.md")).toBeInTheDocument();
    expect(screen.getByText("docs/release-notes.md")).toBeInTheDocument();
    expect(fetchWithAuthMock).toHaveBeenCalledWith(
      "/deepspace/chats/conv-1/deliverables?limit=100",
      { method: "GET" },
    );
  });

  it("shows live workflow progress and sends a scoped cancellation request", async () => {
    fetchWithAuthMock.mockImplementation(async (url: string, init?: RequestInit) => {
      if (url === "/deepspace/chats/conv-1/workflow-runs?limit=50") {
        return jsonResponse([
          {
            run_id: "job-1",
            workflow: "background-job",
            status: "running",
            current_phase: "research",
            started_at: "2026-01-01T00:00:00.000Z",
            completed_at: null,
            events: [
              { sequence: 1, type: "workflow/phase", payload: { title: "research" } },
              { sequence: 2, type: "workflow/log", payload: { message: "started" } },
            ],
          },
        ]);
      }
      if (url === "/deepspace/chats/conv-1/workflow-runs/job-1/cancel" && init?.method === "POST") {
        return jsonResponse({ run_id: "job-1", status: "cancelling" });
      }
      return jsonResponse([]);
    });

    render(<WorkflowsTab conversationId="conv-1" />);
    expect(await screen.findByText("background-job")).toBeInTheDocument();
    fireEvent.click(screen.getByText("background-job"));
    expect(screen.getByText("research")).toBeInTheDocument();
    expect(screen.getByText("started")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Stop workflow" }));
    await waitFor(() => {
      expect(fetchWithAuthMock).toHaveBeenCalledWith(
        "/deepspace/chats/conv-1/workflow-runs/job-1/cancel",
        { method: "POST" },
      );
    });
  });

  it("keeps trajectory entries renderable when legacy turns share a number", async () => {
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined);
    fetchWithAuthMock.mockImplementation(async (url: string) => {
      if (url === "/deepspace/chats/conv-1/trajectory?max_turns=50") {
        return jsonResponse({
          turns: [
            {
              turn: 0,
              duration_ms: 1,
              records: [
                {
                  id: "legacy-turn-first",
                  turn: 0,
                  step: 1,
                  kind: "tool",
                  label: "TOOL",
                  timestamp: "00:00:00.000",
                  started_at_ms: 0,
                  duration_ms: 1,
                  title: "first legacy event",
                  content: "First legacy event",
                  status: "completed",
                },
              ],
            },
            {
              turn: 0,
              duration_ms: 2,
              records: [
                {
                  id: "legacy-turn-second",
                  turn: 0,
                  step: 1,
                  kind: "tool",
                  label: "TOOL",
                  timestamp: "00:00:00.001",
                  started_at_ms: 1,
                  duration_ms: 1,
                  title: "second legacy event",
                  content: "Second legacy event",
                  status: "completed",
                },
              ],
            },
          ],
        });
      }
      return jsonResponse({});
    });

    render(<TrajectoryTab conversationId="conv-1" />);

    expect(await screen.findAllByText("Turn 0")).toHaveLength(2);
    expect(consoleError.mock.calls.some(([message]) => String(message).includes("same key"))).toBe(
      false,
    );
  });

  it("opens an unsaved virtual file and HTML preview tab when requestedPreview is set", async () => {
    renderPanel({
      conversationId: "conv-1",
      open: true,
      onClose: vi.fn(),
      requestedPreview: {
        seq: 101,
        kind: "html",
        id: "html:preview-1",
        title: "todo_app.html",
        path: "/home/AverQel/Kinesis/todo_app.html",
        language: "html",
        content: "<html><body><h1>TODO</h1></body></html>",
        diffStats: {
          path: "/home/AverQel/Kinesis/todo_app.html",
          additions: 1,
          deletions: 0,
          diffLines: [{ type: "added", text: "<html><body><h1>TODO</h1></body></html>" }],
        },
      },
    });

    const previewRoot = await screen.findByTestId("deepspace-side-panel-preview");
    expect(previewRoot).toHaveTextContent("todo_app.html");
    expect(previewRoot).toHaveTextContent("+1");
    expect(previewRoot.querySelector("iframe")).not.toBeNull();

    fireEvent.click(screen.getByRole("tab", { name: /^Diff$/i }));
    expect(await screen.findByTestId("deepspace-side-panel-diff")).toHaveTextContent("TODO");
  });
});
