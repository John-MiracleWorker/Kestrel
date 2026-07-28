from __future__ import annotations

import ast
import re
from collections.abc import Callable
from pathlib import Path

from .models import ParsedFile, ParsedImport, ParsedReference, ParsedSymbol

PARSER_VERSIONS: dict[str, str] = {
    "python": "ast-v1",
    "javascript": "structural-regex-v1",
    "typescript": "structural-regex-v1",
    "go": "structural-regex-v1",
    "rust": "structural-regex-v1",
    "java": "structural-regex-v1",
    "kotlin": "structural-regex-v1",
    "swift": "structural-regex-v1",
    "text": "lexical-v1",
}


_LANGUAGE_BY_SUFFIX = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".swift": "swift",
}

_IDENTIFIER = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")
_NON_REFERENCE_WORDS = {
    "as",
    "async",
    "await",
    "break",
    "case",
    "catch",
    "class",
    "const",
    "continue",
    "def",
    "default",
    "do",
    "else",
    "enum",
    "export",
    "extends",
    "false",
    "final",
    "fn",
    "for",
    "from",
    "func",
    "function",
    "if",
    "impl",
    "import",
    "in",
    "interface",
    "let",
    "mod",
    "new",
    "none",
    "null",
    "object",
    "package",
    "private",
    "protocol",
    "pub",
    "public",
    "record",
    "return",
    "static",
    "struct",
    "super",
    "switch",
    "this",
    "throw",
    "trait",
    "true",
    "try",
    "type",
    "typealias",
    "use",
    "val",
    "var",
    "void",
    "when",
    "where",
    "while",
}


def language_for_path(path: Path) -> str:
    return _LANGUAGE_BY_SUFFIX.get(path.suffix.lower(), "text")


def parser_version(language: str) -> str:
    return PARSER_VERSIONS[language]


def parse_file(path: Path, content: str, language: str) -> ParsedFile:
    if language == "python":
        return _parse_python(content)
    parser = _STRUCTURAL_PARSERS.get(language)
    if parser is None:
        return _parse_text(content)
    return parser(content)


class _PythonVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self._scope: list[str] = []
        self.symbols: list[ParsedSymbol] = []
        self.imports: list[ParsedImport] = []
        self.references: list[ParsedReference] = []

    def _add_symbol(self, node: ast.AST, name: str, kind: str) -> None:
        qualified = ".".join((*self._scope, name))
        self.symbols.append(
            ParsedSymbol(
                name=name,
                qualified_name=qualified,
                kind=kind,
                line=int(getattr(node, "lineno", 1)),
                column=int(getattr(node, "col_offset", 0)) + 1,
            )
        )

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._add_symbol(node, node.name, "class")
        self._scope.append(node.name)
        self.generic_visit(node)
        self._scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._add_symbol(node, node.name, "method" if self._scope else "function")
        self._scope.append(node.name)
        self.generic_visit(node)
        self._scope.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._add_symbol(node, node.name, "method" if self._scope else "function")
        self._scope.append(node.name)
        self.generic_visit(node)
        self._scope.pop()

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.imports.append(
                ParsedImport(
                    module=alias.name,
                    imported_name=alias.asname,
                    line=node.lineno,
                    column=node.col_offset + 1,
                )
            )
            reference_name = alias.asname or alias.name.split(".", 1)[0]
            self.references.append(
                ParsedReference(
                    name=reference_name,
                    line=node.lineno,
                    column=node.col_offset + 1,
                )
            )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        prefix = "." * node.level + (node.module or "")
        for alias in node.names:
            module = f"{prefix}.{alias.name}" if prefix else alias.name
            self.imports.append(
                ParsedImport(
                    module=module,
                    imported_name=alias.asname,
                    line=node.lineno,
                    column=node.col_offset + 1,
                )
            )
            self.references.append(
                ParsedReference(
                    name=alias.asname or alias.name,
                    line=node.lineno,
                    column=node.col_offset + 1,
                )
            )

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load):
            self.references.append(
                ParsedReference(
                    name=node.id,
                    line=node.lineno,
                    column=node.col_offset + 1,
                )
            )

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if isinstance(node.ctx, ast.Load):
            self.references.append(
                ParsedReference(
                    name=node.attr,
                    line=node.lineno,
                    column=node.col_offset + 1,
                )
            )
        self.generic_visit(node)


