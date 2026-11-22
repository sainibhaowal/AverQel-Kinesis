import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { runInNewContext } from "node:vm";
import { JSDOM } from "jsdom";
import { describe, expect, it, vi } from "vitest";

const repositoryRoot = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const preloadSource = readFileSync(
  resolve(repositoryRoot, "applications/desktop/electron/preload.cjs"),
  "utf-8",
);

function bootPreload(url: string, devToolsEnabled = false) {
  const dom = new JSDOM(
    "<!doctype html><html><head></head><body><div id='app'></div></body></html>",
    {
      url,
    },
  );
  dom.window.scrollTo = vi.fn();
  const exposed: Record<string, unknown> = {};
  const invoke = vi.fn(async (channel: string) => {
    if (channel === "window:is-maximized") return false;
    if (channel === "window:devtools-enabled") return devToolsEnabled;
    return { ok: true };
  });
  const listeners = new Map<string, (event: unknown, payload: unknown) => void>();
  const on = vi.fn((channel: string, listener: (event: unknown, payload: unknown) => void) => {
    listeners.set(channel, listener);
  });
  const removeListener = vi.fn((channel: string) => {
    listeners.delete(channel);
  });
  const send = vi.fn();
  runInNewContext(preloadSource, {
    clearTimeout,
    document: dom.window.document,
    history: dom.window.history,
    location: dom.window.location,
    MutationObserver: dom.window.MutationObserver,
    process: { platform: "linux" },
    queueMicrotask,
    require: (name: string) => {
      if (name !== "electron") throw new Error(`Unexpected preload import: ${name}`);
      return {
        contextBridge: {
          exposeInMainWorld: (key: string, value: unknown) => {
            exposed[key] = value;
          },
        },
        ipcRenderer: { invoke, on, removeListener, send },
      };
    },
    setTimeout,
    URL,
    URLSearchParams,
    window: dom.window,
  });
  dom.window.dispatchEvent(new dom.window.Event("DOMContentLoaded"));

  return { dom, exposed, invoke, listeners, on, removeListener, send };
}

async function waitForTitlebar(dom: JSDOM): Promise<HTMLElement> {
  for (let attempt = 0; attempt < 10; attempt += 1) {
    const titlebar = dom.window.document.querySelector<HTMLElement>("#averqel-electron-titlebar");
    if (titlebar) return titlebar;
    await new Promise((resolvePromise) => setTimeout(resolvePromise, 0));
  }
  throw new Error("Electron window controls were not mounted.");
}

describe("frameless Electron shell", () => {
  it("shows only minimize, maximize, and close controls in the top strip", async () => {
    const { dom, invoke } = bootPreload("https://averqel.com/dashboard");
    const titlebar = await waitForTitlebar(dom);
    const buttons = Array.from(titlebar.querySelectorAll<HTMLButtonElement>("button")).filter(
      (button) => button.style.display !== "none",
    );

    expect(buttons.map((button) => button.getAttribute("aria-label"))).toEqual([
      "Minimize window",
      "Maximize window",
      "Close window",
    ]);
    const brand = titlebar.querySelector<HTMLElement>('[data-titlebar-brand]');
    expect(brand).toHaveAttribute("role", "img");
    expect(brand).toHaveAttribute("aria-label", "AverQel Kinesis");
    expect(brand).toHaveTextContent("AverQel Kinesis");
    expect(brand?.querySelector("img")).toHaveAttribute("src", "/brand/AntiAtom_Icon.svg");
    expect(brand?.querySelector("img")).toHaveAttribute("alt", "");
    expect(brand?.querySelector("img")).toHaveAttribute("width", "48");
    expect(brand?.querySelector("img")).toHaveAttribute("height", "48");
    expect(
      dom.window.document.documentElement.style.getPropertyValue("--averqel-desktop-chrome-height"),
    ).toBe("56px");
    expect(invoke).toHaveBeenCalledWith("window:is-maximized");
    buttons[0]?.click();
    buttons[1]?.click();
    buttons[2]?.click();
    await vi.waitFor(() => {
      expect(invoke).toHaveBeenCalledWith("window:minimize");
      expect(invoke).toHaveBeenCalledWith("window:toggle-maximize");
      expect(invoke).toHaveBeenCalledWith("window:close");
    });
    dom.window.close();
  });

  it("does not remount shell chrome for descendant application updates", async () => {
    const { dom } = bootPreload("https://averqel.com/dashboard");
    await waitForTitlebar(dom);

    const appRoot = dom.window.document.querySelector("#app");
    const child = dom.window.document.createElement("div");
    appRoot?.append(child);
    child.append(dom.window.document.createElement("span"));
    await new Promise((resolvePromise) => setTimeout(resolvePromise, 0));

    expect(dom.window.document.querySelectorAll("#averqel-electron-titlebar")).toHaveLength(1);
    expect(dom.window.document.querySelectorAll("#averqel-electron-titlebar-spacer")).toHaveLength(
      1,
    );
    dom.window.close();
  });

  it("shows the developer tools control in the development shell", async () => {
    const { dom } = bootPreload("http://127.0.0.1:11030/dashboard", true);
    const titlebar = await waitForTitlebar(dom);
    const devtools = titlebar.querySelector<HTMLButtonElement>(
      '[aria-label="Toggle Developer Tools (Console)"]',
    );

    await vi.waitFor(() => {
      expect(devtools).toHaveStyle({ display: "inline-flex" });
    });
    dom.window.close();
  });

  it("forwards Ask DeepSeek selections and unsubscribes cleanly", () => {
    const { dom, exposed, listeners, removeListener, send } = bootPreload(
      "http://127.0.0.1:11030/dashboard",
    );
    const electron = exposed.electron as {
      contextMenu: {
        setAskDeepSeekAvailable: (available: boolean) => void;
        onAskDeepSeek: (callback: (text: string) => void) => () => void;
      };
    };
    const callback = vi.fn();

    electron.contextMenu.setAskDeepSeekAvailable(true);
    expect(send).toHaveBeenCalledWith("deepspace:ask-selection-available", true);
    electron.contextMenu.setAskDeepSeekAvailable(false);
    expect(send).toHaveBeenCalledWith("deepspace:ask-selection-available", false);

    const unsubscribe = electron.contextMenu.onAskDeepSeek(callback);
    expect(listeners.has("deepspace:ask-selection")).toBe(true);

    listeners.get("deepspace:ask-selection")?.({}, { text: "  selected text  " });
    expect(callback).toHaveBeenCalledWith("selected text");

    callback.mockClear();
    listeners.get("deepspace:ask-selection")?.({}, { text: "   " });
    listeners.get("deepspace:ask-selection")?.({}, { text: 42 });
    expect(callback).not.toHaveBeenCalled();

    unsubscribe();
    expect(removeListener).toHaveBeenCalledWith(
      "deepspace:ask-selection",
      expect.any(Function),
    );
    dom.window.close();
  });

});
