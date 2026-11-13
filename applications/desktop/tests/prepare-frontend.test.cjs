const assert = require("node:assert/strict");
const test = require("node:test");

const frontendBuildModule = import("../scripts/spawn-frontend-build.mjs");

test("launches the Windows pnpm command shim through ComSpec", async () => {
  const { spawnFrontendBuild } = await frontendBuildModule;
  let invocation;
  const env = { ComSpec: "C:\\Windows\\System32\\cmd.exe" };
  const result = { status: 0 };

  const returned = spawnFrontendBuild({
    platform: "win32",
    env,
    cwd: "C:\\workspace",
    stdio: "inherit",
    spawnSyncImpl: (...args) => {
      invocation = args;
      return result;
    },
  });

  assert.deepEqual(invocation, [
    "C:\\Windows\\System32\\cmd.exe",
    ["/d", "/c", "pnpm.cmd", "--dir", "frontend", "build"],
    { cwd: "C:\\workspace", stdio: "inherit", env },
  ]);
  assert.equal(returned, result);
});

test("launches pnpm directly on Unix", async () => {
  const { spawnFrontendBuild } = await frontendBuildModule;
  let invocation;
  const env = { PATH: "/usr/bin" };

  spawnFrontendBuild({
    platform: "linux",
    env,
    cwd: "/workspace",
    stdio: "inherit",
    spawnSyncImpl: (...args) => {
      invocation = args;
      return { status: 0 };
    },
  });

  assert.deepEqual(invocation, [
    "pnpm",
    ["--dir", "frontend", "build"],
    { cwd: "/workspace", stdio: "inherit", env },
  ]);
});
