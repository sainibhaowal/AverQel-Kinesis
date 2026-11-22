"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");

const {
  MAX_ASK_SELECTION_LENGTH,
  buildContextMenuTemplate,
  isDeepSpaceUrl,
  normalizeSelectionText,
} = require("../electron/context-menu.cjs");

function labels(template) {
  return template
    .filter((item) => item.type !== "separator")
    .map((item) => item.label || item.role);
}

function itemByLabel(template, label) {
  return template.find((item) => item.label === label);
}

function assertReloadEntries(template, startIndex = 0) {
  const reloadItems = template
    .slice(startIndex)
    .filter((item) => item.role === "reload" || item.role === "forceReload");
  assert.deepEqual(
    reloadItems.map((item) => [item.role, item.label]),
    [
      ["reload", "Reload"],
      ["forceReload", "Force Reload"],
    ],
  );
}

test("normalizes selected text without losing line breaks", () => {
  assert.equal(normalizeSelectionText("  one\r\ntwo  "), "one\ntwo");
  assert.equal(normalizeSelectionText(""), "");
  assert.equal(normalizeSelectionText(null), "");
  assert.equal(normalizeSelectionText("   "), "");
});

test("caps very long selected text", () => {
  const normalized = normalizeSelectionText("x".repeat(MAX_ASK_SELECTION_LENGTH + 50));
  assert.equal(normalized.length, MAX_ASK_SELECTION_LENGTH);
  assert.ok(normalized.endsWith("…"));
});

test("recognizes only DeepSpace renderer documents", () => {
  assert.equal(
    isDeepSpaceUrl("http://127.0.0.1:11030/dashboard/deepspace"),
    true,
  );
  assert.equal(
    isDeepSpaceUrl("http://127.0.0.1:11030/dashboard/deepspace/chats/1?x=1"),
    true,
  );
  assert.equal(
    isDeepSpaceUrl("http://127.0.0.1:11030/dashboard/documentation/deepspace"),
    false,
  );
  assert.equal(
    isDeepSpaceUrl("http://127.0.0.1:11030/dashboard/deepspaces"),
    false,
  );
  assert.equal(isDeepSpaceUrl("not a url"), false);
  assert.equal(isDeepSpaceUrl(undefined), false);
});

test("an empty context still offers Reload and Force Reload", () => {
  const template = buildContextMenuTemplate({});
  assert.deepEqual(labels(template), ["Reload", "Force Reload"]);
  assertReloadEntries(template);
});

test("an editable field offers undo, redo, cut, copy, paste, delete, and select all", () => {
  const template = buildContextMenuTemplate({
    isEditable: true,
    editFlags: {
      canUndo: true,
      canRedo: false,
      canCut: true,
      canCopy: false,
      canPaste: true,
      canDelete: true,
      canSelectAll: true,
    },
  });

  const byRole = new Map(
    template.filter((item) => item.role).map((item) => [item.role, item]),
  );
  assert.equal(byRole.get("undo").enabled, true);
  assert.equal(byRole.get("redo").enabled, false);
  assert.equal(byRole.get("cut").enabled, true);
  assert.equal(byRole.get("copy").enabled, false);
  assert.equal(byRole.get("paste").enabled, true);
  assert.equal(byRole.get("delete").enabled, true);
  assert.equal(byRole.get("selectAll").enabled, true);
  assert.ok(!labels(template).includes("Ask DeepSeek"));
  assertReloadEntries(template);
});

test("selected text adds Ask DeepSeek and forwards the normalized selection", () => {
  const calls = [];
  const template = buildContextMenuTemplate(
    {
      isEditable: false,
      selectionText: "  first line\r\nsecond line  ",
      editFlags: { canCopy: true },
    },
    {
      onAskDeepSeek: (text) => calls.push(text),
    },
  );

  const ask = itemByLabel(template, "Ask DeepSeek");
  assert.ok(ask, "Ask DeepSeek should be in the menu");
  assert.ok(labels(template).indexOf("Ask DeepSeek") < labels(template).indexOf("Copy"));
  ask.click();
  assert.deepEqual(calls, ["first line\nsecond line"]);
  assert.ok(itemByLabel(template, "Copy"));
  assertReloadEntries(template);
});

