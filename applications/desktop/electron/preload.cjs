const { contextBridge, ipcRenderer } = require("electron");
let shellObserver;
let titlebarThemeObserver;

const TITLEBAR_HEIGHT = 40;

function titlebarPalette() {
  const dark = document.documentElement?.classList.contains("dark") === true;
  return dark
    ? {
        bar: "#070b0d",
        border: "rgba(148,163,184,.14)",
        brand: "#c7d9d9",
        button: "rgba(255,255,255,.035)",
        buttonBorder: "rgba(148,163,184,.2)",
        buttonText: "#dce7e8",
        buttonHover: "rgba(255,255,255,.12)",
        buttonHoverBorder: "rgba(148,163,184,.38)",
      }
    : {
        bar: "#f6fbfa",
        border: "rgba(15,118,110,.18)",
        brand: "#164e4a",
        button: "rgba(15,118,110,.045)",
        buttonBorder: "rgba(15,118,110,.2)",
        buttonText: "#155e59",
        buttonHover: "rgba(15,118,110,.12)",
        buttonHoverBorder: "rgba(15,118,110,.42)",
      };
}

function applyTitlebarTheme() {
  const bar = document.getElementById("averqel-electron-titlebar");
  if (!bar) return;

  const palette = titlebarPalette();
  bar.style.background = palette.bar;
  bar.style.borderBottomColor = palette.border;

  const brand = bar.querySelector("[data-titlebar-brand]");
  if (brand) brand.style.color = palette.brand;

  bar.querySelectorAll("button").forEach((button) => {
    button.style.background = palette.button;
    button.style.borderColor = palette.buttonBorder;
    button.style.color = palette.buttonText;
  });
}

function observeTitlebarTheme() {
  if (titlebarThemeObserver || !document.documentElement) return;
  titlebarThemeObserver = new MutationObserver(applyTitlebarTheme);
  titlebarThemeObserver.observe(document.documentElement, {
    attributes: true,
    attributeFilter: ["class"],
  });
  applyTitlebarTheme();
}

function createWindowControl({ label, glyph, channel, className = "" }) {
  const button = document.createElement("button");
  button.type = "button";
  button.setAttribute("aria-label", label);
  button.dataset.windowControl = channel;
  button.textContent = glyph;
  button.style.cssText = [
    "display:inline-flex",
    "width:30px",
    "height:28px",
    "align-items:center",
    "justify-content:center",
    "border:1px solid rgba(148,163,184,.18)",
    "border-radius:9px",
    "background:transparent",
    "color:inherit",
    "font:500 17px/1 system-ui,sans-serif",
    "cursor:pointer",
    "pointer-events:auto",
    "-webkit-app-region:no-drag",
    "app-region:no-drag",
    "transition:background .15s,border-color .15s,color .15s",
    className,
  ].join(";");
  button.addEventListener("mouseenter", () => {
    const palette = titlebarPalette();
    button.style.background =
      channel === "window:close"
        ? "rgba(239,68,68,.85)"
        : palette.buttonHover;
    button.style.borderColor =
      channel === "window:close"
        ? "rgba(248,113,113,.8)"
        : palette.buttonHoverBorder;
    if (channel === "window:close") button.style.color = "#fff";
  });
  button.addEventListener("mouseleave", () => {
    const palette = titlebarPalette();
    button.style.background = palette.button;
    button.style.borderColor = palette.buttonBorder;
    button.style.color = palette.buttonText;
  });
  return button;
}

