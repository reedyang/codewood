from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

try:
    from tree_sitter import Language, Parser  # type: ignore[import-untyped]
    _TS_AVAILABLE = True
except ImportError:  # pragma: no cover
    _TS_AVAILABLE = False


_LANGUAGE_LOADERS: Dict[str, Callable[[], Any]] = {}
_PARSER_CACHE: Dict[str, Optional[Parser]] = {}
_PARSER_TLS = threading.local()

# Extension -> tree-sitter language name
_SUFFIX_TO_LANG: Dict[str, str] = {
    # Programming languages
    ".py": "python", ".pyi": "python", ".pyx": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "tsx",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin", ".kts": "kotlin",
    ".scala": "scala", ".sc": "scala",
    ".c": "c", ".h": "c",
    ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".hpp": "cpp", ".hh": "cpp", ".hxx": "cpp",
    ".cs": "c_sharp",
    ".rb": "ruby",
    ".php": "php", ".php3": "php", ".php4": "php", ".php5": "php", ".phtml": "php",
    ".swift": "swift", ".m": "swift", ".mm": "swift",
    ".lua": "lua",
    ".pl": "perl", ".pm": "perl", ".t": "perl",
    # Shell / scripts
    ".sh": "bash", ".bash": "bash", ".zsh": "bash", ".bashrc": "bash", ".bash_profile": "bash",
    # Markup
    ".html": "html", ".htm": "html",
    ".css": "css",
    ".xml": "xml", ".xsl": "xml", ".xslt": "xml", ".xsd": "xml", ".svg": "xml",
    ".md": "markdown", ".mdx": "markdown",
    # Data / config
    ".json": "json",
    ".yaml": "yaml", ".yml": "yaml",
    ".toml": "toml",
    # SQL
    ".sql": "sql",
    # HCL / Terraform
    ".hcl": "hcl", ".tf": "hcl",
}

# Module name and attribute name for each language's loader function.
# Defaults to ("tree_sitter_{lang}", "language") if not listed.
_LANG_MODULE_MAP: Dict[str, Tuple[str, str]] = {
    "python": ("tree_sitter_python", "language"),
    "javascript": ("tree_sitter_javascript", "language"),
    "typescript": ("tree_sitter_typescript", "language_typescript"),
    "tsx": ("tree_sitter_typescript", "language_tsx"),
    "go": ("tree_sitter_go", "language"),
    "rust": ("tree_sitter_rust", "language"),
    "java": ("tree_sitter_java", "language"),
    "kotlin": ("tree_sitter_kotlin", "language"),
    "scala": ("tree_sitter_scala", "language"),
    "c": ("tree_sitter_c", "language"),
    "cpp": ("tree_sitter_cpp", "language"),
    "c_sharp": ("tree_sitter_c_sharp", "language"),
    "ruby": ("tree_sitter_ruby", "language"),
    "php": ("tree_sitter_php", "language_php"),
    "swift": ("tree_sitter_swift", "language"),
    "lua": ("tree_sitter_lua", "language"),
    "perl": ("tree_sitter_perl", "language"),
    "bash": ("tree_sitter_bash", "language"),
    "html": ("tree_sitter_html", "language"),
    "css": ("tree_sitter_css", "language"),
    "xml": ("tree_sitter_xml", "language_xml"),
    "markdown": ("tree_sitter_markdown", "language"),
    "json": ("tree_sitter_json", "language"),
    "yaml": ("tree_sitter_yaml", "language"),
    "toml": ("tree_sitter_toml", "language"),
    "sql": ("tree_sitter_sql", "language"),
    "hcl": ("tree_sitter_hcl", "language"),
}

