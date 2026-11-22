import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ComponentProps } from "react";

import DeepSpaceSidePanel from "../app/dashboard/deepspace/_components/DeepSpaceSidePanel";
import { ModuleRegistryProvider } from "../app/dashboard/deepspace/_lib/module-registry";
import { BUILTIN_PANEL_MODULES } from "../app/dashboard/deepspace/_lib/builtin-modules";

const fetchWithAuthMock = vi.fn();

function renderPanel(props: ComponentProps<typeof DeepSpaceSidePanel>) {
  return render(
    <ModuleRegistryProvider initialModules={BUILTIN_PANEL_MODULES}>
      <DeepSpaceSidePanel {...props} />
    </ModuleRegistryProvider>,
  );
}

vi.mock("@/lib/api", () => ({
  fetchWithAuth: (...args: unknown[]) => fetchWithAuthMock(...args),
}));

function jsonResponse(payload: unknown) {
  return { ok: true, status: 200, json: async () => payload };
}

function failedResponse(message: string, status = 409) {
  return {
    ok: false,
    status,
    json: async () => ({ error: { code: "CONFLICT", message } }),
  };
}

/** The browser answer the mocked runtime sends for one page. */
function browserPage(overrides: Record<string, unknown> = {}) {
  return {
    session_id: "session-1",
    url: "http://localhost:3000/",
    title: "Local page",
    screenshot_base64: "Zmlyc3Qtc2NyZWVuc2hvdA==",
    can_go_back: true,
    can_go_forward: false,
    ...overrides,
  };
}

/** The calls the panel sent to one browser route. */
function routeCalls(route: string): unknown[][] {
  return fetchWithAuthMock.mock.calls.filter(([url]) =>
    String(url).includes(`/browser/${route}`),
  );
}

