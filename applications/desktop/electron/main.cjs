const { app,
  BrowserWindow,
  WebContentsView,
  clipboard,
  dialog,
  Menu,
  ipcMain,
  screen,
  shell,
} = require("electron");
const path = require("node:path");
const {
  ASK_DEEPSPACE_CHANNEL,
  buildContextMenuTemplate,
  isDeepSpaceUrl,
} = require("./context-menu.cjs");
const { LocalRuntime } = require("./local-runtime.cjs");
const { registerLocalRuntimeIpcHandlers } = require("./local-runtime-ipc.cjs");
const { getAgentsHome, getAverQelHome, getWorkspaceRoot } = require("./averqel-home.cjs");
const { DesktopFileDrag, registerFileDragIpcHandlers } = require("./file-drag.cjs");
const { focusWindow } = require("./window-lifecycle.cjs");
const { getWindowGeometry } = require("./window-geometry.cjs");
const { createLocalFrontendServer } = require("./local-frontend.cjs");
const { startBackend } = require("./backend-runtime.cjs");
const { registerFileAccessIpcHandlers } = require("./file-access.cjs");
const { registerBrowserViewIpcHandlers } = require("./browser-view.cjs");

if (process.platform === "linux" && process.env.WAYLAND_DISPLAY) {
  app.commandLine.appendSwitch("disable-features", "Vulkan");
}

let mainWindow;
let localRuntime;
let mainWindowReady = false;
let applicationReady = false;
let averqelUrl;
let localFrontendServer;
let backendRuntime;
let backendShutdownStarted = false;
let browserViewController;
let askDeepSeekAvailable = false;
const nativeFileDrag = new DesktopFileDrag({
  iconPath: path.join(__dirname, "../assets/icon.png"),
});

const trustedHosts = new Set([
  "localhost",
  "127.0.0.1",
  "accounts.google.com",
  "github.com",
]);

function isTrustedUrl(rawUrl) {
  try {
    const url = new URL(rawUrl);
    return (
      (url.protocol === "https:" || url.protocol === "http:") &&
      trustedHosts.has(url.hostname)
    );
  } catch {
    return rawUrl.startsWith("file:");
  }
}

function isDevToolsEnabled() {
  if (!app.isPackaged) return true;
  if (
    process.env.AVERQEL_DEVTOOLS === "1" ||
    process.env.AVERQEL_DEVTOOLS === "true"
  )
    return true;
  if (
    process.argv.includes("--devtools") ||
    process.argv.includes("--enable-logging") ||
    process.argv.includes("--debug")
  )
    return true;
  return isDevToolsUnlocked();
}

let devToolsUnlocked = false;
function isDevToolsUnlocked() {
  return devToolsUnlocked;
}
function setDevToolsUnlocked(enabled) {
  devToolsUnlocked = Boolean(enabled);
}

function appStartUrl() {
  if (!app.isPackaged) {
    return process.env.ELECTRON_START_URL || "http://127.0.0.1:11030";
  }
  return averqelUrl || "http://127.0.0.1:11030";
}

async function startAverQelFrontend() {
  if (averqelUrl !== undefined) return averqelUrl;
  if (!app.isPackaged) {
    averqelUrl = appStartUrl();
    return averqelUrl;
  }

  const rootDirectory = path.join(process.resourcesPath, "frontend");
  localFrontendServer = await createLocalFrontendServer({ rootDirectory });
  averqelUrl = localFrontendServer.url;
  return averqelUrl;
}

function isAverQelRenderer(rawUrl) {
  try {
    return new URL(rawUrl).origin === new URL(appStartUrl()).origin;
  } catch {
    return false;
  }
}

function isAverQelSender(event) {
  return (
    event.sender === mainWindow?.webContents &&
    isAverQelRenderer(event.senderFrame?.url ?? "")
  );
}

