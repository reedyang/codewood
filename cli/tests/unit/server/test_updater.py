"""Unit tests for the desktop host's automatic updater.

The updater module lives under ``desktop/host`` (not a Python package on the
import path), so it is loaded by file path — mirroring ``test_task_notifier``.
The GitHub release listing and the HTTP session are both faked, and every
download goes to a temporary config directory, so no network or GUI is needed.
"""

import importlib.util
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_UPDATER_PATH = (
    Path(__file__).resolve().parents[4] / "desktop" / "host" / "updater.py"
)


def _load_updater_module():
    spec = importlib.util.spec_from_file_location(
        "codewood_test_updater", str(_UPDATER_PATH)
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


updater_mod = _load_updater_module()
UpdateManager = updater_mod.UpdateManager


def _assets(*names):
    return [
        {
            "name": name,
            "browser_download_url": f"https://example.invalid/{name}",
            "size": 0,
        }
        for name in names
    ]


class _FakeResponse:
    def __init__(self, status_code=200, headers=None, chunks=(), on_chunk=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._chunks = list(chunks)
        # Invoked after each chunk is handed out, so a test can simulate the
        # process quitting between two chunks of a transfer.
        self._on_chunk = on_chunk

    def iter_content(self, chunk_size=None):
        del chunk_size
        for chunk in self._chunks:
            yield chunk
            if self._on_chunk is not None:
                self._on_chunk()

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeSession:
    """Records requests and replays queued responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.closed = False

    def get(self, url, headers=None, timeout=None, stream=False):
        del timeout, stream
        self.requests.append({"url": url, "headers": dict(headers or {})})
        if not self.responses:
            raise AssertionError("unexpected request")
        return self.responses.pop(0)

    def close(self):
        self.closed = True


def _release(tag, *names):
    return {
        "tag_name": tag,
        "draft": False,
        "prerelease": False,
        "assets": _assets(*names),
    }


class VersionTests(unittest.TestCase):
    def test_parse_version_ignores_suffixes(self):
        self.assertEqual(updater_mod.parse_version("v1.2.3"), (1, 2, 3))
        self.assertEqual(updater_mod.parse_version("1.2.3-beta.1"), (1, 2, 3))
        self.assertEqual(updater_mod.parse_version("not-a-version"), ())

    def test_is_newer(self):
        self.assertTrue(updater_mod.is_newer("v0.2.0", "0.1.0"))
        self.assertTrue(updater_mod.is_newer("v0.1.1", "0.1.0"))
        self.assertTrue(updater_mod.is_newer("v1.0", "0.9.9"))
        self.assertFalse(updater_mod.is_newer("v0.1.0", "0.1.0"))
        self.assertFalse(updater_mod.is_newer("v0.0.9", "0.1.0"))
        # An unresolvable local version must never fake an update.
        self.assertFalse(updater_mod.is_newer("v9.9.9", ""))

    def test_parse_digest_accepts_only_sha256_hex(self):
        valid = "a" * 64
        self.assertEqual(updater_mod.parse_digest(f"sha256:{valid}"), valid)
        self.assertEqual(updater_mod.parse_digest(f"SHA256:{valid.upper()}"), valid)
        # Anything we cannot compare meaningfully must disable verification
        # rather than be treated as a hash.
        self.assertIsNone(updater_mod.parse_digest(""))
        self.assertIsNone(updater_mod.parse_digest(None))
        self.assertIsNone(updater_mod.parse_digest("sha512:" + "a" * 128))
        self.assertIsNone(updater_mod.parse_digest("sha256:tooshort"))
        self.assertIsNone(updater_mod.parse_digest("sha256:" + "z" * 64))
        self.assertIsNone(updater_mod.parse_digest(valid))

    def test_sha256_file_matches_hashlib(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "payload.bin"
            payload = b"code wood" * 1000
            path.write_bytes(payload)
            self.assertEqual(
                updater_mod.sha256_file(path), hashlib.sha256(payload).hexdigest()
            )


class ProxyTests(unittest.TestCase):
    def test_proxied_url_prefixes_origin(self):
        origin = "https://github.com/reedyang/codewood/releases/download/v1/a.pkg"
        self.assertEqual(updater_mod.proxied_url(origin), f"https://gh-proxy.com/{origin}")

    def test_proxy_enabled_by_default_and_disabled_by_env(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertTrue(updater_mod.proxy_fallback_enabled())
        with patch.dict("os.environ", {"CODEWOOD_UPDATE_PROXY": "0"}):
            self.assertFalse(updater_mod.proxy_fallback_enabled())


class SelectReleaseTests(unittest.TestCase):
    def test_picks_newest_published_release(self):
        releases = [
            _release("v0.1.0", "CodeWood-0.1.0-macos-arm64.pkg"),
            _release("v0.3.0", "CodeWood-0.3.0-macos-arm64.pkg"),
            _release("v0.2.0", "CodeWood-0.2.0-macos-arm64.pkg"),
        ]
        chosen = updater_mod.select_release(releases)
        self.assertEqual(chosen["tag_name"], "v0.3.0")

    def test_ignores_drafts_and_prereleases(self):
        draft = _release("v9.0.0", "CodeWood-9.0.0-macos-arm64.pkg")
        draft["draft"] = True
        pre = _release("v8.0.0", "CodeWood-8.0.0-macos-arm64.pkg")
        pre["prerelease"] = True
        chosen = updater_mod.select_release([draft, pre, _release("v0.2.0")])
        self.assertEqual(chosen["tag_name"], "v0.2.0")


class SelectAssetTests(unittest.TestCase):
    def test_windows_prefers_setup_exe_over_portable_zip(self):
        with patch.object(updater_mod.sys, "platform", "win32"):
            with patch.object(updater_mod, "_machine_arch", return_value="x64"):
                chosen = updater_mod.select_asset(
                    _assets(
                        "CodeWood-0.2.0-windows-x64-portable.zip",
                        "CodeWood-0.2.0-windows-x64-setup.exe",
                    )
                )
        self.assertEqual(chosen["name"], "CodeWood-0.2.0-windows-x64-setup.exe")

    def test_windows_skips_other_architecture(self):
        with patch.object(updater_mod.sys, "platform", "win32"):
            with patch.object(updater_mod, "_machine_arch", return_value="x64"):
                chosen = updater_mod.select_asset(
                    _assets(
                        "CodeWood-0.2.0-windows-arm64-setup.exe",
                        "CodeWood-0.2.0-windows-x64-setup.exe",
                    )
                )
        self.assertEqual(chosen["name"], "CodeWood-0.2.0-windows-x64-setup.exe")

    def test_macos_prefers_pkg(self):
        with patch.object(updater_mod.sys, "platform", "darwin"):
            with patch.object(updater_mod, "_machine_arch", return_value="arm64"):
                chosen = updater_mod.select_asset(
                    _assets(
                        "CodeWood-0.2.0-macos-arm64.dmg",
                        "CodeWood-0.2.0-macos-arm64.pkg",
                        "CodeWood-0.2.0-macos-arm64-portable.tar.gz",
                    )
                )
        self.assertEqual(chosen["name"], "CodeWood-0.2.0-macos-arm64.pkg")

    def test_linux_prefers_appimage_over_deb(self):
        with patch.object(updater_mod.sys, "platform", "linux"):
            with patch.object(updater_mod, "_machine_arch", return_value="x64"):
                chosen = updater_mod.select_asset(
                    _assets(
                        "CodeWood-0.2.0-linux-x86_64.deb",
                        "CodeWood-0.2.0-linux-x86_64.AppImage",
                        "CodeWood-0.2.0-linux-x86_64-portable.tar.gz",
                    )
                )
        self.assertEqual(chosen["name"], "CodeWood-0.2.0-linux-x86_64.AppImage")

    def test_returns_none_when_nothing_installable(self):
        with patch.object(updater_mod.sys, "platform", "linux"):
            with patch.object(updater_mod, "_machine_arch", return_value="x64"):
                chosen = updater_mod.select_asset(
                    _assets(
                        "CodeWood-0.2.0-linux-x86_64-portable.tar.gz",
                        "CodeWood-0.2.0-windows-x64-setup.exe",
                    )
                )
        self.assertIsNone(chosen)


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.config_dir = Path(self._tmp.name)
        self.manager = UpdateManager(config_dir=self.config_dir, interval=99999)
        self.dir = updater_mod.updates_dir(self.config_dir)
        self.asset = "CodeWood-0.2.0-macos-arm64.pkg"

    def _patch_release(self, releases, session):
        return (
            patch.object(self.manager, "fetch_releases", return_value=releases),
            patch.object(self.manager, "_session", return_value=session),
            patch.object(updater_mod, "_current_version", return_value="0.1.0"),
            patch.object(
                updater_mod,
                "select_asset",
                lambda assets: next(
                    (
                        a
                        for a in assets
                        if a["name"] == self.asset
                    ),
                    None,
                ),
            ),
        )

    def test_downloads_new_release_into_cache(self):
        payload = [b"abc", b"def"]
        session = _FakeSession(
            [_FakeResponse(200, {"Content-Length": "6"}, payload)]
        )
        patches = self._patch_release([_release("v0.2.0", self.asset)], session)
        for ctx in patches:
            ctx.start()
            self.addCleanup(ctx.stop)

        state = self.manager.check_once()
        self.assertEqual(state["status"], updater_mod.STATUS_READY)
        self.assertEqual(state["version"], "v0.2.0")
        self.assertEqual(state["progress"], 1.0)
        self.assertEqual((self.dir / self.asset).read_bytes(), b"abcdef")
        self.assertFalse((self.dir / f"{self.asset}.part").exists())

    def test_up_to_date_does_not_download(self):
        session = _FakeSession([])
        patches = self._patch_release([_release("v0.1.0", self.asset)], session)
        for ctx in patches:
            ctx.start()
            self.addCleanup(ctx.stop)

        state = self.manager.check_once()
        self.assertEqual(state["status"], updater_mod.STATUS_UPTODATE)
        self.assertEqual(session.requests, [])

    def test_falls_back_to_mirror_when_origin_fails(self):
        session = _FakeSession(
            [
                _FakeResponse(404),  # origin unavailable
                _FakeResponse(200, {"Content-Length": "6"}, [b"abc", b"def"]),
            ]
        )
        patches = self._patch_release([_release("v0.2.0", self.asset)], session)
        for ctx in patches:
            ctx.start()
            self.addCleanup(ctx.stop)
        env = patch.dict("os.environ", {}, clear=True)
        env.start()
        self.addCleanup(env.stop)

        state = self.manager.check_once()
        self.assertEqual(state["status"], updater_mod.STATUS_READY)
        self.assertEqual((self.dir / self.asset).read_bytes(), b"abcdef")
        origin = session.requests[0]["url"]
        self.assertTrue(origin.startswith("https://example.invalid/"))
        self.assertEqual(session.requests[1]["url"], f"https://gh-proxy.com/{origin}")
        # The source that actually completed is remembered for the next resume.
        self.assertEqual(
            json.loads(
                (self.dir / updater_mod._META_FILENAME).read_text(encoding="utf-8")
            )["source"],
            f"https://gh-proxy.com/{origin}",
        )

    def test_mirror_fallback_can_be_disabled(self):
        session = _FakeSession([_FakeResponse(404)])
        patches = self._patch_release([_release("v0.2.0", self.asset)], session)
        for ctx in patches:
            ctx.start()
            self.addCleanup(ctx.stop)
        env = patch.dict("os.environ", {"CODEWOOD_UPDATE_PROXY": "0"})
        env.start()
        self.addCleanup(env.stop)

        state = self.manager.check_once()
        self.assertEqual(state["status"], updater_mod.STATUS_FAILED)
        self.assertEqual(len(session.requests), 1)

    def _digest_of_payload(self, payload: bytes) -> str:
        return hashlib.sha256(payload).hexdigest()

    def test_accepts_package_matching_published_sha256(self):
        payload = b"abcdef"
        session = _FakeSession(
            [_FakeResponse(200, {"Content-Length": "6"}, [payload])]
        )
        release = _release("v0.2.0", self.asset)
        release["assets"][0]["digest"] = f"sha256:{self._digest_of_payload(payload)}"
        patches = self._patch_release([release], session)
        for ctx in patches:
            ctx.start()
            self.addCleanup(ctx.stop)

        state = self.manager.check_once()
        self.assertEqual(state["status"], updater_mod.STATUS_READY)
        self.assertEqual((self.dir / self.asset).read_bytes(), payload)

    def test_rejects_package_failing_sha256(self):
        """Right size but wrong bytes must never become installable."""
        payload = b"abcdef"
        session = _FakeSession(
            [_FakeResponse(200, {"Content-Length": "6"}, [payload])]
        )
        release = _release("v0.2.0", self.asset)
        release["assets"][0]["digest"] = "sha256:" + "0" * 64
        patches = self._patch_release([release], session)
        for ctx in patches:
            ctx.start()
            self.addCleanup(ctx.stop)

        state = self.manager.check_once()
        self.assertEqual(state["status"], updater_mod.STATUS_FAILED)
        self.assertIn("SHA-256", state["error"])
        # Nothing installable is left behind, and the bad data is not kept.
        self.assertFalse((self.dir / self.asset).exists())
        self.assertFalse((self.dir / f"{self.asset}.part").exists())
        self.assertIsNone(self.manager.installer_path())

    def test_missing_digest_still_downloads(self):
        """Releases without a digest (older uploads) must keep working."""
        session = _FakeSession(
            [_FakeResponse(200, {"Content-Length": "6"}, [b"abcdef"])]
        )
        patches = self._patch_release([_release("v0.2.0", self.asset)], session)
        for ctx in patches:
            ctx.start()
            self.addCleanup(ctx.stop)

        self.assertEqual(self.manager.check_once()["status"], updater_mod.STATUS_READY)

    def test_cached_package_failing_sha256_is_refetched(self):
        """A cached file that no longer matches must not be trusted."""
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / self.asset).write_bytes(b"corrupt")
        payload = b"abcdef"
        session = _FakeSession(
            [_FakeResponse(200, {"Content-Length": "6"}, [payload])]
        )
        release = _release("v0.2.0", self.asset)
        release["assets"][0]["digest"] = f"sha256:{self._digest_of_payload(payload)}"
        patches = self._patch_release([release], session)
        for ctx in patches:
            ctx.start()
            self.addCleanup(ctx.stop)

        state = self.manager.check_once()
        self.assertEqual(state["status"], updater_mod.STATUS_READY)
        self.assertEqual((self.dir / self.asset).read_bytes(), payload)

    def test_resumes_partial_download(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        part = self.dir / f"{self.asset}.part"
        part.write_bytes(b"abc")
        (self.dir / updater_mod._META_FILENAME).write_text(
            json.dumps({"version": "v0.2.0", "asset": self.asset, "url": "u"}),
            encoding="utf-8",
        )
        session = _FakeSession(
            [
                _FakeResponse(
                    206, {"Content-Range": "bytes 3-5/6"}, [b"def"]
                )
            ]
        )
        patches = self._patch_release([_release("v0.2.0", self.asset)], session)
        for ctx in patches:
            ctx.start()
            self.addCleanup(ctx.stop)

        state = self.manager.check_once()
        self.assertEqual(state["status"], updater_mod.STATUS_READY)
        self.assertEqual(session.requests[-1]["headers"]["Range"], "bytes=3-")
        self.assertEqual((self.dir / self.asset).read_bytes(), b"abcdef")

    def test_partial_file_for_other_asset_is_discarded(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        part = self.dir / f"{self.asset}.part"
        part.write_bytes(b"stale")
        (self.dir / updater_mod._META_FILENAME).write_text(
            json.dumps({"asset": "Other.pkg"}), encoding="utf-8"
        )
        session = _FakeSession([_FakeResponse(200, {}, [b"fresh"])])
        patches = self._patch_release([_release("v0.2.0", self.asset)], session)
        for ctx in patches:
            ctx.start()
            self.addCleanup(ctx.stop)

        self.manager.check_once()
        self.assertNotIn("Range", session.requests[-1]["headers"])
        self.assertEqual((self.dir / self.asset).read_bytes(), b"fresh")

    def test_only_newest_package_is_kept(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        old = self.dir / "CodeWood-0.1.0-macos-arm64.pkg"
        old.write_bytes(b"old")
        session = _FakeSession([_FakeResponse(200, {}, [b"new"])])
        patches = self._patch_release([_release("v0.2.0", self.asset)], session)
        for ctx in patches:
            ctx.start()
            self.addCleanup(ctx.stop)

        self.manager.check_once()
        self.assertFalse(old.exists())
        self.assertEqual(
            {p.name for p in self.dir.iterdir()},
            {updater_mod._META_FILENAME, updater_mod._LOCK_FILENAME, self.asset},
        )

    def test_partial_larger_than_release_restarts_from_zero(self):
        """A corrupt (oversized) partial must be discarded, not resumed past."""
        self.dir.mkdir(parents=True, exist_ok=True)
        part = self.dir / f"{self.asset}.part"
        part.write_bytes(b"x" * 40)  # longer than the real 6-byte payload
        (self.dir / updater_mod._META_FILENAME).write_text(
            json.dumps({"version": "v0.2.0", "asset": self.asset, "url": "u"}),
            encoding="utf-8",
        )
        session = _FakeSession(
            [_FakeResponse(200, {"Content-Length": "6"}, [b"abcdef"])]
        )
        patches = self._patch_release([_release("v0.2.0", self.asset)], session)
        for ctx in patches:
            ctx.start()
            self.addCleanup(ctx.stop)
        # The release advertises 6 bytes, so the 40-byte partial is corrupt.
        size_patch = patch.object(
            updater_mod,
            "select_asset",
            lambda assets: {
                "name": self.asset,
                "browser_download_url": f"https://example.invalid/{self.asset}",
                "size": 6,
            },
        )
        size_patch.start()
        self.addCleanup(size_patch.stop)

        state = self.manager.check_once()
        self.assertEqual(state["status"], updater_mod.STATUS_READY)
        # Restarted at 0 (no Range header) and produced exactly the payload.
        self.assertNotIn("Range", session.requests[-1]["headers"])
        self.assertEqual((self.dir / self.asset).read_bytes(), b"abcdef")

    def test_mismatched_content_range_restarts_from_zero(self):
        """A 206 that does not start where we asked must not duplicate bytes."""
        self.dir.mkdir(parents=True, exist_ok=True)
        part = self.dir / f"{self.asset}.part"
        part.write_bytes(b"abc")
        (self.dir / updater_mod._META_FILENAME).write_text(
            json.dumps({"version": "v0.2.0", "asset": self.asset, "url": "u"}),
            encoding="utf-8",
        )
        # Server answers as if resuming from 0 even though we asked for 3-.
        session = _FakeSession(
            [
                _FakeResponse(
                    206,
                    {"Content-Range": "bytes 0-5/6"},
                    [b"abcdef"],
                    on_chunk=lambda: self.manager._stop.set(),
                )
            ]
        )
        patches = self._patch_release([_release("v0.2.0", self.asset)], session)
        for ctx in patches:
            ctx.start()
            self.addCleanup(ctx.stop)

        self.manager.check_once()
        self.assertEqual((self.dir / self.asset).read_bytes(), b"abcdef")

    def test_oversized_stream_aborts_immediately(self):
        """The transfer must stop as soon as it exceeds the announced size."""
        session = _FakeSession(
            [_FakeResponse(200, {"Content-Length": "6"}, [b"abc", b"def", b"ghi"])]
        )
        patches = self._patch_release([_release("v0.2.0", self.asset)], session)
        for ctx in patches:
            ctx.start()
            self.addCleanup(ctx.stop)

        state = self.manager.check_once()
        self.assertEqual(state["status"], updater_mod.STATUS_FAILED)
        self.assertIn("exceeded", state["error"])
        self.assertFalse((self.dir / self.asset).exists())
        # No more than the advertised size was written before aborting.
        part = self.dir / f"{self.asset}.part"
        self.assertLessEqual(part.stat().st_size, 6)

    def test_unrelated_cache_content_is_preserved(self):
        """The cache dir is shared: only updater-owned files may be pruned."""
        mcp_icons = self.dir / "mcp_icons"
        mcp_icons.mkdir(parents=True, exist_ok=True)
        (mcp_icons / "server.png").write_bytes(b"icon")
        scratch = self.dir / "scratch.json"
        scratch.write_text("{}", encoding="utf-8")
        old = self.dir / "CodeWood-0.1.0-macos-arm64.pkg"
        old.write_bytes(b"old")

        session = _FakeSession([_FakeResponse(200, {}, [b"new"])])
        patches = self._patch_release([_release("v0.2.0", self.asset)], session)
        for ctx in patches:
            ctx.start()
            self.addCleanup(ctx.stop)

        self.manager.check_once()
        self.assertFalse(old.exists())
        self.assertTrue((mcp_icons / "server.png").is_file())
        self.assertTrue(scratch.is_file())

    def test_interrupted_download_keeps_partial_file(self):
        session = _FakeSession(
            [
                _FakeResponse(
                    200,
                    {},
                    [b"abc", b"def"],
                    on_chunk=lambda: self.manager._stop.set(),
                )
            ]
        )
        patches = self._patch_release([_release("v0.2.0", self.asset)], session)
        for ctx in patches:
            ctx.start()
            self.addCleanup(ctx.stop)

        # The fake response signals "the app is quitting" after the first chunk.
        state = self.manager.check_once()
        self.assertEqual(state["status"], updater_mod.STATUS_FAILED)
        self.assertEqual((self.dir / f"{self.asset}.part").read_bytes(), b"abc")
        self.assertFalse((self.dir / self.asset).exists())


class InstallTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.config_dir = Path(self._tmp.name)
        self.quit_calls = []
        self.manager = UpdateManager(
            config_dir=self.config_dir, on_quit=lambda: self.quit_calls.append(1)
        )
        self.dir = updater_mod.updates_dir(self.config_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.pkg = self.dir / "CodeWood-0.2.0-macos-arm64.pkg"
        self.pkg.write_bytes(b"pkg")

    def _set_ready(self):
        self.manager._update(
            status=updater_mod.STATUS_READY,
            version="v0.2.0",
            asset=self.pkg.name,
            path=str(self.pkg),
        )

    def test_install_launches_package_and_quits(self):
        self._set_ready()
        with patch.object(updater_mod.subprocess, "Popen") as popen, patch.object(
            updater_mod.sys, "platform", "darwin"
        ):
            self.assertTrue(self.manager.install())
        self.assertEqual(popen.call_args[0][0], ["open", str(self.pkg)])
        self.assertEqual(self.quit_calls, [1])

    def test_install_without_package_is_noop(self):
        self.assertFalse(self.manager.install())
        self.assertEqual(self.quit_calls, [])


class DownloadLockTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.config_dir = Path(self._tmp.name)

    def test_second_manager_defers_to_the_first(self):
        """Only one instance may own the shared download at a time."""
        first = UpdateManager(config_dir=self.config_dir)
        second = UpdateManager(config_dir=self.config_dir)
        self.assertTrue(first._acquire_download_lock())
        self.assertFalse(second._acquire_download_lock())
        first._release_download_lock()
        # Released (or died) -> the next instance can take over.
        self.assertTrue(second._acquire_download_lock())
        second._release_download_lock()

    def test_check_once_skips_when_another_instance_downloads(self):
        other = UpdateManager(config_dir=self.config_dir)
        self.assertTrue(other._acquire_download_lock())
        self.addCleanup(other._release_download_lock)

        manager = UpdateManager(config_dir=self.config_dir)
        with patch.object(manager, "fetch_releases") as fetch:
            state = manager.check_once()
        # Deferred before doing any network work.
        fetch.assert_not_called()
        self.assertEqual(state["status"], updater_mod.STATUS_IDLE)

    def test_lock_is_released_after_a_failed_check(self):
        manager = UpdateManager(config_dir=self.config_dir)
        with patch.object(
            manager, "fetch_releases", side_effect=RuntimeError("offline")
        ):
            with self.assertRaises(RuntimeError):
                manager.check_once()
        # A crashed cycle must not leave the lock held for the next cycle.
        self.assertIsNone(manager._lock_handle)
        self.assertTrue(manager._acquire_download_lock())
        manager._release_download_lock()

    def test_lock_is_not_pruned_from_the_cache(self):
        self.assertFalse(updater_mod._is_owned_cache_name(updater_mod._LOCK_FILENAME))

    def test_lock_is_released_on_stop(self):
        manager = UpdateManager(config_dir=self.config_dir)
        self.assertTrue(manager._acquire_download_lock())
        manager.stop()
        self.assertIsNone(manager._lock_handle)


class StateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.manager = UpdateManager(config_dir=Path(self._tmp.name))

    def test_progress_is_bounded(self):
        self.manager._update(received=50, total=200)
        self.assertEqual(self.manager.state()["progress"], 0.25)
        self.manager._update(received=500, total=200)
        self.assertEqual(self.manager.state()["progress"], 1.0)
        self.manager._update(received=0, total=0)
        self.assertEqual(self.manager.state()["progress"], 0.0)

    def test_disabled_by_env(self):
        with patch.dict("os.environ", {"CODEWOOD_UPDATE": "0"}):
            self.assertFalse(updater_mod.update_check_enabled())
            self.assertIsNone(
                updater_mod.start_update_manager(config_dir=Path(self._tmp.name))
            )

    def test_enabled_by_default(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertTrue(updater_mod.update_check_enabled())


if __name__ == "__main__":
    unittest.main()