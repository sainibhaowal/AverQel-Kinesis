const assert = require("node:assert/strict");
const { mkdir, mkdtemp, rm, writeFile } = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const { createLocalFrontendServer } = require("../electron/local-frontend.cjs");

test("serves the packaged frontend locally with route fallback", async () => {
  const rootDirectory = await mkdtemp(
    path.join(os.tmpdir(), "averqel-desktop-frontend-"),
  );
  let server;
  try {
    await writeFile(
      path.join(rootDirectory, "index.html"),
      "<main>local app</main>",
    );
    await writeFile(
      path.join(rootDirectory, "dashboard.html"),
      "<main>dashboard</main>",
    );
    await mkdir(path.join(rootDirectory, "dashboard"));
    await writeFile(
      path.join(rootDirectory, "dashboard", "__next._tree.txt"),
      "tree",
    );
    await writeFile(
      path.join(rootDirectory, "404.html"),
      "<main>not found</main>",
    );
    await writeFile(
      path.join(rootDirectory, "app.js"),
      "console.log('local');",
    );

    server = await createLocalFrontendServer({ rootDirectory });

    const rootResponse = await fetch(server.url);
    assert.equal(rootResponse.status, 200);
    const contentSecurityPolicy =
      rootResponse.headers.get("content-security-policy") || "";
    assert.match(contentSecurityPolicy, /frame-ancestors 'none'/);
    assert.doesNotMatch(contentSecurityPolicy, /unsafe-eval/);
    assert.equal(rootResponse.headers.get("x-content-type-options"), "nosniff");
    assert.equal(await rootResponse.text(), "<main>local app</main>");

    const routeResponse = await fetch(`${server.url}/dashboard`);
    assert.equal(routeResponse.status, 200);
    assert.equal(await routeResponse.text(), "<main>dashboard</main>");

    const treeResponse = await fetch(
      `${server.url}/dashboard/__next._tree.txt?_rsc=smoke`,
    );
    assert.equal(treeResponse.status, 200);
    assert.equal(await treeResponse.text(), "tree");

    const assetResponse = await fetch(`${server.url}/app.js`);
    assert.equal(
      assetResponse.headers.get("content-type"),
      "text/javascript; charset=utf-8",
    );
    assert.equal(await assetResponse.text(), "console.log('local');");

    const traversalResponse = await fetch(
      `${server.url}/%2e%2e%2f%2e%2e%2fetc%2fpasswd`,
    );
    assert.equal(traversalResponse.status, 404);
  } finally {
    await server?.close();
    await rm(rootDirectory, { recursive: true, force: true });
  }
});
