"use strict";

const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const net = require("node:net");
const os = require("node:os");
const path = require("node:path");

const repositoryRoot = path.resolve(__dirname, "../../..");
const backendDirectory = path.join(
  repositoryRoot,
  "backend",
  "dist",
  "averqel-backend",
);
const executablePath = path.join(
  backendDirectory,
  process.platform === "win32" ? "averqel-backend.exe" : "averqel-backend",
);
const timeoutMs = 180_000;

function getAvailablePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const { port } = server.address();
      server.close((error) => (error ? reject(error) : resolve(port)));
    });
  });
}

function stopProcess(child) {
  if (child.exitCode !== null || child.signalCode !== null) return Promise.resolve();

  if (process.platform === "win32" && Number.isInteger(child.pid)) {
    const killer = spawn("taskkill", ["/PID", String(child.pid), "/T", "/F"], {
      stdio: "ignore",
      windowsHide: true,
    });
    return new Promise((resolve) => killer.once("close", resolve));
  }

  return new Promise((resolve) => {
    const timeout = setTimeout(() => child.kill("SIGKILL"), 5_000);
    child.once("exit", () => {
      clearTimeout(timeout);
      resolve();
    });
    child.kill("SIGTERM");
  });
}

async function waitForReady(child, baseUrl, logTail) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (child.exitCode !== null || child.signalCode !== null) {
      throw new Error(
        `Packaged backend exited before readiness (${child.exitCode ?? child.signalCode}).\n${logTail()}`,
      );
    }
    try {
      const response = await fetch(`${baseUrl}/api/v1/health/ready`, {
        signal: AbortSignal.timeout(2_000),
      });
      if (response.ok) return;
    } catch {
      // Startup includes first-run SQLite initialization; retry until bounded timeout.
    }
    await new Promise((resolve) => setTimeout(resolve, 300));
  }
  throw new Error(`Packaged backend did not become ready in ${timeoutMs} ms.\n${logTail()}`);
}

async function main() {
  assert.ok(fs.existsSync(executablePath), `Missing packaged backend: ${executablePath}`);

  const temporaryRoot = fs.mkdtempSync(path.join(os.tmpdir(), "averqel-backend-smoke-"));
  const homeDirectory = path.join(temporaryRoot, "user-data");
  const huggingFaceHome = path.join(homeDirectory, "cache", "huggingface");
  const port = await getAvailablePort();
  const baseUrl = `http://127.0.0.1:${port}`;
  let output = "";
  let child;

  try {
    child = spawn(executablePath, [], {
      cwd: temporaryRoot,
      env: {
        ...process.env,
        AVERQEL_HOME: homeDirectory,
        AVERQEL_DESKTOP_PACKAGED: "1",
        AKS_SKIP_MCP_CATALOG_SYNC: "1",
        AKS_DEEPSPACE_SANDBOX_ENABLED: "false",
        AKS_SEARXNG_LOCAL_RUNTIME_ENABLED: "false",
        HF_HOME: huggingFaceHome,
        HF_HUB_CACHE: path.join(huggingFaceHome, "hub"),
        TRANSFORMERS_CACHE: path.join(huggingFaceHome, "hub"),
        PORT: String(port),
        BACKEND_PORT: String(port),
        PYTHONUNBUFFERED: "1",
      },
      stdio: ["ignore", "pipe", "pipe"],
      windowsHide: true,
    });

    const collect = (chunk) => {
      output = (output + chunk.toString()).slice(-128_000);
    };
    child.stdout.on("data", collect);
    child.stderr.on("data", collect);

    await waitForReady(child, baseUrl, () => output);

    const live = await fetch(`${baseUrl}/api/v1/health/live`);
    const ready = await fetch(`${baseUrl}/api/v1/health/ready`);
    const openApiResponse = await fetch(`${baseUrl}/openapi.json`);
    assert.equal(live.status, 200, "Liveness endpoint must respond successfully");
    assert.equal(ready.status, 200, "Readiness endpoint must respond successfully");
    assert.equal(openApiResponse.status, 200, "OpenAPI must be served by the frozen API");

    const openApi = await openApiResponse.json();
    const paths = Object.keys(openApi.paths ?? {});
    for (const requiredPath of [
      "/api/v1/health/ready",
      "/api/v1/providers/embeddings/hub",
      "/api/v1/providers/rerankers/hub",
    ]) {
      assert.ok(paths.includes(requiredPath), `Frozen API omitted ${requiredPath}`);
    }

    assert.ok(
      fs.existsSync(path.join(homeDirectory, "db", "knowledge.db")),
      "First launch must create the local SQLite database under AVERQEL_HOME",
    );
    assert.ok(
      fs.existsSync(path.join(homeDirectory, "storage")),
      "First launch must create local storage under AVERQEL_HOME",
    );

    process.stdout.write(
      `Packaged backend smoke passed (${process.platform}/${process.arch}): live=${live.status}, ready=${ready.status}, Hugging Face embedding/reranker routes present.\n`,
    );
  } catch (error) {
    process.stderr.write(`${error.stack ?? error}\n`);
    process.stderr.write(`Temporary smoke home: ${homeDirectory}\n${output}\n`);
    process.exitCode = 1;
  } finally {
    if (child) await stopProcess(child);
    fs.rmSync(temporaryRoot, { recursive: true, force: true });
  }
}

main().catch((error) => {
  process.stderr.write(`${error.stack ?? error}\n`);
  process.exitCode = 1;
});
