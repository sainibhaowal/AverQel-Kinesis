"use strict";

const { createWriteStream, existsSync, mkdirSync } = require("node:fs");
const path = require("node:path");
const { spawn } = require("node:child_process");

const DEFAULT_BACKEND_PORT = 11001;
const DEFAULT_START_TIMEOUT_MS = 120_000;

function backendExecutablePath(resourcesPath, platform = process.platform) {
  return path.join(
    resourcesPath,
    "backend-runtime",
    "averqel-backend",
    platform === "win32" ? "averqel-backend.exe" : "averqel-backend",
  );
}

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function stopChild(child, platform, timeoutMs = 5_000) {
  if (child.exitCode !== null || child.signalCode !== null) return;

  if (platform === "win32" && Number.isInteger(child.pid)) {
    const { spawn: spawnChild } = require("node:child_process");
    const taskkill = spawnChild(
      "taskkill",
      ["/PID", String(child.pid), "/T", "/F"],
      { stdio: "ignore", windowsHide: true },
    );
    await new Promise((resolve) => taskkill.once("close", resolve));
    return;
  }

  const exited = new Promise((resolve) => child.once("exit", resolve));
  child.kill("SIGTERM");
  await Promise.race([exited, delay(timeoutMs)]);
  if (child.exitCode === null && child.signalCode === null) child.kill("SIGKILL");
}

/**
 * Start the backend shipped in this desktop package and wait for its own API
 * readiness endpoint before the renderer is opened.
 *
 * @param {{resourcesPath: string, homeDirectory: string, port?: number, timeoutMs?: number, pollIntervalMs?: number, platform?: string, spawnImpl?: Function, fetchImpl?: Function}} options
 */
async function startBackend({
  resourcesPath,
  homeDirectory,
  port = DEFAULT_BACKEND_PORT,
  timeoutMs = DEFAULT_START_TIMEOUT_MS,
  pollIntervalMs = 300,
  platform = process.platform,
  spawnImpl = spawn,
  fetchImpl = globalThis.fetch,
}) {
  const executablePath = backendExecutablePath(resourcesPath, platform);
  if (!existsSync(executablePath)) {
    throw new Error(`The packaged AverQel backend is missing: ${executablePath}`);
  }

  const logsDirectory = path.join(homeDirectory, "logs");
  mkdirSync(logsDirectory, { recursive: true, mode: 0o700 });
  const processLogPath = path.join(logsDirectory, "backend-process.log");
  const logStream = createWriteStream(processLogPath, {
    flags: "a",
    mode: 0o600,
  });
  const huggingFaceHome = path.join(homeDirectory, "cache", "huggingface");
  mkdirSync(huggingFaceHome, { recursive: true, mode: 0o700 });
  const env = {
    ...process.env,
    AVERQEL_HOME: homeDirectory,
    AVERQEL_DESKTOP_PACKAGED: "1",
    // The isolated-executor service is not part of the desktop package. Do
    // not advertise a phantom localhost:3020 dependency in packaged builds.
    AKS_DEEPSPACE_SANDBOX_ENABLED: "false",
    AKS_SEARXNG_LOCAL_RUNTIME_ENABLED: "false",
    HF_HOME: huggingFaceHome,
    HF_HUB_CACHE: path.join(huggingFaceHome, "hub"),
    TRANSFORMERS_CACHE: path.join(huggingFaceHome, "hub"),
    PORT: String(port),
    BACKEND_PORT: String(port),
    PYTHONUNBUFFERED: "1",
  };
  const child = spawnImpl(executablePath, [], {
    cwd: homeDirectory,
    env,
    stdio: ["ignore", "pipe", "pipe"],
    windowsHide: true,
  });
  let spawnError;
  child.once("error", (error) => {
    spawnError = error;
  });
  child.stdout?.pipe(logStream);
  child.stderr?.pipe(logStream);

  const readyUrl = `http://127.0.0.1:${port}/api/v1/health/ready`;
  const deadline = Date.now() + timeoutMs;
  try {
    while (Date.now() < deadline) {
      if (spawnError) throw spawnError;
      if (child.exitCode !== null || child.signalCode !== null) {
        throw new Error(
          `The packaged AverQel backend exited before it was ready (code ${child.exitCode ?? child.signalCode}). Check ${processLogPath}.`,
        );
      }
      try {
        const response = await fetchImpl(readyUrl, {
          signal: AbortSignal.timeout(2_000),
        });
        if (response.ok) {
          return {
            child,
            port,
            url: `http://127.0.0.1:${port}`,
            logPath: processLogPath,
            stop: async () => {
              await stopChild(child, platform);
              logStream.end();
            },
          };
        }
      } catch {
        // The API is not listening yet; keep polling until the bounded deadline.
      }
      await delay(pollIntervalMs);
    }
    throw new Error(
      `The packaged AverQel backend did not become ready within ${Math.round(timeoutMs / 1000)} seconds. Check ${processLogPath}.`,
    );
  } catch (error) {
    await stopChild(child, platform);
    logStream.end();
    throw error;
  }
}

module.exports = {
  DEFAULT_BACKEND_PORT,
  backendExecutablePath,
  startBackend,
};
