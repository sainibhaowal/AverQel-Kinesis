const assert = require("node:assert/strict");
const test = require("node:test");

const { getWindowGeometry } = require("../electron/window-geometry.cjs");

test("fits and centers the shell inside a compact display work area", () => {
  assert.deepEqual(
    getWindowGeometry({ x: 0, y: 0, width: 1024, height: 768 }),
    {
      width: 976,
      height: 720,
      minWidth: 960,
      minHeight: 640,
      x: 24,
      y: 24,
    },
  );
});

test("keeps the preferred shell size centered on a larger display", () => {
  assert.deepEqual(
    getWindowGeometry({ x: 1920, y: 0, width: 1920, height: 1080 }),
    {
      width: 1280,
      height: 800,
      minWidth: 960,
      minHeight: 640,
      x: 2240,
      y: 140,
    },
  );
});
