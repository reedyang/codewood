import ctypes
import json
import io
import os
import struct
import tempfile
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest.mock import patch

from cli.core.sandbox.windows import (
    SANDBOX_CAP_SID_FILENAME,
    SANDBOX_USER_OFFLINE,
    SANDBOX_USER_ONLINE,
    SANDBOX_USERS_GROUP,
    WAIT_OBJECT_0,
    WAIT_TIMEOUT,
    WindowsSandboxBackend,
    WindowsSandboxProcess,
    _acl_record_path,
    _build_env_block,
    _cap_sid_path,
    _credential_check_cache,
    _ensure_sandbox_runner_copy,
    _flag_path,
    _load_acl_record,
    _load_pending_cleanup,
    _load_secret,
    _missing_profile_read_dirs,
    _random_password,
    _record_acl_dirs,
    _rebuilt_flag_path,
    _secret_path,
    _save_secret,
    _users_ready_path,
    launch_elevated_setup,
    _run_set_password,
    _shell_runner_exe_path,
    _select_user,
)


def _patch_shared_root(tmp_path):
    """Redirect the shared sandbox state root into a temp dir for a test."""
    return patch(
        "cli.core.sandbox.windows._shared_sandbox_root", return_value=Path(tmp_path)
    )


def _start_shared_root_patch(testcase):
    testcase._root_patch = _patch_shared_root(testcase._tmp.name)
    testcase._root_patch.start()
    testcase.addCleanup(testcase._root_patch.stop)
    testcase.shared = Path(testcase._tmp.name)


class _SyncThread:
    """Run spawned background threads inline so tests stay deterministic."""

    def __init__(self, target=None, daemon=None, **kwargs):
        self._target = target
        self._args = tuple(kwargs.get("args", ()) or ())

    def start(self):
        if self._target is not None:
            self._target(*self._args)


class RandomPasswordTests(unittest.TestCase):
    def test_contains_all_character_classes(self):
        for _ in range(50):
            pw = _random_password()
            self.assertGreaterEqual(len(pw), 14)
            self.assertTrue(any(c.isupper() for c in pw), pw)
            self.assertTrue(any(c.islower() for c in pw), pw)
            self.assertTrue(any(c.isdigit() for c in pw), pw)
            self.assertTrue(any(not c.isalnum() for c in pw), pw)

    def test_respects_requested_length(self):
        for length in (14, 15, 16, 20):
            self.assertEqual(len(_random_password(length)), length)