def _parse_python(content: str) -> ParsedFile:
    try:
        tree = ast.parse(content)
    except (SyntaxError, ValueError):
        return _parse_text(content)
    visitor = _PythonVisitor()
    visitor.visit(tree)
    return ParsedFile(
        symbols=_dedupe_symbols(visitor.symbols),
        imports=_dedupe_imports(visitor.imports),
        references=_dedupe_references(visitor.references),
    )


def _parse_javascript(content: str) -> ParsedFile:
    patterns = (
        (re.compile(r"\bclass\s+([A-Za-z_$][\w$]*)"), "class"),
        (
            re.compile(r"\b(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\("),
            "function",
        ),
        (
            re.compile(
                r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*"
                r"(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>"
            ),
            "function",
        ),
        (
            re.compile(
                r"(?m)^\s*(?:(?:public|private|protected|static|async|readonly)\s+)*"
                r"(?!if\b|for\b|while\b|switch\b|catch\b|with\b)"
                r"([A-Za-z_$][\w$]*)\s*\([^;{}]*\)\s*"
                r"(?:\:\s*[^{=>]+)?\s*\{"
            ),
            "method",
        ),
        (re.compile(r"\binterface\s+([A-Za-z_$][\w$]*)"), "interface"),
        (re.compile(r"\btype\s+([A-Za-z_$][\w$]*)\s*="), "type"),
    )
    imports: list[ParsedImport] = []
    for match in re.finditer(r"\bimport(?:\s+[\s\S]*?\s+from\s+|\s*)[\"']([^\"']+)[\"']", content):
        imports.append(_parsed_import(content, match, match.group(1)))
    for match in re.finditer(r"\brequire\s*\(\s*[\"']([^\"']+)[\"']\s*\)", content):
        imports.append(_parsed_import(content, match, match.group(1)))
    return _parse_structural(content, patterns, imports)


def _parse_go(content: str) -> ParsedFile:
    patterns = (
        (re.compile(r"(?m)^\s*type\s+([A-Za-z_]\w*)\s+"), "type"),
        (
            re.compile(r"(?m)^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)\s*\("),
            "function",
        ),
    )
    imports = [
        _parsed_import(content, match, match.group(1))
        for match in re.finditer(r"(?m)^\s*(?:import\s+)?[\"`]([^\"`]+)[\"`]", content)
    ]
    return _parse_structural(content, patterns, imports)


def _parse_rust(content: str) -> ParsedFile:
    patterns = (
        (re.compile(r"\bstruct\s+([A-Za-z_]\w*)"), "struct"),
        (re.compile(r"\benum\s+([A-Za-z_]\w*)"), "enum"),
        (re.compile(r"\btrait\s+([A-Za-z_]\w*)"), "trait"),
        (re.compile(r"\bfn\s+([A-Za-z_]\w*)\s*[\(<]"), "function"),
        (re.compile(r"\btype\s+([A-Za-z_]\w*)\s*="), "type"),
    )
    imports = [
        _parsed_import(content, match, match.group(1).strip())
        for match in re.finditer(r"(?m)^\s*use\s+([^;]+);", content)
    ]
    return _parse_structural(content, patterns, imports)


def _parse_java(content: str) -> ParsedFile:
    patterns = (
        (
            re.compile(r"\b(?:class|record)\s+([A-Za-z_]\w*)"),
            "class",
        ),
        (re.compile(r"\binterface\s+([A-Za-z_]\w*)"), "interface"),
        (re.compile(r"\benum\s+([A-Za-z_]\w*)"), "enum"),
        (
            re.compile(
                r"(?m)^\s*(?:(?:public|protected|private|static|final|abstract|"
                r"synchronized|native|default)\s+)*(?:<[^>]+>\s+)?"
                r"[A-Za-z_$][\w$<>\[\],.? ]*\s+([A-Za-z_$][\w$]*)\s*"
                r"\([^;{}]*\)\s*(?:throws\s+[^{]+)?\{"
            ),
            "method",
        ),
    )
    imports = [
        _parsed_import(content, match, match.group(1))
        for match in re.finditer(r"(?m)^\s*import\s+(?:static\s+)?([^;]+);", content)
    ]
    return _parse_structural(content, patterns, imports)


