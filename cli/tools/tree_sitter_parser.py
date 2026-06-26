from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

try:
    from tree_sitter import Language, Parser  # type: ignore[import-untyped]
    _TS_AVAILABLE = True
except ImportError:  # pragma: no cover
    _TS_AVAILABLE = False


_LANGUAGE_LOADERS: Dict[str, Callable[[], Any]] = {}
_PARSER_CACHE: Dict[str, Parser] = {}

_SUFFIX_TO_LANG: Dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".c": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cxx",
    ".h": "c",
    ".hpp": "cpp",
    ".cs": "c_sharp",
    ".rb": "ruby",
    ".php": "php",
    ".swift": "swift",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".m": "swift",
    ".mm": "swift",
}


def _try_load_language(name: str) -> Optional[Language]:
    if not _TS_AVAILABLE:
        return None
    loader = _LANGUAGE_LOADERS.get(name)
    if loader is None:
        module_names = {
            "python": "tree_sitter_python",
            "javascript": "tree_sitter_javascript",
        }
        mod_name = module_names.get(name, f"tree_sitter_{name}")
        try:
            mod = __import__(mod_name, fromlist=["language"])
            loader_fn = getattr(mod, "language", None)
            if callable(loader_fn):
                loader = loader_fn
            _LANGUAGE_LOADERS[name] = loader if callable(loader) else None
        except ImportError:
            _LANGUAGE_LOADERS[name] = None
            return None
    if loader is None or not callable(loader):
        return None
    try:
        return Language(loader())
    except Exception:
        return None


def _get_parser(lang_name: str) -> Optional[Parser]:
    if not _TS_AVAILABLE:
        return None
    cached = _PARSER_CACHE.get(lang_name)
    if cached is not None:
        return cached
    lang = _try_load_language(lang_name)
    if lang is None:
        _PARSER_CACHE[lang_name] = None
        return None
    parser = Parser(lang)
    _PARSER_CACHE[lang_name] = parser
    return parser


def _get_text(node: Any, source: bytes) -> str:
    try:
        return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")
    except Exception:
        return ""


def _node_text_attr(node: Any) -> Optional[str]:
    try:
        t = node.text
        if isinstance(t, bytes):
            return t.decode("utf-8", errors="replace")
        return str(t) if t else None
    except Exception:
        return None


def parse_file_tree_sitter(path: Path) -> Tuple[List[str], List[str], List[Tuple[str, str]], List[str]]:
    suffix = path.suffix.lower()
    lang_name = _SUFFIX_TO_LANG.get(suffix)
    if lang_name is None:
        return [], [], [], []

    parser = _get_parser(lang_name)
    if parser is None:
        return [], [], [], []

    try:
        source = path.read_bytes()
    except Exception:
        return [], [], [], []

    try:
        tree = parser.parse(source)
    except Exception:
        return [], [], [], []

    root = tree.root_node
    symbols: List[str] = []
    imports: List[str] = []
    calls: List[Tuple[str, str]] = []
    tokens: List[str] = []

    if lang_name == "python":
        _extract_python(root, source, symbols, imports, calls)
    elif lang_name in ("javascript", "typescript", "tsx"):
        _extract_javascript(root, source, symbols, imports, calls)
    else:
        return [], [], [], []

    # Generate tokens from symbols + imports + path
    token_set: Set[str] = set()
    for s in symbols:
        for w in _split_identifier(s):
            token_set.add(w)
    for imp in imports:
        for w in _split_identifier(imp):
            token_set.add(w)
    tokens = sorted(token_set)[:300]

    # De-dup while keeping order
    seen_syms: Set[str] = set()
    deduped_syms: List[str] = []
    for s in symbols:
        if s not in seen_syms:
            seen_syms.add(s)
            deduped_syms.append(s)
    symbols = deduped_syms[:120]

    seen_imps: Set[str] = set()
    deduped_imps: List[str] = []
    for imp in imports:
        if imp not in seen_imps:
            seen_imps.add(imp)
            deduped_imps.append(imp)
    imports = deduped_imps[:120]

    seen_calls: Set[Tuple[str, str]] = set()
    deduped_calls: List[Tuple[str, str]] = []
    for c in calls:
        if c not in seen_calls:
            seen_calls.add(c)
            deduped_calls.append(c)
            if len(deduped_calls) >= 400:
                break

    return symbols, imports, deduped_calls, tokens


def _split_identifier(s: str) -> List[str]:
    words: List[str] = []
    for part in re.split(r"[^A-Za-z0-9]+", s):
        # camelCase / PascalCase / snake_case splitting
        sub = re.sub(r"([a-z])([A-Z])", r"\1 \2", part)
        sub = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", sub)
        for w in sub.replace("_", " ").split():
            wl = w.lower()
            if len(wl) >= 2:
                words.append(wl)
    return words


