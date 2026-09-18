"""Automatic update checker/downloader for the Code Wood desktop GUI.

The GUI host starts one background thread that queries the project's GitHub
releases (``https://github.com/reedyang/codewood/releases``, via the
machine-readable ``api.github.com`` endpoint) at launch and then once an hour.
When a release newer than the running build exists, the correct installer for
this platform is downloaded **silently** (no dialogs, no progress UI of its
own) into the ``cache`` sub-directory of the config directory:

- Windows: the x64 ``…-windows-x64-setup.exe`` installer.
- macOS: the ``…-macos-<arch>.pkg`` installer package.
- Linux: the ``…-linux-<arch>.AppImage`` (the distro-agnostic, no-root
  "universal" Linux format); a ``.deb`` is accepted as a fallback.

Downloads are resumable: the payload is written to ``<asset>.part`` and an
HTTP ``Range`` request continues from the current offset, so quitting Code
Wood mid-download and starting it again later resumes instead of restarting.
Only the newest release's package is kept — a stale package (and its partial
file) is removed before a new download begins.

If the origin download fails the asset is retried through
``https://gh-proxy.com/<url>`` (disable with ``CODEWOOD_UPDATE_PROXY=0``).

The frontend shows nothing while a download is in flight; once the package is
complete it is told (``update_state()`` poll + a pushed
``codewood:update-ready`` event) and shows an **Update** button in the title
bar. Clicking it launches the installer and quits Code Wood.

Set ``CODEWOOD_UPDATE=0`` to disable the whole feature.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import platform
import re
import shutil
import struct
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, TextIO, Tuple


_GITHUB_API_RELEASES_URL = (
    "https://api.github.com/repos/reedyang/codewood/releases"
)

#: Seconds between two automatic checks (checks run at launch, then hourly).
CHECK_INTERVAL_SECONDS = 3600.0

#: Prefix of the GitHub download mirror used as a fallback. GitHub's asset CDN
#: is unreachable from some networks (notably mainland China, where
#: ``github.com``/``objects.githubusercontent.com`` connections time out), so a
#: failed direct transfer is retried through ``https://gh-proxy.com/<url>``.
_PROXY_PREFIX = "https://gh-proxy.com/"

#: Set ``CODEWOOD_UPDATE_PROXY=0`` to disable the mirror fallback.
_PROXY_ENV_VAR = "CODEWOOD_UPDATE_PROXY"

#: Networking timeouts: (connect, read) for the metadata request; the payload
#: download uses a longer read timeout because asset chunks are large.
_META_TIMEOUT = (10.0, 30.0)
_DOWNLOAD_TIMEOUT = (10.0, 120.0)
_CHUNK_SIZE = 256 * 1024

#: Sub-directory of the user config dir holding downloaded packages.
_CACHE_SUBDIR = "cache"

#: Sidecar recording which release/asset a partially downloaded file belongs
#: to, so a resumed ``.part`` file is only reused for the same target.
_META_FILENAME = "update.json"

#: Suffix of an in-progress download (``<asset>.part``).
_PART_SUFFIX = ".part"

#: Lock file serializing downloads across processes sharing a config dir.
_LOCK_FILENAME = "update.lock"

#: ``_transfer`` outcomes: finished, resumable failure (keep the partial), or
#: corrupt response (discard the partial and give up on this release).
_TRANSFER_DONE = "done"
_TRANSFER_RETRY = "retry"
_TRANSFER_FATAL = "fatal"

#: Cache file names used before packages were stored directly in ``cache/``;
#: removed once on first use so the old directory does not linger.
_LEGACY_SUBDIR = "updates"

#: Every extension an installer can have on a supported platform, used to
#: recognise this module's own leftovers inside the shared cache directory.
_INSTALLER_EXTENSIONS = (".exe", ".pkg", ".dmg", ".appimage", ".deb")


def _is_owned_cache_name(name: str) -> bool:
    """True for a file name this module created (installer, partial, sidecar)."""
    if name == _LOCK_FILENAME:
        # Never prune the lock file: another process may hold it open.
        return False
    if name == _META_FILENAME or name.endswith(_PART_SUFFIX):
        return True
    return name.lower().endswith(_INSTALLER_EXTENSIONS)

# Statuses reported to the frontend (mirrored by the web layer).
STATUS_IDLE = "idle"
STATUS_CHECKING = "checking"
STATUS_UPTODATE = "up-to-date"
STATUS_DOWNLOADING = "downloading"
STATUS_READY = "ready"
STATUS_FAILED = "failed"


def _app_info_module():
    """Load ``cli/config/app_info.py`` by file path, or None.

    Loaded by file path instead of ``import cli.config.app_info`` so the GUI
    host never pulls in the heavy ``cli`` package (whose ``__init__`` imports
    the full ``Agent``) just to read branding/version constants. Mirrors
    ``notifier._load_app_info_module``.
    """
    if getattr(sys, "frozen", False):
        meipass = Path(getattr(sys, "_MEIPASS", "") or ".")
        candidates = [
            meipass / "cli" / "config" / "app_info.py",
            Path(__file__).resolve().parents[2] / "cli" / "config" / "app_info.py",
        ]
    else:
        candidates = [
            Path(__file__).resolve().parents[2] / "cli" / "config" / "app_info.py",
        ]
    for path in candidates:
        if not path.is_file():
            continue
        try:
            spec = importlib.util.spec_from_file_location(
                "codewood_host_updater_app_info", str(path)
            )
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
        except Exception:
            continue
    return None


def _current_version() -> str:
    """Version of the running build (``""`` when it cannot be resolved)."""
    module = _app_info_module()
    getter = getattr(module, "get_app_version", None) if module else None
    if not callable(getter):
        return ""
    try:
        return str(getter() or "").strip()
    except Exception:
        return ""


def _app_display_name() -> str:
    module = _app_info_module()
    getter = getattr(module, "get_app_name", None) if module else None
    if not callable(getter):
        return "Code Wood"
    try:
        return str(getter() or "").strip() or "Code Wood"
    except Exception:
        return "Code Wood"


def _user_agent() -> str:
    return f"{_app_display_name()}/{_current_version() or '0.0.0'}"


def _config_dir() -> Path:
    """User-level config directory (``~/.config/codewood`` by default)."""
    module = _app_info_module()
    getter = getattr(module, "get_app_global_config_dir", None) if module else None
    if callable(getter):
        try:
            return Path(getter())
        except Exception:
            pass
    return Path.home() / ".config" / "codewood"


def updates_dir(config_dir: Optional[Path] = None) -> Path:
    """Directory holding downloaded installers (``<config>/cache``).

    The cache sub-directory of the config directory is where the downloaded
    package lives, per the auto-update requirement.
    """
    base = Path(config_dir) if config_dir is not None else _config_dir()
    return base / _CACHE_SUBDIR


def update_check_enabled() -> bool:
    """Whether automatic update checks are enabled (``CODEWOOD_UPDATE=0`` opts out)."""
    raw = str(os.environ.get("CODEWOOD_UPDATE", "")).strip().lower()
    return raw not in ("0", "false", "no", "off")


def proxy_fallback_enabled() -> bool:
    """Whether the GitHub mirror fallback is enabled.

    On by default (a direct GitHub download is attempted first); set
    ``CODEWOOD_UPDATE_PROXY=0`` to keep every request pointed at the origin.
    """
    raw = str(os.environ.get(_PROXY_ENV_VAR, "")).strip().lower()
    return raw not in ("0", "false", "no", "off")


def proxied_url(url: str) -> str:
    """Return *url* routed through the download mirror.

    The mirror is addressed by prefixing the full origin URL, e.g.
    ``https://gh-proxy.com/https://github.com/...``.
    """
    return f"{_PROXY_PREFIX}{url}"


_CONTENT_RANGE_RE = re.compile(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", re.IGNORECASE)


def parse_digest(value: Any) -> Optional[str]:
    """Return the lowercase hex SHA-256 from a GitHub asset ``digest`` field.

    GitHub reports ``"sha256:<hex>"``. Any other algorithm, or a malformed
    value, returns None so the caller *skips* verification instead of
    comparing against something meaningless.
    """
    raw = str(value or "").strip()
    if not raw:
        return None
    algorithm, _, hexdigest = raw.partition(":")
    if algorithm.strip().lower() != "sha256":
        return None
    digest = hexdigest.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        return None
    return digest


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """Streaming SHA-256 of *path* (the payloads are ~150 MB)."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_content_range(value: Any) -> Optional[Tuple[int, int, int]]:
    """Parse a ``Content-Range`` header into ``(start, end, total)``.

    ``total`` is 0 when the server reports ``*`` (unknown length). Returns None
    for a missing/unparseable header.
    """
    match = _CONTENT_RANGE_RE.search(str(value or ""))
    if not match:
        return None
    total_raw = match.group(3)
    return (
        int(match.group(1)),
        int(match.group(2)),
        0 if total_raw == "*" else int(total_raw),
    )


