"use strict";

/**
 * Pure builders for the Electron renderer context menu.
 *
 * Electron passes a `ContextMenuParams` object to the `context-menu` event.
 * This module turns those parameters into a template Electron can hand to
 * `Menu.buildFromTemplate`, without importing Electron itself so the menu
 * rules can be tested with plain Node.
 */

const MAX_ASK_SELECTION_LENGTH = 12_000;
const MAX_DICTIONARY_SUGGESTIONS = 5;

/** The renderer channel that receives the selected text from the menu. */
const ASK_DEEPSPACE_CHANNEL = "deepspace:ask-selection";

const IGNORED_SELECTION_KEYS = new Set(["", " "]);

/**
 * Tell whether a renderer URL is a DeepSpace view.
 *
 * The main process keeps the Ask DeepSeek item out of the menu unless the
 * renderer has reported a mounted composer and the current document is still
 * DeepSpace. This keeps a stale availability message from a previous page out
 * of the menu after a full navigation or reload.
 *
 * @param {unknown} rawUrl Renderer URL.
 * @returns {boolean} True for `/dashboard/deepspace` and its subpaths.
 */
function isDeepSpaceUrl(rawUrl) {
  if (typeof rawUrl !== "string" || rawUrl.length === 0) return false;
  try {
    const { pathname } = new URL(rawUrl);
    return (
      pathname === "/dashboard/deepspace" ||
      pathname.startsWith("/dashboard/deepspace/")
    );
  } catch {
    return false;
  }
}

/**
 * Normalize selected text before it leaves the native menu.
 *
 * @param {unknown} value Raw `params.selectionText`.
 * @param {number} [maxLength] Largest number of characters to keep.
 * @returns {string} Trimmed, newline-normalized text, or an empty string.
 */
function normalizeSelectionText(value, maxLength = MAX_ASK_SELECTION_LENGTH) {
  if (typeof value !== "string") return "";
  const normalized = value.replace(/\r\n?/g, "\n").replace(/\u0000/g, "").trim();
  if (IGNORED_SELECTION_KEYS.has(normalized)) return "";
  if (normalized.length <= maxLength) return normalized;
  return `${normalized.slice(0, Math.max(0, maxLength - 1)).trimEnd()}…`;
}

function addSeparator(template) {
  if (template.length === 0) return;
  if (template[template.length - 1]?.type === "separator") return;
  template.push({ type: "separator" });
}

function flagEnabled(flags, name) {
  return flags[name] !== false;
}

/**
 * Build the native right-click menu template.
 *
 * The template always ends with Reload and Force Reload. DevTools entries are
 * added only while the shell allows developer tools.
 *
 * @param {Record<string, unknown>} params Electron `ContextMenuParams`.
 * @param {{
 *   devToolsEnabled?: boolean,
 *   askDeepSeekLabel?: string,
 *   onAskDeepSeek?: (text: string) => void,
 *   onOpenLink?: (url: string) => void,
 *   onCopyText?: (text: string) => void,
 *   onCopyImage?: (x: number, y: number) => void,
 *   onSaveImage?: (url: string) => void,
 *   onInspectElement?: (x: number, y: number) => void,
 *   onToggleDevTools?: () => void,
 *   onReplaceMisspelling?: (word: string) => void,
 *   onAddToDictionary?: (word: string) => void,
 * }} [options]
 * @returns {Array<Record<string, unknown>>} Electron menu template entries.
 */
