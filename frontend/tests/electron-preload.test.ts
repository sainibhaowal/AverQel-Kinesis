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
        ipcRenderer: { invoke },
      };
    },
    setTimeout,
    URL,
    URLSearchParams,
    window: dom.window,
  });
  dom.window.dispatchEvent(new dom.window.Event("DOMContentLoaded"));

  return { dom, exposed, invoke };
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
    expect(titlebar.querySelector('[role="img"]')).toBeNull();
    expect(
      dom.window.document.documentElement.style.getPropertyValue("--averqel-desktop-chrome-height"),
    ).toBe("40px");
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

});