@unittest.skipUnless(os.name == "nt", "Windows sandbox backend requires ctypes.WinDLL")
class WindowsSandboxBackendProvisionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.config_dir = Path(self._tmp.name)
        _start_shared_root_patch(self)
        self.backend = WindowsSandboxBackend()

    def tearDown(self):
        self._tmp.cleanup()

    def test_provision_retries_when_password_policy_rejects(self):
        # Seed a secret whose offline password has no digits (the real-world
        # case: a random 14-char password that fails the complexity policy).
        _save_secret(
            self.config_dir,
            {"offline": "AAAABBBBCCCCDD", "online": _random_password()},
        )
        progress = []

        def fake_run(argv, timeout=180, stdin_data=None):
            cmd = " ".join(argv)
            if "New-LocalUser" in cmd and "CodewoodSandOffline" in cmd:
                if "AAAABBBBCCCCDD" in cmd:
                    return SimpleNamespace(
                        returncode=1, stderr="InvalidPasswordException", stdout=""
                    )
            if "New-LocalUser" in cmd:
                return SimpleNamespace(returncode=0, stderr="", stdout="")
            return SimpleNamespace(returncode=0, stderr="", stdout="")

        with patch(
            "cli.core.sandbox.windows._user_exists", return_value=True
        ), patch(
            "cli.core.sandbox.windows._run_process", side_effect=fake_run
        ), patch(
            "cli.core.sandbox.windows._load_or_create_cap_sids",
            return_value={"workspace": "S-1-1", "readonly": "S-1-2"},
        ), patch(
            "cli.core.sandbox.windows._ps_grant_modify_sid"
        ), patch(
            "cli.core.sandbox.windows._ps_grant_read_group", return_value=0
        ), patch(
            "cli.core.sandbox.windows.threading.Thread", _SyncThread
        ):
            result = self.backend.provision(
                self.config_dir, None, "workspace_write", progress=progress.append
            )

        self.assertTrue(result["ok"], result["errors"])
        # The rejected password must have been replaced and persisted.
        saved = _load_secret(self.config_dir)
        self.assertNotEqual(saved["offline"], "AAAABBBBCCCCDD")
        self.assertTrue(any(c.isdigit() for c in saved["offline"]))
        self.assertTrue(any(not c.isalnum() for c in saved["offline"]))
        # A retry attempt was announced on the console stream.
        self.assertTrue(any("(attempt 2)" in line for line in progress))

    def test_provision_cleans_old_user_acls_before_deletion(self):
        _save_secret(
            self.config_dir,
            {"offline": _random_password(), "online": _random_password()},
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            calls = []

            def fake_run(argv, timeout=180, stdin_data=None):
                calls.append(" ".join(argv))
                return SimpleNamespace(returncode=0, stderr="", stdout="")

            with patch(
                "cli.core.sandbox.windows._user_exists", return_value=True
            ), patch(
                "cli.core.sandbox.windows._run_process", side_effect=fake_run
            ), patch(
                "cli.core.sandbox.windows._load_or_create_cap_sids",
                return_value={
                    "workspace": "S-1-5-21-1-2-3-4",
                    "readonly": "S-1-5-21-5-6-7-8",
                },
            ), patch(
                "cli.core.sandbox.windows._ps_grant_modify_sid"
            ), patch(
                "cli.core.sandbox.windows._ps_grant_read_group", return_value=0
            ), patch(
                "cli.core.sandbox.windows.threading.Thread", _SyncThread
            ):
                result = self.backend.provision(
                    self.config_dir, str(root), "workspace_write"
                )
            self.assertTrue(result["ok"], result["errors"])
            remove_idx = next(
                i for i, c in enumerate(calls) if "Remove-LocalUser" in c
            )
            cleanup_idx = next(
                i for i, c in enumerate(calls) if "icacls" in c and "/remove:g" in c
            )
            self.assertLess(cleanup_idx, remove_idx)

    def test_provision_grants_profile_read_and_records_it(self):
        _save_secret(
            self.config_dir,
            {"offline": _random_password(), "online": _random_password()},
        )
        granted_timeouts = []

        def fake_grant(timeout=1800):
            granted_timeouts.append(timeout)
            return True

        with patch(
            "cli.core.sandbox.windows._user_exists", return_value=True
        ), patch(
            "cli.core.sandbox.windows._run_process",
            return_value=SimpleNamespace(returncode=0, stderr="", stdout=""),
        ), patch(
            "cli.core.sandbox.windows._load_or_create_cap_sids",
            return_value={"workspace": "S-1-1", "readonly": "S-1-2"},
        ), patch(
            "cli.core.sandbox.windows._ps_grant_modify_sid"
        ), patch(
            "cli.core.sandbox.windows._grant_profile_read", side_effect=fake_grant
        ):
            result = self.backend.provision(
                self.config_dir, None, "workspace_write"
            )

        self.assertTrue(result["ok"], result["errors"])
        # Provisioning must request the profile read grant with a generous
        # timeout (the first grant propagates the ACE over the whole profile)
        # and record the wildcard entry so a rebuild sweeps the subdirectories.
        # It is called once explicitly and once through the workspace-ACL
        # refresh.
        self.assertTrue(granted_timeouts)
        self.assertTrue(all(t == 1800 for t in granted_timeouts))
        self.assertIn(str(Path.home() / "*"), _load_acl_record())

    def test_provision_ignores_profile_read_failure(self):
        _save_secret(
            self.config_dir,
            {"offline": _random_password(), "online": _random_password()},
        )
        with patch(
            "cli.core.sandbox.windows._user_exists", return_value=True
        ), patch(
            "cli.core.sandbox.windows._run_process",
            return_value=SimpleNamespace(returncode=0, stderr="", stdout=""),
        ), patch(
            "cli.core.sandbox.windows._load_or_create_cap_sids",
            return_value={"workspace": "S-1-1", "readonly": "S-1-2"},
        ), patch(
            "cli.core.sandbox.windows._ps_grant_modify_sid"
        ), patch(
            "cli.core.sandbox.windows._grant_profile_read", return_value=False
        ):
            result = self.backend.provision(
                self.config_dir, None, "workspace_write"
            )

        # Directories that require elevation simply fail; that must not make
        # the whole provisioning fail.
        self.assertTrue(result["ok"], result["errors"])

    def test_provision_users_refreshes_password_in_place(self):
        # Credentials mismatch (first verify fails) but both accounts exist;
        # the in-place refresh restores logon, so the accounts (and their
        # SIDs) are kept: no ACL sweep, no delete/recreate.
        original = {"offline": _random_password(), "online": _random_password()}
        _save_secret(self.config_dir, original)
        calls = []

        def fake_run(argv, timeout=180, stdin_data=None):
            calls.append(" ".join(argv))
            return SimpleNamespace(returncode=0, stderr="", stdout="")

        with patch(
            "cli.core.sandbox.windows._user_exists", return_value=True
        ), patch(
            "cli.core.sandbox.windows._run_process", side_effect=fake_run
        ), patch.object(
            self.backend,
            "verify_credentials",
            side_effect=[False, True],
        ), patch(
            "cli.core.sandbox.windows._load_or_create_cap_sids",
            return_value={"workspace": "S-1-1", "readonly": "S-1-2"},
        ), patch(
            "cli.core.sandbox.windows._ps_grant_modify_sid"
        ), patch(
            "cli.core.sandbox.windows._grant_profile_read", return_value=True
        ):
            result = self.backend.provision_users(
                self.config_dir, None, "workspace_write"
            )

        self.assertTrue(result["ok"], result["errors"])
        joined = "\n".join(calls)
        self.assertIn("Set-LocalUser", joined)
        self.assertNotIn("Remove-LocalUser", joined)
        self.assertNotIn("New-LocalUser", joined)
        self.assertNotIn("icacls /remove:g", joined)
        # The stored secret is untouched on the refresh path.
        self.assertEqual(_load_secret(self.config_dir), original)
        # Group membership is still ensured for the kept accounts.
        self.assertIn("Add-LocalGroupMember", joined)

    def test_provision_users_rotates_password_when_policy_rejects(self):
        # The local password policy (history/complexity) rejects setting the
        # account password back to the stored secret; the refresh must rotate
        # to a fresh password, persist it, and keep the account SID instead
        # of falling back to delete+recreate.
        _save_secret(
            self.config_dir,
            {"offline": "OLDPASSW0RD!", "online": _random_password()},
        )
        calls = []

        def fake_run(argv, timeout=180, stdin_data=None):
            cmd = " ".join(argv)
            calls.append(cmd)
            if "Set-LocalUser" in cmd and "OLDPASSW0RD!" in cmd:
                return SimpleNamespace(
                    returncode=1, stderr="InvalidPasswordException", stdout=""
                )
            return SimpleNamespace(returncode=0, stderr="", stdout="")

        with patch(
            "cli.core.sandbox.windows._user_exists", return_value=True
        ), patch(
            "cli.core.sandbox.windows._run_process", side_effect=fake_run
        ), patch.object(
            self.backend,
            "verify_credentials",
            side_effect=[False, True],
        ), patch(
            "cli.core.sandbox.windows._load_or_create_cap_sids",
            return_value={"workspace": "S-1-1", "readonly": "S-1-2"},
        ), patch(
            "cli.core.sandbox.windows._ps_grant_modify_sid"
        ), patch(
            "cli.core.sandbox.windows._grant_profile_read", return_value=True
        ):
            result = self.backend.provision_users(
                self.config_dir, None, "workspace_write"
            )

        self.assertTrue(result["ok"], result["errors"])
        joined = "\n".join(calls)
        # No SID-changing rebuild: the account is kept, only the secret
        # rotates to a fresh password that satisfies the policy.
        self.assertNotIn("Remove-LocalUser", joined)
        self.assertNotIn("New-LocalUser", joined)
        saved = _load_secret(self.config_dir)
        self.assertNotEqual(saved["offline"], "OLDPASSW0RD!")
        self.assertGreaterEqual(len(saved["offline"]), 14)
        self.assertTrue(any(c.isdigit() for c in saved["offline"]))
        self.assertTrue(any(not c.isalnum() for c in saved["offline"]))

    def test_provision_users_falls_back_to_recreate_when_refresh_fails(self):
        # Set-LocalUser fails (e.g. foreign/disabled account), so the old
        # behaviour runs: sweep first, then delete and recreate the users.
        _save_secret(
            self.config_dir,
            {"offline": _random_password(), "online": _random_password()},
        )
        calls = []

        def fake_run(argv, timeout=180, stdin_data=None):
            cmd = " ".join(argv)
            calls.append(cmd)
            if "Set-LocalUser" in cmd:
                return SimpleNamespace(returncode=1, stderr="boom", stdout="")
            return SimpleNamespace(returncode=0, stderr="", stdout="")

        with patch(
            "cli.core.sandbox.windows._user_exists", return_value=True
        ), patch(
            "cli.core.sandbox.windows._run_process", side_effect=fake_run
        ), patch.object(
            self.backend,
            "verify_credentials",
            return_value=False,
        ), patch(
            "cli.core.sandbox.windows._load_or_create_cap_sids",
            return_value={"workspace": "S-1-1", "readonly": "S-1-2"},
        ), patch(
            "cli.core.sandbox.windows._ps_grant_modify_sid"
        ), patch(
            "cli.core.sandbox.windows._grant_profile_read", return_value=True
        ):
            result = self.backend.provision_users(
                self.config_dir, None, "workspace_write"
            )

        self.assertTrue(result["ok"], result["errors"])
        joined = "\n".join(calls)
        self.assertIn("Set-LocalUser", joined)
        self.assertIn("Remove-LocalUser", joined)
        self.assertIn("New-LocalUser", joined)
        # The sweep still runs before the SID-changing deletion.
        cleanup_idx = next(
            i for i, c in enumerate(calls) if "icacls" in c and "/remove:g" in c
        )
        remove_idx = next(i for i, c in enumerate(calls) if "Remove-LocalUser" in c)
        self.assertLess(cleanup_idx, remove_idx)

    def test_provision_users_writes_ready_flag(self):
        _save_secret(
            self.config_dir,
            {"offline": _random_password(), "online": _random_password()},
        )
        with patch(
            "cli.core.sandbox.windows._user_exists", return_value=True
        ), patch(
            "cli.core.sandbox.windows._run_process",
            return_value=SimpleNamespace(returncode=0, stderr="", stdout=""),
        ), patch(
            "cli.core.sandbox.windows._load_or_create_cap_sids",
            return_value={"workspace": "S-1-1", "readonly": "S-1-2"},
        ), patch(
            "cli.core.sandbox.windows._ps_grant_modify_sid"
        ), patch(
            "cli.core.sandbox.windows._grant_profile_read", return_value=True
        ):
            result = self.backend.provision_users(
                self.config_dir, None, "workspace_write"
            )

        self.assertTrue(result["ok"], result["errors"])
        ready = _users_ready_path(self.config_dir)
        self.assertTrue(ready.exists())
        data = json.loads(ready.read_text(encoding="utf-8"))
        self.assertTrue(data["users_ready"])
        self.assertTrue(data["firewall_ok"])

    def test_provision_users_hides_users_from_signin_screen(self):
        _save_secret(
            self.config_dir,
            {"offline": _random_password(), "online": _random_password()},
        )
        calls = []

        def fake_run(argv, timeout=180, stdin_data=None):
            calls.append(" ".join(argv))
            return SimpleNamespace(returncode=0, stderr="", stdout="")

        with patch(
            "cli.core.sandbox.windows._user_exists", return_value=True
        ), patch(
            "cli.core.sandbox.windows._run_process", side_effect=fake_run
        ), patch(
            "cli.core.sandbox.windows._load_or_create_cap_sids",
            return_value={"workspace": "S-1-1", "readonly": "S-1-2"},
        ), patch(
            "cli.core.sandbox.windows._ps_grant_modify_sid"
        ), patch(
            "cli.core.sandbox.windows._grant_profile_read", return_value=True
        ):
            result = self.backend.provision_users(
                self.config_dir, None, "workspace_write"
            )

        self.assertTrue(result["ok"], result["errors"])
        reg_calls = [c for c in calls if c.startswith("reg.exe add")]
        self.assertEqual(len(reg_calls), 2)
        joined = " ".join(reg_calls)
        self.assertIn(SANDBOX_USER_OFFLINE, joined)
        self.assertIn(SANDBOX_USER_ONLINE, joined)
        self.assertIn(
            "HKLM\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion"
            "\\Winlogon\\SpecialAccounts\\UserList",
            joined,
        )
        self.assertIn("REG_DWORD", joined)
        self.assertIn(" /d 0 /f", joined)

    def test_provision_users_writes_rebuilt_flag_when_recreating(self):
        _save_secret(
            self.config_dir,
            {"offline": _random_password(), "online": _random_password()},
        )
        flag = _rebuilt_flag_path(self.config_dir)
        self.assertFalse(flag.exists())

        def fake_run(argv, timeout=180, stdin_data=None):
            cmd = " ".join(argv)
            if "Set-LocalUser" in cmd:
                return SimpleNamespace(returncode=1, stderr="boom", stdout="")
            return SimpleNamespace(returncode=0, stderr="", stdout="")

        with patch(
            "cli.core.sandbox.windows._user_exists", return_value=True
        ), patch(
            "cli.core.sandbox.windows._run_process", side_effect=fake_run
        ), patch.object(
            self.backend,
            "verify_credentials",
            return_value=False,
        ), patch(
            "cli.core.sandbox.windows._load_or_create_cap_sids",
            return_value={"workspace": "S-1-1", "readonly": "S-1-2"},
        ), patch(
            "cli.core.sandbox.windows._ps_grant_modify_sid"
        ), patch(
            "cli.core.sandbox.windows._grant_profile_read", return_value=True
        ):
            result = self.backend.provision_users(
                self.config_dir, None, "workspace_write"
            )

        self.assertTrue(result["ok"], result["errors"])
        # The rebuild must leave a marker so the serve-side ACL phase knows
        # to re-propagate the sandbox ACEs over pre-existing files.
        self.assertTrue(flag.exists())

    def test_provision_users_keeps_no_rebuilt_flag_when_refreshed(self):
        _save_secret(
            self.config_dir,
            {"offline": _random_password(), "online": _random_password()},
        )
        with patch(
            "cli.core.sandbox.windows._user_exists", return_value=True
        ), patch(
            "cli.core.sandbox.windows._run_process",
            return_value=SimpleNamespace(returncode=0, stderr="", stdout=""),
        ), patch.object(
            self.backend,
            "verify_credentials",
            side_effect=[False, True],
        ), patch(
            "cli.core.sandbox.windows._load_or_create_cap_sids",
            return_value={"workspace": "S-1-1", "readonly": "S-1-2"},
        ), patch(
            "cli.core.sandbox.windows._ps_grant_modify_sid"
        ), patch(
            "cli.core.sandbox.windows._grant_profile_read", return_value=True
        ):
            result = self.backend.provision_users(
                self.config_dir, None, "workspace_write"
            )

        self.assertTrue(result["ok"], result["errors"])
        # In-place password refresh keeps the SIDs: no rebuild, no flag.
        self.assertFalse(_rebuilt_flag_path(self.config_dir).exists())

    def test_provision_acls_repropagates_after_rebuild(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            (ws / ".git").mkdir()
            _rebuilt_flag_path(self.config_dir).write_text(
                json.dumps({"rebuilt_at": 1}), encoding="utf-8"
            )
            calls = []

            def fake_run(argv, timeout=180, stdin_data=None):
                calls.append(" ".join(argv))
                return SimpleNamespace(returncode=0, stderr="", stdout="")

            with patch(
                "cli.core.sandbox.windows._user_exists", return_value=True
            ), patch(
                "cli.core.sandbox.windows._run_process", side_effect=fake_run
            ), patch(
                "cli.core.sandbox.windows._load_or_create_cap_sids",
                return_value={"workspace": "S-1-1", "readonly": "S-1-2"},
            ), patch(
                "cli.core.sandbox.windows._ps_grant_modify_sid"
            ), patch(
                "cli.core.sandbox.windows._ps_grant_read_group", return_value=0
            ), patch(
                "cli.core.sandbox.windows._grant_profile_read", return_value=True
            ), patch(
                "cli.core.sandbox.windows.threading.Thread", _SyncThread
            ):
                result = self.backend.provision_acls(
                    self.config_dir, str(ws), "workspace_write"
                )

            self.assertTrue(result["ok"], result["errors"])
            joined = "\n".join(calls)
            self.assertIn("Apply-Tree", joined)
            # Workspace in write mode, runtime dirs in runtime mode, and the
            # protected .git subtree excluded from the grant propagation.
            self.assertIn("'write'", joined)
            self.assertIn("'runtime'", joined)
            self.assertIn(str((ws / ".git").resolve()), joined)
            # The marker is consumed once the propagation finished.
            self.assertFalse(_rebuilt_flag_path(self.config_dir).exists())

    def test_provision_acls_skips_repropagation_without_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            (ws / ".git").mkdir()
            calls = []

            def fake_run(argv, timeout=180, stdin_data=None):
                calls.append(" ".join(argv))
                return SimpleNamespace(returncode=0, stderr="", stdout="")

            with patch(
                "cli.core.sandbox.windows._user_exists", return_value=True
            ), patch(
                "cli.core.sandbox.windows._run_process", side_effect=fake_run
            ), patch(
                "cli.core.sandbox.windows._load_or_create_cap_sids",
                return_value={"workspace": "S-1-1", "readonly": "S-1-2"},
            ), patch(
                "cli.core.sandbox.windows._ps_grant_modify_sid"
            ), patch(
                "cli.core.sandbox.windows._ps_grant_read_group", return_value=0
            ), patch(
                "cli.core.sandbox.windows._grant_profile_read", return_value=True
            ):
                result = self.backend.provision_acls(
                    self.config_dir, str(ws), "workspace_write"
                )

            self.assertTrue(result["ok"], result["errors"])
            self.assertNotIn("Apply-Tree", "\n".join(calls))
            self.assertFalse(_rebuilt_flag_path(self.config_dir).exists())

    def test_provision_acls_refuses_without_users(self):
        with patch("cli.core.sandbox.windows._user_exists", return_value=False):
            result = self.backend.provision_acls(self.config_dir, None)
        self.assertFalse(result["ok"])
        self.assertTrue(
            any("users are missing" in e for e in result["errors"])
        )
        self.assertFalse(_flag_path(self.config_dir).exists())

    def test_wait_and_provision_acls_runs_after_ready_flag(self):
        _users_ready_path(self.config_dir).write_text(
            json.dumps({"users_ready": True, "firewall_ok": True}),
            encoding="utf-8",
        )
        calls = []

        def fake_acls(*args, **kwargs):
            calls.append(args)
            return {"ok": True}

        with patch.object(self.backend, "provision_acls", side_effect=fake_acls):
            result = self.backend.wait_and_provision_acls(
                self.config_dir, None, "workspace_write", timeout=5
            )
        self.assertTrue(result["ok"])
        self.assertEqual(len(calls), 1)

    def test_cleanup_all_recorded_acls_sweeps_runtime_root(self):
        calls = []
        with patch(
            "cli.core.sandbox.windows._run_process",
            side_effect=lambda argv, timeout=180, stdin_data=None: (
                calls.append(argv)
                or SimpleNamespace(returncode=0, stderr="", stdout="")
            ),
        ):
            self.backend.cleanup_all_recorded_acls(self.config_dir)
        remove_g = [c for c in calls if c[0] == "icacls" and "/remove:g" in c]
        self.assertGreaterEqual(len(remove_g), 2)
        self.assertIn(SANDBOX_USER_OFFLINE, remove_g[0])
        self.assertIn(SANDBOX_USER_ONLINE, remove_g[1])

    def test_pending_cleanup_journal_resumes_after_interruption(self):
        rec = [str(Path(self._tmp.name) / "ws1"), str(Path(self._tmp.name) / "ws2")]
        _record_acl_dirs(rec)
        recorded = sorted(_load_acl_record())
        swept = []
        state = {"fails": 1}

        def fake_cleanup(workspace_root, config_dir=None):
            swept.append(workspace_root)
            if state["fails"] > 0:
                state["fails"] -= 1
                raise RuntimeError("interrupted mid-sweep")

        with patch(
            "cli.core.sandbox.windows._run_process",
            return_value=SimpleNamespace(returncode=0, stderr="", stdout=""),
        ), patch.object(
            self.backend, "cleanup_workspace_acls", side_effect=fake_cleanup
        ):
            self.backend.cleanup_all_recorded_acls(self.config_dir)
        # The interrupted entry stays journaled for the next startup.
        pending = _load_pending_cleanup(self.config_dir)
        self.assertEqual(len(pending), 1)
        self.assertIn(recorded[0], pending)
        with patch(
            "cli.core.sandbox.windows._run_process",
            return_value=SimpleNamespace(returncode=0, stderr="", stdout=""),
        ), patch.object(self.backend, "cleanup_workspace_acls"):
            self.backend.resume_pending_cleanup(self.config_dir)
        self.assertFalse(_load_pending_cleanup(self.config_dir))

    def test_status_provisioned_with_users_ready_only(self):
        _save_secret(
            self.config_dir, {"offline": "X1", "online": "Y2"}
        )
        _cap_sid_path(self.config_dir).write_text("{}", encoding="utf-8")
        _users_ready_path(self.config_dir).write_text(
            json.dumps({"users_ready": True, "firewall_ok": True}),
            encoding="utf-8",
        )
        with patch("cli.core.sandbox.windows._user_exists", return_value=True):
            status = self.backend.status(self.config_dir)
        # The background ACL phase is still running; the users-ready flag
        # written by the elevated setup is enough to count as provisioned.
        self.assertTrue(status["provisioned"])
        self.assertTrue(status["firewall_ok"])
        self.assertFalse(status["degraded"])


@unittest.skipUnless(os.name == "nt", "Windows sandbox backend requires ctypes.WinDLL")
class WindowsSandboxBackendCredentialTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.config_dir = Path(self._tmp.name)
        _start_shared_root_patch(self)
        self.backend = WindowsSandboxBackend()
        _credential_check_cache.clear()

    def tearDown(self):
        self._tmp.cleanup()
        _credential_check_cache.clear()

    def test_verify_credentials_true_when_both_logons_succeed(self):
        state = {"logons": 0}

        def fake_logon(user, domain, password, logon_type, provider, token):
            state["logons"] += 1
            return True

        with patch(
            "cli.core.sandbox.windows._load_secret",
            return_value={"offline": "X1", "online": "Y2"},
        ), patch(
            "cli.core.sandbox.windows._user_exists", return_value=True
        ), patch(
            "cli.core.sandbox.windows._win",
            return_value={
                "LogonUserW": fake_logon,
                "CloseHandle": lambda h: None,
            },
        ):
            self.assertTrue(self.backend.verify_credentials(self.config_dir))
        self.assertEqual(state["logons"], 2)

    def test_verify_credentials_false_when_logon_rejected(self):
        with patch(
            "cli.core.sandbox.windows._load_secret",
            return_value={"offline": "X1", "online": "Y2"},
        ), patch(
            "cli.core.sandbox.windows._user_exists", return_value=True
        ), patch(
            "cli.core.sandbox.windows._win",
            return_value={
                "LogonUserW": lambda *a: False,
                "CloseHandle": lambda h: None,
            },
        ):
            self.assertFalse(self.backend.verify_credentials(self.config_dir))

    def test_verify_credentials_fresh_bypasses_cache(self):
        secret = {"offline": "X1", "online": "Y2"}
        failing = {
            "LogonUserW": lambda *a: False,
            "CloseHandle": lambda h: None,
        }
        succeeding = {
            "LogonUserW": lambda *a: True,
            "CloseHandle": lambda h: None,
        }
        # Prime the cache with False (passwords rejected).
        with patch(
            "cli.core.sandbox.windows._load_secret", return_value=secret
        ), patch(
            "cli.core.sandbox.windows._user_exists", return_value=True
        ), patch(
            "cli.core.sandbox.windows._win", return_value=failing
        ):
            self.assertFalse(self.backend.verify_credentials(self.config_dir))
        # Without fresh, the cached False is returned even though logon now
        # succeeds; fresh=True bypasses the cache and re-verifies.
        with patch(
            "cli.core.sandbox.windows._load_secret", return_value=secret
        ), patch(
            "cli.core.sandbox.windows._user_exists", return_value=True
        ), patch(
            "cli.core.sandbox.windows._win", return_value=succeeding
        ):
            self.assertFalse(self.backend.verify_credentials(self.config_dir))
            self.assertTrue(
                self.backend.verify_credentials(self.config_dir, fresh=True)
            )

    def test_verify_credentials_none_when_users_missing(self):
        _save_secret(self.config_dir, {"offline": "X1", "online": "Y2"})
        with patch("cli.core.sandbox.windows._user_exists", return_value=False):
            self.assertIsNone(self.backend.verify_credentials(self.config_dir))

    def test_verify_credentials_none_without_secret(self):
        with patch("cli.core.sandbox.windows._user_exists", return_value=True):
            self.assertIsNone(self.backend.verify_credentials(self.config_dir))


class SelectUserTests(unittest.TestCase):
    def test_read_only_uses_offline(self):
        self.assertEqual(_select_user("read_only", True), SANDBOX_USER_OFFLINE)
        self.assertEqual(_select_user("read_only", False), SANDBOX_USER_OFFLINE)

    def test_workspace_write_network_off_uses_offline(self):
        self.assertEqual(
            _select_user("workspace_write", False), SANDBOX_USER_OFFLINE
        )

    def test_workspace_write_network_on_uses_online(self):
        self.assertEqual(
            _select_user("workspace_write", True), SANDBOX_USER_ONLINE
        )

    def test_invalid_level_raises(self):
        with self.assertRaises(ValueError):
            _select_user("banana", True)


class ShellRunnerExePathTests(unittest.TestCase):
    def test_dev_interpreter_returns_none(self):
        # In development (not frozen) the python ``-c`` loader path is used,
        # so no packaged shell-runner.exe is expected.
        self.assertIsNone(_shell_runner_exe_path())

    @patch("cli.core.sandbox.windows.sys")
    def test_frozen_resolves_bundle_layout(self, fake_sys):
        fake_sys.frozen = True
        fake_sys.executable = r"C:\Program Files\CodeWood\codewood.exe"
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            bundle = Path(td) / "shell-runner" / "shell-runner.exe"
            bundle.parent.mkdir(parents=True)
            bundle.write_bytes(b"x")
            # Resolve() only keeps long names for paths that exist.
            (Path(td) / "codewood.exe").write_bytes(b"x")
            fake_sys.executable = str(Path(td) / "codewood.exe")
            self.assertEqual(
                os.path.normcase(_shell_runner_exe_path()),
                os.path.normcase(str(bundle.resolve())),
            )


class SandboxRunnerMirrorTests(unittest.TestCase):
    """``_ensure_sandbox_runner_copy`` mirrors the packaged runner into the
    ACL-granted sandbox ``runner`` dir (outside the per-command ``tmp``
    scratch dir) so ``CreateProcessWithLogonW`` can start it as the sandbox
    user regardless of where the app is installed."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.shared = Path(self._tmp.name) / "shared"
        self._root_patch = _patch_shared_root(self.shared)
        self._root_patch.start()
        self.addCleanup(self._root_patch.stop)
        # Mirroring calls the best-effort ACL grant; in unit tests the sandbox
        # users don't exist, so a real PowerShell round-trip would be slow and
        # would not change the mirror outcome.  Stub it out.
        self._acl_patch = patch(
            "cli.core.sandbox.windows._ps_grant_read_group", return_value=0
        )
        self._acl_patch.start()
        self.addCleanup(self._acl_patch.stop)

    def _make_bundle(self, name="shell-runner", with_internal=True):
        src = self.shared / "install" / name
        exe = src / f"{name}.exe"
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_bytes(b"MZ fake runner")
        if with_internal:
            internal = src / "_internal"
            internal.mkdir(parents=True, exist_ok=True)
            (internal / "runner.dll").write_bytes(b"dll")
        return exe

    def test_missing_source_returns_original(self):
        path = str(self.shared / "nope" / "shell-runner.exe")
        self.assertEqual(_ensure_sandbox_runner_copy(path), path)

    def test_nested_bundle_mirrored_and_idempotent(self):
        exe = self._make_bundle()
        mirrored = _ensure_sandbox_runner_copy(str(exe))
        target = Path(mirrored)
        self.assertTrue(target.is_file())
        self.assertTrue(
            str(target).startswith(str(self.shared / "runner"))
        )
        self.assertTrue((target.parent / "_internal" / "runner.dll").is_file())
        first_mtime = os.path.getmtime(target)
        # A second call with an unchanged source must hit the process-lifetime
        # cache and not recopy (same mtime).
        self.assertEqual(_ensure_sandbox_runner_copy(str(exe)), mirrored)
        self.assertEqual(os.path.getmtime(target), first_mtime)

    def test_mirror_lives_outside_tmp_scratch_dir(self):
        # The runner must NOT be mirrored under ``tmp``: per-command cleanup
        # sweeps that directory, which would delete the mirror and force a
        # multi-GB re-copy on the very next spawn.
        exe = self._make_bundle()
        mirrored = _ensure_sandbox_runner_copy(str(exe))
        self.assertNotIn(
            os.path.normcase("tmp"),
            Path(os.path.normcase(str(Path(mirrored).relative_to(self.shared)))).parts,
        )
        self.assertTrue(Path(mirrored).is_file())

    def test_refreshes_when_source_newer(self):
        exe = self._make_bundle()
        mirrored = _ensure_sandbox_runner_copy(str(exe))
        future = os.path.getmtime(exe) + 1000
        os.utime(exe, (future, future))
        mirrored2 = _ensure_sandbox_runner_copy(str(exe))
        self.assertEqual(mirrored2, mirrored)
        self.assertGreaterEqual(os.path.getmtime(Path(mirrored2)), future)

    def test_cache_ignored_when_mirror_deleted(self):
        # If the mirror is gone (e.g. the runner dir was cleaned up between
        # processes), a cached hit must not return a stale path — re-mirror.
        exe = self._make_bundle()
        mirrored = _ensure_sandbox_runner_copy(str(exe))
        os.remove(mirrored)
        mirrored2 = _ensure_sandbox_runner_copy(str(exe))
        self.assertTrue(Path(mirrored2).is_file())
        self.assertEqual(mirrored2, mirrored)

    def test_flat_layout_mirrors_exe_and_internal_only(self):
        app_base = self.shared / "install"
        app_base.mkdir(parents=True, exist_ok=True)
        (app_base / "codewood.exe").write_bytes(b"MZ app")
        (app_base / "huge_unrelated.bin").write_bytes(b"x" * 4096)
        flat_exe = app_base / "shell-runner.exe"
        flat_exe.write_bytes(b"MZ flat runner")
        internal = app_base / "_internal"
        internal.mkdir()
        (internal / "runner.dll").write_bytes(b"dll")

        fake_sys = SimpleNamespace(
            frozen=True, executable=str(app_base / "codewood.exe")
        )
        with patch("cli.core.sandbox.windows.sys", fake_sys):
            mirrored = _ensure_sandbox_runner_copy(str(flat_exe))

        target = Path(mirrored)
        self.assertEqual(target.parent.name, "_flat")
        self.assertTrue(target.is_file())
        self.assertTrue((target.parent / "_internal" / "runner.dll").is_file())
        # The rest of the application bundle must never be mirrored.
        self.assertFalse((target.parent / "huge_unrelated.bin").exists())


class LaunchElevatedSetupTests(unittest.TestCase):
    @patch("cli.core.sandbox.windows._win")
    @patch("cli.core.sandbox.windows.sys")
    def test_frozen_omits_script_arg(self, fake_sys, fake_win):
        fake_sys.frozen = True
        fake_sys.executable = r"C:\CodeWood\codewood.exe"
        captured = {}

        def fake_shell_execute(*args):
            captured["args"] = args
            return 42  # > 32 => ShellExecuteW success

        fake_win.return_value = {"ShellExecuteW": fake_shell_execute}
        ok = launch_elevated_setup(r"C:\cfg", r"C:\ws", "workspace_write")
        self.assertTrue(ok)
        _, verb, exe, params, _, _ = captured["args"]
        self.assertEqual(verb, "runas")
        self.assertEqual(exe, r"C:\CodeWood\codewood.exe")
        self.assertNotIn("main.py", params)
        self.assertTrue(params.startswith("sandbox setup --gui "))
        self.assertIn("--level workspace_write", params)

    @patch("cli.core.sandbox.windows._win")
    @patch("cli.core.sandbox.windows.sys")
    def test_source_includes_script_arg(self, fake_sys, fake_win):
        fake_sys.frozen = False
        fake_sys.executable = r"C:\Python313\python.exe"
        captured = {}

        def fake_shell_execute(*args):
            captured["args"] = args
            return 42

        fake_win.return_value = {"ShellExecuteW": fake_shell_execute}
        ok = launch_elevated_setup(r"C:\cfg", r"C:\ws", "read_only")
        self.assertTrue(ok)
        _, verb, exe, params, _, _ = captured["args"]
        self.assertEqual(exe, r"C:\Python313\python.exe")
        self.assertIn("main.py", params)
        self.assertIn("main.py sandbox setup --gui", params)
        self.assertIn("--level read_only", params)


@unittest.skipUnless(os.name == "nt", "Windows sandbox backend requires ctypes.WinDLL")
class BuildEnvBlockTests(unittest.TestCase):
    def test_double_null_terminated_and_skips_equals_keys(self):
        block = _build_env_block({"PATH": "/bin", "=C:": "C:\\", "X": "1"})
        raw = ctypes.string_at(block, ctypes.sizeof(block)).decode(
            "utf-16-le", errors="replace"
        )
        text = raw
        self.assertIn("PATH=/bin", text)
        self.assertIn("X=1", text)
        self.assertNotIn("=C:", text)
        self.assertTrue(text.endswith("\x00\x00"))


def _fake_win(exit_code=259, signaled=False):
    state = {"exit_code": exit_code, "terminated": False, "closed": [], "signaled": signaled}

    def get_exit(hproc, out):
        try:
            out._obj.value = state["exit_code"]
        except Exception:
            pass
        return True

    def wait_single(h, ms):
        return WAIT_OBJECT_0 if state["signaled"] else WAIT_TIMEOUT

    def terminate(h, code):
        state["terminated"] = True
        state["exit_code"] = 1
        return True

    def close(h):
        state["closed"].append(h)
        return True

    w = {
        "GetExitCodeProcess": get_exit,
        "WaitForSingleObject": wait_single,
        "TerminateProcess": terminate,
        "CloseHandle": close,
    }
    return w, state


class WindowsSandboxProcessTests(unittest.TestCase):
    def test_poll_running_returns_none_then_exit_code(self):
        w, state = _fake_win()
        proc = WindowsSandboxProcess(w, 1, 2, 3, 42, None, None, exit_file=None)
        self.assertIsNone(proc.poll())
        state["signaled"] = True
        with tempfile.TemporaryDirectory() as tmp:
            exit_file = Path(tmp) / "exit.tmp"
            exit_file.write_bytes(struct.pack("<I", 0))
            proc._exit_file = str(exit_file)
            self.assertEqual(proc.poll(), 0)
        self.assertEqual(proc.returncode, 0)

    def test_wait_returns_exit_code(self):
        w, _ = _fake_win(signaled=True)
        with tempfile.TemporaryDirectory() as tmp:
            exit_file = Path(tmp) / "exit.tmp"
            exit_file.write_bytes(struct.pack("<I", 7))
            proc = WindowsSandboxProcess(
                w, 1, 2, 3, 42, None, None, exit_file=str(exit_file)
            )
            self.assertEqual(proc.wait(timeout=5), 7)

    def test_pid_exposed(self):
        w, _ = _fake_win()
        proc = WindowsSandboxProcess(w, 1, 2, 3, 4242, None, None)
        self.assertEqual(proc.pid, 4242)

    def test_kill_terminates_and_closes_job(self):
        w, state = _fake_win()
        proc = WindowsSandboxProcess(w, 1, 2, 3, 42, None, None)
        proc.kill()
        self.assertTrue(state["terminated"])
        self.assertIn(3, state["closed"])

    def test_cleanup_paths_unlinked_on_exit(self):
        w, _ = _fake_win(signaled=True)
        with tempfile.TemporaryDirectory() as tmp:
            exit_file = Path(tmp) / "exit.tmp"
            exit_file.write_bytes(struct.pack("<I", 0))
            extra = Path(tmp) / "cmd-extra.txt"
            extra.write_text("cmd.exe /c echo hi", encoding="utf-8")
            proc = WindowsSandboxProcess(
                w,
                1,
                2,
                3,
                42,
                None,
                None,
                exit_file=str(exit_file),
                cleanup_paths=[str(extra)],
            )
            self.assertEqual(proc.wait(timeout=5), 0)
            self.assertFalse(exit_file.exists())
            self.assertFalse(extra.exists())

    def test_cleanup_paths_missing_file_ignored(self):
        w, _ = _fake_win(signaled=True)
        with tempfile.TemporaryDirectory() as tmp:
            exit_file = Path(tmp) / "exit.tmp"
            exit_file.write_bytes(struct.pack("<I", 0))
            proc = WindowsSandboxProcess(
                w, 1, 2, 3, 42, None, None,
                exit_file=str(exit_file),
                cleanup_paths=[str(Path(tmp) / "nope.txt")],
            )
            self.assertEqual(proc.wait(timeout=5), 0)
            self.assertFalse(exit_file.exists())


class WindowsSandboxBackendStatusTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.config_dir = Path(self._tmp.name)
        _start_shared_root_patch(self)
        self.backend = WindowsSandboxBackend()

    def tearDown(self):
        self._tmp.cleanup()

    def test_status_not_provisioned_without_files(self):
        with patch("cli.core.sandbox.windows._user_exists", return_value=True):
            status = self.backend.status(self.config_dir)
        self.assertTrue(status["supported"])
        self.assertFalse(status["provisioned"])

    def test_status_users_foreign_when_users_exist_without_secret(self):
        with patch("cli.core.sandbox.windows._user_exists", return_value=True):
            status = self.backend.status(self.config_dir)
        self.assertTrue(status["users_exist"])
        self.assertFalse(status["secret_exists"])
        self.assertTrue(status["users_foreign"])

    def test_status_users_not_foreign_when_secret_exists(self):
        (self.shared / "sandbox_secret.bin").write_bytes(b"x")
        with patch("cli.core.sandbox.windows._user_exists", return_value=True):
            status = self.backend.status(self.config_dir)
        self.assertTrue(status["secret_exists"])
        self.assertFalse(status["users_foreign"])

    def test_status_provisioned_when_users_secret_flag_ready(self):
        (self.shared / "sandbox_secret.bin").write_bytes(b"x")
        (self.shared / SANDBOX_CAP_SID_FILENAME).write_text(
            '{"workspace": "S-1-5-21-1-2-3-4", "readonly": "S-1-5-21-5-6-7-8"}'
        )
        (self.shared / "sandbox_provisioned.flag").write_text(
            '{"provisioned": true, "firewall_ok": true}'
        )
        with patch("cli.core.sandbox.windows._user_exists", return_value=True):
            status = self.backend.status(self.config_dir)
        self.assertTrue(status["provisioned"])
        self.assertFalse(status["degraded"])
        self.assertTrue(status["group_exists"])
        self.assertEqual(status["users_group"], SANDBOX_USERS_GROUP)

    def test_status_degraded_when_firewall_missing(self):
        (self.shared / "sandbox_secret.bin").write_bytes(b"x")
        (self.shared / SANDBOX_CAP_SID_FILENAME).write_text(
            '{"workspace": "S-1-5-21-1-2-3-4", "readonly": "S-1-5-21-5-6-7-8"}'
        )
        (self.shared / "sandbox_provisioned.flag").write_text(
            '{"provisioned": true, "firewall_ok": false}'
        )
        with patch("cli.core.sandbox.windows._user_exists", return_value=True):
            status = self.backend.status(self.config_dir)
        self.assertFalse(status["provisioned"])
        self.assertTrue(status["degraded"])

    def test_status_not_provisioned_when_old_flag_without_firewall_field(self):
        (self.shared / "sandbox_secret.bin").write_bytes(b"x")
        (self.shared / "sandbox_provisioned.flag").write_text("{}")
        with patch("cli.core.sandbox.windows._user_exists", return_value=True):
            status = self.backend.status(self.config_dir)
        self.assertFalse(status["provisioned"])

    def test_is_provisioned_false_when_users_missing(self):
        (self.shared / "sandbox_secret.bin").write_bytes(b"x")
        (self.shared / "sandbox_provisioned.flag").write_text(
            '{"provisioned": true, "firewall_ok": true}'
        )
        with patch("cli.core.sandbox.windows._user_exists", return_value=False):
            self.assertFalse(self.backend.is_provisioned(self.config_dir))


class WindowsSandboxBackendAclTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.config_dir = Path(self._tmp.name)
        _start_shared_root_patch(self)

    def tearDown(self):
        self._tmp.cleanup()

    def _apply(self, root, level, config_dir=None):
        backend = WindowsSandboxBackend()
        if config_dir is None:
            config_dir = self.config_dir
        calls = []
        with patch(
            "cli.core.sandbox.windows._run_process",
            side_effect=lambda argv, timeout=180, stdin_data=None: (
                calls.append(argv)
                or type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
            ),
        ), patch(
            "cli.core.sandbox.windows._load_or_create_cap_sids",
            return_value={
                "workspace": "S-1-5-21-1-2-3-4",
                "readonly": "S-1-5-21-5-6-7-8",
            },
        ), patch(
            "cli.core.sandbox.windows._grant_profile_read", return_value=True
        ), patch(
            "cli.core.sandbox.windows.threading.Thread", _SyncThread
        ):
            backend.apply_workspace_acls(str(root), level, config_dir)
        return calls

    def test_apply_workspace_acls_read_only_single_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            calls = self._apply(root, "read_only")
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][0], "powershell")
            script = calls[0][4]
            self.assertIn("Apply-ACL", script)
            self.assertIn("'readonly'", script)
            self.assertIn(".git", script)
            self.assertIn(SANDBOX_USERS_GROUP, script)
            self.assertIn("'revoke'", script)
            self.assertIn(str(Path(tempfile.gettempdir()).resolve()), script)

    def test_apply_workspace_acls_workspace_write_single_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = self._apply(tmp, "workspace_write")
            self.assertEqual(len(calls), 1)
            script = calls[0][4]
            self.assertIn("'write'", script)
            self.assertIn("'Modify'", script)
            self.assertIn("S-1-5-21-1-2-3-4", script)
            self.assertIn(str(Path(tempfile.gettempdir()).resolve()), script)

    def test_apply_workspace_acls_records_profile_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._apply(tmp, "workspace_write")
            self.assertIn(str(Path.home() / "*"), _load_acl_record())
            self.assertIn(
                str(Path(tempfile.gettempdir()).resolve()), _load_acl_record()
            )

    def test_apply_workspace_acls_grants_profile_read_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []

            def fake_run(argv, timeout=180, stdin_data=None):
                calls.append(argv)
                if argv[0] == "powershell" and "Get-Acl" in " ".join(argv):
                    return SimpleNamespace(
                        returncode=0,
                        stdout=str(Path.home() / "AppData") + "\n",
                        stderr="",
                    )
                return SimpleNamespace(returncode=1, stderr="", stdout="")

            with patch(
                "cli.core.sandbox.windows._run_process",
                side_effect=fake_run,
            ), patch(
                "cli.core.sandbox.windows._load_or_create_cap_sids",
                return_value={"workspace": "S-1-1", "readonly": "S-1-2"},
            ), patch(
                "cli.core.sandbox.windows.threading.Thread", _SyncThread
            ):
                WindowsSandboxBackend().apply_workspace_acls(
                    tmp, "workspace_write", self.config_dir
                )
            icacls_calls = [c for c in calls if c[0] == "icacls"]
            self.assertGreaterEqual(len(icacls_calls), 1)
            self.assertTrue(
                all(c[1].startswith(str(Path.home())) for c in icacls_calls)
            )
            self.assertTrue(all("(OI)(CI)RX" in c[3] for c in icacls_calls))


    def test_missing_profile_read_dirs_script_avoids_trailing_comma(self):
        """PowerShell rejects ``@(1,2,)`` ("Missing expression after ','"),
        so a trailing comma in the generated check script used to fail the
        whole script, report "nothing missing", and skip the profile grants
        entirely.  The last array entry must not end with a comma."""
        children = [
            Path.home() / "one",
            Path.home() / "two",
            Path.home() / "three",
        ]
        captured = {}

        def fake_run(argv, timeout=180, stdin_data=None):
            captured["script"] = argv[-1]
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with patch("cli.core.sandbox.windows._run_process", side_effect=fake_run):
            self.assertEqual(_missing_profile_read_dirs(children), [])

        script = captured["script"]
        self.assertIn("$dirs=@(", script)
        self.assertNotIn(",);", script)
        self.assertEqual(script.count("',"), len(children) - 1)

    def test_missing_profile_read_dirs_raises_on_check_failure(self):
        """A failed check script must raise so the caller logs/retries instead
        of silently treating the failure as "all grants present"."""
        with patch(
            "cli.core.sandbox.windows._run_process",
            return_value=SimpleNamespace(returncode=1, stdout="", stderr="boom"),
        ):
            with self.assertRaises(RuntimeError):
                _missing_profile_read_dirs([Path.home() / "one"])


class WindowsSandboxBackendCleanupTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.config_dir = Path(self._tmp.name)
        _start_shared_root_patch(self)
        self.backend = WindowsSandboxBackend()

    def tearDown(self):
        self._tmp.cleanup()

    def test_cleanup_workspace_acls_strips_names_and_cap_sids(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            calls = []
            with patch(
                "cli.core.sandbox.windows._run_process",
                side_effect=lambda argv, timeout=180, stdin_data=None: (
                    calls.append(argv)
                    or SimpleNamespace(returncode=0, stderr="", stdout="")
                ),
            ), patch(
                "cli.core.sandbox.windows._load_or_create_cap_sids",
                return_value={
                    "workspace": "S-1-5-21-1-2-3-4",
                    "readonly": "S-1-5-21-5-6-7-8",
                },
            ):
                self.backend.cleanup_workspace_acls(str(root), self.config_dir)
            icacls_calls = [c for c in calls if c[0] == "icacls"]
            # One batched invocation per root (all three identities in a
            # single icacls call); the inheritable ACEs are dropped without a
            # recursive ``/t`` walk (auto-inheritance cleans the descendants).
            self.assertEqual(len(icacls_calls), 1)
            self.assertIn("/q", icacls_calls[0])
            self.assertNotIn("/t", icacls_calls[0])
            names = {icacls_calls[0][3], icacls_calls[0][4], icacls_calls[0][5]}
            self.assertEqual(
                names,
                {SANDBOX_USER_OFFLINE, SANDBOX_USER_ONLINE, SANDBOX_USERS_GROUP},
            )
            ps = [c for c in calls if c[0] == "powershell"]
            self.assertEqual(len(ps), 1)
            self.assertIn("S-1-5-21-1-2-3-4", ps[0][4])
            self.assertIn("S-1-5-21-5-6-7-8", ps[0][4])

    def test_cleanup_workspace_acls_missing_root_is_noop(self):
        with patch("cli.core.sandbox.windows._run_process") as run:
            self.backend.cleanup_workspace_acls(
                r"D:\does-not-exist-cw-test", self.config_dir
            )
        run.assert_not_called()

    def test_acl_record_updated_on_apply_and_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch(
                "cli.core.sandbox.windows._run_process",
                return_value=SimpleNamespace(returncode=0, stderr="", stdout=""),
            ), patch(
                "cli.core.sandbox.windows._load_or_create_cap_sids",
                return_value={
                    "workspace": "S-1-5-21-1-2-3-4",
                    "readonly": "S-1-5-21-5-6-7-8",
                },
            ), patch(
                "cli.core.sandbox.windows._grant_profile_read", return_value=True
            ), patch(
                "cli.core.sandbox.windows.threading.Thread", _SyncThread
            ):
                self.backend.apply_workspace_acls(
                    str(root), "workspace_write", self.config_dir
                )
                self.assertIn(str(root.resolve()), _load_acl_record())
                self.backend.cleanup_workspace_acls(str(root), self.config_dir)
                self.assertNotIn(str(root.resolve()), _load_acl_record())

    def test_acl_record_lives_in_shared_root(self):
        with patch(
            "cli.core.sandbox.windows._run_process",
            return_value=SimpleNamespace(returncode=0, stderr="", stdout=""),
        ):
            self.backend.cleanup_workspace_acls(str(self._tmp.name), self.config_dir)
        self.assertEqual(_acl_record_path().parent, Path(self._tmp.name))

    def test_run_set_password_enables_separately_without_enabled_flag(self):
        # Windows 11 (build 26200) Set-LocalUser has no -Enabled parameter
        # (NamedParameterNotFound); the in-place refresh must enable via the
        # separate Enable-LocalUser cmdlet and set the password without the
        # flag, otherwise every refresh fails and falls back to recreate.
        calls = []

        def fake_run(argv, timeout=180, stdin_data=None):
            calls.append(" ".join(argv))
            return SimpleNamespace(returncode=0, stderr="", stdout="")

        with patch("cli.core.sandbox.windows._run_process", side_effect=fake_run):
            _run_set_password("CodewoodSandOffline", "Pw!12345")

        self.assertEqual(len(calls), 2)
        self.assertIn("Enable-LocalUser -Name 'CodewoodSandOffline'", calls[0])
        self.assertIn(
            "Set-LocalUser -Name 'CodewoodSandOffline' -Password", calls[1]
        )
        self.assertNotIn("-Enabled", calls[1])


@unittest.skipUnless(os.name == "nt", "Windows sandbox backend requires ctypes.WinDLL")
class SandboxStateSharedRootTests(unittest.TestCase):
    """All data directories share one machine-local sandbox installation."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        _start_shared_root_patch(self)
        self.shared = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_state_files_live_in_shared_root_regardless_of_config_dir(self):
        cfg_dir = Path(self._tmp.name) / "some" / "config"
        self.assertEqual(_secret_path(cfg_dir).parent, self.shared)
        self.assertEqual(_flag_path(cfg_dir).parent, self.shared)
        self.assertEqual(_cap_sid_path(cfg_dir).parent, self.shared)
        # config_dir=None must not change the location either.
        self.assertEqual(_secret_path(None).parent, self.shared)
        self.assertEqual(_flag_path(None).parent, self.shared)
        self.assertEqual(_cap_sid_path(None).parent, self.shared)

    def test_secret_written_and_read_through_shared_root(self):
        _save_secret(self._tmp.name, {"offline": "X1", "online": "Y2"})
        self.assertTrue((self.shared / "sandbox_secret.bin").exists())
        self.assertEqual(
            _load_secret(self._tmp.name), {"offline": "X1", "online": "Y2"}
        )
        self.assertEqual(_load_secret(None), {"offline": "X1", "online": "Y2"})


if __name__ == "__main__":
    unittest.main()
