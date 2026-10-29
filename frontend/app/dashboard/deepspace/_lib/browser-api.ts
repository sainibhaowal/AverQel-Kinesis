import { fetchWithAuth } from "@/lib/api";

/** What one page of the conversation's browser looks like at one moment. */
export interface BrowserPageState {
  /** The browser session the page belongs to. */
  session_id: string;
  /** The address the browser is showing. */
  url: string;
  /** The title of the page. */
  title: string;
  /** The screenshot of the page, as base64 PNG bytes. */
  screenshot_base64: string;
  /** Whether the browser's history holds an earlier page. */
  can_go_back: boolean;
  /** Whether the browser's history holds a later page. */
  can_go_forward: boolean;
  /** The width of the page viewport the screenshot shows, in pixels. */
  viewport_width: number;
  /** The height of the page viewport the screenshot shows, in pixels. */
  viewport_height: number;
}

/** The viewport size a runtime from before the viewport fields used. */
const DEFAULT_VIEWPORT_WIDTH = 1280;
const DEFAULT_VIEWPORT_HEIGHT = 900;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function asString(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function asPositiveNumber(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) && value > 0 ? value : fallback;
}

/** The endpoint prefix of one conversation's browser routes. */
function chatBase(chatId: string): string {
  return `/deepspace/chats/${encodeURIComponent(chatId)}`;
}

/**
 * Read the message the server sent with a failed response.
 *
 * @param response - The failed response whose body carries the message.
 * @returns The server message, or null when the body carries none.
 */
async function readErrorMessage(response: Response): Promise<string | null> {
  try {
    const payload: unknown = await response.json();
    if (!isRecord(payload)) return null;
    const error = payload.error;
    if (isRecord(error) && typeof error.message === "string" && error.message.trim()) {
      return error.message.trim();
    }
    if (typeof payload.detail === "string" && payload.detail.trim()) return payload.detail.trim();
    if (typeof payload.message === "string" && payload.message.trim())
      return payload.message.trim();
  } catch {
    // A gateway can answer with a non-JSON body; the status fallback is used then.
  }
  return null;
}

/**
 * Send one request and read its JSON body.
 *
 * @param path - The endpoint, relative to the API root.
 * @param init - The request options, when the call is not a plain GET.
 * @returns The parsed response body.
 * @throws Error When the response is not ok; the message is the server's own when it sent one.
 */
async function requestJson(path: string, init?: RequestInit): Promise<unknown> {
  const response = (await fetchWithAuth(path, init)) as Response;
  if (!response.ok) {
    const message = await readErrorMessage(response);
    throw new Error(message ?? `The request failed (HTTP ${response.status}).`);
  }
  return (await response.json()) as unknown;
}

/**
 * Read one page out of a browser response.
 *
 * @param value - The response body.
 * @returns The page the browser is showing.
 * @throws Error When the response carries no session, which means the
 *   deployment has no browser runtime behind the route.
 */
function toBrowserPage(value: unknown): BrowserPageState {
  const record = isRecord(value) ? value : {};
  const sessionId = asString(record.session_id);
  if (!sessionId) {
    throw new Error("The deployment returned no browser session.");
  }
  return {
    session_id: sessionId,
    url: asString(record.url),
    title: asString(record.title),
    screenshot_base64: asString(record.screenshot_base64),
    can_go_back: record.can_go_back === true,
    can_go_forward: record.can_go_forward === true,
    viewport_width: asPositiveNumber(record.viewport_width, DEFAULT_VIEWPORT_WIDTH),
    viewport_height: asPositiveNumber(record.viewport_height, DEFAULT_VIEWPORT_HEIGHT),
  };
}

/**
 * Open the conversation's browser session, or return the one it holds.
 *
 * @param chatId - The conversation that owns the browser session.
 * @returns The page the browser is showing.
 */
export async function openBrowserSession(chatId: string): Promise<BrowserPageState> {
  const payload = await requestJson(`${chatBase(chatId)}/browser/session`, { method: "POST" });
  return toBrowserPage(payload);
}

/**
 * Open an address in the conversation's browser.
 *
 * @param chatId - The conversation that owns the browser session.
 * @param url - The address to open.
 * @returns The page the browser is showing.
 */
