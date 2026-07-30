"""Python script scanner — uses the :mod:`ast` module to detect file
I/O operations that create, modify, or delete files."""

from __future__ import annotations

import ast
from typing import Optional, Set

from .base import ScriptTargetScanner
from .registry import _registry

_WRITE_MODE_CHARS: Set[str] = {"w", "a", "x"}


def _ast_extract_string_value(node: ast.expr) -> Optional[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _ast_is_write_mode(node: ast.expr) -> bool:
    val = _ast_extract_string_value(node)
    if val is None:
        return False
    return bool(set(val) & _WRITE_MODE_CHARS)


class _PythonFileTargetVisitor(ast.NodeVisitor):
    """Walk a Python AST and collect string-literal file paths from calls
    that may create, modify, or delete files."""

    def __init__(self) -> None:
        self.paths: Set[str] = set()

    def _record_path(self, node: ast.expr) -> None:
        val = _ast_extract_string_value(node)
        if val is not None:
            self.paths.add(val)

    def visit_Call(self, node: ast.Call) -> None:
        self._handle_open_call(node)
        self._handle_pathlib_write_call(node)
        self._handle_os_remove_call(node)
        self._handle_shutil_call(node)
        self._handle_json_pickle_dump_call(node)
        self.generic_visit(node)

    def _handle_open_call(self, node: ast.Call) -> None:
        func = node.func
        is_open = (
            (isinstance(func, ast.Name) and func.id == "open")
            or (isinstance(func, ast.Attribute) and func.attr == "open")
        )
        if not is_open or not node.args:
            return
        mode_expr: Optional[ast.expr] = None
        if len(node.args) >= 2:
            mode_expr = node.args[1]
        else:
            for kw in node.keywords:
                if kw.arg == "mode":
                    mode_expr = kw.value
                    break
        if mode_expr is None or not _ast_is_write_mode(mode_expr):
            return
        self._record_path(node.args[0])

    def _handle_pathlib_write_call(self, node: ast.Call) -> None:
        func = node.func
        if not isinstance(func, ast.Attribute):
            return
        if func.attr not in ("write_text", "write_bytes"):
            return
        inner = func.value
        if not isinstance(inner, ast.Call):
            return
        inner_func = inner.func
        if isinstance(inner_func, ast.Name) and inner_func.id == "Path":
            if inner.args:
                self._record_path(inner.args[0])

    def _handle_os_remove_call(self, node: ast.Call) -> None:
        func = node.func
        if not isinstance(func, ast.Attribute):
            return
        if func.attr not in ("remove", "unlink", "rename", "rmdir", "makedirs", "mkdir"):
            return
        if not isinstance(func.value, ast.Name):
            return
        if func.value.id != "os":
            return
        for arg in node.args[: (2 if func.attr == "rename" else 1)]:
            self._record_path(arg)

    def _handle_shutil_call(self, node: ast.Call) -> None:
        func = node.func
        if not isinstance(func, ast.Attribute):
            return
        if func.attr not in ("copy", "copy2", "move", "rmtree", "copytree"):
            return
        if not isinstance(func.value, ast.Name):
            return
        if func.value.id != "shutil":
            return
        max_args = 1 if func.attr == "rmtree" else 2
        for arg in node.args[:max_args]:
            self._record_path(arg)

    def _handle_json_pickle_dump_call(self, node: ast.Call) -> None:
        func = node.func
        if not isinstance(func, ast.Attribute):
            return
        if func.attr not in ("dump", "dumps"):
            return
        if not isinstance(func.value, ast.Name):
            return
        if func.value.id not in ("json", "pickle"):
            return
        if len(node.args) < 2:
            return
        fp = node.args[1]
        if isinstance(fp, ast.Call):
            self._handle_open_call(fp)


class PythonScriptScanner(ScriptTargetScanner):
    """Scans Python (``.py``) scripts for file I/O targets."""

    @classmethod
    def extensions(cls) -> Set[str]:
        return {".py", ".py3", ".pyw"}

    @classmethod
    def interpreter_names(cls) -> Set[str]:
        return {"python", "python3", "pythonw", "py"}

    @classmethod
    def inline_flags(cls) -> Set[str]:
        return {"-c", "-m"}

    def _extract_raw_paths(self, source: str) -> Set[str]:
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return set()
        visitor = _PythonFileTargetVisitor()
        visitor.visit(tree)
        return visitor.paths


# Auto-register
_registry.register(PythonScriptScanner)
