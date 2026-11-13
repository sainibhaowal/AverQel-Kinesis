import { existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { spawnFrontendBuild } from "./spawn-frontend-build.mjs";

const desktopDirectory = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "..",
);
const repositoryRoot = path.resolve(desktopDirectory, "../..");
const environment = {
  ...process.env,
  BUILD_TARGET: "desktop",
  NEXT_PUBLIC_BUILD_TARGET: "desktop",
  NEXT_PUBLIC_API_URL: "http://127.0.0.1:11001/api/v1",
};

const result = spawnFrontendBuild({
  cwd: repositoryRoot,
  env: environment,
  stdio: "inherit",
});

if (result.error) throw result.error;
if (result.status !== 0) process.exit(result.status ?? 1);

const exportDirectory = path.join(repositoryRoot, "frontend", "out");
if (!existsSync(exportDirectory)) {
  throw new Error(
    `The desktop frontend build did not produce ${exportDirectory}`,
  );
}
