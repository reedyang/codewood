import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cli.tools.shell import _unwrap_windows_powershell_wrapper
from cli.tools.shell import _windows_powershell_command_argv
from cli.tools.shell import _strip_powershell_clixml_output
from cli.tools.shell import _ClixmlStreamFilter
from cli.tools.shell import _encode_powershell_script_as_encoded_command
from cli.tools.shell import _normalize_windows_powershell_command_for_compat
from cli.tools.shell import _enforce_git_no_pager_for_shell_command
from cli.tools.shell import enforce_workspace_rg_for_shell_command
from cli.tools.shell import _is_read_only_command
from cli.tools.shell import normalize_shell_command_for_summary
from cli.tools.shell import _normalize_windows_shell_path_separators
from cli.tools.shell import strip_redundant_cd_prefix


class ShellCommandPolicyTests(unittest.TestCase):
    class _DummyAgent:
        def __init__(self, repo_root: str):
            self._self_repo_root = repo_root

    def test_windows_bare_command_passes_through(self):
        with patch("cli.tools.shell.os.name", "nt"):
            self.assertEqual(
                _unwrap_windows_powershell_wrapper("Get-ChildItem -Force"),
                "Get-ChildItem -Force",
            )

    def test_windows_powershell_wrapper_is_unwrapped(self):
        with patch("cli.tools.shell.os.name", "nt"):
            res = _unwrap_windows_powershell_wrapper(
                'powershell -ExecutionPolicy Bypass -Command "Get-Date"'
            )
        self.assertEqual(res, "Get-Date")

    def test_windows_powershell_exe_and_pwsh_are_unwrapped(self):
        with patch("cli.tools.shell.os.name", "nt"):
            self.assertEqual(
                _unwrap_windows_powershell_wrapper('powershell.exe -Command "Get-Date"'),
                "Get-Date",
            )
            self.assertEqual(
                _unwrap_windows_powershell_wrapper('pwsh -Command "Get-Date"'),
                "Get-Date",
            )

    def test_unwrap_strips_outer_double_quote_wrapper(self):
        with patch("cli.tools.shell.os.name", "nt"):
            res = _unwrap_windows_powershell_wrapper(
                '"powershell -ExecutionPolicy Bypass -Command \\"Get-Date\\""'
            )
        self.assertEqual(res, "Get-Date")

    def test_unwrap_encoded_command_decodes_script(self):
        script = "$x = @'\nline1\n'@; Write-Output $x"
        encoded = _encode_powershell_script_as_encoded_command(script)
        with patch("cli.tools.shell.os.name", "nt"):
            res = _unwrap_windows_powershell_wrapper(
                f"powershell -ExecutionPolicy Bypass -EncodedCommand {encoded}"
            )
        self.assertEqual(res, script)

    def test_unwrap_noop_on_non_windows(self):
        wrapped = 'powershell -Command "Get-Date"'
        with patch("cli.tools.shell.os.name", "posix"):
            self.assertEqual(_unwrap_windows_powershell_wrapper(wrapped), wrapped)

    def test_windows_powershell_command_argv(self):
        with patch(
            "cli.tools.shell._windows_powershell_executable", return_value="powershell"
        ):
            argv = _windows_powershell_command_argv("Get-ChildItem -Force")
        self.assertEqual(
            argv,
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "$ProgressPreference = 'SilentlyContinue'; Get-ChildItem -Force",
            ],
        )

    def test_windows_powershell_command_argv_interactive_omits_noninteractive(self):
        with patch(
            "cli.tools.shell._windows_powershell_executable", return_value="powershell"
        ):
            argv = _windows_powershell_command_argv("python", interactive=True)
        self.assertNotIn("-NonInteractive", argv)
        self.assertEqual(
            argv[-1],
            "$ProgressPreference = 'SilentlyContinue'; python",
        )

    def test_windows_powershell_command_argv_multiline_uses_encoded_command(self):
        with patch(
            "cli.tools.shell._windows_powershell_executable", return_value="powershell"
        ):
            argv = _windows_powershell_command_argv("$a = 1\n$a")
        self.assertIn("-EncodedCommand", argv)
        encoded = argv[-1]
        self.assertEqual(
            base64.b64decode(encoded).decode("utf-16-le"),
            "$ProgressPreference = 'SilentlyContinue'; $a = 1\n$a",
        )

    def test_strip_powershell_clixml_output_removes_document_and_header(self):
        doc = (
            "#< CLIXML\r\n"
            "real output\r\n"
            "<Objs Version=\"1.1.0.1\" "
            "xmlns=\"http://schemas.microsoft.com/powershell/2004/04\">"
            "<Obj S=\"progress\" RefId=\"0\">x</Obj></Objs>\r\n"
        )
        cleaned = _strip_powershell_clixml_output(doc)
        self.assertNotIn("CLIXML", cleaned)
        self.assertNotIn("<Objs", cleaned)
        self.assertNotIn("</Objs>", cleaned)
        self.assertIn("real output", cleaned)

    def test_strip_powershell_clixml_output_handles_split_header_and_doc(self):
        text = (
            "#< CLIXML\n"
            "npx ccusage codex\n"
            "<Objs Version=\"1.1.0.1\" "
            "xmlns=\"http://schemas.microsoft.com/powershell/2004/04\">"
            "<Obj S=\"progress\" RefId=\"0\">"
            "<AV>Preparing modules for first use.</AV></Obj></Objs>\n"
        )
        cleaned = _strip_powershell_clixml_output(text)
        self.assertNotIn("CLIXML", cleaned)
        self.assertNotIn("<Objs", cleaned)
        self.assertNotIn("</Objs>", cleaned)
        self.assertEqual(cleaned.strip(), "npx ccusage codex")

    def test_strip_powershell_clixml_output_keeps_normal_output(self):
        self.assertEqual(
            _strip_powershell_clixml_output("hello\nworld\n"),
            "hello\nworld\n",
        )

    def test_clixml_stream_filter_drops_header_and_doc_split_across_chunks(self):
        stream = (
            "#< CLIXML\n"
            "real output line\n"
            '<Objs Version="1.1.0.1" '
            'xmlns="http://schemas.microsoft.com/powershell/2004/04">'
            "<Obj S=\"progress\" RefId=\"0\">"
            "<AV>Preparing modules for first use.</AV></Obj></Objs>\n"
        )
        f = _ClixmlStreamFilter()
        out = "".join(f.feed(stream[i : i + 5]) for i in range(0, len(stream), 5))
        out += f.flush()
        self.assertEqual(out, "real output line\n")

    def test_clixml_stream_filter_emits_plain_output_immediately(self):
        f = _ClixmlStreamFilter()
        self.assertEqual(f.feed("hello "), "hello ")
        self.assertEqual(f.feed("world\n"), "world\n")
        self.assertEqual(f.feed("tail"), "tail")
        f.flush()

    def test_clixml_stream_filter_buffers_doc_until_end_tag(self):
        f = _ClixmlStreamFilter()
        head = (
            "#< CLIXML\nreal\n"
            '<Objs Version="1.1.0.1" '
            'xmlns="http://schemas.microsoft.com/powershell/2004/04">'
        )
        self.assertEqual(f.feed(head), "real\n")
        self.assertEqual(f.feed("<Obj>data</Obj>"), "")
        self.assertEqual(f.feed("</Objs>"), "")
        self.assertEqual(f.feed("after\n"), "after\n")
        f.flush()

    def test_enforce_workspace_rg_for_shell_command_rewrites_plain_rg(self):
        agent = self._DummyAgent("D:/repo")
        with patch("cli.tools.shell.os.name", "nt"), patch(
            "cli.tools.shell._workspace_rg_executable_path",
            return_value=Path("D:/repo/bin/rg.exe"),
        ):
            rewritten = enforce_workspace_rg_for_shell_command(agent, "rg -n TODO src")
        self.assertTrue(rewritten.lower().startswith('"d:\\repo\\bin\\rg.exe"') or rewritten.lower().startswith("d:\\repo\\bin\\rg.exe"))
        self.assertIn("-n TODO src", rewritten)

    def test_enforce_workspace_rg_for_shell_command_rewrites_powershell_wrapped_rg(self):
        agent = self._DummyAgent("D:/repo")
        with patch("cli.tools.shell.os.name", "nt"), patch(
            "cli.tools.shell._workspace_rg_executable_path",
            return_value=Path("D:/repo/bin/rg.exe"),
        ):
            rewritten = enforce_workspace_rg_for_shell_command(
                agent,
                'powershell -ExecutionPolicy Bypass -Command "rg -n TODO src"',
            )
        self.assertIn("powershell -ExecutionPolicy Bypass -Command", rewritten)
        self.assertIn("D:\\repo\\bin\\rg.exe", rewritten)

    def test_normalize_shell_command_for_summary_hides_rg_executable_path(self):
        with patch("cli.tools.shell.os.name", "nt"):
            summary = normalize_shell_command_for_summary(
                'D:\\repo\\bin\\rg.exe -n TODO src'
            )
        self.assertEqual(summary, "rg -n TODO src")

    def test_normalize_shell_command_for_summary_hides_rg_path_in_powershell_payload(self):
        with patch("cli.tools.shell.os.name", "nt"):
            summary = normalize_shell_command_for_summary(
                'powershell -ExecutionPolicy Bypass -Command "D:\\repo\\bin\\rg.exe -n TODO src"'
            )
        self.assertIn("powershell -ExecutionPolicy Bypass -Command", summary)
        self.assertIn("rg -n TODO src", summary)
        self.assertNotIn("D:\\repo\\bin\\rg.exe", summary)

    def test_normalize_compat_no_change_for_simple_command(self):
        cmd = 'powershell -ExecutionPolicy Bypass -Command "Get-Date"'
        with patch("cli.tools.shell.os.name", "nt"):
            self.assertEqual(_normalize_windows_powershell_command_for_compat(cmd), cmd)

    def test_normalize_compat_decodes_literal_newlines_to_encoded_command(self):
        cmd = (
            'powershell -ExecutionPolicy Bypass -Command '
            '"$content = @\'\\n@echo off\\nsetlocal\\necho hi\\n\'@; '
            'Write-Output $content"'
        )
        with patch("cli.tools.shell.os.name", "nt"):
            normalized = _normalize_windows_powershell_command_for_compat(cmd)
        # Should switch to -EncodedCommand to dodge cmd.exe quoting.
        self.assertIn("-EncodedCommand", normalized)
        self.assertNotIn("\\n", normalized.split("-EncodedCommand", 1)[1])
        encoded = normalized.split("-EncodedCommand", 1)[1].strip()
        decoded = base64.b64decode(encoded).decode("utf-16-le")
        # Real LFs should appear where the model wrote ``\n``.
        self.assertIn("\n@echo off\nsetlocal\necho hi\n", decoded)
        # Here-string opener must be followed immediately by a real newline,
        # which is what made cmd.exe-dispatched scripts fail before.
        self.assertIn("@'\n", decoded)

    def test_normalize_compat_decodes_real_newlines_in_payload(self):
        cmd = (
            'powershell -ExecutionPolicy Bypass -Command '
            '"$x = @\'\necho hi\n\'@; Write-Output $x"'
        )
        with patch("cli.tools.shell.os.name", "nt"):
            normalized = _normalize_windows_powershell_command_for_compat(cmd)
        self.assertIn("-EncodedCommand", normalized)
        encoded = normalized.split("-EncodedCommand", 1)[1].strip()
        decoded = base64.b64decode(encoded).decode("utf-16-le")
        self.assertIn("@'\necho hi\n'@", decoded)

    def test_normalize_compat_handles_backslash_escaped_outer_quotes(self):
        cmd = (
            'powershell -ExecutionPolicy Bypass -Command '
            '\\"$x = @\'\\n@echo off\\n\'@; Write-Output $x\\"'
        )
        with patch("cli.tools.shell.os.name", "nt"):
            normalized = _normalize_windows_powershell_command_for_compat(cmd)
        self.assertIn("-EncodedCommand", normalized)
        encoded = normalized.split("-EncodedCommand", 1)[1].strip()
        decoded = base64.b64decode(encoded).decode("utf-16-le")
        self.assertIn("@'\n@echo off\n'@", decoded)
        # Backslash-escaped outer wrappers should be peeled — neither an
        # opening nor closing literal ``\"`` belongs in the final script.
        self.assertNotIn('\\"', decoded)

    def test_normalize_compat_noop_on_non_windows(self):
        cmd = (
            'powershell -ExecutionPolicy Bypass -Command '
            '"$x = @\'\\necho hi\\n\'@"'
        )
        with patch("cli.tools.shell.os.name", "posix"):
            self.assertEqual(_normalize_windows_powershell_command_for_compat(cmd), cmd)

    def test_git_diff_gets_no_pager_flag(self):
        with patch("cli.tools.shell.os.name", "nt"):
            rewritten = _enforce_git_no_pager_for_shell_command("git diff HEAD")
        self.assertEqual(rewritten, "git --no-pager diff HEAD")

    def test_git_show_gets_no_pager_flag(self):
        with patch("cli.tools.shell.os.name", "nt"):
            rewritten = _enforce_git_no_pager_for_shell_command("git show HEAD~1")
        self.assertEqual(rewritten, "git --no-pager show HEAD~1")

    def test_git_log_gets_no_pager_flag(self):
        with patch("cli.tools.shell.os.name", "nt"):
            rewritten = _enforce_git_no_pager_for_shell_command("git log --oneline -5")
        self.assertEqual(rewritten, "git --no-pager log --oneline -5")

    def test_git_stash_show_gets_no_pager_flag(self):
        with patch("cli.tools.shell.os.name", "nt"):
            rewritten = _enforce_git_no_pager_for_shell_command("git stash show -p")
        self.assertEqual(rewritten, "git --no-pager stash show -p")

    def test_existing_no_pager_is_not_duplicated(self):
        with patch("cli.tools.shell.os.name", "nt"):
            rewritten = _enforce_git_no_pager_for_shell_command("git --no-pager diff HEAD")
        self.assertEqual(rewritten, "git --no-pager diff HEAD")

    def test_non_pager_subcommands_are_left_alone(self):
        with patch("cli.tools.shell.os.name", "nt"):
            self.assertEqual(
                _enforce_git_no_pager_for_shell_command("git commit -m fix"),
                "git commit -m fix",
            )
            self.assertEqual(
                _enforce_git_no_pager_for_shell_command("git add ."),
                "git add .",
            )
            self.assertEqual(
                _enforce_git_no_pager_for_shell_command("git push origin main"),
                "git push origin main",
            )

    def test_git_exe_variant_gets_no_pager_flag(self):
        with patch("cli.tools.shell.os.name", "nt"):
            rewritten = _enforce_git_no_pager_for_shell_command("git.exe diff HEAD")
        self.assertEqual(rewritten, "git.exe --no-pager diff HEAD")

    def test_global_option_value_is_skipped_when_finding_subcommand(self):
        with patch("cli.tools.shell.os.name", "nt"):
            rewritten = _enforce_git_no_pager_for_shell_command("git -C repo diff HEAD")
        self.assertEqual(rewritten, "git --no-pager -C repo diff HEAD")

    def test_powershell_wrapped_git_diff_gets_no_pager_flag(self):
        with patch("cli.tools.shell.os.name", "nt"):
            rewritten = _enforce_git_no_pager_for_shell_command(
                'powershell -ExecutionPolicy Bypass -Command "git diff HEAD"'
            )
        self.assertIn("git --no-pager diff HEAD", rewritten)

    def test_cmd_wrapped_git_diff_gets_no_pager_flag(self):
        with patch("cli.tools.shell.os.name", "nt"):
            rewritten = _enforce_git_no_pager_for_shell_command(
                'cmd /c "git diff HEAD"'
            )
        self.assertIn("git --no-pager diff HEAD", rewritten)

    def test_no_pager_git_diff_still_read_only(self):
        self.assertTrue(_is_read_only_command("git --no-pager diff HEAD"))
        self.assertTrue(_is_read_only_command("git --no-pager log --oneline"))
        self.assertTrue(_is_read_only_command("git --no-pager status"))


