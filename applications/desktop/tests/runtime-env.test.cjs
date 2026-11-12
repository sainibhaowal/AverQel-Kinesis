const assert = require("node:assert/strict");
const { mkdtemp, rm, writeFile } = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const { spawn } = require("node:child_process");
const test = require("node:test");

const runtimeEnvScript = path.resolve(
  __dirname,
  "../../../frontend/scripts/run-with-runtime-env.sh",
);

function runRuntimeEnv(command, environment) {
  return new Promise((resolve, reject) => {
    const child = spawn("bash", [runtimeEnvScript, command], {
      env: environment,
      stdio: ["ignore", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => {
      stdout += chunk;
    });
    child.stderr.on("data", (chunk) => {
      stderr += chunk;
    });
    child.once("error", reject);
    child.once("close", (code) => resolve({ code, stdout, stderr }));
  });
}

test("explicit launcher ports override stale runtime defaults", async () => {
  const stateDirectory = await mkdtemp(
    path.join(os.tmpdir(), "averqel-runtime-env-"),
  );
  try {
    await writeFile(
      path.join(stateDirectory, ".env.local"),
      [
        "NEXT_PUBLIC_API_URL=http://127.0.0.1:10000/api/v1",
        "NEXT_PUBLIC_BUILD_TARGET=web",
        "PORT=10030",
        "RUNTIME_ONLY_VALUE=from-runtime-file",
        "",
      ].join("\n"),
    );

    const nodeCommand = `${JSON.stringify(process.execPath)} -e "process.stdout.write(JSON.stringify({api:process.env.NEXT_PUBLIC_API_URL,target:process.env.NEXT_PUBLIC_BUILD_TARGET,port:process.env.PORT,runtime:process.env.RUNTIME_ONLY_VALUE}))"`;
    const result = await runRuntimeEnv(
      nodeCommand,
      {
        ...process.env,
        AVERQEL_FRONTEND_STATE_DIR: stateDirectory,
        NEXT_PUBLIC_API_URL: "http://127.0.0.1:11000/api/v1",
        NEXT_PUBLIC_BUILD_TARGET: "desktop",
        PORT: "11030",
      },
    );

    assert.equal(result.code, 0, result.stderr);
    assert.deepEqual(JSON.parse(result.stdout), {
      api: "http://127.0.0.1:11000/api/v1",
      target: "desktop",
      port: "11030",
      runtime: "from-runtime-file",
    });
  } finally {
    await rm(stateDirectory, { recursive: true, force: true });
  }
});
