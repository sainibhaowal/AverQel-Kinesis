import http from "node:http";
import https from "node:https";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { existsSync } from "node:fs";
import { homedir } from "node:os";
import process from "node:process";
import { setTimeout as delay } from "node:timers/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";

const rootDir = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "..",
);
const mode = process.argv[2] ?? "dev";

const [nodeMajor, nodeMinor] = process.versions.node.split(".").map(Number);
if (nodeMajor < 22 || (nodeMajor === 22 && nodeMinor < 12)) {
  console.error(
    `Electron development requires Node.js >=22.12.0; found ${process.versions.node}. ` +
      "Install/use Node 22, then run `pnpm --dir applications/desktop install --frozen-lockfile`.",
  );
  process.exit(1);
}

if (mode !== "dev") {
  console.error(`Unknown Electron mode: ${mode}`);
  console.error("Usage: pnpm electron dev");
  process.exit(1);
}

const pnpm = process.platform === "win32" ? "pnpm.cmd" : "pnpm";
const harnessHome =
  process.env.AVERQEL_HARNESS_HOME?.trim() ||
  path.join(homedir(), ".averqel-harness");

function resolveHarnessPort(environmentName, fallback) {
  const raw = process.env[environmentName]?.trim() || fallback;
  const port = Number(raw);
  if (!Number.isInteger(port) || port < 1024 || port > 65535) {
    throw new Error(`${environmentName} must be a TCP port between 1024 and 65535.`);
  }
  return String(port);
}

const backendPort = resolveHarnessPort("AVERQEL_HARNESS_BACKEND_PORT", "11000");
const frontendPort = resolveHarnessPort("AVERQEL_HARNESS_FRONTEND_PORT", "11030");
const frontendUrl =
  process.env.ELECTRON_START_URL?.trim() || `http://127.0.0.1:${frontendPort}`;
const apiUrl = `http://127.0.0.1:${backendPort}/api/v1`;
const sharedEnvironment = {
  ...process.env,
  AVERQEL_HARNESS_HOME: harnessHome,
  AVERQEL_HOME: harnessHome,
  AVERQEL_HARNESS_BACKEND_PORT: backendPort,
  AVERQEL_HARNESS_FRONTEND_PORT: frontendPort,
  ELECTRON_START_URL: frontendUrl,
  NEXT_PUBLIC_API_URL: apiUrl,
  NEXT_PUBLIC_BUILD_TARGET: "desktop",
  AKS_TEST_ALLOW_BOOTSTRAP_SKIP: "1",
  NEXT_TELEMETRY_DISABLED: "1",
  PYTHONUNBUFFERED: "1",
  AKS_DEEPSPACE_BROWSER_AUTOMATION_ENABLED: "true",
};
delete sharedEnvironment.ELECTRON_RUN_AS_NODE;
delete sharedEnvironment.ELECTRON_NO_ATTACH_CONSOLE;

function assertLoopbackUrl(rawUrl, label) {
  let parsed;
  try {
    parsed = new URL(rawUrl);
  } catch {
    throw new Error(`${label} must be a valid HTTP or HTTPS URL.`);
  }
  const loopback =
    parsed.hostname === "localhost" ||
    parsed.hostname === "127.0.0.1" ||
    parsed.hostname === "[::1]" ||
    parsed.hostname.endsWith(".localhost");
  if (
    (parsed.protocol !== "http:" && parsed.protocol !== "https:") ||
    !loopback
  ) {
    throw new Error(`${label} must point to a local loopback address.`);
  }
}

assertLoopbackUrl(frontendUrl, "ELECTRON_START_URL");

const backendDirectory = path.join(rootDir, "backend");
const backendPythonCandidates =
  process.platform === "win32"
    ? [
        process.env.AVERQEL_PYTHON,
        path.join(backendDirectory, ".venv", "Scripts", "python.exe"),
        "python.exe",
        "python",
      ]
    : [
        process.env.AVERQEL_PYTHON,
        path.join(backendDirectory, ".venv", "bin", "python"),
        "python3",
        "python",
      ];
