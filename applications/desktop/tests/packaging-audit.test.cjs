const assert = require("node:assert/strict");
const { mkdtempSync, mkdirSync, writeFileSync } = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

test("desktop backend bundle audit rejects model weights", async () => {
  const { auditBackendBundle, bundledModelWeights } = await import(
    "../scripts/audit-backend-bundle.mjs"
  );
  const root = mkdtempSync(path.join(os.tmpdir(), "averqel-bundle-audit-"));
  mkdirSync(path.join(root, "_internal"), { recursive: true });
  const model = path.join(root, "_internal", "model.safetensors");
  writeFileSync(model, "not a real model");

  assert.deepEqual(bundledModelWeights(root), [model]);
  assert.throws(() => auditBackendBundle(root), /model weights/);
});

test("desktop backend bundle audit allows runtime test fixtures", async () => {
  const { auditBackendBundle, bundledModelWeights } = await import(
    "../scripts/audit-backend-bundle.mjs"
  );
  const root = mkdtempSync(path.join(os.tmpdir(), "averqel-bundle-audit-"));
  const fixture = path.join(root, "_internal", "onnxruntime", "datasets");
  mkdirSync(fixture, { recursive: true });
  writeFileSync(path.join(fixture, "logreg_iris.onnx"), "fixture");

  assert.deepEqual(bundledModelWeights(root), []);
  assert.doesNotThrow(() => auditBackendBundle(root));
});