class ReadOnlyGitCommandTests(unittest.TestCase):
    def test_strictly_readonly_git_subcommands(self):
        for cmd in [
            "git status",
            "git status --short",
            "git -C D:/repo status",
            "git -C D:/repo status --short",
            "git -C 'D:/path with space' status",
            "git --no-pager -C D:/repo log --oneline -5",
            "git -C D:/repo diff HEAD",
            "git -C D:/repo show --stat HEAD",
            "git rev-parse --abbrev-ref HEAD",
            "git ls-files --others --exclude-standard",
            "git -C D:/repo ls-tree HEAD",
            "git blame src/a.py",
            "git grep foo",
            "git config user.name",
            "git describe --tags",
            "git shortlog -n",
            "git -c core.quotepath=false status",
            "git --no-pager diff --stat",
            "git diff -- src/a.py",
        ]:
            self.assertTrue(_is_read_only_command(cmd), cmd)

    def test_conditional_readonly_listing_forms(self):
        for cmd in [
            "git branch",
            "git branch -a",
            "git branch -vv",
            "git branch --list",
            "git -C D:/repo branch --show-current",
            "git tag",
            "git tag -l",
            "git tag -n",
            "git remote",
            "git remote -v",
            "git remote show origin",
            "git remote get-url origin",
            "git stash list",
            "git stash show -p",
            "git submodule status",
            "git worktree list",
            "git notes show HEAD",
            "git notes list",
            "git reflog",
            "git reflog show HEAD",
            "git --version",
            "git --help",
            "git help status",
            "git",
        ]:
            self.assertTrue(_is_read_only_command(cmd), cmd)

    def test_writing_git_subcommands_not_readonly(self):
        for cmd in [
            "git add .",
            "git commit -m x",
            "git checkout master",
            "git checkout -- file",
            "git reset --hard",
            "git clean -fd",
            "git push origin main",
            "git pull",
            "git fetch",
            "git clone https://example.com/x",
            "git merge dev",
            "git rebase dev",
            "git cherry-pick abc",
            "git apply patch.diff",
            "git restore file",
            "git switch dev",
            "git rm file",
            "git mv a b",
            "git init",
            "git gc",
            "git prune",
            "git update-ref refs/x y",
            "git symbolic-ref HEAD refs/heads/x",
            "git stash",
            "git stash push",
            "git stash pop",
            "git stash apply",
            "git stash drop",
            "git stash clear",
            "git branch -d old",
            "git branch -D old",
            "git branch --delete old",
            "git branch -m new",
            "git branch -c copy",
            "git branch -u origin/main",
            "git tag -d v1",
            "git tag -a v1 -m msg",
            "git tag -f v1",
            "git remote add origin url",
            "git remote remove origin",
            "git remote set-url origin url",
            "git remote prune origin",
            "git submodule add https://example.com/x",
            "git submodule update",
            "git worktree add ../wt",
            "git notes add -m x",
            "git reflog expire --expire=now --all",
        ]:
            self.assertFalse(_is_read_only_command(cmd), cmd)

    def test_redirect_or_pipe_disqualifies_git_whitelist(self):
        self.assertFalse(_is_read_only_command("git log > out.txt"))
        self.assertFalse(_is_read_only_command("git status | grep x"))


