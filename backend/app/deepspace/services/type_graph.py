"""The type graph of a workspace.

A type graph is the map of what a codebase defines and what refers to
what: the classes, functions, methods, interfaces, and types a workspace
declares, and the references between them. It is the answer to "where is
this defined, and what uses it".

The graph is built from the workspace's own source, so it never leaves the
workspace. Python is parsed with the standard library's own parser, and
TypeScript and JavaScript are read for their declarations, because those
languages need a parser the product does not carry.
"""

from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

#: The file extensions the graph reads.
SOURCE_SUFFIXES = frozenset({".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"})

#: The largest file the graph reads.
MAX_FILE_BYTES = 512_000

#: The largest number of files the graph walks.
MAX_FILES = 4_000

#: The kinds of symbol the graph holds.
SymbolKind = Literal["class", "function", "method", "interface", "type", "constant"]


@dataclass(frozen=True, slots=True)
class TypeSymbol:
    """One declaration the graph holds.

    Attributes:
        name: The declaration's name.
        kind: What kind of declaration it is.
        path: The file it lives in, relative to the workspace.
        line: The line it starts on.
        parent: The class or interface it belongs to, when it is a member.
        signature: The declaration's first line, as it was written.
    """

    name: str
    kind: SymbolKind
    path: str
    line: int
    parent: str | None = None
    signature: str = ""

    @property
    def qualified_name(self) -> str:
        """Return the name the symbol is addressed by.

        Returns:
            The qualified name.
        """
        if self.parent:
            return f"{self.parent}.{self.name}"
        return self.name


@dataclass(frozen=True, slots=True)
class TypeEdge:
    """One reference between two declarations.

    Attributes:
        source: The symbol that refers.
        target: The symbol that is referred to.
        path: The file the reference lives in.
        line: The line the reference lives on.
    """

    source: str
    target: str
    path: str
    line: int


@dataclass(frozen=True, slots=True)
class TypeGraph:
    """The declarations of one workspace and the references between them.

    Attributes:
        root: The workspace the graph was built from.
        symbols: The declarations, by their qualified name.
        edges: The references between the declarations.
        files: The files that were read.
    """

    root: str
    symbols: dict[str, TypeSymbol] = field(default_factory=dict)
    edges: tuple[TypeEdge, ...] = ()
    files: tuple[str, ...] = ()

    def definitions_of(self, name: str) -> list[TypeSymbol]:
        """Return the declarations that carry a name.

        Args:
            name: The name to look for.

        Returns:
            The declarations, in path order.
        """
        lowered = name.strip().lower()
        return [
            symbol
            for symbol in sorted(self.symbols.values(), key=lambda item: (item.path, item.line))
            if symbol.name.lower() == lowered or symbol.qualified_name.lower() == lowered
        ]

    def references_to(self, name: str) -> list[TypeEdge]:
        """Return the references that point at a name.

        Args:
            name: The name to look for.

        Returns:
            The references.
        """
        lowered = name.strip().lower()
        return [edge for edge in self.edges if edge.target.lower() == lowered]

    def as_dict(self) -> dict[str, Any]:
        """Return the graph as a JSON-serializable mapping.

        Returns:
            The graph.
        """
        return {
            "root": self.root,
            "files": list(self.files),
            "symbols": [
                {
                    "name": symbol.name,
                    "qualified_name": symbol.qualified_name,
                    "kind": symbol.kind,
                    "path": symbol.path,
                    "line": symbol.line,
                    "signature": symbol.signature,
                }
                for symbol in sorted(self.symbols.values(), key=lambda item: (item.path, item.line))
            ],
            "edges": [
                {
                    "source": edge.source,
                    "target": edge.target,
                    "path": edge.path,
                    "line": edge.line,
                }
                for edge in self.edges
            ],
        }


def _skipped(path: Path) -> bool:
    """Report whether a path is one the graph does not read.

    Args:
        path: The path to check.

    Returns:
        True when the path is skipped.
    """
    parts = set(path.parts)
    return bool(
        parts
        & {
            ".git",
            "node_modules",
            "__pycache__",
            ".venv",
            "venv",
            "dist",
            "build",
            ".next",
            ".mypy_cache",
            ".pytest_cache",
        }
    )


def _python_symbols(
    path: Path, relative: str
) -> tuple[list[TypeSymbol], list[tuple[str, str, int]]]:
    """Read the declarations and the references of one Python file.

    Args:
        path: The file to read.
        relative: The file's path relative to the workspace.

    Returns:
        The declarations, and the references as ``(source, target, line)``.
    """
    symbols: list[TypeSymbol] = []
    references: list[tuple[str, str, int]] = []
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source)
    except (OSError, SyntaxError, ValueError):
        return symbols, references

    def _signature(node: ast.AST) -> str:
        try:
            line = source.splitlines()[getattr(node, "lineno", 1) - 1]
        except (IndexError, OSError):
            return ""
        return line.strip()[:200]

    def _walk(node: ast.AST, parent: str | None) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                symbols.append(
                    TypeSymbol(
                        name=child.name,
                        kind="class",
                        path=relative,
                        line=child.lineno,
                        parent=parent,
                        signature=_signature(child),
                    )
                )
                _walk(child, child.name)
                continue
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                symbols.append(
                    TypeSymbol(
                        name=child.name,
                        kind="method" if parent else "function",
                        path=relative,
                        line=child.lineno,
                        parent=parent,
                        signature=_signature(child),
                    )
                )
                _walk(child, parent)
                continue
            if isinstance(child, ast.Assign) and parent is None:
                for target in child.targets:
                    if isinstance(target, ast.Name) and target.id.isupper():
                        symbols.append(
                            TypeSymbol(
                                name=target.id,
                                kind="constant",
                                path=relative,
                                line=child.lineno,
                                signature=_signature(child),
                            )
                        )
            if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
                references.append((parent or "<module>", child.id, child.lineno))
            if isinstance(child, ast.Attribute):
                references.append((parent or "<module>", child.attr, child.lineno))
            _walk(child, parent)

    _walk(tree, None)
    return symbols, references