function createWindow() {
  askDeepSeekAvailable = false;
  const startupDisplay = screen.getDisplayNearestPoint(screen.getCursorScreenPoint());
  const windowGeometry = getWindowGeometry(startupDisplay.workArea);
  mainWindow = new BrowserWindow({
    ...windowGeometry,
    frame: false,
    show: false,
    title: "AverQel Kinesis",
    icon: path.join(__dirname, "../assets/icon.png"),
    backgroundColor: "#070b0d",
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      spellcheck: true,
    },
  });

  mainWindow.once("ready-to-show", () => {
    mainWindowReady = true;
    focusWindow(mainWindow);
  });

  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (!isTrustedUrl(url)) {
      void shell.openExternal(url);
    }
    return { action: "deny" };
  });

  // Keep the native Electron title branded even if Next.js metadata from a
  // previously cached development render tries to replace it.
  mainWindow.webContents.on("page-title-updated", (event) => {
    event.preventDefault();
    mainWindow?.setTitle("AverQel Kinesis");
  });

  const windowContents = mainWindow.webContents;
  windowContents.once("destroyed", () => {
    void nativeFileDrag.releaseSender(windowContents.id);
  });

  mainWindow.webContents.on("will-navigate", (event, url) => {
    if (!isTrustedUrl(url)) {
      event.preventDefault();
      void shell.openExternal(url);
    }
  });

  // Enable DevTools keyboard shortcuts (F12, Ctrl+Shift+I, Cmd+Option+I) when allowed
  mainWindow.webContents.on("before-input-event", (event, input) => {
    if (input.type !== "keyDown") return;
    const isF12 = input.key === "F12";
    const isCtrlShiftI =
      (input.control || input.meta) &&
      input.shift &&
      input.key.toLowerCase() === "i";
    if (isF12 || isCtrlShiftI) {
      if (!isDevToolsEnabled()) return;
      event.preventDefault();
      if (mainWindow.webContents.isDevToolsOpened()) {
        mainWindow.webContents.closeDevTools();
      } else {
        mainWindow.webContents.openDevTools({ mode: "detach" });
      }
    }
  });

  // Native right-click menu. Edit roles come from Electron so cut/copy/paste
  // always act on the focused control; Ask DeepSeek forwards the selection to
  // the DeepSpace composer without submitting it.
  mainWindow.webContents.on("context-menu", (_event, params) => {
    const window = mainWindow;
    if (!window || window.isDestroyed()) return;

    const openExternalLink = (rawUrl) => {
      if (typeof rawUrl !== "string") return;
      try {
        const url = new URL(rawUrl);
        if (url.protocol !== "https:" && url.protocol !== "http:") return;
        void shell.openExternal(url.toString());
      } catch {
        // A malformed link is ignored rather than opening an arbitrary target.
      }
    };

    const template = buildContextMenuTemplate(params, {
      devToolsEnabled: isDevToolsEnabled(),
      askDeepSeek:
        askDeepSeekAvailable && isDeepSpaceUrl(window.webContents.getURL()),
      onAskDeepSeek: (text) => {
        if (window.isDestroyed() || window.webContents.isDestroyed()) return;
        window.webContents.send(ASK_DEEPSPACE_CHANNEL, { text });
      },
      onOpenLink: openExternalLink,
      onCopyText: (text) => clipboard.writeText(text),
      onCopyImage: (x, y) => window.webContents.copyImageAt(x, y),
      onSaveImage: (url) => {
        if (url.startsWith("http://") || url.startsWith("https://") || url.startsWith("data:")) {
          window.webContents.downloadURL(url);
        }
      },
      onInspectElement: (x, y) => {
        window.webContents.inspectElement(x, y);
        if (!window.webContents.isDevToolsOpened()) {
          window.webContents.openDevTools({ mode: "detach" });
        }
      },
      onToggleDevTools: () => toggleDevTools(),
      onReplaceMisspelling: (word) => window.webContents.replaceMisspelling(word),
      onAddToDictionary: (word) =>
        window.webContents.session.addWordToSpellCheckerDictionary(word),
    });
    const contextMenu = Menu.buildFromTemplate(template);
    contextMenu.popup({ window });
  });

  mainWindow.on("closed", () => {
    browserViewController?.close();
    mainWindow = null;
    mainWindowReady = false;
    askDeepSeekAvailable = false;
  });

  if (!app.isPackaged) {
    mainWindow.webContents.session.clearCache().catch(() => {});
  }

  void mainWindow.loadURL(appStartUrl());
}

function showWindow() {
  // A second launch can arrive while the bundled API or static frontend is
  // still starting. Never open a window against the development fallback URL.
  if (!applicationReady) return;
  if (!mainWindow) {
    createWindow();
    return;
  }
  if (mainWindowReady) focusWindow(mainWindow);
}

function toggleDevTools() {
  if (!mainWindow) return;
  if (!isDevToolsEnabled()) return;
  if (mainWindow.webContents.isDevToolsOpened()) {
    mainWindow.webContents.closeDevTools();
  } else {
    mainWindow.webContents.openDevTools({ mode: "detach" });
  }
}

ipcMain.on("deepspace:ask-selection-available", (event, available) => {
  if (!isAverQelSender(event)) return;
  askDeepSeekAvailable = available === true;
});

