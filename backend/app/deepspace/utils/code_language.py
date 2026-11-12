"""Code language detection for DeepSpace.

Provides a single authoritative file-extension → syntax-highlighting language
table shared by all code surfaces: the read tool's persisted `lang` hint,
document preview, diff review, and LSP configuration. Matches the semantics
of @deepseek-ai/dsh-util-code-language.

Language IDs are the grammar IDs the highlighter's alias table resolves,
so a language returned here reaches the highlighter unchanged.

Usage:
    from app.deepspace.utils.code_language import (
        language_for_path,
        read_lang_hint_for_path,
        CODE_HIGHLIGHT_EXTENSIONS,
        EXTENSION_TO_LANGUAGE,
    )

    language_for_path("main.py")        # "python"
    language_for_path("script.ts")      # "typescript"
    language_for_path("config.json")    # "json"

    read_lang_hint_for_path("main.py")  # "py"
    read_lang_hint_for_path("script.ts")# "ts"
    read_lang_hint_for_path("config.json") # "json"
"""

from __future__ import annotations

from pathlib import Path

__all__ = [
    "language_for_path",
    "read_lang_hint_for_path",
    "CODE_HIGHLIGHT_EXTENSIONS",
    "EXTENSION_TO_LANGUAGE",
    "LANGUAGE_EXTENSIONS",
    "SHORT_BY_LANGUAGE",
    "READ_LANG_BY_EXTENSION",
]


# Canonical language ID → list of lowercase extensions (without dot)
# Covers common source, config, script, data, and markup extensions.
# Not an exhaustive linguist registry — only extensions where highlighting helps.
LANGUAGE_EXTENSIONS: dict[str, tuple[str, ...]] = {
    # Web / JS / TS
    "typescript": ("ts", "tsx", "mts", "cts"),
    "javascript": ("js", "jsx", "mjs", "cjs"),
    "json": ("json", "jsonc", "jsonl", "ndjson", "ipynb"),
    # Python
    "python": ("py", "pyw", "pyi"),
    # Systems
    "rust": ("rs",),
    "go": ("go",),
    "c": ("c", "h"),
    "cpp": ("cc", "cpp", "cxx", "hh", "hpp", "hxx"),
    "csharp": ("cs",),
    "java": ("java",),
    "kotlin": ("kt", "kts"),
    "swift": ("swift",),
    # Scripting
    "shellscript": ("sh", "bash", "zsh"),
    "fish": ("fish",),
    "powershell": ("ps1", "psm1", "psd1"),
    "bat": ("bat", "cmd"),
    "perl": ("pl", "pm"),
    "ruby": ("rb", "rake", "gemspec"),
    "php": ("php",),
    "lua": ("lua",),
    "r": ("r",),
    "julia": ("jl",),
    # Data / Config
    "yaml": ("yaml", "yml"),
    "toml": ("toml",),
    "ini": ("ini", "conf", "cfg", "properties"),
    "dotenv": ("env",),
    "csv": ("csv",),
    "sql": ("sql",),
    "xml": ("xml", "xsd", "xsl", "xslt", "plist", "svg"),
    "hcl": ("tf", "tfvars", "hcl"),
    "nix": ("nix",),
    # Markup / Docs
    "markdown": ("md", "markdown"),
    "mdx": ("mdx",),
    "html": ("html", "htm", "xhtml"),
    "css": ("css",),
    "scss": ("scss",),
    "less": ("less",),
    "rst": ("rst",),
    "latex": ("tex", "sty", "cls"),
    "bibtex": ("bib",),
    "asciidoc": ("adoc",),
    # Diff / Patch
    "diff": ("diff", "patch"),
    # Build
    "make": ("makefile", "mk"),
    "cmake": ("cmake",),
    "gradle": ("gradle",),
    "groovy": ("groovy",),
    # Frontend frameworks
    "vue": ("vue",),
    "svelte": ("svelte",),
    # Functional
    "haskell": ("hs",),
    "fsharp": ("fs", "fsi", "fsx"),
    "clojure": ("clj", "cljs", "edn"),
    "erlang": ("erl", "hrl"),
    "elixir": ("ex", "exs"),
    "scala": ("scala",),
    "ocaml": ("ml", "mli"),
    # Other
    "dart": ("dart",),
    "graphql": ("graphql", "gql"),
    "proto": ("proto",),
    "verilog": ("v",),
    "system-verilog": ("sv", "svh"),
    "vb": ("vb",),
    "log": ("log",),
    "http": ("http",),
}

