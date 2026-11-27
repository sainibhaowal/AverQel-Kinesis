"use strict";

const { createWriteStream, existsSync, mkdirSync } = require("node:fs");
const path = require("node:path");
const { spawn } = require("node:child_process");
const { getAgentsHome } = require("./averqel-home.cjs");
const os = require("node:os");

/**
 * Attempt to detect the user's actual shell PATH by checking common
 * shell configuration files. The packaged Electron app inherits a minimal
 * PATH that lacks nvm, pnpm, volta, etc. This tries to reconstruct it.
 */
function detectUserShellPath() {
  const homeDir = os.homedir();
  const shell = process.env.SHELL || "";
  const isZsh = shell.includes("zsh");
  const isBash = shell.includes("bash");
  const isFish = shell.includes("fish");

  // Common paths added by version managers and package managers
  const commonPaths = [
    // nvm
    path.join(homeDir, ".nvm", "versions", "node"),
    // pnpm
    path.join(homeDir, ".local", "share", "pnpm"),
    path.join(homeDir, ".pnpm"),
    // volta
    path.join(homeDir, ".volta", "bin"),
    // bun
    path.join(homeDir, ".bun", "bin"),
    // asdf
    path.join(homeDir, ".asdf", "shims"),
    path.join(homeDir, ".asdf", "bin"),
    // Homebrew (Linux)
    "/opt/homebrew/bin",
    "/home/linuxbrew/.linuxbrew/bin",
    // User local bins
    path.join(homeDir, ".local", "bin"),
    // npm global
    path.join(homeDir, ".npm-global", "bin"),
    // Go
    path.join(homeDir, "go", "bin"),
    // Cargo
    path.join(homeDir, ".cargo", "bin"),
    // PyPI user
    path.join(homeDir, ".local", "bin"),
    // Local Node installations (common for Codex CLI)
    path.join(homeDir, ".local", "opt"),
  ];

  // Try to find nvm node versions
  let nvmNodeBin = "";
  try {
    const nvmDir = path.join(homeDir, ".nvm", "versions", "node");
    const fs = require("node:fs");
    if (fs.existsSync(nvmDir)) {
      const versions = fs.readdirSync(nvmDir).sort().reverse();
      for (const version of versions) {
        const binPath = path.join(nvmDir, version, "bin");
        if (fs.existsSync(binPath)) {
          nvmNodeBin = binPath;
          break;
        }
      }
    }
  } catch {}

  // Try to find local Node installations (.local/opt/node-*/bin)
  // Collect ALL node bin paths, not just the first one
  const localNodeBins = [];
  try {
    const localOptDir = path.join(homeDir, ".local", "opt");
    const fs = require("node:fs");
    if (fs.existsSync(localOptDir)) {
      const entries = fs.readdirSync(localOptDir).filter(e => e.startsWith("node-"));
      for (const entry of entries.sort().reverse()) {
        const binPath = path.join(localOptDir, entry, "bin");
        if (fs.existsSync(binPath)) {
          localNodeBins.push(binPath);
        }
      }
    }
  } catch {}

  // Build candidate PATH entries
  const extraPaths = [];
  if (nvmNodeBin) extraPaths.push(nvmNodeBin);
  for (const bin of localNodeBins) extraPaths.push(bin);
  for (const p of commonPaths) {
    try {
      const fs = require("node:fs");
      if (fs.existsSync(p)) extraPaths.push(p);
    } catch {}
  }

  return extraPaths.join(path.delimiter);
}

/**
 * Detect Antigravity CLI executable in common locations.
 * Returns the full path if found, null otherwise.
 */
