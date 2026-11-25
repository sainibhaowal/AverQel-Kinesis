const { URL } = require("node:url");

const CHANNELS = {
  show: "browser-view:show",
  hide: "browser-view:hide",
  close: "browser-view:close",
  navigate: "browser-view:navigate",
  back: "browser-view:back",
  forward: "browser-view:forward",
  reload: "browser-view:reload",
  exportState: "browser-view:export-state",
  state: "browser-view:state",
};

const PARTITION = "persist:averqel-deepspace-browser";
const BLANK_URL = "about:blank";
const MAX_BROWSER_VIEWS = 8;

function isViewId(value) {
  return typeof value === "string" && value.length > 0 && value.length <= 256 && /^[a-zA-Z0-9:_-]+$/.test(value);
}

function httpUrl(value) {
  if (typeof value !== "string" || value.length > 8192) return null;
  try {
    const parsed = new URL(value);
    if ((parsed.protocol !== "http:" && parsed.protocol !== "https:") || parsed.username || parsed.password) {
      return null;
    }
    return parsed.toString();
  } catch {
    return null;
  }
}

function boundsWithinWindow(value, contentSize) {
  if (!value || !contentSize || typeof value !== "object") return null;
  const values = [value.x, value.y, value.width, value.height];
  if (!values.every((item) => typeof item === "number" && Number.isFinite(item))) return null;

  const x = Math.max(0, Math.floor(value.x));
  const y = Math.max(0, Math.floor(value.y));
  const maxWidth = Math.max(0, Math.floor(contentSize[0]) - x);
  const maxHeight = Math.max(0, Math.floor(contentSize[1]) - y);
  const width = Math.min(Math.floor(value.width), maxWidth);
  const height = Math.min(Math.floor(value.height), maxHeight);
  if (width <= 0 || height <= 0) return null;
  return { x, y, width, height };
}