def parse_version(text: Any) -> Tuple[int, ...]:
    """Parse a release tag into a comparable tuple (``"v1.2.3" -> (1, 2, 3)``).

    Only the leading dotted numeric part is significant; pre-release/build
    suffixes (``-beta.1``, ``+build``) are ignored so an unexpected suffix can
    never make a newer release sort lower.
    """
    raw = str(text or "").strip().lstrip("vV")
    match = re.match(r"^(\d+(?:\.\d+)*)", raw)
    if not match:
        return ()
    return tuple(int(part) for part in match.group(1).split("."))


def is_newer(candidate: Any, current: Any) -> bool:
    """True when *candidate* is a strictly newer version than *current*."""
    new = parse_version(candidate)
    cur = parse_version(current)
    if not new:
        return False
    if not cur:
        # An unparseable local version (dev run) must not fake an update.
        return False
    width = max(len(new), len(cur))
    return new + (0,) * (width - len(new)) > cur + (0,) * (width - len(cur))


#: ``IMAGE_FILE_MACHINE_*`` PE header values of the architectures an installer
#: can be built for, mapped to the names used in release asset file names.
_PE_MACHINE_ARCH = {
    0x014C: "x86",  # IMAGE_FILE_MACHINE_I386
    0x01C4: "arm64",  # IMAGE_FILE_MACHINE_ARMNT (32-bit ARM)
    0x8664: "x64",  # IMAGE_FILE_MACHINE_AMD64
    0xAA64: "arm64",  # IMAGE_FILE_MACHINE_ARM64
}