function buildContextMenuTemplate(params = {}, options = {}) {
  const template = [];
  const editFlags = params.editFlags || {};
  const selectionText = normalizeSelectionText(params.selectionText);
  const misspelledWord =
    typeof params.misspelledWord === "string" ? params.misspelledWord.trim() : "";
  const suggestions = Array.isArray(params.dictionarySuggestions)
    ? Array.from(
        new Set(
          params.dictionarySuggestions
            .filter((entry) => typeof entry === "string" && entry.trim().length > 0)
            .map((entry) => entry.trim()),
        ),
      ).slice(0, MAX_DICTIONARY_SUGGESTIONS)
    : [];

  if (misspelledWord && suggestions.length > 0) {
    for (const suggestion of suggestions) {
      template.push({
        label: suggestion,
        click: () => options.onReplaceMisspelling?.(suggestion),
      });
    }
  }
  if (misspelledWord && typeof options.onAddToDictionary === "function") {
    template.push({
      label: "Add to Dictionary",
      click: () => options.onAddToDictionary?.(misspelledWord),
    });
  }
  if (template.length > 0) addSeparator(template);

  if (selectionText && options.askDeepSeek !== false) {
    template.push({
      id: "deepspace-ask-deepseek",
      label: options.askDeepSeekLabel || "Ask DeepSeek",
      click: () => options.onAskDeepSeek?.(selectionText),
    });
    addSeparator(template);
  }

  if (params.isEditable) {
    template.push(
      {
        role: "undo",
        label: "Undo",
        enabled: flagEnabled(editFlags, "canUndo"),
      },
      {
        role: "redo",
        label: "Redo",
        enabled: flagEnabled(editFlags, "canRedo"),
      },
      { type: "separator" },
      {
        role: "cut",
        label: "Cut",
        enabled: flagEnabled(editFlags, "canCut"),
      },
      {
        role: "copy",
        label: "Copy",
        enabled: flagEnabled(editFlags, "canCopy"),
      },
      {
        role: "paste",
        label: "Paste",
        enabled: flagEnabled(editFlags, "canPaste"),
      },
      {
        role: "delete",
        label: "Delete",
        enabled: flagEnabled(editFlags, "canDelete"),
      },
      { type: "separator" },
      {
        role: "selectAll",
        label: "Select All",
        enabled: flagEnabled(editFlags, "canSelectAll"),
      },
    );
  } else if (selectionText) {
    template.push({
      role: "copy",
      label: "Copy",
      enabled: flagEnabled(editFlags, "canCopy"),
    });
  }

  if (typeof params.linkURL === "string" && params.linkURL.length > 0) {
    const hasLinkActions =
      typeof options.onOpenLink === "function" ||
      typeof options.onCopyText === "function";
    if (hasLinkActions) {
      addSeparator(template);
      if (typeof options.onOpenLink === "function") {
        template.push({
          label: "Open Link in Browser",
          click: () => options.onOpenLink?.(params.linkURL),
        });
      }
      if (typeof options.onCopyText === "function") {
        template.push({
          label: "Copy Link Address",
          click: () => options.onCopyText?.(params.linkURL),
        });
      }
    }
  }

  if (params.mediaType === "image") {
    const hasImageAddress =
      typeof params.srcURL === "string" && params.srcURL.length > 0;
    const hasImageActions =
      typeof options.onCopyImage === "function" ||
      (hasImageAddress &&
        (typeof options.onCopyText === "function" ||
          typeof options.onSaveImage === "function"));
    if (hasImageActions) {
      addSeparator(template);
      if (typeof options.onCopyImage === "function") {
        template.push({
          label: "Copy Image",
          click: () =>
            options.onCopyImage?.(Number(params.x) || 0, Number(params.y) || 0),
        });
      }
      if (hasImageAddress && typeof options.onCopyText === "function") {
        template.push({
          label: "Copy Image Address",
          click: () => options.onCopyText?.(params.srcURL),
        });
      }
      if (hasImageAddress && typeof options.onSaveImage === "function") {
        template.push({
          label: "Save Image As…",
          click: () => options.onSaveImage?.(params.srcURL),
        });
      }
    }
  }

  addSeparator(template);
  template.push(
    { role: "reload", label: "Reload" },
    { role: "forceReload", label: "Force Reload" },
  );

  if (options.devToolsEnabled) {
    addSeparator(template);
    template.push({
      label: "Inspect Element",
      click: () => options.onInspectElement?.(params.x, params.y),
    });
    template.push({
      label: "Toggle Developer Tools",
      click: () => options.onToggleDevTools?.(),
    });
  }

  return template;
}

module.exports = {
  ASK_DEEPSPACE_CHANNEL,
  MAX_ASK_SELECTION_LENGTH,
  buildContextMenuTemplate,
  isDeepSpaceUrl,
  normalizeSelectionText,
};