function createTitlebar() {
  const bar = document.createElement("div");
  bar.id = "averqel-electron-titlebar";
  bar.setAttribute("role", "toolbar");
  bar.setAttribute("aria-label", "Window controls");
  bar.style.cssText = [
    "position:fixed",
    "top:0",
    "left:0",
    "right:0",
    `height:${TITLEBAR_HEIGHT}px`,
    "z-index:2147483647",
    "display:flex",
    "align-items:center",
    "justify-content:space-between",
    "box-sizing:border-box",
    "overflow:hidden",
    "padding:0 10px",
    "border-bottom:1px solid rgba(148,163,184,.12)",
    "background:#070b0d",
    "user-select:none",
    "pointer-events:auto",
    "-webkit-app-region:drag",
    "app-region:drag",
  ].join(";");

  const brand = document.createElement("div");
  brand.dataset.titlebarBrand = "true";
  brand.textContent = "AverQel Kinesis";
  brand.style.cssText = [
    "min-width:0",
    "overflow:hidden",
    "text-overflow:ellipsis",
    "white-space:nowrap",
    "padding-left:6px",
    "font:600 11px/1 system-ui,sans-serif",
    "letter-spacing:.08em",
    "text-transform:uppercase",
    "pointer-events:none",
  ].join(";");

  const controls = document.createElement("div");
  controls.setAttribute("role", "group");
  controls.setAttribute(
    "aria-label",
    "Minimize, maximize, and close the window",
  );
  controls.style.cssText =
    "display:flex;align-items:center;gap:6px;pointer-events:auto;-webkit-app-region:no-drag;app-region:no-drag";

  const devtools = createWindowControl({
    label: "Toggle Developer Tools (Console)",
    glyph: "</>",
    channel: "window:toggle-devtools",
  });
  devtools.style.fontSize = "12px";
  devtools.style.fontFamily = "monospace";
  const minimize = createWindowControl({
    label: "Minimize window",
    glyph: "−",
    channel: "window:minimize",
  });
  const maximize = createWindowControl({
    label: "Maximize window",
    glyph: "□",
    channel: "window:toggle-maximize",
  });
  const close = createWindowControl({
    label: "Close window",
    glyph: "×",
    channel: "window:close",
  });

  const setMaximized = (value) => {
    const maximized = value === true;
    maximize.textContent = maximized ? "▣" : "□";
    maximize.setAttribute(
      "aria-label",
      maximized ? "Restore window" : "Maximize window",
    );
  };
  const toggleMaximize = async () => {
    try {
      setMaximized(await ipcRenderer.invoke("window:toggle-maximize"));
    } catch {
      // Keep the current control label if Electron cannot update the window.
    }
  };

  devtools.style.display = "none";
  devtools.addEventListener(
    "click",
    () =>
      void ipcRenderer.invoke("window:toggle-devtools").catch(() => undefined),
  );
  void ipcRenderer
    .invoke("window:devtools-enabled")
    .then((enabled) => {
      if (enabled) devtools.style.display = "inline-flex";
    })
    .catch(() => undefined);

  minimize.addEventListener(
    "click",
    () => void ipcRenderer.invoke("window:minimize").catch(() => undefined),
  );
  maximize.addEventListener("click", () => void toggleMaximize());
  close.addEventListener(
    "click",
    () => void ipcRenderer.invoke("window:close").catch(() => undefined),
  );
  void ipcRenderer
    .invoke("window:is-maximized")
    .then(setMaximized)
    .catch(() => undefined);

  controls.append(devtools, minimize, maximize, close);
  bar.append(brand, controls);
  bar.addEventListener("dblclick", (event) => {
    if (!event.target.closest("button")) void toggleMaximize();
  });
  return bar;
}

function installShellLayout() {
  const spacer = document.getElementById("averqel-electron-titlebar-spacer");
  if (!spacer) return;

  let style = document.getElementById("averqel-electron-shell-style");
  if (!style) {
    style = document.createElement("style");
    style.id = "averqel-electron-shell-style";
    style.textContent = `
      html, body {
        height: 100svh !important;
        max-height: 100svh !important;
        overflow: hidden !important;
        overscroll-behavior: none !important;
        margin: 0 !important;
        padding: 0 !important;
      }
      body {
        display: flex !important;
        flex-direction: column !important;
      }
      #averqel-electron-titlebar-spacer {
        display: block !important;
        flex: 0 0 ${TITLEBAR_HEIGHT}px !important;
        width: 100% !important;
        height: ${TITLEBAR_HEIGHT}px !important;
        min-height: ${TITLEBAR_HEIGHT}px !important;
        max-height: ${TITLEBAR_HEIGHT}px !important;
        pointer-events: none !important;
      }
      body > #averqel-electron-titlebar-spacer ~ div[data-averqel-app-root],
      body > #averqel-electron-titlebar-spacer + div[data-averqel-app-root] {
        height: calc(100svh - ${TITLEBAR_HEIGHT}px) !important;
        min-height: 0 !important;
        max-height: calc(100svh - ${TITLEBAR_HEIGHT}px) !important;
        flex: 1 1 auto !important;
        overflow: hidden !important;
      }
      .app-shell-grid {
        height: 100% !important;
        max-height: 100% !important;
        overflow: hidden !important;
      }
      .app-shell-grid > .dashboard-sidebar-shell {
        height: calc(100% - 2rem) !important;
      }
      .app-shell-grid > div.relative.my-2 {
        height: calc(100% - 1rem) !important;
      }
      @media (min-width: 640px) {
        .app-shell-grid > div.relative.my-2 {
          height: calc(100% - 2rem) !important;
        }
      }
    `;
    (document.head || document.documentElement).append(style);
  }

  try {
    window.scrollTo(0, 0);
  } catch {
    // jsdom test environment does not implement window.scrollTo
  }
  document.documentElement.scrollTop = 0;
  document.body.scrollTop = 0;

  document.documentElement.style.setProperty(
    "--averqel-desktop-chrome-height",
    `${TITLEBAR_HEIGHT}px`,
  );
  document.documentElement.style.setProperty(
    "--averqel-desktop-content-offset",
    `${TITLEBAR_HEIGHT}px`,
  );
}

