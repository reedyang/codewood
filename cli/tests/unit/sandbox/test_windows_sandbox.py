import ctypes
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
    _load_secret,
    _random_password,
    _secret_path,
    _save_secret,
    launch_elevated_setup,
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
            "cli.core.sandbox.windows._user_exists", return_value=False
        ), patch(
            "cli.core.sandbox.windows._run_process", side_effect=fake_run
        ), patch(
            "cli.core.sandbox.windows._load_or_create_cap_sids",
            return_value={"workspace": "S-1-1", "readonly": "S-1-2"},
        ), patch(
            "cli.core.sandbox.windows._ps_grant_modify_sid"
        ), patch(
            "cli.core.sandbox.windows._ps_grant_read_group", return_value=0
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
    ACL-granted sandbox runtime dir so ``CreateProcessWithLogonW`` can start
    it as the sandbox user regardless of where the app is installed."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.shared = Path(self._tmp.name) / "shared"
        self._root_patch = _patch_shared_root(self.shared)
        self._root_patch.start()
        self.addCleanup(self._root_patch.stop)

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
            str(target).startswith(str(self.shared / "tmp" / "runner"))
        )
        self.assertTrue((target.parent / "_internal" / "runner.dll").is_file())
        first_mtime = os.path.getmtime(target)
        # A second call with an unchanged source must not recopy (same mtime).
        self.assertEqual(_ensure_sandbox_runner_copy(str(exe)), mirrored)
        self.assertEqual(os.path.getmtime(target), first_mtime)

    def test_refreshes_when_source_newer(self):
        exe = self._make_bundle()
        mirrored = _ensure_sandbox_runner_copy(str(exe))
        future = os.path.getmtime(exe) + 1000
        os.utime(exe, (future, future))
        mirrored2 = _ensure_sandbox_runner_copy(str(exe))
        self.assertEqual(mirrored2, mirrored)
        self.assertGreaterEqual(os.path.getmtime(Path(mirrored2)), future)

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

    def test_apply_workspace_acls_workspace_write_single_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = self._apply(tmp, "workspace_write")
            self.assertEqual(len(calls), 1)
            script = calls[0][4]
            self.assertIn("'write'", script)
            self.assertIn("'Modify'", script)
            self.assertIn("S-1-5-21-1-2-3-4", script)


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
            self.assertEqual(len(icacls_calls), 6)
            self.assertTrue(all("/t" in c and "/q" in c for c in icacls_calls))
            names = {c[3] for c in icacls_calls}
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