function detectAntigravityCli() {
  const homeDir = os.homedir();
  const platform = process.platform;

  const candidates = [];

  // 1. ANTIGRAVITY_CLI_PATH env var (if user set it)
  if (process.env.ANTIGRAVITY_CLI_PATH) {
    candidates.push(process.env.ANTIGRAVITY_CLI_PATH);
  }

  // 2. Get all PATH entries from detectUserShellPath
  const shellPaths = detectUserShellPath().split(path.delimiter);
  for (const p of shellPaths) {
    candidates.push(path.join(p, platform === "win32" ? "agy.exe" : "agy"));
  }

  // 3. Common installation locations not covered by shell paths
  if (platform === "win32") {
    candidates.push(
      path.join(process.env.USERPROFILE || homeDir, "AppData", "Roaming", "npm", "agy.cmd"),
      path.join(process.env.USERPROFILE || homeDir, "AppData", "Local", "Programs", "Antigravity", "agy.exe"),
      path.join(process.env.USERPROFILE || homeDir, ".local", "bin", "agy.cmd"),
    );
  } else {
    candidates.push(
      // pnpm global
      path.join(homeDir, ".local", "share", "pnpm", "agy"),
      path.join(homeDir, ".pnpm", "agy"),
      // volta
      path.join(homeDir, ".volta", "bin", "agy"),
      // bun
      path.join(homeDir, ".bun", "bin", "agy"),
      // npm global
      path.join(homeDir, ".npm-global", "bin", "agy"),
      // asdf
      path.join(homeDir, ".asdf", "shims", "agy"),
      path.join(homeDir, ".asdf", "bin", "agy"),
      // Homebrew
      "/opt/homebrew/bin/agy",
      "/usr/local/bin/agy",
      // System
      "/usr/bin/agy",
      // User local
      path.join(homeDir, ".local", "bin", "agy"),
    );
  }

  // 4. Check each candidate
  const fs = require("node:fs");
  for (const candidate of candidates) {
    try {
      if (candidate && fs.existsSync(candidate) && fs.statSync(candidate).isFile()) {
        if (platform !== "win32") {
          const mode = fs.statSync(candidate).mode;
          if (mode & 0o111) return candidate;
        } else {
          return candidate;
        }
      }
    } catch {}
  }

  return null;
}


/**
 * Detect Opencode CLI executable in common locations.
 * Returns the full path if found, null otherwise.
 */
function detectOpencodeCli() {
  const homeDir = os.homedir();
  const platform = process.platform;

  const candidates = [];

  // 1. OPENCODE_CLI_PATH env var (if user set it)
  if (process.env.OPENCODE_CLI_PATH) {
    candidates.push(process.env.OPENCODE_CLI_PATH);
  }

  // 2. Get all PATH entries from detectUserShellPath
  const shellPaths = detectUserShellPath().split(path.delimiter);
  for (const p of shellPaths) {
    candidates.push(path.join(p, platform === "win32" ? "opencode.cmd" : "opencode"));
  }

  // 3. Common installation locations not covered by shell paths
  if (platform === "win32") {
    candidates.push(
      path.join(process.env.USERPROFILE || homeDir, "AppData", "Roaming", "npm", "opencode.cmd"),
      path.join(process.env.USERPROFILE || homeDir, "AppData", "Local", "Programs", "Opencode", "opencode.exe"),
      path.join(process.env.USERPROFILE || homeDir, ".local", "bin", "opencode.cmd"),
    );
  } else {
    candidates.push(
      // pnpm global
      path.join(homeDir, ".local", "share", "pnpm", "opencode"),
      path.join(homeDir, ".pnpm", "opencode"),
      // volta
      path.join(homeDir, ".volta", "bin", "opencode"),
      // bun
      path.join(homeDir, ".bun", "bin", "opencode"),
      // npm global
      path.join(homeDir, ".npm-global", "bin", "opencode"),
      // asdf
      path.join(homeDir, ".asdf", "shims", "opencode"),
      path.join(homeDir, ".asdf", "bin", "opencode"),
      // Homebrew
      "/opt/homebrew/bin/opencode",
      "/usr/local/bin/opencode",
      // System
      "/usr/bin/opencode",
      // User local
      path.join(homeDir, ".local", "bin", "opencode"),
    );
  }

  // 4. Check each candidate
  const fs = require("node:fs");
  for (const candidate of candidates) {
    try {
      if (candidate && fs.existsSync(candidate) && fs.statSync(candidate).isFile()) {
        // On Unix, check executable bit
        if (platform !== "win32") {
          const mode = fs.statSync(candidate).mode;
          if (mode & 0o111) return candidate; // executable bit set
        } else {
          return candidate;
        }
      }
    } catch {}
  }

  return null;
}


/**
 * Detect Codex CLI executable in common locations.
 * Returns the full path if found, null otherwise.
 */