# Node-type names per language for symbol/import/call extraction.
# Key: language name. Value: (symbol_types, import_types, call_types, def_types)
_TS_SPEC: Dict[str, Tuple[Set[str], Set[str], Set[str], Set[str]]] = {
    "python": (
        {"function_definition", "class_definition"},
        {"import_statement", "import_from_statement", "future_import_statement"},
        {"call"},
        {"function_definition"},
    ),
    "javascript": (
        {"function_declaration", "class_declaration", "method_definition",
         "lexical_declaration", "variable_declaration",
         "public_field_definition", "export_statement"},
        {"import_statement", "import"},
        {"call_expression"},
        {"function_declaration", "method_definition", "arrow_function"},
    ),
    "typescript": (
        {"function_declaration", "class_declaration", "method_definition",
         "lexical_declaration", "variable_declaration",
         "public_field_definition", "export_statement",
         "interface_declaration", "type_alias_declaration", "enum_declaration"},
        {"import_statement", "import"},
        {"call_expression"},
        {"function_declaration", "method_definition", "arrow_function"},
    ),
    "tsx": (
        {"function_declaration", "class_declaration", "method_definition",
         "lexical_declaration", "variable_declaration",
         "public_field_definition", "export_statement",
         "interface_declaration", "type_alias_declaration", "enum_declaration"},
        {"import_statement", "import"},
        {"call_expression"},
        {"function_declaration", "method_definition", "arrow_function"},
    ),
    "go": (
        {"function_declaration", "method_declaration", "type_spec",
         "struct_type", "interface_type"},
        {"import_declaration"},
        {"call_expression"},
        {"function_declaration", "method_declaration"},
    ),
    "rust": (
        {"function_item", "struct_item", "enum_item", "trait_item",
         "impl_item", "const_item", "static_item", "type_item",
         "macro_definition", "mod_item"},
        {"use_declaration", "extern_crate_declaration"},
        {"call_expression", "macro_invocation"},
        {"function_item", "closure_expression"},
    ),
    "java": (
        {"method_declaration", "class_declaration",
         "interface_declaration", "enum_declaration",
         "constructor_declaration"},
        {"import_declaration", "package_declaration"},
        {"method_invocation"},
        {"method_declaration", "constructor_declaration"},
    ),
    "kotlin": (
        {"function_declaration", "class_declaration",
         "object_declaration", "interface_declaration",
         "enum_class", "type_alias", "property_declaration"},
        {"import_header", "import"},
        {"call_expression"},
        {"function_declaration"},
    ),
    "scala": (
        {"function_definition", "class_definition", "object_definition",
         "trait_definition", "val_definition", "var_definition"},
        {"import_declaration"},
        {"call_expression", "function_definition", "class_definition",
         "method_invocation", "generic_function"},
        {"function_definition", "class_definition", "object_definition",
         "trait_definition"},
    ),
    "c": (
        {"function_definition", "struct_specifier", "enum_specifier",
         "union_specifier", "type_definition", "preproc_function_def"},
        {"preproc_include"},
        {"call_expression"},
        {"function_definition"},
    ),
    "cpp": (
        {"function_definition", "class_specifier", "struct_specifier",
         "enum_specifier", "union_specifier", "type_definition",
         "namespace_definition", "concept_definition",
         "alias_declaration", "template_declaration"},
        {"preproc_include", "using_declaration", "using_directive"},
        {"call_expression"},
        {"function_definition", "method_definition", "constructor_definition"},
    ),
    "c_sharp": (
        {"method_declaration", "class_declaration",
         "struct_declaration", "interface_declaration",
         "enum_declaration", "constructor_declaration",
         "property_declaration", "namespace_declaration",
         "record_declaration"},
        {"using_directive"},
        {"invocation_expression"},
        {"method_declaration", "constructor_declaration",
         "anonymous_method_expression", "lambda_expression"},
    ),
    "ruby": (
        {"method", "class", "module", "singleton_class"},
        set(),  # imports handled specially below
        {"call", "command"},
        {"method", "do_block", "brace_block"},
    ),
    "php": (
        {"function_definition", "class_declaration",
         "interface_declaration", "trait_declaration",
         "method_declaration", "enum_declaration",
         "anonymous_function_creation_expression", "arrow_function"},
        {"namespace_use_declaration", "require_once_expression",
         "include_once_expression", "include_expression"},
        {"function_call_expression", "method_call_expression",
         "scoped_call_expression", "member_call_expression"},
        {"function_definition", "method_declaration",
         "anonymous_function_creation_expression", "arrow_function"},
    ),
    "swift": (
        {"function_declaration", "class_declaration",
         "struct_declaration", "enum_declaration",
         "protocol_declaration", "extension_declaration",
         "actor_declaration", "typealias_declaration"},
        {"import_declaration"},
        {"call_expression", "simple_identifier"},
        {"function_declaration", "closure_expression"},
    ),
    "lua": (
        {"function_declaration", "variable_declaration",
         "local_variable_declaration"},
        set(),  # imports handled specially below
        {"function_call"},
        {"function_declaration"},
    ),
    "perl": (
        {"subroutine_declaration_statement"},
        {"use_statement"},
        set(),  # basic structure only
        set(),
    ),
    "bash": (
        {"function_definition"},
        set(),  # imports handled specially below
        {"command"},
        {"function_definition"},
    ),
    "sql": (
        set(),  # no symbols — structural matching via file path only
        set(),
        set(),
        set(),
    ),
    "hcl": (
        {"block"},  # resource/data/variable blocks
        set(),
        set(),
        set(),
    ),
    "html": (
        {"element", "script_element", "style_element"},
        set(),
        set(),
        set(),
    ),
    "css": (
        {"rule_set", "media_statement", "keyframes_statement", "supports_statement"},
        {"import_statement"},
        set(),
        set(),
    ),
    "xml": (
        {"element"},
        set(),
        set(),
        set(),
    ),
    "markdown": (
        {"atx_heading", "setext_heading", "fenced_code_block", "indented_code_block"},
        set(),
        set(),
        set(),
    ),
    "json": (set(), set(), set(), set()),
    "yaml": (set(), set(), set(), set()),
    "toml": (set(), set(), set(), set()),
}

