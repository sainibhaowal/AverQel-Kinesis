"""Codex-style patch parsing and application.

Ported from @deepseek-ai/dsh-fs-patch. Parses multi-file patches in the
OpenAI Codex format and applies them to file contents.

Format:
    *** Begin Patch
    *** Add File: <path>
    +<line>
    ...
    *** Delete File: <path>
    *** Update File: <path>
    @@ <context>
    -<old line>
    +<new line>
    ...
    *** Move to: <new path>
    *** End Patch
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class PatchAction(Enum):
    ADD = "add"
    DELETE = "delete"
    UPDATE = "update"


@dataclass(frozen=True, slots=True)
class PatchHunk:
    """A single hunk within an update file."""

    old_lines: list[str] = field(default_factory=list)
    new_lines: list[str] = field(default_factory=list)
    context: str = ""


@dataclass(frozen=True, slots=True)
class PatchFile:
    """A file operation within a patch."""

    action: PatchAction
    path: str
    move_to: str | None = None
    hunks: list[PatchHunk] = field(default_factory=list)
    content: str = ""  # For add files


@dataclass(frozen=True, slots=True)
class ParseResult:
    """Result of parsing a patch."""

    ok: bool
    files: list[PatchFile] = field(default_factory=list)
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ApplyResult:
    """Result of applying a patch to a file."""

    ok: bool
    content: str = ""
    error: str | None = None


def _normalize_line_endings(text: str) -> str:
    """Normalize line endings to LF."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def parse_patch(patch_text: str) -> ParseResult:
    """Parse a Codex-style patch into a list of file operations.

    Args:
        patch_text: The patch text, potentially wrapped in *** Begin Patch / *** End Patch.

    Returns:
        ParseResult with ok=True and files list, or ok=False with error.
    """
    text = _normalize_line_endings(patch_text).strip()
    if not text:
        return ParseResult(ok=False, error="Empty patch")

    # Strip wrapper
    if text.startswith("*** Begin Patch"):
        text = text[len("*** Begin Patch") :].strip()
    if text.endswith("*** End Patch"):
        text = text[: -len("*** End Patch")].strip()

    files: list[PatchFile] = []
    current_file: PatchFile | None = None
    current_hunk: PatchHunk | None = None
    lines = text.split("\n")
    i = 0

    while i < len(lines):
        line = lines[i]

        if line.startswith("*** Add File:"):
            if current_file is not None:
                if current_hunk is not None:
                    current_file.hunks.append(current_hunk)
                    current_hunk = None
                files.append(current_file)
            path = line[len("*** Add File:") :].strip()
            current_file = PatchFile(action=PatchAction.ADD, path=path)
            current_hunk = None
            i += 1
            # Collect added lines
            added_lines: list[str] = []
            while i < len(lines) and not lines[i].startswith("***"):
                if lines[i].startswith("+"):
                    added_lines.append(lines[i][1:])
                elif lines[i].strip() == "":
                    added_lines.append("")
                i += 1
            current_file = PatchFile(
                action=PatchAction.ADD,
                path=path,
                content="\n".join(added_lines) + ("\n" if added_lines else ""),
            )
            continue

        elif line.startswith("*** Delete File:"):
            if current_file is not None:
                if current_hunk is not None:
                    current_file.hunks.append(current_hunk)
                    current_hunk = None
                files.append(current_file)
            path = line[len("*** Delete File:") :].strip()
            current_file = PatchFile(action=PatchAction.DELETE, path=path)
            current_hunk = None
            i += 1
            continue

        elif line.startswith("*** Update File:"):
            if current_file is not None:
                if current_hunk is not None:
                    current_file.hunks.append(current_hunk)
                    current_hunk = None
                files.append(current_file)
            path = line[len("*** Update File:") :].strip()
            current_file = PatchFile(action=PatchAction.UPDATE, path=path)
            current_hunk = None
            i += 1
            continue

        elif line.startswith("*** Move to:"):
            if current_file is not None:
                current_file = PatchFile(
                    action=current_file.action,
                    path=current_file.path,
                    move_to=line[len("*** Move to:") :].strip(),
                    hunks=current_file.hunks,
                    content=current_file.content,
                )
            i += 1
            continue

        elif line.startswith("@@"):
            if current_hunk is not None and current_file is not None:
                current_file.hunks.append(current_hunk)
            context = line[2:].strip()
            current_hunk = PatchHunk(context=context)
            i += 1
            continue

        elif current_hunk is not None:
            if line.startswith("-"):
                current_hunk.old_lines.append(line[1:])
            elif line.startswith("+"):
                current_hunk.new_lines.append(line[1:])
            elif line.startswith(" "):
                current_hunk.old_lines.append(line[1:])
                current_hunk.new_lines.append(line[1:])
            elif line.strip() == "":
                # Blank line in context
                current_hunk.old_lines.append("")
                current_hunk.new_lines.append("")
            i += 1
            continue

        else:
            i += 1
            continue

    # Finalize
    if current_file is not None:
        if current_hunk is not None:
            current_file.hunks.append(current_hunk)
        files.append(current_file)

    if not files:
        return ParseResult(ok=False, error="No file operations found in patch")

    return ParseResult(ok=True, files=files)