def _windows_build_arch() -> str:
    """Architecture the running executable was built for (``""`` when unknown).

    On Windows ``platform.machine()`` describes the *machine*, not the running
    process: an x64 build emulated on Windows-on-ARM still reports ``"ARM64"``
    (via ``PROCESSOR_ARCHITEW6432``). Selecting the installer from that value
    made such a build reject its own ``…-windows-x64-…`` package and never
    update. The installer has to match the running build, so the PE header of
    the running executable is inspected instead — the same trick as
    ``cli/main.py:_is_arm64_python``.
    """
    if sys.platform != "win32":
        return ""
    try:
        with open(sys.executable, "rb") as handle:
            # DOS header: ``e_lfanew`` at offset 60 -> PE header offset.
            handle.seek(60)
            pe_offset = struct.unpack("<I", handle.read(4))[0]
            handle.seek(pe_offset)
            if handle.read(4) != b"PE\x00\x00":
                return ""
            return _PE_MACHINE_ARCH.get(struct.unpack("<H", handle.read(2))[0], "")
    except Exception:
        return ""


def _machine_arch() -> str:
    """Normalized architecture of the running build (``arm64``/``x64``/…)."""
    machine = (_windows_build_arch() or platform.machine()).lower()
    if machine in ("arm64", "aarch64"):
        return "arm64"
    if machine in ("x86_64", "amd64"):
        return "x64"
    return machine


def _arch_alias_groups() -> List[Tuple[str, ...]]:
    """Architecture spellings used in asset names, most specific first."""
    arch = _machine_arch()
    if arch == "arm64":
        return [("arm64", "aarch64")]
    if arch == "x64":
        return [("x64", "amd64", "x86_64"), ("intel",)]
    return [(arch,)]


def _platform_asset_names() -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    """Return (os keywords, preferred extensions) for the current platform.

    Linux prefers the AppImage: it is the only fully distro-agnostic, no-root
    package format, so it is the "universal" choice; ``.deb`` is a fallback.
    """
    if sys.platform == "win32":
        return ("windows",), (".exe",)
    if sys.platform == "darwin":
        return ("macos", "darwin"), (".pkg", ".dmg")
    return ("linux",), (".appimage", ".deb")