# Languages where the root node type is different from the node type returned by parser
# (these all have consistent top-level children, handled uniformly)
_ROOT_OVERLAY: Dict[str, Set[str]] = {
    "kotlin": {"function_declaration", "class_declaration", "object_declaration",
               "interface_declaration", "enum_class", "type_alias", "property_declaration",
               "import_header", "import"},
    "scala": {"function_definition", "class_definition", "object_definition",
              "trait_definition", "val_definition", "var_definition",
              "import_declaration", "package_clause"},
    "lua": {"function_declaration", "variable_declaration",
            "local_variable_declaration", "function_call", "return_statement"},
    "perl": {"subroutine_declaration_statement", "use_statement",
             "expression_statement", "statement"},
    "bash": {"function_definition", "command", "variable_assignment",
             "if_statement", "while_statement", "for_statement", "case_statement"},
    "hcl": {"block", "attribute", "variable", "output", "resource", "data"},
    "html": {"element", "script_element", "style_element", "doctype"},
    "css": {"rule_set", "media_statement", "keyframes_statement",
            "import_statement", "supports_statement"},
    "xml": {"element", "processing_instruction", "doctype"},
    "markdown": {"atx_heading", "setext_heading", "paragraph",
                 "fenced_code_block", "indented_code_block", "list"},
    "json": {"object", "array"},
    "yaml": {"block_mapping", "block_sequence", "flow_node"},
    "toml": {"table", "table_array", "pair"},
}


def _try_load_language(name: str) -> Optional[Language]:
    if not _TS_AVAILABLE:
        return None
    loader = _LANGUAGE_LOADERS.get(name)
    if loader is None:
        mod_name, attr_name = _LANG_MODULE_MAP.get(
            name, (f"tree_sitter_{name}", "language")
        )
        try:
            mod = __import__(mod_name, fromlist=[attr_name])
            loader_fn = getattr(mod, attr_name, None)
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


_PARSER_TLS = threading.local()

def _get_parser(lang_name: str) -> Optional[Parser]:
    if not _TS_AVAILABLE:
        return None
    # Thread-local cache so concurrent ThreadPool workers do not share a
    # single Parser object -- tree-sitter's Parser.parse() is NOT
    # thread-safe and will deadlock/hang when called concurrently on the
    # same instance.
    try:
        tls_cache = _PARSER_TLS.cache
    except AttributeError:
        tls_cache = {}
        _PARSER_TLS.cache = tls_cache
    cached = tls_cache.get(lang_name)
    if cached is not None:
        return cached
    lang = _try_load_language(lang_name)
    if lang is None:
        tls_cache[lang_name] = None
        return None
    parser = Parser(lang)
    tls_cache[lang_name] = parser
    return parser


def _get_text(node: Any, source: bytes) -> str:
    try:
        return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")
    except Exception:
        return ""


def _node_text(node: Any) -> Optional[str]:
    try:
        t = node.text
        if isinstance(t, bytes):
            return t.decode("utf-8", errors="replace")
        return str(t) if t else None
    except Exception:
        return None


