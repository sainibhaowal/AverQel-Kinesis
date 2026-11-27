const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const test = require("node:test");

const { PARTITION, boundsWithinWindow, httpUrl, registerBrowserViewIpcHandlers } = require("../electron/browser-view.cjs");

class FakeContents extends EventEmitter {
  constructor() {
    super();
    this.url = "about:blank";
    this.title = "";
    this.loading = false;
    this.back = false;
    this.forward = false;
    this.session = {
      cookies: {
        get: async () => [],
      },
    };
  }
  isDestroyed() { return false; }
  getURL() { return this.url; }
  getTitle() { return this.title; }
  isLoading() { return this.loading; }
  canGoBack() { return this.back; }
  canGoForward() { return this.forward; }
  loadURL(url) { this.url = url; this.loading = true; return Promise.resolve(); }
  executeJavaScript() { return Promise.resolve(null); }
  setWindowOpenHandler(handler) { this.windowOpenHandler = handler; }
  goBack() { this.back = false; }
  goForward() { this.forward = false; }
  reload() { this.loading = true; }
  close() { this.emit("destroyed"); }
}

class FakeView {
  constructor(options) {
    this.options = options;
    this.webContents = new FakeContents();
    this.bounds = null;
    this.visible = false;
  }
  setBounds(bounds) { this.bounds = bounds; }
  setVisible(visible) { this.visible = visible; }
}

function setup({ authorized = true } = {}) {
  const handlers = new Map();
  const views = [];
  const sent = [];
  const removed = [];
  const window = {
    isDestroyed: () => false,
    getContentSize: () => [1000, 800],
    contentView: {
      addChildView: (view) => views.push(view),
      removeChildView: (view) => removed.push(view),
    },
    webContents: {
      isDestroyed: () => false,
      send: (...args) => sent.push(args),
    },
  };
  const controller = registerBrowserViewIpcHandlers({
    ipcMain: { handle: (channel, handler) => handlers.set(channel, handler) },
    WebContentsView: FakeView,
    getWindow: () => window,
    isTrustedSender: () => authorized,
  });
  return { controller, handlers, removed, sent, views };
}

test("HTTP(S) browser URLs reject credentials and non-web schemes", () => {
  assert.equal(httpUrl("https://example.com/path"), "https://example.com/path");
  assert.equal(httpUrl("http://user:pass@example.com"), null);
  assert.equal(httpUrl("file:///etc/passwd"), null);
  assert.equal(httpUrl("javascript:alert(1)"), null);
  assert.equal(httpUrl("https://example.com/" + "x".repeat(8200)), null);
});

test("browser view bounds are clamped to the Electron content area", () => {
  assert.deepEqual(
    boundsWithinWindow({ x: -10, y: 20, width: 1200, height: 900 }, [1000, 800]),
    { x: 0, y: 20, width: 1000, height: 780 },
  );
  assert.equal(boundsWithinWindow({ x: 1000, y: 0, width: 20, height: 40 }, [1000, 800]), null);
  assert.equal(boundsWithinWindow({ x: 0, y: 0, width: Number.NaN, height: 20 }, [1000, 800]), null);
});

test("trusted renderer gets an isolated native page and can browse external HTTPS sites", async () => {
  const { handlers, sent, views } = setup();
  const event = {};
  await handlers.get("browser-view:show")(event, "conv-1:0", { x: 20, y: 30, width: 500, height: 600 });

  assert.equal(views.length, 1);
  assert.equal(views[0].options.webPreferences.partition, PARTITION);
  assert.equal(views[0].options.webPreferences.nodeIntegration, false);
  assert.equal(views[0].options.webPreferences.contextIsolation, true);
  assert.equal(views[0].options.webPreferences.sandbox, true);
  assert.deepEqual(views[0].bounds, { x: 20, y: 30, width: 500, height: 600 });

  await handlers.get("browser-view:navigate")(event, "conv-1:0", "https://example.com/");
  assert.equal(views[0].webContents.url, "https://example.com/");
  views[0].webContents.emit("did-start-navigation", {}, "https://example.com/", false, true);
  assert.equal(views[0].visible, true);
  assert.equal(sent.at(-1)?.[0], "browser-view:state");
  assert.equal(sent.at(-1)?.[1].viewId, "conv-1:0");
});

