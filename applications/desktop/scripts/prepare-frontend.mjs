import { existsSync } from "node:fs";
import { spawnSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const desktopDirectory = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "..",
);
const repositoryRoot = path.resolve(desktopDirectory, "../..");
const pnpm = process.platform === "win32" ? "pnpm.cmd" : "pnpm";
const environment = {
  ...process.env,
  BUILD_TARGET: "desktop",
  NEXT_PUBLIC_BUILD_TARGET: "desktop",
  NEXT_PUBLIC_API_URL: "http://127.0.0.1:11001/api/v1",
};

const result = spawnSync(pnpm, ["--dir", "frontend", "build"], {
  cwd: repositoryRoot,
  env: environment,
  stdio: "inherit",
  // Windows package-manager launchers are .cmd files and need cmd.exe.
  shell: process.platform === "win32",
});

if (result.error) throw result.error;
if (result.status !== 0) process.exit(result.status ?? 1);

const exportDirectory = path.join(repositoryRoot, "frontend", "out");
if (!existsSync(exportDirectory)) {
  throw new Error(
    `The desktop frontend build did not produce ${exportDirectory}`,
  );
}
