import base64
import unittest
from pathlib import Path
from unittest.mock import patch

from cli.tools.shell import _enforce_windows_powershell_command_prefix
from cli.tools.shell import _normalize_windows_powershell_command_for_compat
from cli.tools.shell import _enforce_git_no_pager_for_shell_command
from cli.tools.shell import enforce_workspace_rg_for_shell_command
from cli.tools.shell import _is_read_only_command
from cli.tools.shell import normalize_shell_command_for_summary


class ShellCommandPolicyTests(unittest.TestCase):
    class _DummyAgent:
        def __init__(self, repo_root: str):
            self._self_repo_root = repo_root

    def test_windows_powershell_requires_bypass_command_prefix(self):
        with patch("cli.tools.shell.os.name", "nt"):
            res = _enforce_windows_powershell_command_prefix(
                'powershell -Command "Get-ChildItem -Force"'
            )
        self.assertFalse(res.get("ok", True))
        self.assertIn("ExecutionPolicy Bypass -Command", str(res.get("error", "")))

    def test_windows_powershell_exe_is_normalized(self):
        with patch("cli.tools.shell.os.name", "nt"):
            res = _enforce_windows_powershell_command_prefix(
                'powershell.exe -ExecutionPolicy Bypass -Command "Get-Date"'
            )
        self.assertTrue(res.get("ok"))
        self.assertEqual(
            res.get("command"),
            'powershell -ExecutionPolicy Bypass -Command "Get-Date"',
        )

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

    def test_enforce_strips_outer_double_quote_wrapper(self):
        with patch("cli.tools.shell.os.name", "nt"):
            res = _enforce_windows_powershell_command_prefix(
                '"powershell -ExecutionPolicy Bypass -Command \\"Get-Date\\""'
            )
        self.assertTrue(res.get("ok"))
        self.assertNotIn(
            '"powershell',
            res.get("command", ""),
            "Outer wrapper should have been stripped before dispatch",
        )

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


if __name__ == "__main__":
    unittest.main()