def _find_match(content_lines: list[str], old_lines: list[str], start: int = 0) -> int:
    """Find the starting index where old_lines match content_lines.

    Tries exact match first, then trailing-whitespace-trimmed, then both-ends
    trimmed, then Unicode punctuation normalization.
    """
    if not old_lines:
        return start

    # Pass 1: exact match
    for idx in range(start, len(content_lines) - len(old_lines) + 1):
        if content_lines[idx : idx + len(old_lines)] == old_lines:
            return idx

    # Pass 2: trailing whitespace trimmed
    trimmed_old = [line.rstrip() for line in old_lines]
    for idx in range(start, len(content_lines) - len(old_lines) + 1):
        if [line.rstrip() for line in content_lines[idx : idx + len(old_lines)]] == trimmed_old:
            return idx

    # Pass 3: both ends trimmed
    stripped_old = [line.strip() for line in old_lines]
    for idx in range(start, len(content_lines) - len(old_lines) + 1):
        if [line.strip() for line in content_lines[idx : idx + len(old_lines)]] == stripped_old:
            return idx

    # Pass 4: Unicode punctuation → ASCII
    def _normalize_unicode(s: str) -> str:
        return (
            s.replace("\u2018", "'")
            .replace("\u2019", "'")
            .replace("\u201c", '"')
            .replace("\u201d", '"')
            .replace("\u2014", "-")
            .replace("\u2013", "-")
        )

    norm_old = [_normalize_unicode(line) for line in old_lines]
    for idx in range(start, len(content_lines) - len(old_lines) + 1):
        if [
            _normalize_unicode(line) for line in content_lines[idx : idx + len(old_lines)]
        ] == norm_old:
            return idx

    return -1


def apply_hunks(content: str, hunks: list[PatchHunk]) -> ApplyResult:
    """Apply a list of hunks to file content.

    Args:
        content: The original file content.
        hunks: List of PatchHunk to apply.

    Returns:
        ApplyResult with ok=True and new content, or ok=False with error.
    """
    if not hunks:
        return ApplyResult(ok=True, content=content)

    content_lines = content.split("\n")
    # Track if original ended with newline
    trailing_newline = content.endswith("\n")
    if trailing_newline:
        content_lines = content_lines[:-1]

    result_lines = list(content_lines)
    offset = 0

    for hunk in hunks:
        if not hunk.old_lines and not hunk.new_lines:
            continue

        # Find match position
        match_idx = _find_match(result_lines, hunk.old_lines, offset)
        if match_idx == -1:
            # Try from beginning if not found from offset
            match_idx = _find_match(result_lines, hunk.old_lines, 0)
            if match_idx == -1:
                return ApplyResult(
                    ok=False,
                    error=f"Could not find context for hunk: {hunk.context or '(no context)'}",
                )

        # Apply replacement
        end_idx = match_idx + len(hunk.old_lines)
        result_lines[match_idx:end_idx] = hunk.new_lines
        offset = match_idx + len(hunk.new_lines)

    new_content = "\n".join(result_lines)
    if trailing_newline:
        new_content += "\n"

    return ApplyResult(ok=True, content=new_content)


def apply_patch_to_file(original_content: str, patch_file: PatchFile) -> ApplyResult:
    """Apply a single PatchFile to file content.

    Args:
        original_content: The original file content.
        patch_file: The PatchFile to apply.

    Returns:
        ApplyResult with ok=True and new content, or ok=False with error.
    """
    if patch_file.action == PatchAction.ADD:
        return ApplyResult(ok=True, content=patch_file.content)

    if patch_file.action == PatchAction.DELETE:
        return ApplyResult(ok=True, content="")

    if patch_file.action == PatchAction.UPDATE:
        return apply_hunks(original_content, patch_file.hunks)

    return ApplyResult(ok=False, error=f"Unknown action: {patch_file.action}")


__all__ = [
    "PatchAction",
    "PatchHunk",
    "PatchFile",
    "ParseResult",
    "ApplyResult",
    "parse_patch",
    "apply_hunks",
    "apply_patch_to_file",
]
