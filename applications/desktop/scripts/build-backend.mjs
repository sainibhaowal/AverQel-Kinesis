import { existsSync, copyFileSync, mkdirSync, cpSync } from "node:fs";
import { spawnSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const desktopRoot = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "..",
);
const repositoryRoot = path.resolve(desktopRoot, "../..");
const backendRoot = path.join(repositoryRoot, "backend");
const pythonCandidates = [
  process.env.AVERQEL_PYTHON
    ? { command: process.env.AVERQEL_PYTHON, prefixArgs: [] }
    : null,
  ...(process.platform === "win32"
    ? [
        { command: "python", prefixArgs: [] },
        { command: "py", prefixArgs: ["-3.12"] },
      ]
    : [
        { command: "python3.12", prefixArgs: [] },
        { command: "python3", prefixArgs: [] },
      ]),
].filter(Boolean);

function run(command, args, options = {}) {
  const result = spawnSync(command, args, {
    cwd: repositoryRoot,
    stdio: "inherit",
    env: process.env,
    ...options,
  });
  if (result.error) throw result.error;
  if (result.status !== 0) {
    throw new Error(`${command} exited with status ${result.status ?? "unknown"}.`);
  }
}

function resolvePython() {
  for (const candidate of pythonCandidates) {
    const check = spawnSync(candidate.command, [...candidate.prefixArgs, "--version"], {
      cwd: repositoryRoot,
      encoding: "utf8",
      stdio: "pipe",
    });
    const version = `${check.stdout ?? ""}${check.stderr ?? ""}`;
    if (check.status === 0 && /Python 3\.12\./.test(version)) {
      return candidate;
    }
  }
  throw new Error(
    "A native Python 3.12 interpreter is required. Install Python 3.12 or set AVERQEL_PYTHON to its executable.",
  );
}

const python = resolvePython();
const buildEnvironmentDirectory = path.join(backendRoot, "build", "desktop-venv");
const buildPython = path.join(
  buildEnvironmentDirectory,
  process.platform === "win32" ? "Scripts/python.exe" : "bin/python",
);
if (!existsSync(buildPython)) {
  run(python.command, [
    ...python.prefixArgs,
    "-m",
    "venv",
    buildEnvironmentDirectory,
  ]);
}

const requirementFiles = [
  path.join(backendRoot, "requirements.txt"),
  path.join(backendRoot, "requirements-build.txt"),
];

for (const requirementFile of requirementFiles) {
  run(buildPython, [
    "-m",
    "pip",
    "install",
    "--disable-pip-version-check",
    "--no-input",
    "-r",
    requirementFile,
  ]);
}

const playwrightBrowsersDirectory = path.join(
  backendRoot,
  "build",
  "playwright-browsers",
);
run(buildPython, ["-m", "playwright", "install", "chromium"], {
  env: {
    ...process.env,
    PLAYWRIGHT_BROWSERS_PATH: playwrightBrowsersDirectory,
  },
});

// Ensure alembic migrations are copied to the build directory for PyInstaller
const alembicSourceDir = path.join(backendRoot, "alembic");
const alembicDestDir = path.join(backendRoot, "build", "pyinstaller-home", "alembic");
if (existsSync(alembicSourceDir)) {
  if (existsSync(alembicDestDir)) {
    // Remove existing directory to ensure clean copy
    const { rmSync } = await import("node:fs");
    rmSync(alembicDestDir, { recursive: true, force: true });
  }
  mkdirSync(alembicDestDir, { recursive: true });
  cpSync(alembicSourceDir, alembicDestDir, { recursive: true });
  console.log(`Copied alembic migrations to ${alembicDestDir}`);
}

// Copy alembic.ini to the build directory
const alembicIniSource = path.join(backendRoot, "alembic.ini");
const alembicIniDest = path.join(backendRoot, "build", "pyinstaller-home", "alembic.ini");
if (existsSync(alembicIniSource)) {
  copyFileSync(alembicIniSource, alembicIniDest);
  console.log(`Copied alembic.ini to ${alembicIniDest}`);
}

const isolatedHome = path.join(backendRoot, "build", "pyinstaller-home");
const environment = {
  ...process.env,
  AVERQEL_HOME: isolatedHome,
  AKS_DATABASE_URL: `sqlite:///${path.join(isolatedHome, "db", "analysis.db")}`,
  AKS_LOCAL_STORAGE_PATH: path.join(isolatedHome, "storage"),
  AKS_SKIP_MCP_CATALOG_SYNC: "1",
  PYTHONNOUSERSITE: "1",
};

run(
  buildPython,
  [
    "-m",
    "PyInstaller",
    "--noconfirm",
    "--clean",
    "--distpath",
    path.join(backendRoot, "dist"),
    "--workpath",
    path.join(backendRoot, "build", "pyinstaller-work"),
    path.join(backendRoot, "averqel-backend.spec"),
  ],
  { env: environment },
);

const executable = path.join(
  backendRoot,
  "dist",
  "averqel-backend",
  process.platform === "win32" ? "averqel-backend.exe" : "averqel-backend",
);
if (!existsSync(executable)) {
  throw new Error(`The packaged backend executable was not created: ${executable}`);
}
process.stdout.write(`Packaged local backend: ${executable}\n`);
