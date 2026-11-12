const assert = require("node:assert/strict");
const { promises: fs } = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const {
  DesktopFileDrag,
  MAX_ACTIVE_HANDLES_PER_SENDER,
  MAX_DRAG_BYTES,
  registerFileDragIpcHandlers,
  resolveDragFileName,
} = require("../electron/file-drag.cjs");

test("drag filenames remove paths, platform-reserved names, and unsafe characters", () => {
  assert.equal(resolveDragFileName("../../CON", "application/pdf"), "_CON.pdf");
  assert.equal(resolveDragFileName("a:b?.txt"), "a_b_.txt");
  assert.equal(resolveDragFileName("/"), "download");
  assert.equal(resolveDragFileName("already.txt", "application/pdf"), "already.txt");
  assert.equal(resolveDragFileName(null), "download");
  assert.equal(resolveDragFileName("...   "), "download");
  assert.ok(Buffer.byteLength(resolveDragFileName("x".repeat(300)), "utf8") <= 255);
  assert.ok(Buffer.byteLength(resolveDragFileName(`${"é".repeat(180)}.txt`), "utf8") <= 255);
});

test("file drag validates owner and payload and cleans temporary files after a write failure", async (t) => {
  const tempRoot = await fs.mkdtemp(path.join(os.tmpdir(), "averqel-drag-test-"));
  t.after(() => fs.rm(tempRoot, { recursive: true, force: true }));
  const drag = new DesktopFileDrag({ tempRoot });

  await assert.rejects(drag.prepare(-1, {}), /Invalid drag owner/);
  await assert.rejects(drag.prepare("1", {}), /Invalid drag owner/);
  await assert.rejects(drag.prepare(1, null), /Invalid file drag payload/);
  await assert.rejects(drag.prepare(1, []), /Invalid file drag payload/);
  await assert.rejects(drag.prepare(1, "not an object"), /Invalid file drag payload/);
  await assert.rejects(drag.prepare(1, { name: "empty", bytes: new Uint8Array() }), /empty/);

  const originalWriteFile = fs.writeFile;
  fs.writeFile = async () => {
    throw new Error("simulated storage failure");
  };
  try {
    await assert.rejects(
      drag.prepare(1, { name: "failure.txt", bytes: new Uint8Array([1]) }),
      /simulated storage failure/,
    );
  } finally {
    fs.writeFile = originalWriteFile;
  }
  assert.deepEqual(await fs.readdir(tempRoot), []);

  const fromArrayBuffer = await drag.prepare(1, {
    name: "buffer.bin",
    bytes: new Uint8Array([4, 5]).buffer,
  });
  const fromBuffer = await drag.prepare(1, { name: "buffer-copy.bin", bytes: Buffer.from([6, 7]) });
  await drag.dispose();
  assert.equal(await drag.release(1, fromArrayBuffer.handle), false);
  assert.equal(await drag.release(1, fromBuffer.handle), false);
});

test("file drag prepares private bounded files and releases on owner lifecycle", async (t) => {
  const tempRoot = await fs.mkdtemp(path.join(os.tmpdir(), "averqel-drag-test-"));
  t.after(() => fs.rm(tempRoot, { recursive: true, force: true }));
  const drag = new DesktopFileDrag({ tempRoot, ttlMs: 60_000 });
  t.after(() => drag.dispose());

  const prepared = await drag.prepare(7, {
    name: "quarterly.pdf",
    contentType: "application/pdf",
    bytes: new Uint8Array([37, 80, 68, 70]),
  });
  const actualDirectory = (await fs.readdir(tempRoot)).map((name) => path.join(tempRoot, name))[0];
  assert.equal(await fs.readFile(path.join(actualDirectory, "quarterly.pdf"), "utf8"), "%PDF");
  if (process.platform !== "win32") {
    assert.equal((await fs.stat(actualDirectory)).mode & 0o777, 0o700);
  }

  let started;
  assert.equal(drag.start(7, prepared.handle, (payload) => { started = payload; }), true);
  assert.equal(path.basename(started.file), prepared.name);
  assert.equal(await drag.releaseSender(8), undefined);
  assert.equal(await drag.release(8, prepared.handle), false);
  assert.equal((await fs.readdir(tempRoot)).length, 1);
  assert.equal(await drag.releaseSender(7), undefined);
  await assert.rejects(fs.access(actualDirectory));
});

test("file drag refuses oversized and cross-renderer handles", async (t) => {
  const tempRoot = await fs.mkdtemp(path.join(os.tmpdir(), "averqel-drag-test-"));
  t.after(() => fs.rm(tempRoot, { recursive: true, force: true }));
  const drag = new DesktopFileDrag({ tempRoot, ttlMs: 60_000 });
  t.after(() => drag.dispose());
  await assert.rejects(
    drag.prepare(1, { name: "large.bin", bytes: new Uint8Array(MAX_DRAG_BYTES + 1) }),
    /too large/,
  );
  await assert.rejects(drag.prepare(1, { name: "invalid.bin", bytes: "not bytes" }), /too large/);
  const item = await drag.prepare(1, { name: "small.txt", bytes: new Uint8Array([1]) });
  assert.throws(() => drag.start(2, item.handle, () => {}), /unauthorized/);
  assert.throws(() => drag.start(1, item.handle, null), /unavailable/);
  assert.throws(() => drag.start(1, 1, () => {}), /Invalid native drag handle/);
  assert.equal(drag.start(1, item.handle, () => {}), true);
  assert.throws(() => drag.start(1, item.handle, () => {}), /already been started/);
  assert.equal(await drag.cancel(1, item.handle), true);
  assert.equal(await drag.release(1, item.handle), false);

  const callbackFailure = await drag.prepare(1, { name: "retry.txt", bytes: new Uint8Array([1]) });
  assert.throws(() => drag.start(1, callbackFailure.handle, () => { throw new Error("native failure"); }), /native failure/);
  assert.equal(drag.start(1, callbackFailure.handle, () => {}), true);
});

