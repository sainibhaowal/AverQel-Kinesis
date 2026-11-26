export type LibraryFileKind =
  | "markdown"
  | "code"
  | "diff"
  | "csv"
  | "spreadsheet"
  | "pdf"
  | "docx"
  | "pptx"
  | "image"
  | "svg"
  | "video"
  | "audio"
  | "archive"
  | "text";

function extensionFor(name: string) {
  return name.split(".").pop()?.toLowerCase() ?? "";
}

const LIBREOFFICE_PAGE_EXTENSIONS = new Set([
  "doc",
  "docx",
  "docm",
  "dot",
  "dotx",
  "dotm",
  "odt",
  "ott",
  "odm",
  "oth",
  "rtf",
  "ppt",
  "pptx",
  "pptm",
  "pot",
  "potx",
  "potm",
  "pps",
  "ppsx",
  "ppsm",
  "odp",
  "otp",
  "xls",
  "xlsx",
  "xlsm",
  "xlt",
  "xltx",
  "xltm",
  "xlm",
  "xla",
  "xlw",
  "xlsb",
  "ods",
  "ots",
  "odg",
  "otg",
  "odc",
  "otc",
  "odf",
  "otf",
  "odi",
  "oti",
]);

export function libraryFileSupportsServerPagePreview(name: string, contentType: string) {
  const extension = extensionFor(name);
  const normalized = contentType.toLowerCase().split(";", 1)[0];
  return (
    extension === "pdf" ||
    normalized === "application/pdf" ||
    LIBREOFFICE_PAGE_EXTENSIONS.has(extension)
  );
}

const CODE_EXTENSIONS = new Set([
  "py",
  "ts",
  "tsx",
  "js",
  "jsx",
  "mjs",
  "cjs",
  "java",
  "c",
  "h",
  "cc",
  "cpp",
  "cxx",
  "hpp",
  "go",
  "rs",
  "sql",
  "yaml",
  "yml",
  "xml",
  "html",
  "htm",
  "css",
  "scss",
  "sh",
  "bash",
  "toml",
  "ini",
  "env",
  "graphql",
  "vue",
  "svelte",
]);

export function libraryFileKind(name: string, contentType: string): LibraryFileKind {
  const extension = extensionFor(name);
  const normalized = contentType.toLowerCase().split(";", 1)[0];
  if (normalized === "text/markdown" || ["md", "mdx"].includes(extension)) return "markdown";
  if (extension === "diff" || extension === "patch" || normalized === "text/x-diff") return "diff";
  if (
    ["csv", "tsv"].includes(extension) ||
    ["text/csv", "text/x-csv", "text/tab-separated-values"].includes(normalized)
  ) {
    return "csv";
  }
  if (
    [
      "xlsx",
      "xls",
      "xlsm",
      "xlt",
      "xltx",
      "xltm",
      "xlm",
      "xla",
      "xlw",
      "xlsb",
      "ods",
      "ots",
    ].includes(extension) ||
    normalized.includes("spreadsheet") ||
    normalized === "application/vnd.ms-excel"
  ) {
    return "spreadsheet";
  }
  if (extension === "pdf" || normalized === "application/pdf") return "pdf";
  if (
    ["doc", "docx", "docm", "dot", "dotx", "dotm", "odt", "ott", "odm", "oth", "rtf"].includes(
      extension,
    ) ||
    normalized.includes("wordprocessingml") ||
    normalized === "application/msword" ||
    normalized === "application/vnd.oasis.opendocument.text" ||
    normalized === "application/rtf" ||
    normalized === "text/rtf"
  )
    return "docx";
  if (
    ["ppt", "pptx", "pptm", "pot", "potx", "potm", "pps", "ppsx", "ppsm", "odp", "otp"].includes(
      extension,
    ) ||
    normalized.includes("presentationml") ||
    normalized.includes("powerpoint")
  )
    return "pptx";
  if (extension === "svg" || normalized === "image/svg+xml") return "svg";
  if (
    ["png", "jpg", "jpeg", "webp", "avif", "gif", "bmp", "ico"].includes(extension) ||
    [
      "image/png",
      "image/jpeg",
      "image/webp",
      "image/avif",
      "image/gif",
      "image/bmp",
      "image/x-icon",
      "image/vnd.microsoft.icon",
    ].includes(normalized)
  ) {
    return "image";
  }
  if (normalized.startsWith("video/") || ["mp4", "webm", "mov", "m4v"].includes(extension)) {
    return "video";
  }
  if (normalized.startsWith("audio/") || ["mp3", "wav", "ogg", "m4a", "flac"].includes(extension)) {
    return "audio";
  }
  if (
    ["zip", "tar", "gz", "tgz", "7z", "rar"].includes(extension) ||
    normalized.includes("zip") ||
    normalized.includes("tar") ||
    normalized.includes("gzip") ||
    normalized.includes("7z") ||
    normalized.includes("rar")
  )
    return "archive";
  // A MIME type of text/plain is not source code.  Keep source extensions in
  // the editor-only code mode, but give ordinary TXT/log files their split
  // edit + readable preview workspace.
  if (CODE_EXTENSIONS.has(extension)) return "code";
  if (normalized.startsWith("text/")) return "text";
  return "text";
}

