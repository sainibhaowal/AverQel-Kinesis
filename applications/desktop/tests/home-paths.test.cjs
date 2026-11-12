const assert = require("node:assert/strict");
const { mkdtemp, rm } = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const modulePath = require.resolve("../electron/averqel-home.cjs");

test("desktop home helpers expand configured roots and keep app data separate from agents", async (t) => {
  const originalHome = process.env.AVERQEL_HOME;
  const originalAgents = process.env.AVERQEL_AGENTS_HOME;
  const root = await mkdtemp(path.join(os.tmpdir(), "averqel-home-paths-"));
  t.after(async () => {
    if (originalHome === undefined) delete process.env.AVERQEL_HOME;
    else process.env.AVERQEL_HOME = originalHome;
    if (originalAgents === undefined) delete process.env.AVERQEL_AGENTS_HOME;
    else process.env.AVERQEL_AGENTS_HOME = originalAgents;
    delete require.cache[modulePath];
    await rm(root, { recursive: true, force: true });
  });

  process.env.AVERQEL_HOME = path.join(root, "app");
  process.env.AVERQEL_AGENTS_HOME = path.join(root, "agents");
  delete require.cache[modulePath];
  const { getAgentsHome, getAverQelHome, injectAverQelHome } = require(modulePath);

  const appHome = getAverQelHome();
  const agentsHome = getAgentsHome();
  const input = { PATH: "/usr/bin" };
  const childEnvironment = injectAverQelHome(input);

  assert.equal(appHome, path.join(root, "app"));
  assert.equal(agentsHome, path.join(root, "agents"));
  assert.notEqual(appHome, agentsHome);
  assert.equal(childEnvironment.AVERQEL_HOME, appHome);
  assert.equal(childEnvironment.AVERQEL_AGENTS_HOME, agentsHome);
  assert.deepEqual(input, { PATH: "/usr/bin" });
  assert.equal(require("node:fs").existsSync(agentsHome), false);
});

test("desktop home expansion supports the current user's tilde paths", async (t) => {
  const originalHome = process.env.AVERQEL_HOME;
  const originalAgents = process.env.AVERQEL_AGENTS_HOME;
  const originalHomedir = os.homedir;
  const root = await mkdtemp(path.join(os.tmpdir(), "averqel-home-tilde-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  try {
    os.homedir = () => root;
    process.env.AVERQEL_HOME = "~/application-data";
    process.env.AVERQEL_AGENTS_HOME = "~\\shared-agents";
    delete require.cache[modulePath];
    const { expandUserPath, getAgentsHome, getAverQelHome } = require(modulePath);
    assert.equal(getAverQelHome(), path.join(root, "application-data"));
    assert.equal(getAgentsHome(), path.join(root, "shared-agents"));
    assert.equal(expandUserPath("~"), root);
  } finally {
    os.homedir = originalHomedir;
    if (originalHome === undefined) delete process.env.AVERQEL_HOME;
    else process.env.AVERQEL_HOME = originalHome;
    if (originalAgents === undefined) delete process.env.AVERQEL_AGENTS_HOME;
    else process.env.AVERQEL_AGENTS_HOME = originalAgents;
    delete require.cache[modulePath];
  }
});

test("desktop roots default to separate home directories without creating shared agents", async (t) => {
  const originalHome = process.env.AVERQEL_HOME;
  const originalAgents = process.env.AVERQEL_AGENTS_HOME;
  const originalHomedir = os.homedir;
  const root = await mkdtemp(path.join(os.tmpdir(), "averqel-home-defaults-"));
  t.after(async () => {
    if (originalHome === undefined) delete process.env.AVERQEL_HOME;
    else process.env.AVERQEL_HOME = originalHome;
    if (originalAgents === undefined) delete process.env.AVERQEL_AGENTS_HOME;
    else process.env.AVERQEL_AGENTS_HOME = originalAgents;
    os.homedir = originalHomedir;
    delete require.cache[modulePath];
    await rm(root, { recursive: true, force: true });
  });

  delete process.env.AVERQEL_HOME;
  delete process.env.AVERQEL_AGENTS_HOME;
  os.homedir = () => root;
  delete require.cache[modulePath];
  const { getAgentsHome, getAverQelHome } = require(modulePath);
  assert.equal(getAverQelHome(), path.join(root, ".averqel-harness"));
  assert.equal(getAgentsHome(), path.join(root, ".agents"));
  assert.equal(require("node:fs").existsSync(path.join(root, ".agents")), false);
});