# Lowercase extension → canonical language ID
# Use a dict for O(1) lookup; keys are lowercase extensions without dot.
EXTENSION_TO_LANGUAGE: dict[str, str] = {}
for lang, exts in LANGUAGE_EXTENSIONS.items():
    for ext in exts:
        EXTENSION_TO_LANGUAGE[ext.lower()] = lang

# All recognized extensions (for document preview registry)
CODE_HIGHLIGHT_EXTENSIONS: tuple[str, ...] = tuple(sorted(EXTENSION_TO_LANGUAGE.keys()))


# Persisted `lang` hint for the read card, keyed by canonical language ID.
# Where the suffix names itself better than the language does, that suffix
# is used (e.g., "tf" for HCL, "gradle" for Groovy).
SHORT_BY_LANGUAGE: dict[str, str] = {
    "typescript": "ts",
    "javascript": "js",
    "shellscript": "sh",
    "fish": "fish",
    "json": "json",
    "csv": "csv",
    "python": "py",
    "ruby": "rb",
    "go": "go",
    "rust": "rs",
    "java": "java",
    "c": "c",
    "cpp": "cpp",
    "csharp": "cs",
    "kotlin": "kotlin",
    "swift": "swift",
    "php": "php",
    "yaml": "yaml",
    "toml": "toml",
    "ini": "ini",
    "dotenv": "env",
    "log": "log",
    "diff": "diff",
    "http": "http",
    "markdown": "md",
    "mdx": "mdx",
    "rst": "rst",
    "latex": "tex",
    "bibtex": "bib",
    "asciidoc": "adoc",
    "html": "html",
    "css": "css",
    "scss": "scss",
    "less": "less",
    "sql": "sql",
    "xml": "xml",
    "lua": "lua",
    "bat": "bat",
    "powershell": "ps1",
    "r": "r",
    "julia": "jl",
    "dart": "dart",
    "scala": "scala",
    "clojure": "clj",
    "erlang": "erl",
    "elixir": "ex",
    "haskell": "hs",
    "fsharp": "fs",
    "vb": "vb",
    "perl": "pl",
    "verilog": "v",
    "system-verilog": "sv",
    "graphql": "graphql",
    "proto": "proto",
    "hcl": "hcl",
    "nix": "nix",
    "vue": "vue",
    "svelte": "svelte",
    "make": "make",
    "cmake": "cmake",
    "groovy": "groovy",
    "gradle": "gradle",
    "ocaml": "ml",
}

# Overrides where the extension names itself better than the language short name.
# These are the exact strings persisted in recorded sessions.
READ_LANG_BY_EXTENSION: dict[str, str] = {
    "tsx": "tsx",
    "jsx": "jsx",
    "tf": "tf",
    "tfvars": "tfvars",
    "gradle": "gradle",
    "ipynb": "ipynb",
    "jsonc": "jsonc",
    "jsonl": "jsonl",
    "ndjson": "ndjson",
    "pyi": "pyi",
    "mts": "mts",
    "cts": "cts",
    "mjs": "mjs",
    "cjs": "cjs",
}


def _extension_for_path(path: str) -> str | None:
    """Extract the lowercase extension from a path (without the dot)."""
    # Handle both path separators
    slash = max(path.rfind("/"), path.rfind("\\"))
    base = path[slash + 1 :]
    dot = base.rfind(".")
    if dot < 0:
        return None
    ext = base[dot + 1 :].lower()
    return ext if ext else None


