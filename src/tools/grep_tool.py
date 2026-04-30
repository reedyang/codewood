"""
High-performance recursive text grep for Smart Shell (Python ``re`` semantics).

- Directory walk uses ``os.walk`` (C implementation).
- Optional extension filter; defaults to common text extensions.
- Parallel per-file scan via ``ThreadPoolExecutor`` (I/O bound).
- Output: UTF-8 text, one match per line: ``line_number<TAB>absolute_path<TAB>line_text``.
"""

from __future__ import annotations

import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Collection, Dict, List, Optional, Set, Tuple

# Default extensions when caller omits ``extensions`` (text-oriented).
DEFAULT_TEXT_EXTENSIONS: Set[str] = {
    ".py",
    ".pyw",
    ".pyi",
    ".md",
    ".txt",
    ".json",
    ".jsonc",
    ".yml",
    ".yaml",
    ".xml",
    ".html",
    ".htm",
    ".css",
    ".js",
    ".mjs",
    ".cjs",
    ".ts",
    ".tsx",
    ".jsx",
    ".vue",
    ".java",
    ".go",
    ".rs",
    ".c",
    ".h",
    ".cpp",
    ".hpp",
    ".cs",
    ".rb",
    ".php",
    ".swift",
    ".kt",
    ".kts",
    ".sql",
    ".sh",
    ".ps1",
    ".bat",
    ".cmd",
    ".ini",
    ".cfg",
    ".toml",
    ".properties",
    ".env",
    ".gitignore",
    ".dockerignore",
    ".editorconfig",
    ".gradle",
    ".scala",
    ".sbt",
    ".lua",
    ".r",
    ".jl",
    ".ex",
    ".exs",
    ".zig",
    ".nim",
    ".dart",
    ".graphql",
    ".gql",
    ".svelte",
}

DEFAULT_SKIP_DIR_NAMES: Set[str] = {
    ".git",
    ".svn",
    ".hg",
    "__pycache__",
    ".venv",
    "venv",
    "env",
    "node_modules",
    ".npm",
    ".yarn",
    "dist",
    "build",
    ".mypy_cache",
    ".pytest_cache",
    ".tox",
    ".eggs",
    ".cursor",
    ".idea",
    "target",
    "out",
    "bin",
    "obj",
    ".next",
    ".nuxt",
    "coverage",
    "htmlcov",
}


def _normalize_extensions(raw: Optional[Collection[str]]) -> Set[str]:
    if raw is None:
        return set(DEFAULT_TEXT_EXTENSIONS)
    out: Set[str] = set()
    for x in raw:
        s = (x or "").strip().lower()
        if not s:
            continue
        if not s.startswith("."):
            s = "." + s
        out.add(s)
    return out if out else set(DEFAULT_TEXT_EXTENSIONS)


def _compile_pattern(pattern: str, ignore_case: bool, multiline: bool) -> re.Pattern[str]:
    flags = 0
    if ignore_case:
        flags |= re.IGNORECASE
    if multiline:
        flags |= re.MULTILINE
    return re.compile(pattern, flags)


def _is_probably_binary(sample: bytes) -> bool:
    if not sample:
        return False
    if b"\x00" in sample[:8192]:
        return True
    chunk = sample[:4096]
    if not chunk:
        return False
    textish = sum(1 for b in chunk if 32 <= b < 127 or b in (9, 10, 13))
    return textish < len(chunk) * 0.65


def _grep_file_lines(
    path: Path,
    regex: re.Pattern[str],
    max_file_bytes: int,
    counter: threading.RLock,
    budget: List[int],
) -> List[Tuple[int, str, str]]:
    """
    Returns list of (line_no, abs_path_str, line_text_single_line).
    Stops adding when shared budget[0] reaches 0.
    """
    results: List[Tuple[int, str, str]] = []
    try:
        st = path.stat()
    except OSError:
        return results
    if st.st_size > max_file_bytes:
        return results

    try:
        with open(path, "rb") as bf:
            head = bf.read(8192)
        if _is_probably_binary(head):
            return results
    except OSError:
        return results

    abs_str = str(path.resolve())
    encodings = ("utf-8-sig", "utf-8", "gbk", "latin-1")
    for enc in encodings:
        try:
            with open(path, "r", encoding=enc, errors="replace", newline="") as f:
                for line_no, line in enumerate(f, start=1):
                    with counter:
                        if budget[0] <= 0:
                            return results
                    if not regex.search(line):
                        continue
                    safe = line.rstrip("\r\n").replace("\n", " ").replace("\r", " ")
                    with counter:
                        if budget[0] <= 0:
                            return results
                        budget[0] -= 1
                    results.append((line_no, abs_str, safe))
            break
        except OSError:
            continue
    return results