export async function navigateBrowser(chatId: string, url: string): Promise<BrowserPageState> {
  const payload = await requestJson(`${chatBase(chatId)}/browser/navigate`, {
    method: "POST",
    body: JSON.stringify({ url }),
  });
  return toBrowserPage(payload);
}

/**
 * Move to the earlier page in the browser's own history.
 *
 * @param chatId - The conversation that owns the browser session.
 * @returns The page the browser is showing.
 */
export async function goBrowserBack(chatId: string): Promise<BrowserPageState> {
  const payload = await requestJson(`${chatBase(chatId)}/browser/back`, { method: "POST" });
  return toBrowserPage(payload);
}

/**
 * Move to the later page in the browser's own history.
 *
 * @param chatId - The conversation that owns the browser session.
 * @returns The page the browser is showing.
 */
export async function goBrowserForward(chatId: string): Promise<BrowserPageState> {
  const payload = await requestJson(`${chatBase(chatId)}/browser/forward`, { method: "POST" });
  return toBrowserPage(payload);
}

/**
 * Load the page the browser is showing again.
 *
 * @param chatId - The conversation that owns the browser session.
 * @returns The page the browser is showing.
 */
export async function reloadBrowser(chatId: string): Promise<BrowserPageState> {
  const payload = await requestJson(`${chatBase(chatId)}/browser/reload`, { method: "POST" });
  return toBrowserPage(payload);
}

/**
 * Click one point of the page the conversation's browser is showing.
 *
 * @param chatId - The conversation that owns the browser session.
 * @param x - The horizontal position in the page viewport, in pixels.
 * @param y - The vertical position in the page viewport, in pixels.
 * @returns The page the browser is showing.
 */
export async function clickBrowser(
  chatId: string,
  x: number,
  y: number,
): Promise<BrowserPageState> {
  const payload = await requestJson(`${chatBase(chatId)}/browser/click`, {
    method: "POST",
    body: JSON.stringify({ x, y }),
  });
  return toBrowserPage(payload);
}

/**
 * Insert text into the focused field of the conversation's browser page.
 *
 * @param chatId - The conversation that owns the browser session.
 * @param text - The text to insert.
 * @returns The page the browser is showing.
 */
export async function typeIntoBrowser(chatId: string, text: string): Promise<BrowserPageState> {
  const payload = await requestJson(`${chatBase(chatId)}/browser/type`, {
    method: "POST",
    body: JSON.stringify({ text }),
  });
  return toBrowserPage(payload);
}

/**
 * Press one key in the conversation's browser page.
 *
 * @param chatId - The conversation that owns the browser session.
 * @param key - The key to press.
 * @param modifiers - The modifier keys held while the key is pressed.
 * @returns The page the browser is showing.
 */
export async function pressBrowserKey(
  chatId: string,
  key: string,
  modifiers: string[] = [],
): Promise<BrowserPageState> {
  const payload = await requestJson(`${chatBase(chatId)}/browser/key`, {
    method: "POST",
    body: JSON.stringify({ key, modifiers }),
  });
  return toBrowserPage(payload);
}

/**
 * Scroll the conversation's browser page.
 *
 * @param chatId - The conversation that owns the browser session.
 * @param deltaX - The horizontal distance to scroll, in pixels.
 * @param deltaY - The vertical distance to scroll, in pixels.
 * @returns The page the browser is showing.
 */
export async function scrollBrowser(
  chatId: string,
  deltaX: number,
  deltaY: number,
): Promise<BrowserPageState> {
  const payload = await requestJson(`${chatBase(chatId)}/browser/scroll`, {
    method: "POST",
    body: JSON.stringify({ delta_x: deltaX, delta_y: deltaY }),
  });
  return toBrowserPage(payload);
}

/**
 * Read the screenshot of the page the browser is showing.
 *
 * @param chatId - The conversation that owns the browser session.
 * @returns The page the browser is showing.
 */
export async function readBrowserScreenshot(chatId: string): Promise<BrowserPageState> {
  const payload = await requestJson(`${chatBase(chatId)}/browser/screenshot`);
  return toBrowserPage(payload);
}

/**
 * Close the conversation's browser session.
 *
 * @param chatId - The conversation that owns the browser session.
 * @returns Whether a session was closed.
 */
export async function closeBrowserSession(chatId: string): Promise<boolean> {
  const payload = await requestJson(`${chatBase(chatId)}/browser/session`, { method: "DELETE" });
  return isRecord(payload) && payload.closed === true;
}
