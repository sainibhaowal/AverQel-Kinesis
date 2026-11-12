"use strict";

const { randomUUID } = require("node:crypto");
const { promises: fs, rmSync } = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const MAX_DRAG_BYTES = 32 * 1024 * 1024;
const MAX_ACTIVE_HANDLES_PER_SENDER = 4;
const HANDLE_TTL_MS = 60_000;
const MIME_EXTENSIONS = new Map([
  ["application/json", ".json"],
  ["application/pdf", ".pdf"],
  ["application/zip", ".zip"],
  ["application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".docx"],
  ["application/vnd.openxmlformats-officedocument.presentationml.presentation", ".pptx"],
  ["application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"],
  ["image/jpeg", ".jpg"],
  ["image/png", ".png"],
  ["image/webp", ".webp"],
  ["text/html", ".html"],
  ["text/markdown", ".md"],
  ["text/csv", ".csv"],
  ["text/plain", ".txt"],
  ["text/xml", ".xml"],
]);
const RESERVED_WINDOWS_NAMES = /^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?$/i;

function truncateUtf8(value, maxBytes) {
  let output = "";
  for (const character of value) {
    if (Buffer.byteLength(output + character, "utf8") > maxBytes) break;
    output += character;
  }
  return output;
}

function resolveDragFileName(rawName, contentType = "") {
  const source = typeof rawName === "string" ? rawName : "download";
  const basename = source.replace(/\\/g, "/").split("/").pop() || "download";
  let safe = basename
    .normalize("NFC")
    .replace(/[\u0000-\u001f\u007f\u202a-\u202e\u2066-\u2069<>:"/\\|?*]/g, "_")
    .replace(/[. ]+$/g, "")
    .trim();
  if (!safe || safe === "." || safe === "..") safe = "download";
  if (RESERVED_WINDOWS_NAMES.test(safe)) safe = `_${safe}`;

  const mime = String(contentType).split(";", 1)[0].trim().toLowerCase();
  const expectedExtension = MIME_EXTENSIONS.get(mime);
  if (expectedExtension && !path.extname(safe)) safe += expectedExtension;

  if (Buffer.byteLength(safe, "utf8") > 255) {
    const extension = path.extname(safe);
    const stem = extension ? safe.slice(0, -extension.length) : safe;
    const boundedExtension = truncateUtf8(extension, 64);
    safe = `${truncateUtf8(stem, 255 - Buffer.byteLength(boundedExtension, "utf8"))}${boundedExtension}`;
  }
  return safe;
}

function toBuffer(value) {
  if (value instanceof ArrayBuffer) return Buffer.from(value);
  if (Buffer.isBuffer(value)) return value;
  if (ArrayBuffer.isView(value)) {
    return Buffer.from(value.buffer, value.byteOffset, value.byteLength);
  }
  return null;
}

class DesktopFileDrag {
  constructor({
    tempRoot = os.tmpdir(),
    iconPath = "",
    ttlMs = HANDLE_TTL_MS,
    removeTreeSync = rmSync,
    platform = process.platform,
  } = {}) {
    this.tempRoot = tempRoot;
    this.iconPath = iconPath;
    this.ttlMs = ttlMs;
    this.removeTreeSync = removeTreeSync;
    this.platform = platform;
    this.handles = new Map();
  }

  async prepare(senderId, input) {
    if (!Number.isSafeInteger(senderId) || senderId < 0) throw new Error("Invalid drag owner.");
    if (!input || typeof input !== "object" || Array.isArray(input)) {
      throw new Error("Invalid file drag payload.");
    }
    const bytes = toBuffer(input.bytes);
    if (!bytes || bytes.byteLength === 0 || bytes.byteLength > MAX_DRAG_BYTES) {
      throw new Error("The file is empty, unsupported, or too large for native drag-out.");
    }
    let senderHandleCount = 0;
    for (const item of this.handles.values()) {
      if (item.senderId === senderId) senderHandleCount += 1;
    }
    if (senderHandleCount >= MAX_ACTIVE_HANDLES_PER_SENDER) {
      throw new Error("Too many native drag files are being staged by this window.");
    }

    const handle = randomUUID();
    const directory = await fs.mkdtemp(path.join(this.tempRoot, "averqel-drag-"));
    try {
      if (this.platform !== "win32") await fs.chmod(directory, 0o700);
      const filePath = path.join(directory, resolveDragFileName(input.name, input.contentType));
      await fs.writeFile(filePath, bytes, { flag: "wx", mode: 0o600 });
      const timer = setTimeout(() => {
        void this.release(senderId, handle);
      }, this.ttlMs);
      timer.unref();
      this.handles.set(handle, { senderId, directory, filePath, timer, started: false });
      return { handle, name: path.basename(filePath) };
    } catch (error) {
      await fs.rm(directory, { recursive: true, force: true });
      throw error;
    }
  }

  start(senderId, handle, startDrag) {
    const item = this._ownedItem(senderId, handle);
    if (item.started) throw new Error("This native drag handle has already been started.");
    if (typeof startDrag !== "function") throw new Error("Native drag is unavailable.");
    startDrag({ file: item.filePath, icon: this.iconPath });
    item.started = true;
    return true;
  }

  async cancel(senderId, handle) {
    return this.release(senderId, handle);
  }

  async release(senderId, handle) {
    const item = this.handles.get(handle);
    if (!item || item.senderId !== senderId) return false;
    this.handles.delete(handle);
    clearTimeout(item.timer);
    await fs.rm(item.directory, { recursive: true, force: true });
    return true;
  }

  async releaseSender(senderId) {
    const owned = [...this.handles.entries()].filter(([, item]) => item.senderId === senderId);
    await Promise.all(owned.map(([handle]) => this.release(senderId, handle)));
  }

  async dispose() {
    const handles = [...this.handles.entries()];
    await Promise.all(handles.map(([handle, item]) => this.release(item.senderId, handle)));
  }

  disposeSync() {
    for (const [handle, item] of this.handles) {
      this.handles.delete(handle);
      clearTimeout(item.timer);
      try {
        this.removeTreeSync(item.directory, { recursive: true, force: true });
      } catch {
        // Temp cleanup is best-effort; private directories also have a TTL.
      }
    }
  }

  _ownedItem(senderId, handle) {
    if (typeof handle !== "string") throw new Error("Invalid native drag handle.");
    const item = this.handles.get(handle);
    if (!item || item.senderId !== senderId) throw new Error("Unknown or unauthorized drag handle.");
    return item;
  }
}

function registerFileDragIpcHandlers({ ipcMain, getManager, isAverQelSender }) {
  const manager = () => getManager();
  const authorize = (event, action) => {
    if (!isAverQelSender(event)) throw new Error(`Unauthorized file drag ${action} request.`);
    if (!Number.isSafeInteger(event.sender?.id)) throw new Error("Invalid file drag sender.");
    return event.sender.id;
  };
  ipcMain.handle("file-drag:prepare", async (event, input) =>
    manager().prepare(authorize(event, "prepare"), input),
  );
  ipcMain.handle("file-drag:start", async (event, handle) => {
    const ownerId = authorize(event, "start");
    return manager().start(ownerId, handle, ({ file, icon }) =>
      event.sender.startDrag({ file, icon }),
    );
  });
  ipcMain.handle("file-drag:cancel", async (event, handle) =>
    manager().cancel(authorize(event, "cancel"), handle),
  );
  ipcMain.handle("file-drag:release", async (event, handle) =>
    manager().release(authorize(event, "release"), handle),
  );
}

module.exports = {
  DesktopFileDrag,
  HANDLE_TTL_MS,
  MAX_ACTIVE_HANDLES_PER_SENDER,
  MAX_DRAG_BYTES,
  registerFileDragIpcHandlers,
  resolveDragFileName,
};
