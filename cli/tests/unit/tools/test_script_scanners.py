"""Unit tests for cli.tools.script_scanners."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Set

from cli.tools.script_scanners.base import (
    ScriptExecution,
    ScriptTargetScanner,
    _resolve_raw_paths,
    _split_command,
    _exe_base,
    parse_script_execution,
)
from cli.tools.script_scanners.registry import ScriptScannerRegistry, get_registry
from cli.tools.script_scanners.python import PythonScriptScanner, _PythonFileTargetVisitor
from cli.tools.script_scanners.bash import BashScriptScanner
from cli.tools.script_scanners.powershell import PowerShellScriptScanner
from cli.tools.script_scanners.node import NodeScriptScanner
from cli.tools.script_scanners import expand_command_file_paths


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


def _tmpdir() -> Path:
    return Path(tempfile.mkdtemp())


# ============================================================================
# PythonScriptScanner
# ============================================================================


class PythonScannerMetadataTests(unittest.TestCase):
    def test_extensions(self):
        self.assertEqual(PythonScriptScanner.extensions(), {".py", ".py3", ".pyw"})

    def test_interpreter_names(self):
        self.assertIn("python", PythonScriptScanner.interpreter_names())
        self.assertIn("python3", PythonScriptScanner.interpreter_names())
        self.assertIn("py", PythonScriptScanner.interpreter_names())

    def test_inline_flags(self):
        self.assertEqual(PythonScriptScanner.inline_flags(), {"-c", "-m"})

    def test_option_value_flags_defaults_to_empty(self):
        self.assertEqual(PythonScriptScanner.option_value_flags(), set())


class PythonScannerExtractTests(unittest.TestCase):
    """Test _PythonFileTargetVisitor._extract_raw_paths via PythonScriptScanner."""

    def setUp(self):
        self.scanner = PythonScriptScanner()

    def _extract(self, source: str) -> Set[str]:
        return self.scanner._extract_raw_paths(source)

    # -- open() ---------------------------------------------------------------

    def test_open_write_mode_w(self):
        self.assertIn("out.txt", self._extract('open("out.txt", "w")'))

    def test_open_write_mode_a(self):
        self.assertIn("out.txt", self._extract("open('out.txt', 'a')"))

    def test_open_write_mode_x(self):
        self.assertIn("out.txt", self._extract('open("out.txt", "x")'))

    def test_open_write_mode_wb(self):
        self.assertIn("out.bin", self._extract('open("out.bin", "wb")'))

    def test_open_write_mode_ab(self):
        self.assertIn("out.txt", self._extract('open("out.txt", "ab")'))

    def test_open_write_mode_w_plus(self):
        self.assertIn("out.txt", self._extract('open("out.txt", "w+")'))

    def test_open_write_mode_a_plus(self):
        self.assertIn("out.txt", self._extract('open("out.txt", "a+")'))

    def test_open_keyword_mode(self):
        src = 'open("out.txt", mode="w")'
        self.assertIn("out.txt", self._extract(src))

    def test_open_read_mode_not_detected(self):
        src = 'open("readonly.txt", "r")'
        self.assertNotIn("readonly.txt", self._extract(src))

    def test_open_no_mode_not_detected(self):
        src = 'open("default.txt")'
        self.assertNotIn("default.txt", self._extract(src))

    def test_open_with_statement(self):
        src = 'with open("data.txt", "w") as f:\n    f.write("x")'
        self.assertIn("data.txt", self._extract(src))

    # -- pathlib --------------------------------------------------------------

    def test_path_write_text(self):
        src = 'Path("config.json").write_text("{}")'
        self.assertIn("config.json", self._extract(src))

    def test_path_write_bytes(self):
        src = 'Path("data.bin").write_bytes(b"x")'
        self.assertIn("data.bin", self._extract(src))

    def test_path_read_text_not_detected(self):
        src = 'Path("input.txt").read_text()'
        self.assertNotIn("input.txt", self._extract(src))

    # -- os -------------------------------------------------------------------

    def test_os_remove(self):
        self.assertIn("temp.txt", self._extract('os.remove("temp.txt")'))

    def test_os_unlink(self):
        self.assertIn("temp.txt", self._extract('os.unlink("temp.txt")'))

    def test_os_rename_both_args(self):
        r = self._extract('os.rename("old.txt", "new.txt")')
        self.assertIn("old.txt", r)
        self.assertIn("new.txt", r)

    def test_os_rmdir(self):
        self.assertIn("mydir", self._extract('os.rmdir("mydir")'))

    def test_os_makedirs(self):
        self.assertIn("newdir", self._extract('os.makedirs("newdir")'))

    def test_os_mkdir(self):
        self.assertIn("somedir", self._extract('os.mkdir("somedir")'))

    # -- shutil ---------------------------------------------------------------

    def test_shutil_copy_both_args(self):
        r = self._extract('shutil.copy("src.txt", "dst.txt")')
        self.assertIn("src.txt", r)
        self.assertIn("dst.txt", r)

    def test_shutil_copy2(self):
        r = self._extract('shutil.copy2("a.txt", "b.txt")')
        self.assertIn("a.txt", r)
        self.assertIn("b.txt", r)

    def test_shutil_move(self):
        r = self._extract('shutil.move("src.txt", "dst.txt")')
        self.assertIn("src.txt", r)
        self.assertIn("dst.txt", r)

    def test_shutil_rmtree_single_arg(self):
        r = self._extract('shutil.rmtree("old_dir")')
        self.assertIn("old_dir", r)

    def test_shutil_copytree_both_args(self):
        r = self._extract('shutil.copytree("src_dir", "dst_dir")')
        self.assertIn("src_dir", r)
        self.assertIn("dst_dir", r)

    # -- json / pickle nested open --------------------------------------------

    def test_json_dump_nested_open(self):
        src = 'json.dump(data, open("out.json", "w"))'
        self.assertIn("out.json", self._extract(src))

    def test_pickle_dump_nested_open(self):
        src = 'pickle.dump(data, open("out.pkl", "wb"))'
        self.assertIn("out.pkl", self._extract(src))

    def test_json_dumps_no_file(self):
        src = 'json.dumps(data)'
        r = self._extract(src)
        self.assertEqual(r, set())

    # -- edge cases -----------------------------------------------------------

    def test_dynamic_variable_not_detected(self):
        src = 'path = "dynamic.txt"\nopen(path, "w")'
        self.assertNotIn("dynamic.txt", self._extract(src))

    def test_fstring_not_detected(self):
        src = 'for i in range(2): open(f"file_{i}.txt", "w")'
        r = self._extract(src)
        self.assertFalse(r)

    def test_syntax_error_returns_empty(self):
        r = self._extract("this is not valid python !!!")
        self.assertEqual(r, set())

    def test_empty_source_returns_empty(self):
        r = self._extract("")
        self.assertEqual(r, set())

    def test_multiple_paths_collected(self):
        src = '''
open("a.txt", "w")
open("b.txt", "a")
Path("c.txt").write_text("x")
os.remove("d.txt")
shutil.copy("e.txt", "f.txt")
'''
        r = self._extract(src)
        self.assertIn("a.txt", r)
        self.assertIn("b.txt", r)
        self.assertIn("c.txt", r)
        self.assertIn("d.txt", r)
        self.assertIn("e.txt", r)
        self.assertIn("f.txt", r)

    def test_no_false_positives_from_module_attributes(self):
        src = "from os import path\npath.join('a','b')\nprint('hello')"
        r = self._extract(src)
        self.assertFalse(r)


class PythonScannerScanTests(unittest.TestCase):
    """Test the full scan() template method with file I/O."""

    def test_scan_resolves_relative_paths(self):
        d = _tmpdir()
        try:
            script = _write(d / "gen.py", 'open("output.txt", "w").write("x")\nos.remove("clean.me")')
            (d / "clean.me").write_text("tmp")
            scanner = PythonScriptScanner()
            result = scanner.scan(script, d)
            self.assertTrue(any("output.txt" in p for p in result))
            self.assertTrue(any("clean.me" in p for p in result))
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)

    def test_scan_handles_non_existent_script(self):
        scanner = PythonScriptScanner()
        result = scanner.scan(Path("/nonexistent/script.py"), Path("/tmp"))
        self.assertEqual(result, set())

    def test_scan_returns_resolved_absolute_paths(self):
        d = _tmpdir()
        try:
            script = _write(d / "gen.py", 'Path("nested/deep.txt").write_text("x")')
            (d / "nested").mkdir(parents=True)
            scanner = PythonScriptScanner()
            result = scanner.scan(script, d)
            for p in result:
                self.assertTrue(Path(p).is_absolute() or str(d) in p)
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)


# ============================================================================
# BashScriptScanner
# ============================================================================


class BashScannerMetadataTests(unittest.TestCase):
    def test_extensions(self):
        for ext in (".sh", ".bash", ".zsh", ".ksh", ".fish"):
            self.assertIn(ext, BashScriptScanner.extensions())

    def test_interpreter_names(self):
        for name in ("bash", "sh", "zsh", "ksh", "dash", "fish"):
            self.assertIn(name, BashScriptScanner.interpreter_names())

    def test_inline_flags(self):
        self.assertEqual(BashScriptScanner.inline_flags(), {"-c"})

    def test_extract_returns_empty(self):
        scanner = BashScriptScanner()
        r = scanner._extract_raw_paths("echo hello > out.txt")
        self.assertEqual(r, set())

    def test_scan_returns_empty(self):
        d = _tmpdir()
        try:
            script = _write(d / "s.sh", "echo hello > out.txt")
            scanner = BashScriptScanner()
            r = scanner.scan(script, d)
            self.assertEqual(r, set())
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)


# ============================================================================
# PowerShellScriptScanner
# ============================================================================


class PowerShellScannerMetadataTests(unittest.TestCase):
    def test_extensions(self):
        for ext in (".ps1", ".psm1"):
            self.assertIn(ext, PowerShellScriptScanner.extensions())

    def test_interpreter_names(self):
        for name in ("powershell", "pwsh"):
            self.assertIn(name, PowerShellScriptScanner.interpreter_names())

    def test_inline_flags(self):
        flags = PowerShellScriptScanner.inline_flags()
        self.assertIn("-command", flags)
        self.assertIn("-c", flags)
        self.assertIn("-encodedcommand", flags)

    def test_option_value_flags(self):
        self.assertEqual(PowerShellScriptScanner.option_value_flags(), {"-file", "-f"})

    def test_extract_returns_empty(self):
        scanner = PowerShellScriptScanner()
        r = scanner._extract_raw_paths('Set-Content -Path "out.txt" -Value "hello"')
        self.assertEqual(r, set())


# ============================================================================
# NodeScriptScanner
# ============================================================================


class NodeScannerMetadataTests(unittest.TestCase):
    def test_extensions(self):
        for ext in (".js", ".mjs", ".cjs"):
            self.assertIn(ext, NodeScriptScanner.extensions())

    def test_interpreter_names(self):
        for name in ("node", "nodejs"):
            self.assertIn(name, NodeScriptScanner.interpreter_names())

    def test_inline_flags(self):
        self.assertEqual(NodeScriptScanner.inline_flags(), {"-e", "-p"})

    def test_extract_returns_empty(self):
        scanner = NodeScriptScanner()
        r = scanner._extract_raw_paths("fs.writeFileSync('out.txt', 'x')")
        self.assertEqual(r, set())


# ============================================================================
# ScriptScannerRegistry
# ============================================================================


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.registry = ScriptScannerRegistry()
        self.registry.register(PythonScriptScanner)
        self.registry.register(BashScriptScanner)
        self.registry.register(PowerShellScriptScanner)
        self.registry.register(NodeScriptScanner)

    def test_find_by_extension_known(self):
        self.assertIs(self.registry.find_by_extension(".py"), PythonScriptScanner)
        self.assertIs(self.registry.find_by_extension(".sh"), BashScriptScanner)
        self.assertIs(self.registry.find_by_extension(".ps1"), PowerShellScriptScanner)
        self.assertIs(self.registry.find_by_extension(".js"), NodeScriptScanner)

    def test_find_by_extension_case_insensitive(self):
        self.assertIs(self.registry.find_by_extension(".PY"), PythonScriptScanner)
        self.assertIs(self.registry.find_by_extension(".Sh"), BashScriptScanner)

    def test_find_by_extension_unknown(self):
        self.assertIsNone(self.registry.find_by_extension(".xyz"))
        self.assertIsNone(self.registry.find_by_extension(""))

    def test_find_by_interpreter_known(self):
        self.assertIs(self.registry.find_by_interpreter("python"), PythonScriptScanner)
        self.assertIs(self.registry.find_by_interpreter("python3"), PythonScriptScanner)
        self.assertIs(self.registry.find_by_interpreter("bash"), BashScriptScanner)
        self.assertIs(self.registry.find_by_interpreter("powershell"), PowerShellScriptScanner)
        self.assertIs(self.registry.find_by_interpreter("pwsh"), PowerShellScriptScanner)
        self.assertIs(self.registry.find_by_interpreter("node"), NodeScriptScanner)

    def test_find_by_interpreter_unknown(self):
        self.assertIsNone(self.registry.find_by_interpreter("rustc"))
        self.assertIsNone(self.registry.find_by_interpreter(""))

    def test_global_registry_is_fully_seeded(self):
        r = get_registry()
        self.assertIsNotNone(r.find_by_extension(".py"))
        self.assertIsNotNone(r.find_by_extension(".sh"))
        self.assertIsNotNone(r.find_by_extension(".ps1"))
        self.assertIsNotNone(r.find_by_extension(".js"))
        self.assertIsNotNone(r.find_by_interpreter("python"))
        self.assertIsNotNone(r.find_by_interpreter("bash"))
        self.assertIsNotNone(r.find_by_interpreter("powershell"))
        self.assertIsNotNone(r.find_by_interpreter("node"))


# ============================================================================
# Base utilities
# ============================================================================


class SplitCommandTests(unittest.TestCase):
    def test_simple_command(self):
        self.assertEqual(_split_command("python script.py"), ["python", "script.py"])

    def test_quoted_arg(self):
        parts = _split_command('python -c "print(1)"')
        self.assertEqual(parts[0], "python")
        self.assertEqual(parts[1], "-c")
        # On Windows (posix=False) shlex preserves the outer quotes;
        # on POSIX it strips them.  Both are valid tokenisations.
        self.assertIn(parts[2], ('"print(1)"', "print(1)"))

    def test_empty_string(self):
        self.assertEqual(_split_command(""), [])


class ExeBaseTests(unittest.TestCase):
    def test_simple(self):
        self.assertEqual(_exe_base("python"), "python")

    def test_dot_exe_stripped(self):
        self.assertEqual(_exe_base("python.exe"), "python")

    def test_full_path(self):
        self.assertEqual(_exe_base("/usr/bin/python3"), "python3")

    def test_windows_path(self):
        self.assertEqual(_exe_base(r"C:\Python39\python.exe"), "python")


class ResolveRawPathsTests(unittest.TestCase):
    def test_resolves_relative(self):
        d = _tmpdir()
        try:
            (d / "existing.txt").write_text("x")
            result = _resolve_raw_paths({"existing.txt"}, d)
            self.assertTrue(any("existing.txt" in p for p in result))
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)

    def test_parent_dir_exists_includes_non_existent(self):
        d = _tmpdir()
        try:
            result = _resolve_raw_paths({"new_file.txt"}, d)
            self.assertTrue(any("new_file.txt" in p for p in result))
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)

    def test_parent_dir_missing_excludes(self):
        d = _tmpdir()
        try:
            result = _resolve_raw_paths({"missing_dir/new_file.txt"}, d)
            self.assertFalse(result)
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)

    def test_directory_expands_to_files(self):
        d = _tmpdir()
        try:
            (d / "sub").mkdir()
            (d / "sub" / "a.txt").write_text("a")
            (d / "sub" / "b.txt").write_text("b")
            result = _resolve_raw_paths({"sub"}, d)
            self.assertTrue(any("a.txt" in p for p in result))
            self.assertTrue(any("b.txt" in p for p in result))
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)


class ScriptExecutionTests(unittest.TestCase):
    def test_auto_populates_extension(self):
        se = ScriptExecution(script_path=Path("/tmp/script.py"))
        self.assertEqual(se.extension, ".py")

    def test_explicit_extension_takes_precedence(self):
        se = ScriptExecution(
            script_path=Path("/tmp/script.py"),
            extension=".txt",
        )
        self.assertEqual(se.extension, ".txt")  # explicit wins

    def test_interpreter_defaults_to_none(self):
        se = ScriptExecution(script_path=Path("/tmp/script.sh"))
        self.assertIsNone(se.interpreter)

    def test_interpreter_explicit(self):
        se = ScriptExecution(script_path=Path("/tmp/s.sh"), interpreter="bash")
        self.assertEqual(se.interpreter, "bash")


# ============================================================================
# parse_script_execution
# ============================================================================


class ParseScriptExecutionTests(unittest.TestCase):
    def setUp(self):
        self.registry = get_registry()
        self.d = _tmpdir()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.d, ignore_errors=True)

    def _parse(self, command: str) -> ScriptExecution | None:
        return parse_script_execution(command, self.d, self.registry)

    def test_python_script(self):
        script = _write(self.d / "build.py", "pass")
        se = self._parse("python build.py")
        self.assertIsNotNone(se)
        if se:
            self.assertEqual(se.interpreter, "python")
            self.assertEqual(se.extension, ".py")

    def test_python3_script(self):
        script = _write(self.d / "run.py", "pass")
        se = self._parse("python3 run.py")
        self.assertIsNotNone(se)
        if se:
            self.assertEqual(se.interpreter, "python3")

    def test_python_inline_minus_c_returns_none(self):
        se = self._parse('python -c "open(\'x.txt\',\'w\')"')
        self.assertIsNone(se)

    def test_python_minus_m_returns_none(self):
        script = _write(self.d / "mymod.py", "pass")
        se = self._parse("python -m mymod")
        self.assertIsNone(se)

    def test_bash_script(self):
        script = _write(self.d / "run.sh", "echo hi")
        se = self._parse("bash run.sh")
        self.assertIsNotNone(se)
        if se:
            self.assertEqual(se.interpreter, "bash")
            self.assertEqual(se.extension, ".sh")

    def test_powershell_file_flag(self):
        script = _write(self.d / "deploy.ps1", "Write-Host hi")
        se = self._parse("powershell -File deploy.ps1")
        self.assertIsNotNone(se)
        if se:
            self.assertEqual(se.interpreter, "powershell")
            self.assertIn(se.extension, (".ps1", ".psm1"))

    def test_powershell_short_f_flag(self):
        script = _write(self.d / "x.ps1", "Write-Host hi")
        se = self._parse("powershell -f x.ps1")
        self.assertIsNotNone(se)
        if se:
            self.assertEqual(se.interpreter, "powershell")

    def test_node_script(self):
        script = _write(self.d / "app.js", "console.log(1)")
        se = self._parse("node app.js")
        self.assertIsNotNone(se)
        if se:
            self.assertEqual(se.interpreter, "node")
            self.assertEqual(se.extension, ".js")

    def test_node_inline_minus_e_returns_none(self):
        se = self._parse("node -e 'console.log(1)'")
        self.assertIsNone(se)

    def test_direct_script_execution(self):
        script = _write(self.d / "tool.py", "#!python\nprint(1)")
        se = self._parse("./tool.py")
        self.assertIsNotNone(se)
        if se:
            self.assertIsNone(se.interpreter)
            self.assertEqual(se.extension, ".py")

    def test_non_script_command_returns_none(self):
        self.assertIsNone(self._parse("echo hello"))
        self.assertIsNone(self._parse("dir /b"))
        self.assertIsNone(self._parse("git status"))

    def test_nonexistent_script_returns_none(self):
        self.assertIsNone(self._parse("python does_not_exist.py"))

    def test_bash_inline_minus_c_returns_none(self):
        se = self._parse("bash -c 'echo hi'")
        self.assertIsNone(se)


# ============================================================================
# expand_command_file_paths (integration)
# ============================================================================


class ExpandCommandFilePathsTests(unittest.TestCase):
    def setUp(self):
        self.d = _tmpdir()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.d, ignore_errors=True)

    def test_python_script_adds_targets_and_preserves_originals(self):
        script = _write(self.d / "gen.py", 'open("output.txt", "w").write("x")\nos.remove("old.tmp")')
        (self.d / "old.tmp").write_text("x")
        result = expand_command_file_paths(
            f"python {script}", self.d, {"original.txt"},
        )
        self.assertIn("original.txt", result)
        self.assertTrue(any("output.txt" in p for p in result))
        self.assertTrue(any("old.tmp" in p for p in result))

    def test_bash_placeholder_returns_original_only(self):
        script = _write(self.d / "s.sh", "echo hi > out.txt")
        result = expand_command_file_paths(f"bash {script}", self.d, {"x.txt"})
        self.assertEqual(result, {"x.txt"})

    def test_powershell_placeholder_returns_original_only(self):
        script = _write(self.d / "s.ps1", "Set-Content out.txt hi")
        result = expand_command_file_paths(
            f"powershell -File {script}", self.d, {"y.txt"},
        )
        self.assertEqual(result, {"y.txt"})

    def test_node_placeholder_returns_original_only(self):
        script = _write(self.d / "s.js", "fs.writeFileSync('out.txt','x')")
        result = expand_command_file_paths(f"node {script}", self.d, {"z.txt"})
        self.assertEqual(result, {"z.txt"})

    def test_inline_code_returns_original_only(self):
        result = expand_command_file_paths(
            'python -c "open(\'x.txt\',\'w\')"', self.d, {"a.txt"},
        )
        self.assertEqual(result, {"a.txt"})

    def test_non_script_command_returns_original(self):
        result = expand_command_file_paths("echo hello", self.d, {"b.txt"})
        self.assertEqual(result, {"b.txt"})

    def test_empty_command_returns_original(self):
        result = expand_command_file_paths("", self.d, {"c.txt"})
        self.assertEqual(result, {"c.txt"})


# ============================================================================
# ScriptTargetScanner ABC
# ============================================================================


class AbstractScannerTests(unittest.TestCase):
    """Verify that the ABC enforces the contract correctly."""

    def test_cannot_instantiate_abstract(self):
        with self.assertRaises(TypeError):
            ScriptTargetScanner()  # type: ignore[abstract]

    def test_concrete_subclass_instantiates(self):
        scanner = PythonScriptScanner()
        self.assertIsInstance(scanner, ScriptTargetScanner)

    def test_scan_template_method_calls_extract_raw_paths(self):
        """Ensure scan() delegates to _extract_raw_paths() and resolves paths."""

        class _FakeScanner(ScriptTargetScanner):
            @classmethod
            def extensions(cls):
                return {".fake"}
            @classmethod
            def interpreter_names(cls):
                return {"fake"}
            def _extract_raw_paths(self, source):
                return {"out.txt"}

        d = _tmpdir()
        try:
            script = _write(d / "test.fake", "whatever")
            scanner = _FakeScanner()
            result = scanner.scan(script, d)
            self.assertTrue(any("out.txt" in p for p in result))
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