def language_for_path(path: str | Path) -> str | None:
    """Derive the syntax-highlighting language from a filename or path.

    The extension is the text after the last dot of the final path segment.
    A leading dot is still the separator (so `.env` → `dotenv`).
    Both path separators are recognized.

    Args:
        path: Decoded filename or path.

    Returns:
        The canonical language ID, or None for unrecognized/absent suffix.
    """
    path_str = str(path)
    ext = _extension_for_path(path_str)
    if ext is None:
        return None
    return EXTENSION_TO_LANGUAGE.get(ext)


def read_lang_hint_for_path(path: str | Path) -> str | None:
    """Derive the Host read card's persisted `lang` hint from a path's extension.

    The value is the language's short name; for some languages that name
    is also the grammar ID (kotlin, swift, java, yaml, json). READ_LANG_BY_EXTENSION
    overrides it where the suffix names itself better (tsx, tf, gradle).

    Args:
        path: The model-facing path the read reported.

    Returns:
        The persisted language hint, or None when the extension maps to none.
    """
    path_str = str(path)
    ext = _extension_for_path(path_str)
    if ext is None:
        return None
    canonical = EXTENSION_TO_LANGUAGE.get(ext)
    if canonical is None:
        return None
    return READ_LANG_BY_EXTENSION.get(ext, SHORT_BY_LANGUAGE[canonical])


def get_language_extensions(language: str) -> tuple[str, ...]:
    """Get all extensions for a given canonical language ID."""
    return LANGUAGE_EXTENSIONS.get(language, ())


def is_recognized_extension(ext: str) -> bool:
    """Check if an extension (without dot) is recognized."""
    return ext.lower() in EXTENSION_TO_LANGUAGE


def get_all_languages() -> tuple[str, ...]:
    """Get all canonical language IDs."""
    return tuple(sorted(LANGUAGE_EXTENSIONS.keys()))


# For LSP configuration generation
def build_lsp_extension_map() -> dict[str, str]:
    """Build the extension → language map for LSP config (with dot prefix)."""
    return {f".{ext}": lang for ext, lang in EXTENSION_TO_LANGUAGE.items()}


# Common file patterns for specific tools
COMMON_CONFIG_EXTENSIONS = frozenset(
    {
        "json",
        "jsonc",
        "yaml",
        "yml",
        "toml",
        "ini",
        "conf",
        "cfg",
        "properties",
        "env",
        "tf",
        "tfvars",
        "hcl",
        "nix",
    }
)

COMMON_SOURCE_EXTENSIONS = frozenset(
    {
        "py",
        "pyw",
        "pyi",
        "rs",
        "go",
        "js",
        "jsx",
        "ts",
        "tsx",
        "java",
        "kt",
        "kts",
        "swift",
        "cs",
        "cpp",
        "cc",
        "cxx",
        "c",
        "h",
        "hpp",
        "hh",
        "rb",
        "php",
        "pl",
        "pm",
        "lua",
        "sh",
        "bash",
        "zsh",
        "fish",
        "ps1",
        "bat",
        "cmd",
        "r",
        "jl",
        "dart",
        "scala",
        "clj",
        "cljs",
        "ex",
        "exs",
        "erl",
        "hrl",
        "hs",
        "fs",
        "fsi",
        "fsx",
        "ml",
        "mli",
        "vue",
        "svelte",
        "graphql",
        "gql",
        "proto",
    }
)

COMMON_MARKUP_EXTENSIONS = frozenset(
    {
        "md",
        "markdown",
        "mdx",
        "html",
        "htm",
        "xhtml",
        "xml",
        "css",
        "scss",
        "less",
        "rst",
        "tex",
        "sty",
        "cls",
        "bib",
        "adoc",
    }
)


__all__ = [
    "language_for_path",
    "read_lang_hint_for_path",
    "CODE_HIGHLIGHT_EXTENSIONS",
    "EXTENSION_TO_LANGUAGE",
    "LANGUAGE_EXTENSIONS",
    "SHORT_BY_LANGUAGE",
    "READ_LANG_BY_EXTENSION",
    "get_language_extensions",
    "is_recognized_extension",
    "get_all_languages",
    "build_lsp_extension_map",
    "COMMON_CONFIG_EXTENSIONS",
    "COMMON_SOURCE_EXTENSIONS",
    "COMMON_MARKUP_EXTENSIONS",
]