_CALL_KEYWORDS: Set[str] = {
    "if", "for", "while", "switch", "catch", "return", "with", "elif",
    "def", "function", "class", "and", "or", "not", "in", "is", "await",
    "yield", "del", "assert", "raise", "lambda", "print", "super",
    "typeof", "sizeof", "new", "delete", "throw", "case", "do", "else",
}


def _extract_python(
    root: Any, source: bytes,
    symbols: List[str], imports: List[str], calls: List[Tuple[str, str]],
) -> None:
    # --- symbols: function & class definitions ---
    for node, name in _iter_named_children(root, ["function_definition", "class_definition"]):
        name_node = node.child_by_field_name("name")
        if name_node is not None:
            s = _node_text_attr(name_node)
            if s:
                symbols.append(s)

    # --- imports ---
    for node in _descendants_by_type(root, {"import_statement", "import_from_statement", "future_import_statement"}):
        raw = _get_text(node, source).strip()
        if raw:
            imports.append(raw)

    # --- call edges ---
    # Walk all function/class bodies to find call expressions
    for def_node in _descendants_by_type(root, {"function_definition", "class_definition"}):
        caller_name = ""
        name_node = def_node.child_by_field_name("name")
        if name_node is not None:
            caller_name = _node_text_attr(name_node) or ""
        for call_node in _descendants_by_type(def_node, {"call"}):
            func_node = call_node.child_by_field_name("function")
            if func_node is None:
                continue
            callee = _node_text_attr(func_node)
            if callee and callee not in _CALL_KEYWORDS:
                calls.append((caller_name, callee))

    # Top-level calls: only direct children of the module root
    for i in range(root.named_child_count):
        stmt = root.named_child(i)
        if stmt.type == "expression_statement":
            call = stmt.child(0) if stmt.child_count > 0 else None
            if call and call.type == "call":
                func_node = call.child_by_field_name("function")
                if func_node:
                    callee = _node_text_attr(func_node)
                    if callee and callee not in _CALL_KEYWORDS:
                        calls.append(("", callee))


def _extract_javascript(
    root: Any, source: bytes,
    symbols: List[str], imports: List[str], calls: List[Tuple[str, str]],
) -> None:
    sym_types = {
        "function_declaration",
        "class_declaration",
        "method_definition",
        "lexical_declaration",
        "variable_declaration",
        "public_field_definition",
        "export_statement",
    }

    # --- symbols ---
    for node in _descendants_by_type(root, sym_types):
        name_node = node.child_by_field_name("name")
        if name_node is not None:
            s = _node_text_attr(name_node)
            if s and s != "exports":
                symbols.append(s)
        # Handle `const foo = function()`, `let bar = () => {}`
        if node.type in ("lexical_declaration", "variable_declaration"):
            for decl in _iter_named_children(node, {"variable_declarator"}):
                n = decl.child_by_field_name("name")
                if n is not None:
                    s = _node_text_attr(n)
                    if s:
                        symbols.append(s)

    # --- arrow functions assigned to variables already handled above ---

    # --- imports ---
    for node in _descendants_by_type(root, {"import_statement", "import"}):
        raw = _get_text(node, source).strip()
        if raw:
            imports.append(raw)
    for node in _descendants_by_type(root, {"call_expression"}):
        func = node.child_by_field_name("function")
        if func and _node_text_attr(func) == "require":
            raw = _get_text(node, source).strip()
            if raw:
                imports.append(raw)

    # --- call edges ---
    for def_node in _descendants_by_type(root, {"function_declaration", "method_definition", "arrow_function"}):
        caller_name = ""
        name_node = def_node.child_by_field_name("name")
        if name_node is not None:
            caller_name = _node_text_attr(name_node) or ""
        for call_node in _descendants_by_type(def_node, {"call_expression"}):
            func_node = call_node.child_by_field_name("function")
            if func_node:
                callee = _node_text_attr(func_node)
                if callee and callee not in _CALL_KEYWORDS:
                    calls.append((caller_name, callee))
    # Top-level calls: direct children of the root/program node
    for i in range(root.named_child_count):
        stmt = root.named_child(i)
        if stmt.type == "expression_statement":
            call = stmt.child(0) if stmt.child_count > 0 else None
            if call and call.type == "call_expression":
                func_node = call.child_by_field_name("function")
                if func_node:
                    callee = _node_text_attr(func_node)
                    if callee and callee not in _CALL_KEYWORDS:
                        calls.append(("", callee))


def _iter_named_children(node: Any, types: Set[str]):
    for i in range(node.named_child_count):
        child = node.named_child(i)
        if child.type in types:
            yield child, None


def _descendants_by_type(node: Any, types: Set[str]):
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type in types:
            yield current
        for i in range(current.named_child_count - 1, -1, -1):
            stack.append(current.named_child(i))