test("browser IPC refuses an untrusted renderer and unsafe navigation", async () => {
  const denied = setup({ authorized: false });
  assert.throws(
    () => denied.handlers.get("browser-view:show")({}, "conv-1:0", { x: 0, y: 0, width: 10, height: 10 }),
    /Unauthorized/,
  );
  assert.equal(denied.views.length, 0);

  const trusted = setup();
  const event = {};
  await trusted.handlers.get("browser-view:show")(event, "conv-1:0", { x: 0, y: 0, width: 10, height: 10 });
  await assert.rejects(
    trusted.handlers.get("browser-view:navigate")(event, "conv-1:0", "file:///etc/passwd"),
    /Only HTTP and HTTPS/,
  );
});

test("browser views keep independent pages per pane and close only the requested page", async () => {
  const { handlers, removed, views } = setup();
  const event = {};
  await handlers.get("browser-view:show")(event, "conv-1:0", { x: 0, y: 0, width: 400, height: 700 });
  await handlers.get("browser-view:show")(event, "conv-1:1", { x: 400, y: 0, width: 400, height: 700 });
  await handlers.get("browser-view:navigate")(event, "conv-1:0", "https://one.example/");
  await handlers.get("browser-view:navigate")(event, "conv-1:1", "https://two.example/");

  assert.equal(views.length, 2);
  assert.equal(views[0].webContents.url, "https://one.example/");
  assert.equal(views[1].webContents.url, "https://two.example/");
  handlers.get("browser-view:close")(event, "conv-1:0");
  assert.deepEqual(removed, [views[0]]);
  assert.equal(views[1].webContents.url, "https://two.example/");
});

test("browser view identifiers are validated and zero-sized bounds do not create a page", async () => {
  const { handlers, views } = setup();
  const event = {};
  assert.throws(
    () => handlers.get("browser-view:show")(event, "../unsafe", { x: 0, y: 0, width: 10, height: 10 }),
    /Invalid browser view identifier/,
  );
  await handlers.get("browser-view:show")(event, "conv-1:0", { x: 0, y: 0, width: 10, height: 10 });
  const emptyState = await handlers.get("browser-view:show")(event, "conv-1:1", { x: 0, y: 0, width: 0, height: 10 });
  assert.equal(emptyState.url, "about:blank");
  assert.equal(views.length, 1);
});

test("browser-view:export-state returns normalized cookies and origin localStorage", async () => {
  const { handlers, views } = setup();
  const event = {};
  await handlers.get("browser-view:show")(event, "conv-1:0", { x: 0, y: 0, width: 400, height: 600 });
  await handlers.get("browser-view:navigate")(event, "conv-1:0", "https://app.example.com/home");
  views[0].webContents.session.cookies.get = async () => [
    {
      name: "sid",
      value: "cookie-secret",
      domain: "app.example.com",
      path: "/",
      httpOnly: true,
      secure: true,
      sameSite: "lax",
      expirationDate: 1900000000,
    },
  ];
  views[0].webContents.executeJavaScript = async () => ({
    origin: "https://app.example.com",
    localStorage: [{ name: "jwt", value: "tok-1" }],
    textSnapshot: "Dashboard OK",
  });
  views[0].webContents.capturePage = async () => ({
    isEmpty: () => false,
    toPNG: () => Buffer.from("png-bytes"),
  });

  const exported = await handlers.get("browser-view:export-state")(event, "conv-1:0");
  assert.deepEqual(exported, {
    cookies: [
      {
        name: "sid",
        value: "cookie-secret",
        domain: "app.example.com",
        path: "/",
        httpOnly: true,
        secure: true,
        sameSite: "Lax",
        expires: 1900000000,
      },
    ],
    origins: [
      {
        origin: "https://app.example.com",
        localStorage: [{ name: "jwt", value: "tok-1" }],
      },
    ],
    active_url: "https://app.example.com/home",
    text_snapshot: "Dashboard OK",
    screenshot_base64: Buffer.from("png-bytes").toString("base64"),
  });
});
