const {
  app,
  BrowserWindow,
  Menu,
  ipcMain,
  safeStorage,
  screen,
  shell,
} = require("electron");
const path = require("node:path");
const { CodexDesktopRuntime } = require("./codex-runtime.cjs");
const { registerCodexIpcHandlers } = require("./codex-ipc.cjs");
const { LocalRuntime } = require("./local-runtime.cjs");
const { registerLocalRuntimeIpcHandlers } = require("./local-runtime-ipc.cjs");
const { getAverQelHome } = require("./averqel-home.cjs");
const { focusWindow } = require("./window-lifecycle.cjs");
const { getWindowGeometry } = require("./window-geometry.cjs");
const { createLocalFrontendServer } = require("./local-frontend.cjs");

if (process.platform === "linux" && process.env.WAYLAND_DISPLAY) {
  app.commandLine.appendSwitch("disable-features", "Vulkan");
}

let mainWindow;
let codexRuntime;
let localRuntime;
let mainWindowReady = false;
let averqelUrl;
let localFrontendServer;

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

  // Enable right-click context menu (Inspect Element only available when DevTools is enabled)
  mainWindow.webContents.on("context-menu", (_event, params) => {
    const template = [];
    if (isDevToolsEnabled()) {
      template.push(
        {
          label: "Inspect Element",
          click: () => {
            mainWindow.webContents.inspectElement(params.x, params.y);
            if (!mainWindow.webContents.isDevToolsOpened()) {
              mainWindow.webContents.openDevTools({ mode: "detach" });
            }
          },
        },
        {
          label: "Toggle Developer Tools",
          click: () => {
            if (mainWindow.webContents.isDevToolsOpened()) {
              mainWindow.webContents.closeDevTools();
            } else {
              mainWindow.webContents.openDevTools({ mode: "detach" });
            }
          },
        },
        { type: "separator" },
      );
    }
    template.push(
      { role: "reload", label: "Reload" },
      { role: "forceReload", label: "Force Reload" },
    );
    const contextMenu = Menu.buildFromTemplate(template);
    contextMenu.popup({ window: mainWindow });
  });

  mainWindow.on("closed", () => {
    mainWindow = null;
    mainWindowReady = false;
  });

  if (!app.isPackaged) {
    mainWindow.webContents.session.clearCache().catch(() => {});
  }

  void mainWindow.loadURL(appStartUrl());
}

function showWindow() {
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
registerCodexIpcHandlers({
  ipcMain,
  getRuntime: () => codexRuntime,
  isAverQelSender,
});
registerLocalRuntimeIpcHandlers({
  ipcMain,
  getRuntime: () => localRuntime,
  isAverQelSender,
});

// Keep this shell separate from other AverQel installations before
// Electron computes its single-instance lock or user-data path.
const averqelHome = getAverQelHome();
process.env.AVERQEL_HOME = averqelHome;
app.setPath("userData", path.join(averqelHome, "electron"));
app.setAppUserModelId("com.averqel.harness");
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
      codexRuntime = new CodexDesktopRuntime({
        app,
        safeStorage,
        shell,
        emit: (streamId, frame) => {
          if (!mainWindow?.isDestroyed()) {
            mainWindow.webContents.send("codex:stream-event", {
              streamId,
              ...frame,
            });
          }
        },
      });
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
      return startAverQelFrontend().then(() => {
        createWindow();
        app.on("activate", showWindow);
      });
    })
    .catch((error) => {
      console.error(
        `Could not start the local AverQel frontend: ${error instanceof Error ? error.message : String(error)}`,
      );
      app.quit();
    });
}

app.on("before-quit", () => {
  codexRuntime?.cancelAllStreams?.();
  localRuntime?.cancelAllStreams?.();
  if (localFrontendServer) {
    void localFrontendServer.close().catch(() => undefined);
    localFrontendServer = undefined;
  }
});

app.on("window-all-closed", () => {
  app.quit();
});