def select_asset(assets: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Pick the installer asset for this platform from a release's assets.

    Selection is by file name: the OS keyword must appear, the architecture
    of the running build (:func:`_machine_arch`) must match when the name
    names one, and the extension must be one of the platform's installer
    formats (an ``.AppImage`` beats a ``.deb``, a ``.pkg`` beats a ``.dmg``).
    Portable archives never match. Returns None when the release ships
    nothing installable for this platform/arch.
    """
    os_keywords, extensions = _platform_asset_names()
    arch_groups = _arch_alias_groups()
    best: Optional[Tuple[Tuple[int, int], Dict[str, Any]]] = None
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = str(asset.get("name") or "").strip()
        url = str(asset.get("browser_download_url") or "").strip()
        if not name or not url:
            continue
        lowered = name.lower()
        if "portable" in lowered or not any(k in lowered for k in os_keywords):
            continue
        ext_rank = next(
            (i for i, ext in enumerate(extensions) if lowered.endswith(ext)), None
        )
        if ext_rank is None:
            continue
        arch_rank = len(arch_groups)
        for index, aliases in enumerate(arch_groups):
            if any(alias in lowered for alias in aliases):
                arch_rank = index
                break
        else:
            # The name advertises *some other* architecture (e.g. a
            # ``windows-arm64`` build on an x64 machine): skip it.
            if re.search(r"(arm64|aarch64|x64|amd64|i386|x86)", lowered):
                continue
        score = (ext_rank, arch_rank)
        if best is None or score < best[0]:
            best = (score, asset)
    return best[1] if best else None


def select_release(releases: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Return the newest published (non-draft, non-prerelease) release."""
    published = [
        rel
        for rel in releases
        if isinstance(rel, dict)
        and not rel.get("draft")
        and not rel.get("prerelease")
        and parse_version(rel.get("tag_name") or rel.get("name"))
    ]
    if not published:
        return None
    return max(
        published,
        key=lambda rel: parse_version(rel.get("tag_name") or rel.get("name")),
    )


class UpdateManager:
    """Background update checker/downloader with a small thread-safe state.

    The state read by the frontend (and by tests) is a plain dict produced by
    :meth:`state`; every mutation happens under ``self._lock``.
    """

    def __init__(
        self,
        config_dir: Optional[Path] = None,
        on_ready: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_quit: Optional[Callable[[], None]] = None,
        interval: float = CHECK_INTERVAL_SECONDS,
    ) -> None:
        self._dir = updates_dir(config_dir)
        self._on_ready = on_ready
        self._on_quit = on_quit
        self._interval = float(interval)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # Guards against two overlapping check/download cycles inside this
        # process (e.g. an hourly tick firing while a manual check runs).
        self._run_lock = threading.Lock()
        self._lock_handle: Optional[TextIO] = None
        self._state: Dict[str, Any] = {
            "status": STATUS_IDLE,
            "version": "",
            "asset": "",
            "path": "",
            "received": 0,
            "total": 0,
            "error": "",
        }

    # -- state ------------------------------------------------------------

    def state(self) -> Dict[str, Any]:
        """Snapshot of the current updater state (safe to call from any thread)."""
        with self._lock:
            snapshot = dict(self._state)
        total = int(snapshot.get("total") or 0)
        received = int(snapshot.get("received") or 0)
        snapshot["progress"] = (
            max(0.0, min(1.0, received / total)) if total > 0 else 0.0
        )
        return snapshot

    def _update(self, **fields: Any) -> None:
        with self._lock:
            self._state.update(fields)

    @property
    def config_dir(self) -> Path:
        """Directory holding the cached installer(s)."""
        return self._dir

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        """Start the background checker (idempotent)."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="update-checker"
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._release_download_lock()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.check_once()
            except Exception as exc:  # pragma: no cover - defensive
                self._update(status=STATUS_FAILED, error=str(exc))
            if self._stop.wait(self._interval):
                break

    # -- single-writer lock -----------------------------------------------

    def _acquire_download_lock(self) -> bool:
        """Take the cross-process download lock; False when another instance owns it.

        Two Code Wood processes can share one config directory — on macOS the
        window's close button only hides the app, so a second launch while the
        first is still downloading is normal. Without this lock both processes
        would append to the same ``.part`` file and corrupt it (an oversized
        file whose bogus length then poisons every later resume).

        ``flock``/``msvcrt.locking`` are advisory and released by the OS when a
        process exits, so a crashed instance never leaves the lock held.
        """
        if self._lock_handle is not None:
            return True
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            handle = open(self._dir / _LOCK_FILENAME, "a+")
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except Exception:
            # Held by another process (or locking unavailable): defer.
            try:
                handle.close()
            except Exception:
                pass
            return False
        self._lock_handle = handle
        return True

    def _release_download_lock(self) -> None:
        handle, self._lock_handle = self._lock_handle, None
        if handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            handle.close()
        except Exception:
            pass

    # -- checking ---------------------------------------------------------

    def _session(self):
        """A ``requests`` session that verifies TLS against the OS trust store.

        Built by loading ``cli/config/tls.py`` by file path (same trick as
        ``app_info``), so enterprise roots trusted by the OS are accepted
        without importing the ``cli`` package. Falls back to plain ``requests``
        when the module cannot be loaded.
        """
        try:
            path = Path(__file__).resolve().parents[2] / "cli" / "config" / "tls.py"
            spec = importlib.util.spec_from_file_location(
                "codewood_host_updater_tls", str(path)
            )
            if spec is not None and spec.loader is not None:
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                return module.https_session()
        except Exception:
            pass
        import requests

        return requests.Session()

    def fetch_releases(self) -> List[Dict[str, Any]]:
        """Query GitHub releases, retrying metadata through the configured mirror."""
        urls = [_GITHUB_API_RELEASES_URL]
        if proxy_fallback_enabled():
            urls.append(proxied_url(_GITHUB_API_RELEASES_URL))
        session = self._session()
        last_error: Optional[Exception] = None
        try:
            for index, url in enumerate(urls):
                try:
                    response = session.get(
                        url,
                        headers={
                            "User-Agent": _user_agent(),
                            "Accept": "application/vnd.github+json",
                        },
                        timeout=_META_TIMEOUT,
                    )
                    response.raise_for_status()
                    payload = response.json()
                    if not isinstance(payload, list):
                        return []
                    return [rel for rel in payload if isinstance(rel, dict)]
                except Exception as exc:
                    last_error = exc
                    if index + 1 < len(urls):
                        log(
                            f"release metadata request failed; retrying via mirror "
                            f"{urls[index + 1]}"
                        )
        finally:
            try:
                session.close()
            except Exception:
                pass
        if last_error is not None:
            raise last_error
        return []

    def check_once(self) -> Dict[str, Any]:
        """Run a single check-and-download cycle; returns the new state."""
        if not self._run_lock.acquire(blocking=False):
            # A cycle is already running in this process.
            return self.state()
        try:
            return self._check_once_locked()
        finally:
            self._run_lock.release()

    def _check_once_locked(self) -> Dict[str, Any]:
        if not self._acquire_download_lock():
            # Another Code Wood process owns the shared download.
            log("another instance is already downloading; skipping this check")
            return self.state()

        try:
            current = _current_version()
            self._update(status=STATUS_CHECKING, error="")
            release = select_release(self.fetch_releases())
            if release is None:
                self._update(status=STATUS_UPTODATE, version="", asset="", path="")
                return self.state()

            version = str(
                release.get("tag_name") or release.get("name") or ""
            ).strip()
            if not is_newer(version, current):
                self._update(status=STATUS_UPTODATE, version="", asset="", path="")
                return self.state()

            assets = release.get("assets")
            asset = select_asset(list(assets) if isinstance(assets, list) else [])
            if asset is None:
                # A newer release exists but ships nothing for this platform yet.
                self._update(status=STATUS_UPTODATE, version="", asset="", path="")
                return self.state()

            name = str(asset.get("name") or "")
            url = str(asset.get("browser_download_url") or "")
            size = int(asset.get("size") or 0)
            digest = parse_digest(asset.get("digest")) or ""
            self._prepare_cache(name, version, url)
            target = self._dir / name
            if target.exists() and (not size or target.stat().st_size == size):
                if not digest or self._matches_digest(target, digest):
                    log(f"{version} already downloaded ({name})")
                    self._set_ready(version, name, target)
                    return self.state()
                # A cached package that fails its checksum is not reusable.
                log(f"cached {name} failed its SHA-256 check; downloading again")
                try:
                    target.unlink()
                except Exception:
                    pass

            log(f"downloading {version} ({name}) -> {target}")
            self._update(
                status=STATUS_DOWNLOADING,
                version=version,
                asset=name,
                path=str(target),
                received=0,
                total=size,
            )
            if not self._download(url, target, size, digest):
                log(f"download of {name} did not complete; will resume next time")
                self._update(status=STATUS_FAILED)
                return self.state()
            log(f"{version} ready to install")
            self._set_ready(version, name, target)
            return self.state()
        finally:
            self._release_download_lock()

    # -- download ---------------------------------------------------------

    def _prune_legacy_dir(self) -> None:
        """Delete the pre-flat-layout ``cache/updates/`` directory, once."""
        try:
            legacy = self._dir / _LEGACY_SUBDIR
            if legacy.is_dir():
                shutil.rmtree(legacy)
        except Exception:
            pass

    def _prepare_cache(self, asset_name: str, version: str, url: str) -> None:
        """Keep only the newest package: drop stale packages, partials, meta.

        Only file names this module owns are touched — a previously downloaded
        installer, its ``.part`` file, and the sidecar. The cache directory is
        shared with unrelated content (the AI scratch cache, MCP icons), so
        anything else in it is left alone.

        A ``.part`` file is kept only when the sidecar says it belongs to this
        exact asset; the sidecar is read *before* it is rewritten, so a partial
        transfer from a different release can never be mistaken for a
        resumable one.
        """
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            return
        self._prune_legacy_dir()
        part_name = f"{asset_name}.part"
        resumable = self._read_meta().get("asset") == asset_name
        try:
            entries = list(self._dir.iterdir())
        except Exception:
            return
        for entry in entries:
            if entry.name in (asset_name, _META_FILENAME):
                continue
            if entry.name == part_name and resumable:
                continue
            if not _is_owned_cache_name(entry.name):
                continue
            try:
                shutil.rmtree(entry) if entry.is_dir() else entry.unlink()
            except Exception:
                pass
        self._write_meta({"version": version, "asset": asset_name, "url": url})

    @staticmethod
    def _matches_digest(path: Path, digest: str) -> bool:
        """True when *path* hashes to *digest* (False on any read error)."""
        try:
            return sha256_file(path) == digest
        except Exception:
            return False

    def _read_meta(self) -> Dict[str, Any]:
        try:
            data = json.loads((self._dir / _META_FILENAME).read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _write_meta(self, payload: Dict[str, Any]) -> None:
        try:
            (self._dir / _META_FILENAME).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            pass

    def _candidate_urls(self, url: str) -> List[str]:
        """Download sources to try in order: origin first, then the mirror.

        When a previous attempt left a partial file, the source that produced it
        is retried first so the transfer continues where it stopped rather than
        jumping between origins.
        """
        candidates = [url]
        if proxy_fallback_enabled() and not url.startswith(_PROXY_PREFIX):
            candidates.append(proxied_url(url))
        previous = str(self._read_meta().get("source") or "")
        if previous in candidates:
            candidates = [previous] + [c for c in candidates if c != previous]
        return candidates

    def _download(self, url: str, target: Path, size: int, digest: str = "") -> bool:
        """Download *target*, falling back to the mirror when the origin fails.

        The origin URL is tried first; if that transfer fails — unreachable
        host, TLS error, stalled body — it is retried through
        ``https://gh-proxy.com/<url>``. Both sources serve the same bytes, so a
        partial file can be resumed by whichever source is reachable.

        A corrupt response (more bytes than the release advertises) is fatal:
        another mirror would serve the same payload, so the retry is skipped.
        """
        candidates = self._candidate_urls(url)
        for index, candidate in enumerate(candidates):
            if self._stop.is_set():
                # Quitting: keep the partial file so the next launch resumes.
                return False
            outcome = self._transfer(candidate, target, size, digest)
            if outcome == _TRANSFER_DONE:
                meta = self._read_meta()
                meta["source"] = candidate
                self._write_meta(meta)
                return True
            if self._stop.is_set():
                return False
            if outcome == _TRANSFER_FATAL:
                return False
            if index + 1 < len(candidates):
                log(f"download failed; retrying via mirror {candidates[index + 1]}")
        return False

    def _transfer(self, url: str, target: Path, size: int, digest: str = "") -> str:
        """Copy one complete transfer from *url* into ``<target>.part``.

        Returns one of ``_TRANSFER_DONE`` / ``_TRANSFER_RETRY`` /
        ``_TRANSFER_FATAL``. On a resumable failure the partial file is left in
        place so the next attempt — or the next launch — continues from its
        current length; on a corrupt one it is deleted so the offset can never
        be trusted again.

        When *digest* is a SHA-256, the assembled file is hashed before the
        atomic rename; a mismatch discards the file and reports a fatal error,
        because a wrong payload is not something a retry through another mirror
        can fix without the same corrupt data.
        """
        part = target.with_name(f"{target.name}.part")
        meta = self._read_meta()
        expected_total = int(size or 0)
        resumed_from = 0
        if part.exists():
            if meta.get("asset") == target.name:
                resumed_from = part.stat().st_size
            else:
                # The partial file belongs to a different release/asset.
                try:
                    part.unlink()
                except Exception:
                    pass
                resumed_from = 0

        # A partial longer than the release itself is corrupt (a previous run
        # appended past the end). Resuming from that offset would only grow it,
        # so it is discarded before any request is made.
        if expected_total and resumed_from > expected_total:
            log(
                f"partial is larger than the release ({resumed_from} > "
                f"{expected_total}); restarting the download"
            )
            try:
                part.unlink()
            except Exception:
                pass
            resumed_from = 0

        headers = {"User-Agent": _user_agent()}
        if resumed_from > 0:
            headers["Range"] = f"bytes={resumed_from}-"

        session = self._session()
        try:
            response = session.get(
                url, headers=headers, stream=True, timeout=_DOWNLOAD_TIMEOUT
            )
            content_range = parse_content_range(response.headers.get("Content-Range"))
            range_end: Optional[int] = None
            if response.status_code == 206:
                if content_range is None:
                    # Cannot verify where the body starts; safer to rewrite.
                    log("206 without Content-Range; restarting the download")
                    mode, resumed_from = "wb", 0
                else:
                    start, end, total = content_range
                    if end < start:
                        raise RuntimeError("malformed Content-Range")
                    if start != resumed_from:
                        # The server resumed somewhere else than we asked, so
                        # appending would duplicate a region. Rewrite instead.
                        log(
                            f"server resumed at {start}, expected {resumed_from}; "
                            f"restarting the download"
                        )
                        mode, resumed_from = "wb", 0
                    else:
                        mode = "ab"
                    expected_total = total or expected_total
                    range_end = end
            elif response.status_code == 200:
                # Server ignored the Range request: restart from scratch.
                mode = "wb"
                resumed_from = 0
                expected_total = expected_total or int(
                    response.headers.get("Content-Length") or 0
                )
            else:
                response.raise_for_status()
                return _TRANSFER_RETRY

            declared = int(response.headers.get("Content-Length") or 0)
            if declared:
                expected_total = max(expected_total, resumed_from + declared)
            self._update(received=resumed_from, total=expected_total)
            with open(part, mode) as handle:
                written = resumed_from
                for chunk in response.iter_content(chunk_size=_CHUNK_SIZE):
                    if self._stop.is_set():
                        # Process is quitting: keep the .part file so the next
                        # launch resumes exactly here.
                        return _TRANSFER_RETRY
                    if not chunk:
                        continue
                    pending = written + len(chunk)
                    limit = range_end + 1 if range_end is not None else expected_total
                    if limit and pending > limit:
                        # The stream is delivering more than advertised: stop
                        # immediately instead of growing a corrupt file (whose
                        # bogus length would poison every later resume). The
                        # offending chunk is never written.
                        log(
                            f"download exceeded the expected size ({pending} > "
                            f"{limit}); discarding the partial file"
                        )
                        self._update(error="download exceeded expected size")
                        return _TRANSFER_FATAL
                    handle.write(chunk)
                    written = pending
                    self._update(received=written)
        except Exception as exc:
            self._update(error=str(exc))
            return _TRANSFER_RETRY
        finally:
            try:
                session.close()
            except Exception:
                pass

        actual = part.stat().st_size
        if expected_total and actual != expected_total:
            if actual > expected_total:
                log(f"partial is oversized ({actual} > {expected_total}); discarding")
                try:
                    part.unlink()
                except Exception:
                    pass
                self._update(error="download exceeded expected size")
                return _TRANSFER_FATAL
            # Truncated transfer: keep the partial file for a later resume.
            return _TRANSFER_RETRY

        if digest:
            # Verify the assembled payload before it becomes the installable
            # package. A size check alone cannot catch a mirror that served the
            # right number of wrong bytes.
            try:
                actual_digest = sha256_file(part)
            except Exception as exc:
                self._update(error=str(exc))
                return _TRANSFER_RETRY
            if actual_digest != digest:
                log(
                    f"checksum mismatch for {target.name} "
                    f"(expected {digest}, got {actual_digest}); discarding"
                )
                try:
                    part.unlink()
                except Exception:
                    pass
                self._update(
                    status=STATUS_FAILED, error="downloaded package failed its SHA-256 check"
                )
                return _TRANSFER_FATAL

        try:
            part.replace(target)
        except Exception as exc:
            self._update(error=str(exc))
            return _TRANSFER_RETRY
        return _TRANSFER_DONE

    def _set_ready(self, version: str, asset_name: str, target: Path) -> None:
        total = int(self.state().get("total") or 0)
        self._update(
            status=STATUS_READY,
            version=version,
            asset=asset_name,
            path=str(target),
            received=total,
            total=total,
            error="",
        )
        if self._on_ready is not None:
            try:
                self._on_ready(self.state())
            except Exception:
                pass

    # -- install ----------------------------------------------------------

    def installer_path(self) -> Optional[Path]:
        """Path of the ready-to-install package, or None."""
        state = self.state()
        if state.get("status") != STATUS_READY:
            return None
        path = Path(str(state.get("path") or ""))
        return path if path.is_file() else None

    def install(self) -> bool:
        """Launch the downloaded installer, then ask the app to quit.

        The installer is started detached so it survives this process exiting;
        ``on_quit`` is invoked afterwards (when supplied) so the host can shut
        the backend down and leave the process.
        """
        path = self.installer_path()
        if path is None:
            return False
        try:
            self._launch_installer(path)
        except Exception as exc:
            self._update(error=str(exc))
            return False
        if self._on_quit is not None:
            try:
                self._on_quit()
            except Exception:
                pass
        return True

    @staticmethod
    def _launch_installer(path: Path) -> None:
        """Start the platform's installer for *path*, detached from this app."""
        if sys.platform == "win32":
            os.startfile(str(path))  # noqa: S606 - user-visible installer package
            return
        if sys.platform == "darwin":
            subprocess.Popen(  # noqa: S603 - our own downloaded package
                ["open", str(path)],
                close_fds=True,
                start_new_session=True,
            )
            return
        if path.suffix.lower() == ".appimage":
            try:
                os.chmod(path, 0o755)
            except Exception:
                pass
            subprocess.Popen(  # noqa: S603 - our own downloaded package
                [str(path)], close_fds=True, start_new_session=True
            )
            return
        subprocess.Popen(  # noqa: S603 - our own downloaded package
            ["xdg-open", str(path)], close_fds=True, start_new_session=True
        )


def log(message: str) -> None:
    """Write an updater line to stderr (the host's diagnostic channel)."""
    try:
        print(f"[update] {message}", file=sys.stderr)
    except Exception:
        pass


def start_update_manager(
    config_dir: Optional[Path] = None,
    on_ready: Optional[Callable[[Dict[str, Any]], None]] = None,
    on_quit: Optional[Callable[[], None]] = None,
) -> Optional[UpdateManager]:
    """Create and start the update manager, or None when disabled/unavailable."""
    if not update_check_enabled():
        return None
    manager = UpdateManager(config_dir=config_dir, on_ready=on_ready, on_quit=on_quit)
    manager.start()
    return manager