def _script_symbols(
    path: Path, relative: str
) -> tuple[list[TypeSymbol], list[tuple[str, str, int]]]:
    """Read the declarations of one TypeScript or JavaScript file.

    The product carries no parser for those languages, so the
    declarations are read from the source text. The reading is
    deliberately shallow: it finds the declarations a reader can see, and
    it never claims more than it found.

    Args:
        path: The file to read.
        relative: The file's path relative to the workspace.

    Returns:
        The declarations, and the references as ``(source, target, line)``.
    """
    symbols: list[TypeSymbol] = []
    references: list[tuple[str, str, int]] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return symbols, references

    import re

    class_pattern = re.compile(r"^\s*(?:export\s+)?(?:abstract\s+)?class\s+([A-Za-z_$][\w$]*)")
    interface_pattern = re.compile(r"^\s*(?:export\s+)?interface\s+([A-Za-z_$][\w$]*)")
    type_pattern = re.compile(r"^\s*(?:export\s+)?type\s+([A-Za-z_$][\w$]*)")
    function_pattern = re.compile(
        r"^\s*(?:export\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)"
    )
    method_pattern = re.compile(
        r"^\s{2,}(?:public\s+|private\s+|protected\s+|static\s+|async\s+)*"
        r"([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*[:{]"
    )
    call_pattern = re.compile(r"\b([A-Za-z_$][\w$]*)\s*\(")

    for number, line in enumerate(lines, start=1):
        for pattern, kind in (
            (class_pattern, "class"),
            (interface_pattern, "interface"),
            (type_pattern, "type"),
            (function_pattern, "function"),
        ):
            match = pattern.match(line)
            if match:
                symbols.append(
                    TypeSymbol(
                        name=match.group(1),
                        kind=kind,  # type: ignore[arg-type]
                        path=relative,
                        line=number,
                        signature=line.strip()[:200],
                    )
                )
                break
        else:
            method = method_pattern.match(line)
            if method and method.group(1) not in {"if", "for", "while", "switch", "catch"}:
                symbols.append(
                    TypeSymbol(
                        name=method.group(1),
                        kind="method",
                        path=relative,
                        line=number,
                        signature=line.strip()[:200],
                    )
                )
        for match in call_pattern.finditer(line):
            references.append(("<module>", match.group(1), number))
    return symbols, references


def build_type_graph(root: Path) -> TypeGraph:
    """Build the type graph of one workspace.

    Args:
        root: The workspace root.

    Returns:
        The graph.
    """
    symbols: dict[str, TypeSymbol] = {}
    edges: list[TypeEdge] = []
    files: list[str] = []
    if not root.is_dir():
        return TypeGraph(root=str(root))
    walked = 0
    for path in sorted(root.rglob("*")):
        if walked >= MAX_FILES:
            break
        if not path.is_file() or path.suffix.lower() not in SOURCE_SUFFIXES:
            continue
        if _skipped(path.relative_to(root)):
            continue
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        walked += 1
        relative = str(path.relative_to(root))
        files.append(relative)
        if path.suffix.lower() == ".py":
            found, references = _python_symbols(path, relative)
        else:
            found, references = _script_symbols(path, relative)
        for symbol in found:
            symbols.setdefault(symbol.qualified_name, symbol)
        for source, target, line in references:
            edges.append(TypeEdge(source=source, target=target, path=relative, line=line))
    return TypeGraph(root=str(root), symbols=symbols, edges=tuple(edges), files=tuple(files))


def graph_fingerprint(graph: TypeGraph) -> str:
    """Return a stable fingerprint of one graph.

    The fingerprint changes when the workspace's declarations change, so a
    caller can tell whether a cached graph is still current.

    Args:
        graph: The graph.

    Returns:
        The fingerprint.
    """
    digest = hashlib.sha256()
    for symbol in sorted(graph.symbols.values(), key=lambda item: item.qualified_name):
        digest.update(symbol.qualified_name.encode("utf-8"))
        digest.update(str(symbol.line).encode("utf-8"))
    return digest.hexdigest()[:16]


__all__ = [
    "MAX_FILE_BYTES",
    "MAX_FILES",
    "SOURCE_SUFFIXES",
    "SymbolKind",
    "TypeEdge",
    "TypeGraph",
    "TypeSymbol",
    "build_type_graph",
    "graph_fingerprint",
]
