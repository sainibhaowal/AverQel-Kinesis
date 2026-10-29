const assert = require("node:assert/strict");
const test = require("node:test");

const { focusWindow } = require("../electron/window-lifecycle.cjs");

test("focusWindow restores, shows, and focuses one existing window", () => {
  const calls = [];
  const window = {
    isDestroyed: () => false,
    isMinimized: () => true,
    restore: () => calls.push("restore"),
    show: () => calls.push("show"),
    focus: () => calls.push("focus"),
  };

  assert.equal(focusWindow(window), true);
  assert.deepEqual(calls, ["restore", "show", "focus"]);
});

test("focusWindow does not touch a destroyed window", () => {
  const calls = [];
  const window = {
    isDestroyed: () => true,
    isMinimized: () => calls.push("is-minimized"),
    restore: () => calls.push("restore"),
    show: () => calls.push("show"),
    focus: () => calls.push("focus"),
  };

  assert.equal(focusWindow(window), false);
  assert.deepEqual(calls, []);
});