export function libraryKindSupportsEditor(kind: LibraryFileKind) {
  return !["pdf", "image", "svg", "video", "audio", "archive"].includes(kind);
}

export function libraryKindSupportsPreview(kind: LibraryFileKind) {
  return kind !== "code";
}

/** Format byte counts concisely for workspace file chips and side-panel headers. */
export function formatWorkspaceBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(bytes < 10 * 1024 ? 1 : 0)} KB`;
  if (bytes < 1024 * 1024 * 1024) {
    const mb = bytes / (1024 * 1024);
    return `${mb < 10 ? mb.toFixed(1) : Math.round(mb)} MB`;
  }
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(1)} GB`;
}

/** Return a short format tag and high-contrast badge palette for any workspace file. */
export function workspaceFileBadge(
  name: string,
  mimeType = "application/octet-stream",
): { label: string; badgeClass: string } {
  const ext = extensionFor(name);
  const kind = libraryFileKind(name, mimeType);
  const label = (ext || kind || "FILE").slice(0, 5).toUpperCase();
  if (kind === "pdf") {
    return {
      label: "PDF",
      badgeClass:
        "border-rose-500/35 bg-rose-500/12 text-rose-700 dark:border-rose-400/35 dark:bg-rose-400/15 dark:text-rose-300",
    };
  }
  if (kind === "svg" || kind === "image" || ["ppm", "pgm", "pbm", "bmp", "ico"].includes(ext)) {
    return {
      label: ext === "svg" ? "SVG" : label,
      badgeClass:
        "border-amber-500/35 bg-amber-500/12 text-amber-800 dark:border-amber-400/35 dark:bg-amber-400/15 dark:text-amber-300",
    };
  }
  if (ext === "html" || ext === "htm") {
    return {
      label: "HTML",
      badgeClass:
        "border-cyan-500/35 bg-cyan-500/12 text-cyan-800 dark:border-cyan-400/35 dark:bg-cyan-400/15 dark:text-cyan-300",
    };
  }
  if (kind === "spreadsheet" || kind === "csv") {
    return {
      label,
      badgeClass:
        "border-emerald-500/35 bg-emerald-500/12 text-emerald-800 dark:border-emerald-400/35 dark:bg-emerald-400/15 dark:text-emerald-300",
    };
  }
  if (kind === "docx" || kind === "pptx" || kind === "markdown") {
    return {
      label,
      badgeClass:
        "border-indigo-500/35 bg-indigo-500/12 text-indigo-800 dark:border-indigo-400/35 dark:bg-indigo-400/15 dark:text-indigo-300",
    };
  }
  if (kind === "video" || kind === "audio" || kind === "archive") {
    return {
      label,
      badgeClass:
        "border-violet-500/35 bg-violet-500/12 text-violet-800 dark:border-violet-400/35 dark:bg-violet-400/15 dark:text-violet-300",
    };
  }
  if (kind === "code") {
    return {
      label,
      badgeClass:
        "border-teal-600/30 bg-teal-500/10 text-teal-800 dark:border-teal-400/30 dark:bg-teal-400/12 dark:text-teal-300",
    };
  }
  return {
    label,
    badgeClass:
      "border-slate-400/35 bg-slate-500/10 text-slate-700 dark:border-white/15 dark:bg-white/10 dark:text-slate-300",
  };
}