def _collect_files_from_root(
    root: Path,
    extensions: Set[str],
    extra_skip: Set[str],
) -> List[Path]:
    skip = DEFAULT_SKIP_DIR_NAMES | extra_skip
    out: List[Path] = []
    root = root.resolve()
    if not root.is_dir():
        return out

    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        # Prune directories in-place
        dirnames[:] = [
            d
            for d in dirnames
            if d not in skip
            and not (d.startswith(".") and d not in (".github",))
        ]
        for fn in filenames:
            p = Path(dirpath) / fn
            try:
                if not p.is_file():
                    continue
            except OSError:
                continue
            suf = p.suffix.lower()
            if suf not in extensions:
                continue
            out.append(p)
    return out


def run_grep(
    *,
    root: Optional[Path],
    files: Optional[List[Path]],
    output_file: Path,
    pattern: str,
    extensions: Optional[Collection[str]] = None,
    ignore_case: bool = False,
    multiline: bool = False,
    max_matches: int = 100_000,
    max_file_bytes: int = 20 * 1024 * 1024,
    exclude_dir_names: Optional[Collection[str]] = None,
    max_workers: Optional[int] = None,
) -> Dict[str, object]:
    """
    Write UTF-8 grep results to ``output_file``.

    Each match line: ``line_number<TAB>absolute_path<TAB>matched_line_text``
    (header explains format; avoids colon ambiguity with Windows drive letters).
    """
    ext_set = _normalize_extensions(extensions)

    extra_skip: Set[str] = set()
    if exclude_dir_names:
        extra_skip.update(str(x).strip() for x in exclude_dir_names if str(x).strip())

    try:
        regex = _compile_pattern(pattern, ignore_case, multiline)
    except re.error as e:
        return {"success": False, "error": f"正则表达式无效: {e}"}

    file_paths: List[Path] = []
    if files:
        file_paths = list(files)
    elif root:
        file_paths = _collect_files_from_root(Path(root), ext_set, extra_skip)
    else:
        return {"success": False, "error": "必须提供 root（目录）或 files（文件列表）"}

    if not file_paths:
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text(
            "# smart-shell grep\n# no files matched extension / root criteria.\n",
            encoding="utf-8",
        )
        return {
            "success": True,
            "match_count": 0,
            "files_with_matches": 0,
            "files_scanned": 0,
            "output_path": str(output_file.resolve()),
            "truncated": False,
            "message": "没有可检索的文件",
        }

    cpu = os.cpu_count() or 4
    workers = max_workers if max_workers is not None else min(32, max(4, cpu * 2))

    budget = [max(0, int(max_matches))]
    lock = threading.RLock()
    all_rows: List[Tuple[int, str, str]] = []

    def task(fp: Path) -> List[Tuple[int, str, str]]:
        return _grep_file_lines(fp, regex, max_file_bytes, lock, budget)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(task, fp): fp for fp in file_paths}
        for fut in as_completed(futures):
            try:
                part = fut.result()
                all_rows.extend(part)
            except Exception as e:
                return {"success": False, "error": f"检索异常: {e}"}

    truncated = budget[0] == 0 and max_matches > 0

    all_rows.sort(key=lambda x: (x[1].lower(), x[0]))
    files_with_matches = len({x[1] for x in all_rows})
    match_count = len(all_rows)

    lines_out = [f"{ln}\t{p}\t{tx}" for ln, p, tx in all_rows]
    header = (
        "# smart-shell grep output\n"
        "# format: line_number<TAB>absolute_path<TAB>matched_line (TAB-separated; line is single-line)\n"
        f"# pattern: {pattern!r} ignore_case={ignore_case} multiline={multiline}\n"
        f"# truncated: {'yes' if truncated else 'no'}\n"
    )

    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(
        header + ("\n".join(lines_out) + "\n" if lines_out else ""),
        encoding="utf-8",
    )

    return {
        "success": True,
        "match_count": match_count,
        "files_with_matches": files_with_matches,
        "files_scanned": len(file_paths),
        "output_path": str(output_file.resolve()),
        "truncated": truncated,
        "message": f"匹配 {match_count} 行，涉及 {files_with_matches} 个文件，已写入输出文件",
    }