test("Ask DeepSeek can be disabled and is absent without a selection", () => {
  const template = buildContextMenuTemplate(
    { isEditable: true, selectionText: "selected" },
    { askDeepSeek: false },
  );
  assert.equal(itemByLabel(template, "Ask DeepSeek"), undefined);

  const noSelection = buildContextMenuTemplate({ isEditable: true });
  assert.equal(itemByLabel(noSelection, "Ask DeepSeek"), undefined);
});

test("links offer open and copy actions", () => {
  const opened = [];
  const copied = [];
  const template = buildContextMenuTemplate(
    { linkURL: "https://example.com/docs" },
    {
      onOpenLink: (url) => opened.push(url),
      onCopyText: (text) => copied.push(text),
    },
  );

  itemByLabel(template, "Open Link in Browser").click();
  itemByLabel(template, "Copy Link Address").click();
  assert.deepEqual(opened, ["https://example.com/docs"]);
  assert.deepEqual(copied, ["https://example.com/docs"]);
  assertReloadEntries(template);
});

test("images offer copy, copy address, and save actions", () => {
  const copiedImages = [];
  const copiedText = [];
  const saved = [];
  const template = buildContextMenuTemplate(
    {
      mediaType: "image",
      srcURL: "https://example.com/image.png",
      x: 12,
      y: 34,
    },
    {
      onCopyImage: (x, y) => copiedImages.push([x, y]),
      onCopyText: (text) => copiedText.push(text),
      onSaveImage: (url) => saved.push(url),
    },
  );

  itemByLabel(template, "Copy Image").click();
  itemByLabel(template, "Copy Image Address").click();
  itemByLabel(template, "Save Image As…").click();
  assert.deepEqual(copiedImages, [[12, 34]]);
  assert.deepEqual(copiedText, ["https://example.com/image.png"]);
  assert.deepEqual(saved, ["https://example.com/image.png"]);
  assertReloadEntries(template);
});

test("links and images do not add an empty separator without actions", () => {
  const linkTemplate = buildContextMenuTemplate({
    linkURL: "https://example.com/docs",
  });
  assert.deepEqual(labels(linkTemplate), ["Reload", "Force Reload"]);

  const imageTemplate = buildContextMenuTemplate({
    mediaType: "image",
    srcURL: "https://example.com/image.png",
  });
  assert.deepEqual(labels(imageTemplate), ["Reload", "Force Reload"]);
  assert.deepEqual(
    imageTemplate.filter((item) => item.type === "separator"),
    [],
  );
});

test("spelling suggestions replace the misspelled word and can be added to the dictionary", () => {
  const replaced = [];
  const added = [];
  const template = buildContextMenuTemplate(
    {
      misspelledWord: "deepsapce",
      dictionarySuggestions: ["deepspace", "deep space", "", 42, "deep space"],
    },
    {
      onReplaceMisspelling: (word) => replaced.push(word),
      onAddToDictionary: (word) => added.push(word),
    },
  );

  assert.deepEqual(
    template.slice(0, 3).map((item) => item.label),
    ["deepspace", "deep space", "Add to Dictionary"],
  );
  itemByLabel(template, "deepspace").click();
  itemByLabel(template, "Add to Dictionary").click();
  assert.deepEqual(replaced, ["deepspace"]);
  assert.deepEqual(added, ["deepsapce"]);
  assertReloadEntries(template);
});

test("developer tools appear only when enabled", () => {
  const inspected = [];
  let toggles = 0;
  const template = buildContextMenuTemplate(
    { x: 5, y: 9 },
    {
      devToolsEnabled: true,
      onInspectElement: (x, y) => inspected.push([x, y]),
      onToggleDevTools: () => {
        toggles += 1;
      },
    },
  );

  itemByLabel(template, "Inspect Element").click();
  itemByLabel(template, "Toggle Developer Tools").click();
  assert.deepEqual(inspected, [[5, 9]]);
  assert.equal(toggles, 1);
  assertReloadEntries(template);

  const withoutDevTools = buildContextMenuTemplate({}, { devToolsEnabled: false });
  assert.equal(itemByLabel(withoutDevTools, "Inspect Element"), undefined);
});