function installShellChrome() {
  if (!document.body) return false;
  if (document.title !== "AverQel Kinesis") document.title = "AverQel Kinesis";

  const root =
    Array.from(document.body.children).find(
      (child) => child.tagName === "DIV" && child.hasAttribute("data-averqel-app-root"),
    ) ||
    Array.from(document.body.children).find(
      (child) =>
        child.tagName === "DIV" &&
        child.id !== "averqel-electron-titlebar" &&
        child.id !== "averqel-electron-titlebar-spacer",
    );
  if (!root) return false;

  let spacer = document.getElementById("averqel-electron-titlebar-spacer");
  if (!spacer) {
    spacer = document.createElement("div");
    spacer.id = "averqel-electron-titlebar-spacer";
    spacer.setAttribute("aria-hidden", "true");
    spacer.style.cssText = `display:block;flex:none;width:100%;height:${TITLEBAR_HEIGHT}px;pointer-events:none`;
  }
  if (spacer.parentElement !== document.body)
    document.body.insertBefore(spacer, root);

  if (spacer.nextElementSibling !== root)
    document.body.insertBefore(root, spacer.nextSibling);

  installShellLayout();
  if (!document.getElementById("averqel-electron-titlebar")) {
    document.body.append(createTitlebar());
  }
  observeTitlebarTheme();
  return true;
}

function observeShellNavigation() {
  installShellChrome();
  if (!document.body || shellObserver) return;
  shellObserver = new MutationObserver(() => {
    installShellChrome();
  });
  shellObserver.observe(document.body, {
    childList: true,
  });
}

if (document.readyState === "loading") {
  window.addEventListener("DOMContentLoaded", observeShellNavigation, {
    once: true,
  });
} else {
  observeShellNavigation();
}

contextBridge.exposeInMainWorld("electron", {
  isElectron: true,
  window: {
    minimize: () => ipcRenderer.invoke("window:minimize"),
    close: () => ipcRenderer.invoke("window:close"),
    toggleMaximize: () => ipcRenderer.invoke("window:toggle-maximize"),
    isMaximized: () => ipcRenderer.invoke("window:is-maximized"),
    isDevToolsEnabled: () => ipcRenderer.invoke("window:devtools-enabled"),
    unlockDevTools: () => ipcRenderer.invoke("window:unlock-devtools"),
    toggleDevTools: () => ipcRenderer.invoke("window:toggle-devtools"),
  },
  openExternal: (url) => ipcRenderer.invoke("open-external", url),
  codex: {
    status: () => ipcRenderer.invoke("codex:status"),
    connect: (accountId, source) =>
      ipcRenderer.invoke("codex:connect", accountId, source),
    models: (accountId) => ipcRenderer.invoke("codex:models", accountId),
    disconnect: (accountId) =>
      ipcRenderer.invoke("codex:disconnect", accountId),
    startStream: (input) => ipcRenderer.invoke("codex:stream-start", input),
    cancelStream: (streamId) =>
      ipcRenderer.invoke("codex:stream-cancel", streamId),
    onStreamEvent: (callback) => {
      if (typeof callback !== "function") return () => undefined;
      const listener = (_event, payload) => callback(payload);
      ipcRenderer.on("codex:stream-event", listener);
      return () => ipcRenderer.removeListener("codex:stream-event", listener);
    },
  },
  localRuntime: {
    status: () => ipcRenderer.invoke("local-runtime:status"),
    startStream: (input) =>
      ipcRenderer.invoke("local-runtime:stream-start", input),
    cancelStream: (streamId) =>
      ipcRenderer.invoke("local-runtime:stream-cancel", streamId),
    onStreamEvent: (callback) => {
      if (typeof callback !== "function") return () => undefined;
      const listener = (_event, payload) => callback(payload);
      ipcRenderer.on("local-runtime:stream-event", listener);
      return () =>
        ipcRenderer.removeListener("local-runtime:stream-event", listener);
    },
  },
});
