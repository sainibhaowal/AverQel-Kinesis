const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const { PassThrough } = require("node:stream");
const { mkdtemp, mkdir, rm, writeFile } = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const {
  backendExecutablePath,
  startBackend,
} = require("../electron/backend-runtime.cjs");

class StubChild extends EventEmitter {
  constructor() {
    super();
    this.exitCode = null;
    this.signalCode = null;
    this.stdout = new PassThrough();
    this.stderr = new PassThrough();
  }

  kill(signal) {
    this.signalCode = signal;
    this.emit("exit", null, signal);
    return true;
  }
}

test("resolves the bundled backend executable for each platform", () => {
  assert.equal(
    backendExecutablePath("/resources", "linux"),
    path.join("/resources", "backend-runtime", "averqel-backend", "averqel-backend"),
  );
  assert.equal(
    backendExecutablePath("C:\\resources", "win32"),
    path.join("C:\\resources", "backend-runtime", "averqel-backend", "averqel-backend.exe"),
  );
});

test("starts the packaged backend with the one app-data home and waits for readiness", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "averqel-backend-runtime-"));
  const resourcesPath = path.join(root, "resources");
  const homeDirectory = path.join(root, ".averqel-harness");
  const executable = backendExecutablePath(resourcesPath, "linux");
  await mkdir(path.dirname(executable), { recursive: true });
  await writeFile(executable, "test executable");

  const child = new StubChild();
  let spawnOptions;
  try {
    const runtime = await startBackend({
      resourcesPath,
      homeDirectory,
      platform: "linux",
      timeoutMs: 1_000,
      pollIntervalMs: 1,
      spawnImpl: (_file, _args, options) => {
        spawnOptions = options;
        return child;
      },
      fetchImpl: async (url) => ({
        ok: url === "http://127.0.0.1:11001/api/v1/health/ready",
      }),
    });

    assert.equal(runtime.port, 11001);
    assert.equal(spawnOptions.cwd, homeDirectory);
    assert.equal(spawnOptions.env.AVERQEL_HOME, homeDirectory);
    assert.equal(spawnOptions.env.AVERQEL_DESKTOP_PACKAGED, "1");
    assert.equal(spawnOptions.env.AKS_DEEPSPACE_SANDBOX_ENABLED, "false");
    assert.equal(spawnOptions.env.HF_HOME, path.join(homeDirectory, "cache", "huggingface"));
    assert.equal(spawnOptions.env.PORT, "11001");
    await runtime.stop();
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("fails clearly when the installer is missing its bundled backend", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "averqel-backend-missing-"));
  try {
    await assert.rejects(
      startBackend({ resourcesPath: root, homeDirectory: root }),
      /packaged AverQel backend is missing/,
    );
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});