class ReadOnlyTestAndTypecheckCommandTests(unittest.TestCase):
    def test_npx_vitest_run_is_read_only(self):
        for cmd in [
            "npx vitest run",
            "npx vitest run --coverage",
            "npx --yes vitest run src/foo.test.ts",
            "npx vitest run --reporter=json",
        ]:
            self.assertTrue(_is_read_only_command(cmd), cmd)

    def test_npx_tsc_is_read_only(self):
        for cmd in [
            "npx tsc",
            "npx tsc --noEmit",
            "npx tsc -p tsconfig.json",
            "npx --yes tsc --noEmit -p tsconfig.json",
        ]:
            self.assertTrue(_is_read_only_command(cmd), cmd)

    def test_python_test_runners_are_read_only(self):
        for cmd in [
            "python -m pytest",
            "python -m pytest tests/unit -q",
            "python3 -m pytest -x",
            "py -m pytest tests",
            "python -m unittest",
            "python -m unittest discover -s tests",
            "python3 -m unittest tests.test_foo",
            "pytest",
            "pytest tests/unit -q",
        ]:
            self.assertTrue(_is_read_only_command(cmd), cmd)

    def test_redirect_or_pipe_still_disqualifies(self):
        self.assertFalse(_is_read_only_command("npx vitest run > out.txt"))
        self.assertFalse(_is_read_only_command("pytest | tee log.txt"))

    def test_normalize_windows_path_separators_converts_executable_and_paths(self):
        cmd = (
            ".venv-windows/Scripts/python.exe -m pytest "
            "cli/tests/unit/shell/test_shell_command_policy.py -q"
        )
        with patch("cli.tools.shell.os.name", "nt"):
            rewritten = _normalize_windows_shell_path_separators(cmd)
        self.assertEqual(
            rewritten,
            ".venv-windows\\Scripts\\python.exe -m pytest "
            "cli\\tests\\unit\\shell\\test_shell_command_policy.py -q",
        )

    def test_normalize_windows_path_separators_converts_cmd_builtin_args(self):
        # cmd.exe built-ins treat ``/`` as a switch (``Invalid switch``).
        with patch("cli.tools.shell.os.name", "nt"):
            self.assertEqual(
                _normalize_windows_shell_path_separators("del cli/tests/x.txt"),
                "del cli\\tests\\x.txt",
            )
            self.assertEqual(
                _normalize_windows_shell_path_separators("type C:/Users/foo/readme.txt"),
                "type C:\\Users\\foo\\readme.txt",
            )

    def test_normalize_windows_path_separators_leaves_urls_and_flags(self):
        with patch("cli.tools.shell.os.name", "nt"):
            for cmd in [
                "git clone https://github.com/a/b.git",
                "git clone git@github.com:org/repo.git",
                "python -m http.server 8000 --directory public/",
                'python -c "print(1)"',
                "docker run -v /host/path:/container/path image",
            ]:
                self.assertEqual(
                    _normalize_windows_shell_path_separators(cmd), cmd, cmd
                )

    def test_normalize_windows_path_separators_skips_pattern_first_tools(self):
        with patch("cli.tools.shell.os.name", "nt"):
            for cmd in [
                'rg -n "src/foo.py" cli',
                "grep -r 'foo/bar' src",
                "git add cli/tests/x.py",
                "git log -- cli/tests/x.py",
            ]:
                self.assertEqual(
                    _normalize_windows_shell_path_separators(cmd), cmd, cmd
                )

    def test_normalize_windows_path_separators_skips_powershell_payloads(self):
        cmd = (
            'powershell -ExecutionPolicy Bypass -Command '
            '"Get-ChildItem C:/Users/foo"'
        )
        with patch("cli.tools.shell.os.name", "nt"):
            self.assertEqual(_normalize_windows_shell_path_separators(cmd), cmd)

    def test_normalize_windows_path_separators_rewrites_cmd_c_payload(self):
        with patch("cli.tools.shell.os.name", "nt"):
            rewritten = _normalize_windows_shell_path_separators(
                'cmd /c "del cli/tests/x.txt"'
            )
        self.assertIn("cli\\tests\\x.txt", rewritten)

    def test_normalize_windows_path_separators_noop_on_non_windows(self):
        cmd = "del cli/tests/x.txt"
        with patch("cli.tools.shell.os.name", "posix"):
            self.assertEqual(_normalize_windows_shell_path_separators(cmd), cmd)