def _node_name(node: Any) -> str:
    name_node = node.child_by_field_name("name")
    if name_node is not None:
        s = _node_text(name_node)
        if s:
            return s
    declarator = node.child_by_field_name("declarator")
    if declarator is not None:
        return _extract_identifier_from_declarator(declarator)
    for i in range(node.named_child_count):
        child = node.named_child(i)
        if child.type in ("identifier", "type_identifier", "field_identifier"):
            s = _node_text(child)
            if s:
                return s
    return ""


def _extract_identifier_from_declarator(node: Any) -> str:
    for child_type in ("identifier", "field_identifier"):
        for i in range(node.named_child_count):
            child = node.named_child(i)
            if child.type == child_type:
                s = _node_text(child)
                if s:
                    return s
            result = _extract_identifier_from_declarator(child)
            if result:
                return result
    return _node_text(node) or ""


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
    spec = _TS_SPEC.get(lang_name, (set(), set(), set(), set()))
    sym_types, imp_types, call_types, def_types = spec

    symbols: List[str] = []
    imports: List[str] = []
    calls: List[Tuple[str, str]] = []

    _extract_symbols(root, sym_types, symbols)
    _extract_imports(root, source, imp_types, lang_name, imports)
    _extract_call_edges(root, source, call_types, def_types, lang_name, calls)

    token_set: Set[str] = set()
    for s in symbols:
        for w in _split_identifier(s):
            token_set.add(w)
    for imp in imports:
        for w in _split_identifier(imp):
            token_set.add(w)
    tokens = sorted(token_set)[:300]

    symbols = _dedup_capped(symbols, 120)
    imports = _dedup_capped(imports, 120)

    seen_calls: Set[Tuple[str, str]] = set()
    deduped_calls: List[Tuple[str, str]] = []
    for c in calls:
        if c not in seen_calls:
            seen_calls.add(c)
            deduped_calls.append(c)
            if len(deduped_calls) >= 400:
                break

    return symbols, imports, deduped_calls, tokens


def _dedup_capped(items: List[str], cap: int) -> List[str]:
    seen: Set[str] = set()
    out: List[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out[:cap]


def _split_identifier(s: str) -> List[str]:
    words: List[str] = []
    for part in re.split(r"[^A-Za-z0-9]+", s):
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
    "yield", "del", "assert", "raise", "lambda", "print", "super", "end",
    "typeof", "sizeof", "new", "delete", "throw", "case", "do", "else",
    "echo", "exit", "source", "local", "my", "our",
}


def _extract_symbols(root: Any, sym_types: Set[str], symbols: List[str]) -> None:
    for node in _descendants_by_type(root, sym_types):
        name = _node_name(node)
        if name and name not in ("exports",):
            symbols.append(name)
        # Handle variable/lexical declarations (JS/TS)
        if node.type in ("lexical_declaration", "variable_declaration"):
            for i in range(node.named_child_count):
                child = node.named_child(i)
                if child.type == "variable_declarator":
                    n = _node_name(child)
                    if n:
                        symbols.append(n)
        # HTML: extract tag name
        if node.type in ("element", "script_element", "style_element"):
            tag = _extract_html_tag_name(node)
            if tag:
                symbols.append(tag)
        # CSS: extract selectors
        if node.type in ("rule_set", "media_statement", "keyframes_statement", "supports_statement"):
            for i in range(node.named_child_count):
                child = node.named_child(i)
                if child.type == "selectors":
                    raw = _node_text(child)
                    if raw:
                        symbols.append(raw)
        # XML: extract tag name
        if node.type == "element":
            tag = _extract_xml_tag_name(node)
            if tag:
                symbols.append(tag)
        # Markdown: extract heading text
        if node.type in ("atx_heading", "setext_heading"):
            for i in range(node.named_child_count):
                child = node.named_child(i)
                if child.type in ("heading_content", "inline"):
                    raw = _node_text(child)
                    if raw:
                        symbols.append(raw.strip())
        # HCL: extract block labels
        if node.type == "block":
            for i in range(node.named_child_count):
                child = node.named_child(i)
                if child.type == "identifier" or child.type == "string_lit":
                    s = _node_text(child)
                    if s:
                        s = s.strip('\'"')
                        if s:
                            symbols.append(s)