function detectCodexCli() {
  const homeDir = os.homedir();
  const platform = process.platform;

  const candidates = [];

  // 1. CODEX_CLI_PATH env var (if user set it)
  if (process.env.CODEX_CLI_PATH) {
    candidates.push(process.env.CODEX_CLI_PATH);
  }

  // 2. Get all PATH entries from detectUserShellPath
  const shellPaths = detectUserShellPath().split(path.delimiter);
  for (const p of shellPaths) {
    candidates.push(path.join(p, platform === "win32" ? "codex.cmd" : "codex"));
  }

  // 3. Common installation locations not covered by shell paths
  if (platform === "win32") {
    candidates.push(
      path.join(process.env.USERPROFILE || homeDir, "AppData", "Roaming", "npm", "codex.cmd"),
      path.join(process.env.USERPROFILE || homeDir, "AppData", "Local", "Programs", "Codex", "codex.exe"),
      path.join(process.env.USERPROFILE || homeDir, ".local", "bin", "codex.cmd"),
    );
  } else {
    candidates.push(
      // pnpm global
      path.join(homeDir, ".local", "share", "pnpm", "codex"),
      path.join(homeDir, ".pnpm", "codex"),
      // volta
      path.join(homeDir, ".volta", "bin", "codex"),
      // bun
      path.join(homeDir, ".bun", "bin", "codex"),
      // npm global
      path.join(homeDir, ".npm-global", "bin", "codex"),
      // asdf
      path.join(homeDir, ".asdf", "shims", "codex"),
      path.join(homeDir, ".asdf", "bin", "codex"),
      // Homebrew
      "/opt/homebrew/bin/codex",
      "/usr/local/bin/codex",
      // System
      "/usr/bin/codex",
      // User local
      path.join(homeDir, ".local", "bin", "codex"),
    );
  }

  // 4. Check each candidate
  const fs = require("node:fs");
  for (const candidate of candidates) {
    try {
      if (candidate && fs.existsSync(candidate) && fs.statSync(candidate).isFile()) {
        // On Unix, check executable bit
        if (platform !== "win32") {
          const mode = fs.statSync(candidate).mode;
          if (mode & 0o111) return candidate; // executable bit set
        } else {
          return candidate;
        }
      }
    } catch {}
  }

  return null;
}

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

  // Detect user's Codex CLI to pass to backend
  const codexCliPath = detectCodexCli();
  const opencodeCliPath = detectOpencodeCli();
  const antigravityCliPath = detectAntigravityCli();
  const userShellPath = detectUserShellPath();

  const env = {
    ...process.env,
    AVERQEL_HOME: homeDirectory,
    AVERQEL_AGENTS_HOME: getAgentsHome(),
    AVERQEL_DESKTOP_PACKAGED: "1",
    AVERQEL_DESKTOP_LOCAL_RUNTIME: "1",
    // Desktop sandbox_execute runs in a private workspace inside the backend's
    // local OS sandbox; it does not need a separately hosted executor.
    AKS_DEEPSPACE_SANDBOX_ENABLED: "true",
    AKS_SEARXNG_LOCAL_RUNTIME_ENABLED: "false",
    HF_HOME: huggingFaceHome,
    HF_HUB_CACHE: path.join(huggingFaceHome, "hub"),
    TRANSFORMERS_CACHE: path.join(huggingFaceHome, "hub"),
    PLAYWRIGHT_BROWSERS_PATH: path.join(resourcesPath, "playwright-browsers"),
    PORT: String(port),
    BACKEND_PORT: String(port),
    PYTHONUNBUFFERED: "1",
    // Pass user's shell PATH so backend can find nvm/pnpm/volta tools
    PATH: userShellPath ? `${userShellPath}${path.delimiter}${process.env.PATH || ""}` : process.env.PATH,
    // Explicitly pass detected Codex CLI path
    ...(codexCliPath ? { CODEX_CLI_PATH: codexCliPath } : {}),
    // Explicitly pass detected Opencode CLI path
    ...(opencodeCliPath ? { OPENCODE_CLI_PATH: opencodeCliPath } : {}),
    // Explicitly pass detected Antigravity CLI path
    ...(antigravityCliPath ? { ANTIGRAVITY_CLI_PATH: antigravityCliPath } : {}),
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
  detectCodexCli,
  detectOpencodeCli,
  detectAntigravityCli,
  detectUserShellPath,
};
