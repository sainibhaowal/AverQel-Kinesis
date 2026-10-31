/**
 * Resolves and initialises the per-user `.averqel-harness` home directory for
 * the Kinesis desktop application.
 *
 * The directory is created the first time `getAverQelHome()` is called.  It is
 * placed at `<os-home>/.averqel-harness` on every platform and exposed to the backend
 * subprocess via the `AVERQEL_HOME` environment variable so that all databases,
 * blobs, cache artefacts, and encrypted secrets are stored in one predictable
 * location — similar to how other tools use `.codex`, `.ssh`, or `.config`.
 *
 * Sub-directory layout:
 *   db/       SQLite databases (knowledge.db, averqel.db)
 *   storage/  User-uploaded file blobs
 *   cache/    Transient artefacts (embedding cache, tmp chunks)
 *   secrets/  Encrypted credential store (never plaintext)
 *   mcp/      MCP server metadata and configuration cache
 *   logs/     Application log files
 *   exports/  User-initiated export archives
 */

"use strict";

const { mkdirSync } = require("node:fs");
const path = require("node:path");
const os = require("node:os");

const SUBDIRS = [
  "db",
  "storage",
  "cache",
  "secrets",
  "mcp",
  "logs",
  "exports",
  "models",
  "keys",
  "workspaces",
  "electron",
  "searxng",
];

/** @type {string | undefined} */
let _cachedHome;

/**
 * Resolve, create, and return the `.averqel-harness` home directory path.
 *
 * Resolution order (first set value wins):
 *  1. `AVERQEL_HOME` environment variable
 *  2. `~/.averqel-harness`
 *
 * @returns {string} Absolute path to the `.averqel-harness` home directory.
 */
function getAverQelHome() {
  if (_cachedHome !== undefined) return _cachedHome;

  const fromEnv = (process.env.AVERQEL_HOME || "").trim();
  const home = fromEnv || path.join(os.homedir(), ".averqel-harness");

  // Create base directory and all sub-directories idempotently.
  mkdirSync(home, { recursive: true, mode: 0o700 });
  for (const sub of SUBDIRS) {
    mkdirSync(path.join(home, sub), { recursive: true, mode: 0o700 });
  }

  _cachedHome = home;
  return home;
}

/**
 * Build an environment object that injects `AVERQEL_HOME` into a subprocess.
 *
 * Pass the result to `spawn()` as the `env` option together with any other
 * environment variables the subprocess needs.
 *
 * @param {Record<string, string>} baseEnv - Existing environment to extend.
 * @returns {Record<string, string>} New environment with `AVERQEL_HOME` set.
 */
function injectAverQelHome(baseEnv) {
  return { ...baseEnv, AVERQEL_HOME: getAverQelHome() };
}

module.exports = { getAverQelHome, injectAverQelHome };