ipcMain.handle("window:minimize", () => mainWindow?.minimize());
ipcMain.handle("window:close", () => mainWindow?.close());
ipcMain.handle("window:toggle-maximize", () => {
  if (!mainWindow) return false;
  if (mainWindow.isMaximized()) mainWindow.unmaximize();
  else mainWindow.maximize();
  return mainWindow.isMaximized();
});
ipcMain.handle("window:is-maximized", () => Boolean(mainWindow?.isMaximized()));
ipcMain.handle("window:devtools-enabled", () => isDevToolsEnabled());
ipcMain.handle("window:unlock-devtools", () => {
  setDevToolsUnlocked(true);
  return true;
});
ipcMain.handle("window:toggle-devtools", () => {
  toggleDevTools();
  return Boolean(mainWindow?.webContents.isDevToolsOpened());
});
ipcMain.handle("window:open-devtools", () => {
  if (isDevToolsEnabled()) {
    mainWindow?.webContents.openDevTools({ mode: "detach" });
    return true;
  }
  return false;
});
ipcMain.handle("open-external", (_event, rawUrl) => {
  if (typeof rawUrl !== "string") return false;
  try {
    const url = new URL(rawUrl);
    if (url.protocol !== "https:" && url.protocol !== "http:") return false;
    void shell.openExternal(url.toString());
    return true;
  } catch {
    return false;
  }
});
registerLocalRuntimeIpcHandlers({
  ipcMain,
  getRuntime: () => localRuntime,
  isAverQelSender,
});
registerFileDragIpcHandlers({
  ipcMain,
  getManager: () => nativeFileDrag,
  isAverQelSender,
});
registerFileAccessIpcHandlers({
  ipcMain,
  getWindow: () => mainWindow,
  getWorkspaceRoot: () => averqelHome,
  getHomeDir: () => require("node:os").homedir(),
});
browserViewController = registerBrowserViewIpcHandlers({
  ipcMain,
  WebContentsView,
  getWindow: () => mainWindow,
  isTrustedSender: isAverQelSender,
});
// Electron computes its single-instance lock or user-data path.
const averqelHome = getAverQelHome();
process.env.AVERQEL_HOME = averqelHome;
process.env.AVERQEL_AGENTS_HOME = getAgentsHome();
app.setPath("userData", path.join(averqelHome, "electron"));
app.setAppUserModelId("com.averqel.kinesis");
const hasSingleInstanceLock = app.requestSingleInstanceLock();

if (!hasSingleInstanceLock) {
  app.quit();
} else {
  app.on("second-instance", () => showWindow());

  app
    .whenReady()
    .then(() => {
      // Kinesis uses its own in-app controls; do not show Electron's default
      // File/Edit/View/Window menu bar in the desktop window.
      Menu.setApplicationMenu(null);
      localRuntime = new LocalRuntime({
        emit: (streamId, frame) => {
          if (!mainWindow?.isDestroyed()) {
            mainWindow.webContents.send("local-runtime:stream-event", {
              streamId,
              ...frame,
            });
          }
        },
      });
      const startBackendIfPackaged = app.isPackaged
        ? startBackend({
            resourcesPath: process.resourcesPath,
            homeDirectory: averqelHome,
          }).then((runtime) => {
            backendRuntime = runtime;
          })
        : Promise.resolve();
      return startBackendIfPackaged.then(() => startAverQelFrontend()).then(() => {
        applicationReady = true;
        createWindow();
        app.on("activate", showWindow);
      });
    })
    .catch((error) => {
      const message = error instanceof Error ? error.message : String(error);
      console.error(`Could not start AverQel Kinesis: ${message}`);
      if (app.isPackaged) {
        dialog.showErrorBox(
          "AverQel Kinesis could not start",
          `${message}\n\nApplication logs are stored in ${path.join(averqelHome, "logs")}.`,
        );
      }
      app.quit();
    });
}

app.on("before-quit", (event) => {
  nativeFileDrag.disposeSync();
  localRuntime?.cancelAllStreams?.();
  if (localFrontendServer) {
    void localFrontendServer.close().catch(() => undefined);
    localFrontendServer = undefined;
  }
  if (backendRuntime && !backendShutdownStarted) {
    event.preventDefault();
    backendShutdownStarted = true;
    const runtime = backendRuntime;
    backendRuntime = undefined;
    void runtime.stop().finally(() => app.quit());
  }
});

app.on("window-all-closed", () => {
  app.quit();
});