describe("DeepSpaceSidePanel browser tab", () => {
  beforeEach(() => {
    fetchWithAuthMock.mockReset();
    fetchWithAuthMock.mockImplementation(async (url: string, init?: RequestInit) => {
      if (url === "/deepspace/chats/conv-1/browser/session" && init?.method === "POST") {
        return jsonResponse(browserPage());
      }
      if (url === "/deepspace/chats/conv-1/browser/navigate") {
        return jsonResponse(
          browserPage({
            url: "http://localhost:3000/docs",
            title: "Docs",
            screenshot_base64: "bmF2aWdhdGVkLXNjcmVlbnNob3Q=",
            can_go_back: true,
          }),
        );
      }
      if (url === "/deepspace/chats/conv-1/browser/back") {
        return jsonResponse(
          browserPage({
            url: "http://localhost:3000/first",
            title: "First page",
            screenshot_base64: "YmFja3Mhc3Q=",
            can_go_forward: true,
          }),
        );
      }
      if (url === "/deepspace/chats/conv-1/browser/forward") {
        return jsonResponse(
          browserPage({
            url: "http://localhost:3000/second",
            title: "Second page",
            screenshot_base64: "Zm9yd2FyZHNob3Q=",
            can_go_back: true,
          }),
        );
      }
      if (url === "/deepspace/chats/conv-1/browser/reload") {
        return jsonResponse(
          browserPage({
            url: "http://localhost:3000/",
            title: "Local page",
            screenshot_base64: "cmVsb2FkZWQtc2NyZWVuc2hvdA==",
          }),
        );
      }
      return jsonResponse({});
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    delete window.electron;
  });

  it("uses the live Electron browser view instead of the screenshot controls", async () => {
    let onState: ((state: ElectronBrowserViewState) => void) | undefined;
    const initialState: ElectronBrowserViewState = {
      viewId: "unused",
      url: "about:blank",
      title: "",
      loading: false,
      canGoBack: false,
      canGoForward: false,
      error: null,
    };
    const browserView = {
      show: vi.fn(async (viewId: string) => ({ ...initialState, viewId })),
      hide: vi.fn(async () => undefined),
      close: vi.fn(async () => undefined),
      navigate: vi.fn(async (viewId: string, url: string) => ({ ...initialState, viewId, url })),
      back: vi.fn(async (viewId: string) => ({ ...initialState, viewId })),
      forward: vi.fn(async (viewId: string) => ({ ...initialState, viewId })),
      reload: vi.fn(async (viewId: string) => ({ ...initialState, viewId })),
      onState: vi.fn((callback: (state: ElectronBrowserViewState) => void) => {
        onState = callback;
        return vi.fn();
      }),
    };
    window.electron = {
      isElectron: true,
      window: {
        minimize: vi.fn(),
        close: vi.fn(),
        toggleMaximize: vi.fn(),
        isMaximized: vi.fn(),
      },
      openExternal: vi.fn(),
      browserView,
    };
    const originalBounds = HTMLElement.prototype.getBoundingClientRect;
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      if (this.getAttribute("aria-label") === "Web page") {
        return { x: 10, y: 20, left: 10, top: 20, right: 410, bottom: 720, width: 400, height: 700, toJSON: () => ({}) } as DOMRect;
      }
      return originalBounds.call(this);
    });
    vi.stubGlobal("requestAnimationFrame", vi.fn(() => 1));
    vi.stubGlobal("cancelAnimationFrame", vi.fn());
    vi.stubGlobal("ResizeObserver", class {
      constructor(private readonly callback: ResizeObserverCallback) {}
      observe() { this.callback([], this as unknown as ResizeObserver); }
      disconnect() {}
    });

    let view!: ReturnType<typeof renderPanel>;
    await act(async () => {
      view = renderPanel({
        conversationId: "conv-1",
        open: true,
        onClose: vi.fn(),
        initialTab: "browser",
      });
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    const address = await screen.findByRole("textbox", { name: "Browser URL" });
    expect(await screen.findByText("Search or enter a website address")).toBeInTheDocument();
    expect(browserView.show).toHaveBeenCalledWith("side-panel-tab-1", { x: 10, y: 20, width: 400, height: 700 });
    expect(screen.queryByText("Quick connect")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Page preview" })).not.toBeInTheDocument();

    fireEvent.change(address, { target: { value: "google.com" } });
    fireEvent.click(screen.getByRole("button", { name: "Go" }));
    expect(browserView.navigate).toHaveBeenCalledWith("side-panel-tab-1", "https://google.com/");

    act(() => {
      onState?.({ ...initialState, viewId: "side-panel-tab-1", url: "https://google.com/", title: "Google" });
    });
    expect(await screen.findByRole("button", { name: "Reload page" })).toBeEnabled();
    act(() => view.unmount());
    expect(browserView.hide).toHaveBeenCalledWith("side-panel-tab-1");
  });

  it("shows the screenshot the browser sent", async () => {
    renderPanel({
      conversationId: "conv-1",
      open: true,
      onClose: vi.fn(),
      initialTab: "browser",
    });

    const screenshot = await screen.findByAltText("The page Local page");
    expect(screenshot).toHaveAttribute(
      "src",
      "data:image/png;base64,Zmlyc3Qtc2NyZWVuc2hvdA==",
    );
    expect(screen.getByText("http://localhost:3000/")).toBeInTheDocument();
    expect(screen.queryByTitle("Browser preview")).not.toBeInTheDocument();
  });

  it("opens an address in the browser and shows the page it returns", async () => {
    renderPanel({
      conversationId: "conv-1",
      open: true,
      onClose: vi.fn(),
      initialTab: "browser",
    });
    await screen.findByAltText("The page Local page");

    const urlInput = screen.getByRole("textbox", { name: "Browser URL" });
    fireEvent.change(urlInput, { target: { value: "http://localhost:3000/docs" } });
    fireEvent.click(screen.getByRole("button", { name: "Go" }));

    expect(await screen.findByAltText("The page Docs")).toHaveAttribute(
      "src",
      "data:image/png;base64,bmF2aWdhdGVkLXNjcmVlbnNob3Q=",
    );
    expect(routeCalls("navigate")).toHaveLength(1);
    expect(routeCalls("navigate")[0][1]).toEqual(
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ url: "http://localhost:3000/docs" }),
      }),
    );
  });

  it("drives the browser's own history with the back, forward, and reload buttons", async () => {
    renderPanel({
      conversationId: "conv-1",
      open: true,
      onClose: vi.fn(),
      initialTab: "browser",
    });
    await screen.findByAltText("The page Local page");

    fireEvent.click(screen.getByRole("button", { name: "Go back" }));
    expect(await screen.findByAltText("The page First page")).toBeInTheDocument();
    expect(routeCalls("back")).toHaveLength(1);

    fireEvent.click(screen.getByRole("button", { name: "Go forward" }));
    expect(await screen.findByAltText("The page Second page")).toBeInTheDocument();
    expect(routeCalls("forward")).toHaveLength(1);

    fireEvent.click(screen.getByRole("button", { name: "Reload the page" }));
    expect(await screen.findByAltText("The page Local page")).toBeInTheDocument();
    expect(routeCalls("reload")).toHaveLength(1);
  });

  it("refuses an address that is not http before the browser is asked", async () => {
    renderPanel({
      conversationId: "conv-1",
      open: true,
      onClose: vi.fn(),
      initialTab: "browser",
    });
    await screen.findByAltText("The page Local page");

    const urlInput = screen.getByRole("textbox", { name: "Browser URL" });
    fireEvent.change(urlInput, { target: { value: "javascript:alert(1)" } });
    fireEvent.click(screen.getByRole("button", { name: "Go" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Only http:// and https:// URLs can be opened.",
    );
    expect(routeCalls("navigate")).toHaveLength(0);
  });

  it("opens a new session when the idle sweep closed the one it had", async () => {
    let sessions = 0;
    fetchWithAuthMock.mockImplementation(async (url: string, init?: RequestInit) => {
      if (url === "/deepspace/chats/conv-1/browser/session" && init?.method === "POST") {
        sessions += 1;
        return jsonResponse(browserPage());
      }
      if (url === "/deepspace/chats/conv-1/browser/navigate") {
        if (sessions < 2) {
          return failedResponse("No such browser session.", 404);
        }
        return jsonResponse(
          browserPage({
            url: "http://localhost:3000/docs",
            title: "Docs",
            screenshot_base64: "bmF2aWdhdGVkLXNjcmVlbnNob3Q=",
          }),
        );
      }
      return jsonResponse({});
    });

    renderPanel({
      conversationId: "conv-1",
      open: true,
      onClose: vi.fn(),
      initialTab: "browser",
    });
    await screen.findByAltText("The page Local page");

    const urlInput = screen.getByRole("textbox", { name: "Browser URL" });
    fireEvent.change(urlInput, { target: { value: "http://localhost:3000/docs" } });
    fireEvent.click(screen.getByRole("button", { name: "Go" }));

    expect(await screen.findByAltText("The page Docs")).toBeInTheDocument();
    expect(routeCalls("navigate")).toHaveLength(2);
    expect(routeCalls("session")).toHaveLength(2);
  });

  it("keeps the embedded view and says why when the deployment has no browser runtime", async () => {
    fetchWithAuthMock.mockImplementation(async (url: string, init?: RequestInit) => {
      if (url === "/deepspace/chats/conv-1/browser/session" && init?.method === "POST") {
        return failedResponse("This deployment has no browser runtime.");
      }
      return jsonResponse({});
    });

    renderPanel({
      conversationId: "conv-1",
      open: true,
      onClose: vi.fn(),
      initialTab: "browser",
    });

    expect(await screen.findByRole("status")).toHaveTextContent(
      "This deployment has no browser runtime.",
    );
    expect(
      screen.getByText("Enter an HTTP(S) address to start browsing"),
    ).toBeInTheDocument();

    const urlInput = screen.getByRole("textbox", { name: "Browser URL" });
    fireEvent.change(urlInput, { target: { value: "http://localhost:3000" } });
    fireEvent.click(screen.getByRole("button", { name: "Go" }));

    const frame = await screen.findByTitle("Browser preview");
    expect(frame).toHaveAttribute("src", "http://localhost:3000/");
    expect(routeCalls("navigate")).toHaveLength(0);
  });
});