class StripRedundantCdPrefixTests(unittest.TestCase):
    """``cd <workspace-root>;`` (PowerShell) / ``&&`` (cmd) prefixes are
    stripped from the GUI/TUI tool-call description; anything else is kept."""

    def _agent_with_root(self, root: str):
        return type("Agent", (), {"workspace_root": root})()

    def test_strips_powershell_semicolon_cd_to_workspace_root(self):
        with tempfile.TemporaryDirectory() as root:
            agent = self._agent_with_root(root)
            self.assertEqual(
                strip_redundant_cd_prefix(agent, f"cd {root}; git status"),
                "git status",
            )

    def test_strips_semicolon_cd_with_space_before_separator(self):
        with tempfile.TemporaryDirectory() as root:
            agent = self._agent_with_root(root)
            self.assertEqual(
                strip_redundant_cd_prefix(agent, f"cd {root} ; git status"),
                "git status",
            )

    def test_strips_quoted_semicolon_cd(self):
        with tempfile.TemporaryDirectory() as root:
            agent = self._agent_with_root(root)
            self.assertEqual(
                strip_redundant_cd_prefix(agent, f'cd "{root}"; git status'),
                "git status",
            )

    def test_keeps_cmd_and_delimiter_support(self):
        with tempfile.TemporaryDirectory() as root:
            agent = self._agent_with_root(root)
            self.assertEqual(
                strip_redundant_cd_prefix(agent, f"cd /d {root} && git status"),
                "git status",
            )
            self.assertEqual(
                strip_redundant_cd_prefix(agent, f"cd {root} && git status"),
                "git status",
            )

    def test_keeps_cd_to_other_directory(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as other:
            agent = self._agent_with_root(root)
            cmd = f"cd {other}; git status"
            self.assertEqual(strip_redundant_cd_prefix(agent, cmd), cmd)

    def test_keeps_cd_to_workspace_subdirectory(self):
        with tempfile.TemporaryDirectory() as root:
            sub = Path(root) / "sub"
            sub.mkdir()
            agent = self._agent_with_root(root)
            cmd = f"cd {sub}; git status"
            self.assertEqual(strip_redundant_cd_prefix(agent, cmd), cmd)

    def test_keeps_relative_cd_to_parent(self):
        with tempfile.TemporaryDirectory() as root:
            agent = self._agent_with_root(root)
            cmd = "cd ..; git status"
            self.assertEqual(strip_redundant_cd_prefix(agent, cmd), cmd)

    def test_keeps_bare_cd_without_separator(self):
        with tempfile.TemporaryDirectory() as root:
            agent = self._agent_with_root(root)
            cmd = f"cd {root}"
            self.assertEqual(strip_redundant_cd_prefix(agent, cmd), cmd)

    def test_keeps_plain_command(self):
        with tempfile.TemporaryDirectory() as root:
            agent = self._agent_with_root(root)
            self.assertEqual(strip_redundant_cd_prefix(agent, "git status"), "git status")

    def test_falls_back_to_shell_cwd_when_workspace_root_missing(self):
        with tempfile.TemporaryDirectory() as root:
            agent = type("Agent", (), {"work_directory": Path(root)})()
            self.assertEqual(
                strip_redundant_cd_prefix(agent, f"cd {root}; git status"),
                "git status",
            )


if __name__ == "__main__":
    unittest.main()
