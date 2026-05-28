#!/usr/bin/env python3
"""Auto-discover and run tests in this repository.

Default behavior:
- Discover from `tests/`
- Use `pytest` if available, otherwise fallback to `unittest`
- Exclude selected test files by default
"""

from __future__ import annotations

import argparse
import fnmatch
import importlib.util
import inspect
import os
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Iterable

from src.config.app_info import get_app_env_var


DEFAULT_EXCLUDED_FILES = ()
_AUTO_ACCEPT_ELICITATION_ENV = get_app_env_var("AUTO_ACCEPT_ELICITATION")


def _has_pytest() -> bool:
    return importlib.util.find_spec("pytest") is not None


def _iter_test_cases(suite: unittest.TestSuite) -> Iterable[unittest.TestCase]:
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _iter_test_cases(item)
        else:
            yield item


def _iter_test_ids(suite: unittest.TestSuite) -> Iterable[str]:
    for case in _iter_test_cases(suite):
        yield case.id()


def _normalize_rel(path_like: str) -> str:
    return str(path_like).replace("\\", "/").lstrip("./")


def _module_name_from_path(path: Path, root: Path) -> str:
    rel = path.resolve().relative_to(root.resolve())
    stem = str(rel.with_suffix(""))
    return "autotest_" + stem.replace("\\", "_").replace("/", "_").replace("-", "_")


def _is_excluded_path(path: Path, project_root: Path, excluded_rel: set[str]) -> bool:
    try:
        rel = _normalize_rel(str(path.resolve().relative_to(project_root.resolve())))
    except Exception:
        rel = _normalize_rel(str(path))
    return rel in excluded_rel


def _filter_suite_excluded(
    suite: unittest.TestSuite, project_root: Path, excluded_rel: set[str]
) -> unittest.TestSuite:
    filtered = unittest.TestSuite()
    for case in _iter_test_cases(suite):
        module = inspect.getmodule(case)
        module_file = getattr(module, "__file__", None)
        if module_file and _is_excluded_path(Path(module_file), project_root, excluded_rel):
            continue
        filtered.addTest(case)
    return filtered


def _discover_unittest_suite_by_files(
    start_dir: Path, pattern: str, project_root: Path, excluded_rel: set[str]
) -> unittest.TestSuite:
    suite = unittest.TestSuite()
    loader = unittest.defaultTestLoader

    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    matched = sorted(
        p
        for p in start_dir.rglob("*.py")
        if p.is_file()
        and fnmatch.fnmatch(p.name, pattern)
        and not _is_excluded_path(p, project_root, excluded_rel)
    )
    for file_path in matched:
        mod_name = _module_name_from_path(file_path, project_root)
        spec = importlib.util.spec_from_file_location(mod_name, str(file_path))
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        spec.loader.exec_module(module)
        suite.addTests(loader.loadTestsFromModule(module))
    return suite


def run_unittest(args: argparse.Namespace, project_root: Path, excluded_rel: set[str]) -> int:
    start_dir = (project_root / args.start_dir).resolve()
    if not start_dir.exists() or not start_dir.is_dir():
        print(f"Start directory does not exist: {start_dir}")
        return 2

    loader = unittest.defaultTestLoader
    suite = loader.discover(start_dir=str(start_dir), pattern=args.pattern)
    suite = _filter_suite_excluded(suite, project_root, excluded_rel)
    discovered_ids = list(_iter_test_ids(suite))
    if not discovered_ids:
        suite = _discover_unittest_suite_by_files(start_dir, args.pattern, project_root, excluded_rel)
        discovered_ids = list(_iter_test_ids(suite))

    if args.list_only:
        if not discovered_ids:
            print("No tests discovered.")
            return 0
        print(f"Discovered {len(discovered_ids)} tests:")
        for tid in sorted(discovered_ids):
            print(f" - {tid}")
        return 0

    # Prevent interactive prompts from blocking automated test runs.
    previous_auto_accept = os.environ.get(_AUTO_ACCEPT_ELICITATION_ENV)
    os.environ[_AUTO_ACCEPT_ELICITATION_ENV] = "1"
    try:
        runner = unittest.TextTestRunner(
            verbosity=args.verbosity,
            failfast=args.failfast,
            buffer=args.buffer,
        )
        result = runner.run(suite)
    finally:
        if previous_auto_accept is None:
            os.environ.pop(_AUTO_ACCEPT_ELICITATION_ENV, None)
        else:
            os.environ[_AUTO_ACCEPT_ELICITATION_ENV] = previous_auto_accept
    return 0 if result.wasSuccessful() else 1


def run_pytest(args: argparse.Namespace, project_root: Path, excluded_rel: set[str]) -> int:
    cmd = [sys.executable, "-m", "pytest", args.start_dir]
    for rel in sorted(excluded_rel):
        cmd.extend(["--ignore", rel])
    if args.pattern and args.pattern != "test*.py":
        cmd.extend(["-k", args.pattern])
    if args.failfast:
        cmd.append("-x")
    if args.verbosity >= 2:
        cmd.append("-vv")
    elif args.verbosity == 0:
        cmd.append("-q")
    if args.list_only:
        cmd.append("--collect-only")

    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")
    env.setdefault(_AUTO_ACCEPT_ELICITATION_ENV, "1")
    proc = subprocess.run(cmd, cwd=str(project_root), env=env)
    return int(proc.returncode)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Auto-discover and run all repository tests.",
    )
    parser.add_argument(
        "--framework",
        choices=["auto", "pytest", "unittest"],
        default="auto",
        help="Test framework to run (default: auto).",
    )
    parser.add_argument(
        "--start-dir",
        default="tests",
        help="Directory to start discovery from (default: tests).",
    )
    parser.add_argument(
        "--pattern",
        default="test*.py",
        help="File pattern for unittest discovery (default: test*.py).",
    )
    parser.add_argument(
        "--exclude-file",
        action="append",
        default=[],
        help="Exclude a relative test file path. Can be specified multiple times.",
    )
    parser.add_argument(
        "--verbosity",
        type=int,
        default=2,
        help="Verbosity level (default: 2).",
    )
    parser.add_argument(
        "--failfast",
        action="store_true",
        help="Stop on first test failure.",
    )
    parser.add_argument(
        "--buffer",
        action="store_true",
        help="Buffer stdout/stderr during tests (unittest only).",
    )
    parser.add_argument(
        "--list-only",
        action="store_true",
        help="Only discover/list tests, do not execute.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    project_root = Path(__file__).resolve().parent.parent
    excluded_rel = {_normalize_rel(p) for p in DEFAULT_EXCLUDED_FILES}
    excluded_rel.update(_normalize_rel(p) for p in (args.exclude_file or []))

    requested = args.framework
    framework = requested
    if requested == "auto":
        framework = "pytest" if _has_pytest() else "unittest"

    print(f"Framework: {framework}")
    print(f"Start dir: {args.start_dir}")
    if excluded_rel:
        print(f"Excluded files: {', '.join(sorted(excluded_rel))}")

    if framework == "pytest":
        if not _has_pytest():
            print("pytest is not installed. Fallback to unittest.")
            return run_unittest(args, project_root, excluded_rel)
        return run_pytest(args, project_root, excluded_rel)

    return run_unittest(args, project_root, excluded_rel)


if __name__ == "__main__":
    raise SystemExit(main())