test("file drag expiration and synchronous disposal release staged files", async (t) => {
  const tempRoot = await fs.mkdtemp(path.join(os.tmpdir(), "averqel-drag-test-"));
  t.after(() => fs.rm(tempRoot, { recursive: true, force: true }));
  const drag = new DesktopFileDrag({ tempRoot, ttlMs: 5 });
  const expired = await drag.prepare(3, { name: "expires.txt", bytes: new Uint8Array([1]) });
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.equal(await drag.release(3, expired.handle), false);
  assert.deepEqual(await fs.readdir(tempRoot), []);

  const active = await drag.prepare(3, { name: "shutdown.txt", bytes: new Uint8Array([2]) });
  drag.disposeSync();
  assert.deepEqual(await fs.readdir(tempRoot), []);
  assert.equal(await drag.release(3, active.handle), false);
});

test("file drag limits staged handles per renderer", async (t) => {
  const tempRoot = await fs.mkdtemp(path.join(os.tmpdir(), "averqel-drag-test-"));
  t.after(() => fs.rm(tempRoot, { recursive: true, force: true }));
  const drag = new DesktopFileDrag({ tempRoot, ttlMs: 60_000 });
  const handles = [];
  for (let index = 0; index < MAX_ACTIVE_HANDLES_PER_SENDER; index += 1) {
    handles.push(await drag.prepare(5, { name: `file-${index}.txt`, bytes: new Uint8Array([index]) }));
  }
  await assert.rejects(
    drag.prepare(5, { name: "overflow.txt", bytes: new Uint8Array([1]) }),
    /Too many native drag files/,
  );
  await Promise.all(handles.map(({ handle }) => drag.release(5, handle)));
});

test("synchronous disposal tolerates filesystem cleanup failures", async (t) => {
  const tempRoot = await fs.mkdtemp(path.join(os.tmpdir(), "averqel-drag-test-"));
  t.after(() => fs.rm(tempRoot, { recursive: true, force: true }));
  const drag = new DesktopFileDrag({ tempRoot, removeTreeSync: () => { throw new Error("locked"); } });
  await drag.prepare(4, { name: "locked.txt", bytes: new Uint8Array([1]) });
  assert.doesNotThrow(() => drag.disposeSync());
  await drag.dispose();
});

test("native drag inherits Windows directory permissions", async (t) => {
  const tempRoot = await fs.mkdtemp(path.join(os.tmpdir(), "averqel-drag-test-"));
  t.after(() => fs.rm(tempRoot, { recursive: true, force: true }));
  const originalChmod = fs.chmod;
  fs.chmod = async () => {
    throw new Error("Windows must use inherited directory ACLs");
  };
  const drag = new DesktopFileDrag({ tempRoot, platform: "win32" });
  try {
    await drag.prepare(6, { name: "windows.txt", bytes: new Uint8Array([1]) });
  } finally {
    fs.chmod = originalChmod;
    await drag.dispose();
  }
});

test("file drag IPC validates renderer identity for each lifecycle call", async () => {
  const handlers = new Map();
  const calls = [];
  registerFileDragIpcHandlers({
    ipcMain: { handle: (channel, handler) => handlers.set(channel, handler) },
    getManager: () => ({
      prepare: (...args) => { calls.push(["prepare", ...args]); return { handle: "opaque" }; },
      start: (ownerId, handle, startDrag) => {
        calls.push(["start", ownerId, handle]);
        startDrag({ file: "opaque", icon: "" });
        return true;
      },
      cancel: (...args) => { calls.push(["cancel", ...args]); return true; },
      release: (...args) => { calls.push(["release", ...args]); return true; },
    }),
    isAverQelSender: (event) => event.authorized,
  });
  const trusted = { authorized: true, sender: { id: 11, startDrag: (input) => calls.push(["native", input]) } };
  const untrusted = { authorized: false, sender: { id: 12 } };
  const invalidSender = { authorized: true, sender: { id: "invalid" } };
  await assert.rejects(() => handlers.get("file-drag:prepare")(untrusted, {}), /Unauthorized/);
  await assert.rejects(() => handlers.get("file-drag:prepare")(invalidSender, {}), /Invalid file drag sender/);
  assert.deepEqual(await handlers.get("file-drag:prepare")(trusted, { name: "x" }), { handle: "opaque" });
  assert.equal(await handlers.get("file-drag:start")(trusted, "opaque"), true);
  assert.equal(await handlers.get("file-drag:cancel")(trusted, "opaque"), true);
  assert.equal(await handlers.get("file-drag:release")(trusted, "opaque"), true);
  assert.deepEqual(calls[0], ["prepare", 11, { name: "x" }]);
  assert.deepEqual(calls[2], ["native", { file: "opaque", icon: "" }]);
  assert.deepEqual(calls[3], ["cancel", 11, "opaque"]);
  assert.deepEqual(calls[4], ["release", 11, "opaque"]);
});
