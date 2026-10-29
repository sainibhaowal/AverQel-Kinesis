import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import DeepSpaceSidePanel from "../app/dashboard/deepspace/_components/DeepSpaceSidePanel";

const fetchWithAuthMock = vi.fn();

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
  });

  it("shows the screenshot the browser sent", async () => {
    render(
      <DeepSpaceSidePanel conversationId="conv-1" open onClose={vi.fn()} initialTab="browser" />,
    );

    const screenshot = await screen.findByAltText("The page Local page");
    expect(screenshot).toHaveAttribute(
      "src",
      "data:image/png;base64,Zmlyc3Qtc2NyZWVuc2hvdA==",
    );
    expect(screen.getByText("http://localhost:3000/")).toBeInTheDocument();
    expect(screen.queryByTitle("Browser preview")).not.toBeInTheDocument();
  });

  it("opens an address in the browser and shows the page it returns", async () => {
    render(
      <DeepSpaceSidePanel conversationId="conv-1" open onClose={vi.fn()} initialTab="browser" />,
    );
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
    render(
      <DeepSpaceSidePanel conversationId="conv-1" open onClose={vi.fn()} initialTab="browser" />,
    );
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
    render(
      <DeepSpaceSidePanel conversationId="conv-1" open onClose={vi.fn()} initialTab="browser" />,
    );
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

    render(
      <DeepSpaceSidePanel conversationId="conv-1" open onClose={vi.fn()} initialTab="browser" />,
    );
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

    render(
      <DeepSpaceSidePanel conversationId="conv-1" open onClose={vi.fn()} initialTab="browser" />,
    );

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