const backendPython = backendPythonCandidates.find(
  (candidate) =>
    candidate && (path.isAbsolute(candidate) ? existsSync(candidate) : true),
);
if (!backendPython) {
  throw new Error(
    "A Python interpreter for the local AverQel backend was not found.",
  );
}

const backendEnvironment = {
  ...sharedEnvironment,
  AKS_ENV: "development",
  BACKEND_PORT: backendPort,
  PORT: backendPort,
};
const frontendEnvironment = {
  ...sharedEnvironment,
  PORT: frontendPort,
};

function start(command, args, environment = sharedEnvironment, cwd = rootDir) {
  return spawn(command, args, {
    cwd,
    env: environment,
    stdio: "inherit",
  });
}

function probe(url, requireSuccess = false) {
  return new Promise((resolve) => {
    const client = url.startsWith("https:") ? https : http;
    const request = client.get(
      url,
      { rejectUnauthorized: false },
      (response) => {
        response.resume();
        resolve(
          !requireSuccess ||
            (response.statusCode >= 200 && response.statusCode < 300),
        );
      },
    );
    request.setTimeout(1000, () => {
      request.destroy();
      resolve(false);
    });
    request.on("error", () => resolve(false));
  });
}

async function waitForFrontend(child) {
  for (let attempt = 0; attempt < 120; attempt += 1) {
    if (await probe(frontendUrl)) return;
    if (child.exitCode !== null) {
      throw new Error(
        "The frontend development server exited before becoming ready.",
      );
    }
    await delay(500);
  }
  throw new Error(
    `Frontend did not become ready at ${frontendUrl} within 60 seconds.`,
  );
}

async function waitForBackend(child) {
  const healthUrl = `${apiUrl}/health/ready`;
  for (let attempt = 0; attempt < 120; attempt += 1) {
    if (await probe(healthUrl, true)) return;
    if (child.exitCode !== null) {
      throw new Error("The local backend exited before becoming ready.");
    }
    await delay(500);
  }
  throw new Error(
    `The local backend did not become ready at ${healthUrl} within 60 seconds.`,
  );
}

let shuttingDown = false;
let backend;
let frontend;
let electron;

function stop(child) {
  if (child && child.exitCode === null) child.kill();
}

function shutdown(code = 0) {
  if (shuttingDown) return;
  shuttingDown = true;
  stop(electron);
  stop(frontend);
  stop(backend);
  process.exitCode = code;
}

process.on("SIGINT", () => shutdown(0));
process.on("SIGTERM", () => shutdown(0));

backend = start(
  backendPython,
  ["run_backend.py"],
  backendEnvironment,
  backendDirectory,
);
backend.once("error", (error) => {
  console.error(`Could not start the local backend: ${error.message}`);
  shutdown(1);
});
backend.once("exit", (code) => {
  if (!shuttingDown && code !== 0) {
    console.error(`Local backend exited with code ${code ?? "unknown"}.`);
    shutdown(code || 1);
  }
});

frontend = start(pnpm, ["--dir", "frontend", "dev"], frontendEnvironment);
frontend.once("error", (error) => {
  console.error(`Could not start the frontend: ${error.message}`);
  shutdown(1);
});
frontend.once("exit", (code) => {
  if (!shuttingDown && code !== 0) {
    console.error(
      `Frontend development server exited with code ${code ?? "unknown"}.`,
    );
    shutdown(code || 1);
  }
});

try {
  await waitForBackend(backend);
  console.log(`Local backend ready at ${apiUrl}; starting the frontend.`);
  await waitForFrontend(frontend);
  console.log(`Frontend ready at ${frontendUrl}; starting Electron.`);
  electron = start(pnpm, ["--dir", "applications/desktop", "dev"]);
  electron.once("error", (error) => {
    console.error(`Could not start Electron: ${error.message}`);
    shutdown(1);
  });
  electron.once("exit", (code, signal) => {
    if (!shuttingDown) {
      if (signal) console.log(`Electron exited from ${signal}.`);
      shutdown(code ?? 0);
    }
  });
} catch (error) {
  console.error(error instanceof Error ? error.message : String(error));
  shutdown(1);
}

await once(process, "exit");