def _extract_html_tag_name(node: Any) -> str:
    for i in range(node.named_child_count):
        child = node.named_child(i)
        if child.type in ("start_tag", "self_closing_tag"):
            for j in range(child.named_child_count):
                cc = child.named_child(j)
                if cc.type == "tag_name":
                    return _node_text(cc) or ""
    return ""


def _extract_xml_tag_name(node: Any) -> str:
    for i in range(node.named_child_count):
        child = node.named_child(i)
        if child.type in ("start_tag", "STag", "self_closing_tag"):
            for j in range(child.named_child_count):
                cc = child.named_child(j)
                if cc.type in ("Name", "tag_name"):
                    return _node_text(cc) or ""
    return ""


def _extract_imports(
    root: Any,
    source: bytes,
    imp_types: Set[str],
    lang_name: str,
    imports: List[str],
) -> None:
    for node in _descendants_by_type(root, imp_types):
        raw = _get_text(node, source).strip()
        if raw:
            imports.append(raw)

    if lang_name == "ruby":
        for node in _descendants_by_type(root, {"call"}):
            method = node.child_by_field_name("method")
            if method:
                meth_name = _node_text(method)
                if meth_name in ("require", "require_relative", "include", "extend", "load", "autoload"):
                    raw = _get_text(node, source).strip()
                    if raw:
                        imports.append(raw)

    if lang_name in ("javascript", "typescript", "tsx"):
        for node in _descendants_by_type(root, {"call_expression"}):
            func = node.child_by_field_name("function")
            if func and _node_text(func) == "require":
                raw = _get_text(node, source).strip()
                if raw:
                    imports.append(raw)

    # Lua: require("module")
    if lang_name == "lua":
        for node in _descendants_by_type(root, {"function_call"}):
            for i in range(node.named_child_count):
                child = node.named_child(i)
                if _node_text(child) == "require":
                    raw = _get_text(node, source).strip()
                    if raw:
                        imports.append(raw)
                    break

    # Bash: source / .  commands
    if lang_name == "bash":
        for node in _descendants_by_type(root, {"command"}):
            for i in range(node.named_child_count):
                child = node.named_child(i)
                text = _node_text(child)
                if text in ("source", "."):
                    raw = _get_text(node, source).strip()
                    if raw:
                        imports.append(raw)
                    break

    # CSS: @import
    if lang_name == "css":
        for node in _descendants_by_type(root, {"import_statement"}):
            raw = _get_text(node, source).strip()
            if raw:
                imports.append(raw)


def _extract_call_edges(
    root: Any,
    source: bytes,
    call_types: Set[str],
    def_types: Set[str],
    lang_name: str,
    calls: List[Tuple[str, str]],
) -> None:
    if not call_types or not def_types:
        return

    for def_node in _descendants_by_type(root, def_types):
        caller_name = _node_name(def_node)
        for call_node in _descendants_by_type(def_node, call_types):
            callee = _resolve_callee(call_node, lang_name)
            if callee and callee not in _CALL_KEYWORDS and callee != caller_name:
                calls.append((caller_name, callee))

    for i in range(root.named_child_count):
        stmt = root.named_child(i)
        inner = stmt
        if stmt.type == "expression_statement" and stmt.child_count > 0:
            inner = stmt.child(0)
        if inner.type in call_types or (
            lang_name == "ruby" and inner.type in ("call", "command")
        ):
            callee = _resolve_callee(inner, lang_name)
            if callee and callee not in _CALL_KEYWORDS:
                calls.append(("", callee))


def _resolve_callee(call_node: Any, lang_name: str) -> Optional[str]:
    func = call_node.child_by_field_name("function")
    if func is not None:
        s = _node_text(func)
        if s:
            return s

    method = call_node.child_by_field_name("method")
    if method is not None:
        s = _node_text(method)
        if s:
            return s

    name_node = call_node.child_by_field_name("name")
    if name_node is not None:
        s = _node_text(name_node)
        if s:
            return s

    for i in range(call_node.named_child_count):
        child = call_node.named_child(i)
        if child.type == "identifier":
            s = _node_text(child)
            if s:
                return s

    return None


def _descendants_by_type(node: Any, types: Set[str]):
    if not types:
        return
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type in types:
            yield current
        for i in range(current.named_child_count - 1, -1, -1):
            stack.append(current.named_child(i))