function registerBrowserViewIpcHandlers({ ipcMain, WebContentsView, getWindow, isTrustedSender }) {
  const views = new Map();

  function requireTrusted(event) {
    if (!isTrustedSender(event)) throw new Error("Unauthorized browser view request.");
  }

  function requireViewId(value) {
    if (!isViewId(value)) throw new Error("Invalid browser view identifier.");
    return value;
  }

  function state(viewId, record = views.get(viewId)) {
    const contents = record?.view.webContents;
    if (!contents || contents.isDestroyed()) {
      return { viewId, url: BLANK_URL, title: "", loading: false, canGoBack: false, canGoForward: false, error: null };
    }
    return {
      viewId,
      url: contents.getURL() || BLANK_URL,
      title: contents.getTitle(),
      loading: contents.isLoading(),
      canGoBack: contents.canGoBack(),
      canGoForward: contents.canGoForward(),
      error: null,
    };
  }

  function publish(viewId, extra = {}) {
    const record = views.get(viewId);
    const target = record?.window;
    if (!target || target.isDestroyed() || target.webContents.isDestroyed()) return;
    target.webContents.send(CHANNELS.state, { ...state(viewId, record), ...extra });
  }

  function closeView(viewId) {
    const record = views.get(viewId);
    if (!record) return;
    if (record.window && !record.window.isDestroyed()) record.window.contentView.removeChildView(record.view);
    if (!record.view.webContents.isDestroyed()) record.view.webContents.close();
    views.delete(viewId);
  }

  function ensureView(viewId, window) {
    let record = views.get(viewId);
    if (record && !record.view.webContents.isDestroyed() && record.window === window) {
      record.lastUsed = Date.now();
      return record;
    }
    if (record) closeView(viewId);
    if (views.size >= MAX_BROWSER_VIEWS) {
      const evictable = [...views.entries()]
        .filter(([, item]) => !item.visible)
        .sort((left, right) => left[1].lastUsed - right[1].lastUsed)[0];
      if (!evictable) throw new Error("The maximum number of in-app browser pages is open.");
      closeView(evictable[0]);
    }

    const view = new WebContentsView({
      webPreferences: {
        partition: PARTITION,
        nodeIntegration: false,
        contextIsolation: true,
        sandbox: true,
        webSecurity: true,
        allowRunningInsecureContent: false,
        devTools: false,
      },
    });
    record = { view, window, visible: false, lastUsed: Date.now() };
    views.set(viewId, record);
    window.contentView.addChildView(view);

    const contents = view.webContents;
    const rejectNonWebNavigation = (event, rawUrl) => {
      if (!httpUrl(rawUrl) && rawUrl !== BLANK_URL) event.preventDefault();
    };
    contents.on("will-navigate", rejectNonWebNavigation);
    contents.on("will-redirect", rejectNonWebNavigation);
    contents.setWindowOpenHandler(({ url }) => {
      const safeUrl = httpUrl(url);
      if (safeUrl) {
        void contents.loadURL(safeUrl).catch((error) =>
          publish(viewId, { error: String(error?.message ?? "The page could not be opened.") }),
        );
      }
      return { action: "deny" };
    });
    contents.on("did-start-navigation", (_event, url, _inPlace, isMainFrame) => {
      if (!isMainFrame) return;
      record.visible = url !== BLANK_URL;
      view.setVisible(record.visible);
      publish(viewId, { url, loading: true, error: null });
    });
    contents.on("did-navigate", () => publish(viewId, { loading: false }));
    contents.on("did-navigate-in-page", () => publish(viewId));
    contents.on("did-finish-load", () => publish(viewId, { loading: false }));
    contents.on("did-fail-load", (_event, errorCode, description, _validatedUrl, isMainFrame) => {
      if (isMainFrame && errorCode !== -3) publish(viewId, { loading: false, error: description || "The page could not be loaded." });
    });
    contents.on("page-title-updated", (_event, title) => publish(viewId, { title }));
    contents.on("destroyed", () => views.delete(viewId));
    void contents.loadURL(BLANK_URL).catch(() => undefined);
    return record;
  }

  ipcMain.handle(CHANNELS.show, (event, rawViewId, rawBounds) => {
    requireTrusted(event);
    const viewId = requireViewId(rawViewId);
    const window = getWindow();
    if (!window || window.isDestroyed()) throw new Error("The desktop window is unavailable.");
    const bounds = boundsWithinWindow(rawBounds, window.getContentSize());
    if (!bounds) {
      const record = views.get(viewId);
      if (record) {
        record.visible = false;
        record.view.setVisible(false);
      }
      return state(viewId);
    }
    const record = ensureView(viewId, window);
    record.view.setBounds(bounds);
    record.visible = record.view.webContents.getURL() !== BLANK_URL;
    record.view.setVisible(record.visible);
    record.lastUsed = Date.now();
    return state(viewId, record);
  });

  ipcMain.handle(CHANNELS.hide, (event, rawViewId) => {
    requireTrusted(event);
    const record = views.get(requireViewId(rawViewId));
    if (record) {
      record.visible = false;
      record.view.setVisible(false);
      record.lastUsed = Date.now();
    }
  });
  ipcMain.handle(CHANNELS.close, (event, rawViewId) => {
    requireTrusted(event);
    closeView(requireViewId(rawViewId));
  });
  ipcMain.handle(CHANNELS.navigate, async (event, rawViewId, rawUrl) => {
    requireTrusted(event);
    const viewId = requireViewId(rawViewId);
    const url = httpUrl(rawUrl);
    if (!url) throw new Error("Only HTTP and HTTPS addresses can be opened.");
    const record = views.get(viewId);
    if (!record || record.view.webContents.isDestroyed()) throw new Error("The browser view is not open.");
    record.visible = true;
    record.lastUsed = Date.now();
    await record.view.webContents.loadURL(url);
    return state(viewId, record);
  });
  ipcMain.handle(CHANNELS.back, (event, rawViewId) => {
    requireTrusted(event);
    const record = views.get(requireViewId(rawViewId));
    if (record?.view.webContents.canGoBack()) record.view.webContents.goBack();
    return state(requireViewId(rawViewId), record);
  });
  ipcMain.handle(CHANNELS.forward, (event, rawViewId) => {
    requireTrusted(event);
    const viewId = requireViewId(rawViewId);
    const record = views.get(viewId);
    if (record?.view.webContents.canGoForward()) record.view.webContents.goForward();
    return state(viewId, record);
  });
  ipcMain.handle(CHANNELS.reload, (event, rawViewId) => {
    requireTrusted(event);
    const viewId = requireViewId(rawViewId);
    const record = views.get(viewId);
    if (record && !record.view.webContents.isDestroyed()) record.view.webContents.reload();
    return state(viewId, record);
  });
  ipcMain.handle(CHANNELS.exportState, async (event, rawViewId) => {
    requireTrusted(event);
    const viewId = requireViewId(rawViewId);
    const record = views.get(viewId);
    const contents = record?.view.webContents;
    if (!contents || contents.isDestroyed()) {
      return { cookies: [], origins: [] };
    }
    const rawCookies = await contents.session.cookies.get({});
    const cookies = rawCookies.map((cookie) => {
      const normalized = {
        name: cookie.name,
        value: cookie.value,
        domain: cookie.domain,
        path: cookie.path || "/",
        httpOnly: Boolean(cookie.httpOnly),
        secure: Boolean(cookie.secure),
      };
      if (cookie.sameSite === "strict") normalized.sameSite = "Strict";
      else if (cookie.sameSite === "lax") normalized.sameSite = "Lax";
      else if (cookie.sameSite === "no_restriction") normalized.sameSite = "None";
      if (typeof cookie.expirationDate === "number" && cookie.expirationDate > 0) {
        normalized.expires = cookie.expirationDate;
      }
      return normalized;
    });
    const origins = [];
    let textSnapshot = null;
    let screenshotBase64 = null;
    const currentUrl = contents.getURL() || "";
    if (currentUrl.startsWith("http://") || currentUrl.startsWith("https://")) {
      try {
        const originSnapshot = await contents.executeJavaScript(
          `(() => {
            const o = window.location && window.location.origin;
            if (!o || (!o.startsWith("http://") && !o.startsWith("https://"))) return null;
            const items = [];
            for (let i = 0; i < window.localStorage.length; i++) {
              const k = window.localStorage.key(i);
              if (k !== null) items.push({ name: k, value: String(window.localStorage.getItem(k) ?? "") });
            }
            const bodyText = document.body && typeof document.body.innerText === "string"
              ? document.body.innerText.slice(0, 32000)
              : "";
            return { origin: o, localStorage: items, textSnapshot: bodyText };
          })()`,
          true,
        );
        if (originSnapshot && typeof originSnapshot.origin === "string") {
          if (typeof originSnapshot.textSnapshot === "string" && originSnapshot.textSnapshot.trim()) {
            textSnapshot = originSnapshot.textSnapshot;
          }
          if (Array.isArray(originSnapshot.localStorage) && originSnapshot.localStorage.length > 0) {
            origins.push({
              origin: originSnapshot.origin,
              localStorage: originSnapshot.localStorage,
            });
          }
        }
      } catch {
        // Ignore cross-origin/navigation race during localStorage snapshot.
      }
      try {
        if (typeof contents.capturePage === "function") {
          const captured = await contents.capturePage();
          if (captured && typeof captured.isEmpty === "function" && !captured.isEmpty()) {
            const png = captured.toPNG();
            if (png && png.length > 0 && png.length <= 3_000_000) {
              screenshotBase64 = png.toString("base64");
            }
          }
        }
      } catch {
        // Ignore transient capturePage failures.
      }
    }
    return {
      cookies,
      origins,
      active_url: currentUrl.startsWith("http://") || currentUrl.startsWith("https://") ? currentUrl : null,
      text_snapshot: textSnapshot,
      screenshot_base64: screenshotBase64,
    };
  });

  return {
    close() {
      for (const viewId of [...views.keys()]) closeView(viewId);
    },
  };
}

module.exports = {
  CHANNELS,
  MAX_BROWSER_VIEWS,
  PARTITION,
  boundsWithinWindow,
  httpUrl,
  isViewId,
  registerBrowserViewIpcHandlers,
};