def _parse_kotlin(content: str) -> ParsedFile:
    patterns = (
        (
            re.compile(r"\b(?:data\s+|sealed\s+)?class\s+([A-Za-z_]\w*)"),
            "class",
        ),
        (re.compile(r"\bobject\s+([A-Za-z_]\w*)"), "object"),
        (re.compile(r"\binterface\s+([A-Za-z_]\w*)"), "interface"),
        (re.compile(r"\bfun\s+([A-Za-z_]\w*)\s*\("), "function"),
    )
    imports = [
        _parsed_import(content, match, match.group(1))
        for match in re.finditer(r"(?m)^\s*import\s+([^\s;]+)", content)
    ]
    return _parse_structural(content, patterns, imports)


def _parse_swift(content: str) -> ParsedFile:
    patterns = (
        (re.compile(r"\bclass\s+([A-Za-z_]\w*)"), "class"),
        (re.compile(r"\bstruct\s+([A-Za-z_]\w*)"), "struct"),
        (re.compile(r"\benum\s+([A-Za-z_]\w*)"), "enum"),
        (re.compile(r"\bprotocol\s+([A-Za-z_]\w*)"), "protocol"),
        (re.compile(r"\bfunc\s+([A-Za-z_]\w*)\s*\("), "function"),
        (re.compile(r"\btypealias\s+([A-Za-z_]\w*)\s*="), "type"),
    )
    imports = [
        _parsed_import(content, match, match.group(1))
        for match in re.finditer(r"(?m)^\s*import\s+([A-Za-z_][\w.]*)", content)
    ]
    return _parse_structural(content, patterns, imports)


def _parse_structural(
    content: str,
    symbol_patterns: tuple[tuple[re.Pattern[str], str], ...],
    imports: list[ParsedImport],
) -> ParsedFile:
    symbols: list[ParsedSymbol] = []
    for pattern, kind in symbol_patterns:
        for match in pattern.finditer(content):
            name = match.group(1)
            line, column = _line_column(content, match.start(1))
            symbols.append(
                ParsedSymbol(
                    name=name,
                    qualified_name=name,
                    kind=kind,
                    line=line,
                    column=column,
                )
            )
    return ParsedFile(
        symbols=_dedupe_symbols(symbols),
        imports=_dedupe_imports(imports),
        references=_lexical_references(content),
    )


def _parse_text(content: str) -> ParsedFile:
    return ParsedFile(references=_lexical_references(content))


def _lexical_references(content: str) -> tuple[ParsedReference, ...]:
    references: list[ParsedReference] = []
    for line_number, line in enumerate(content.splitlines(), start=1):
        for match in _IDENTIFIER.finditer(line):
            name = match.group(0)
            if name.lower() in _NON_REFERENCE_WORDS:
                continue
            references.append(
                ParsedReference(name=name, line=line_number, column=match.start() + 1)
            )
    return _dedupe_references(references)


def _parsed_import(content: str, match: re.Match[str], module: str) -> ParsedImport:
    line, column = _line_column(content, match.start(1))
    return ParsedImport(
        module=module,
        imported_name=None,
        line=line,
        column=column,
    )


def _line_column(content: str, offset: int) -> tuple[int, int]:
    line = content.count("\n", 0, offset) + 1
    last_newline = content.rfind("\n", 0, offset)
    return line, offset - last_newline


def _dedupe_symbols(values: list[ParsedSymbol]) -> tuple[ParsedSymbol, ...]:
    return tuple(
        sorted(
            set(values),
            key=lambda item: (
                item.line,
                item.column,
                item.name.casefold(),
                item.kind,
                item.qualified_name,
            ),
        )
    )


def _dedupe_imports(values: list[ParsedImport]) -> tuple[ParsedImport, ...]:
    return tuple(
        sorted(
            set(values),
            key=lambda item: (
                item.line,
                item.column,
                item.module.casefold(),
                item.imported_name or "",
            ),
        )
    )


def _dedupe_references(values: list[ParsedReference]) -> tuple[ParsedReference, ...]:
    return tuple(
        sorted(
            set(values),
            key=lambda item: (item.line, item.column, item.name.casefold()),
        )
    )


_STRUCTURAL_PARSERS: dict[str, Callable[[str], ParsedFile]] = {
    "javascript": _parse_javascript,
    "typescript": _parse_javascript,
    "go": _parse_go,
    "rust": _parse_rust,
    "java": _parse_java,
    "kotlin": _parse_kotlin,
    "swift": _parse_swift,
}
