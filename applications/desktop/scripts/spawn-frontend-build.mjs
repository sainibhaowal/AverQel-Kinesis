import { spawnSync } from "node:child_process";

const frontendBuildArguments = ["--dir", "frontend", "build"];

export function spawnFrontendBuild({
  platform = process.platform,
  env = process.env,
  spawnSyncImpl = spawnSync,
  ...options
} = {}) {
  const isWindows = platform === "win32";
  const command = isWindows ? env.ComSpec || "cmd.exe" : "pnpm";
  const args = isWindows
    ? ["/d", "/c", "pnpm.cmd", ...frontendBuildArguments]
    : frontendBuildArguments;

  return spawnSyncImpl(command, args, { ...options, env });
}
