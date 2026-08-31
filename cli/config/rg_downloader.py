import json
import os
import platform
import re
import shutil
import ssl
import sys
import tarfile
import tempfile
import threading
import zipfile
from pathlib import Path
from urllib.request import Request, urlopen

from ..core.logging.app_logging import get_logger, get_app_logger_root

# Use the application logger tree (not `logging.getLogger(__name__)`): the
# serve process installs an SSE bridge as `sys.stderr`, and a logger without
# a real handler falls through to `logging.lastResort`, which writes to that
# bridged stderr and would surface internal log lines inside the GUI chat.
_logger = get_logger(f"{get_app_logger_root()}.config.rg_downloader")


def _create_ssl_context() -> ssl.SSLContext:
    """Create an SSL context that works on Windows with system certificates.

    On Windows, Python's ``ssl`` module does not use the system certificate
    store by default, causing ``CERTIFICATE_VERIFY_FAILED`` for HTTPS
    downloads.  This helper tries, in order:
    1. The ``certifi`` package (if installed).
    2. The system default store via ``ssl.create_default_context()`` with
       ``load_default_certs()``.
    3. A permissive fallback (logs a warning) so packaging can proceed even
       in air-gapped or misconfigured environments.
    """
    # 1) certifi — the most portable solution
    try:
        import certifi  # type: ignore[import-untyped]

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass

    # 2) System certificate store (works on most platforms)
    ctx = ssl.create_default_context()
    try:
        ctx.load_default_certs()
    except Exception:
        # load_default_certs is a no-op on some builds; ignore.
        pass

    # If the context has no loaded CAs, fall through to a permissive ctx.
    if ctx.get_ca_certs():
        return ctx

    # 3) Permissive fallback — allows downloads to proceed in environments
    #    where the certificate store is not available (CI containers, etc.).
    _logger.warning(
        "No system CA certificates found; falling back to unverified HTTPS "
        "for rg download.  Install the ``certifi`` package to restore "
        "certificate verification."
    )
    permissive = ssl.create_default_context()
    permissive.check_hostname = False
    permissive.verify_mode = ssl.CERT_NONE
    return permissive


# Lazy-initialised so the env check only runs once per process.
_ssl_ctx: ssl.SSLContext | None = None


def _get_ssl_context() -> ssl.SSLContext:
    global _ssl_ctx
    if _ssl_ctx is None:
        _ssl_ctx = _create_ssl_context()
    return _ssl_ctx

_GITHUB_API_RELEASES_URL = "https://api.github.com/repos/BurntSushi/ripgrep/releases/latest"
_GITHUB_LATEST_REDIRECT_URL = "https://github.com/BurntSushi/ripgrep/releases/latest"

_RG_BINARY_NAME = "rg.exe" if os.name == "nt" else "rg"
_VERSION_FILENAME = "rg-version.txt"

_download_lock = threading.Lock()
_download_in_progress = False
_download_complete = False

RG_STATUS_IDLE = "idle"
RG_STATUS_CHECKING = "checking"
RG_STATUS_DOWNLOADING = "downloading"
RG_STATUS_EXTRACTING = "extracting"
RG_STATUS_FAILED = "failed"
RG_STATUS_SUCCESS = "success"

_rg_status = RG_STATUS_IDLE
_rg_status_message = ""


def get_rg_status() -> str:
    return _rg_status


def get_rg_status_message() -> str:
    return _rg_status_message


def _detect_platform_target() -> str | None:
    system = platform.system()
    machine = platform.machine().lower()

    if machine in ("amd64", "x86_64"):
        arch = "x86_64"
    elif machine in ("arm64", "aarch64"):
        arch = "aarch64"
    else:
        return None

    if system == "Windows":
        return f"{arch}-pc-windows-msvc"
    elif system == "Linux":
        if arch == "x86_64":
            return f"{arch}-unknown-linux-musl"
        else:
            return f"{arch}-unknown-linux-gnu"
    elif system == "Darwin":
        return f"{arch}-apple-darwin"
    return None


def _archive_extension() -> str:
    return ".zip" if os.name == "nt" else ".tar.gz"


def _read_local_version(bin_dir: Path) -> str | None:
    version_file = bin_dir / _VERSION_FILENAME
    try:
        if version_file.is_file():
            return version_file.read_text(encoding="utf-8").strip() or None
    except Exception:
        pass
    return None


def _write_local_version(bin_dir: Path, version: str) -> None:
    version_file = bin_dir / _VERSION_FILENAME
    try:
        version_file.write_text(str(version).strip(), encoding="utf-8")
    except Exception:
        pass


def _resolve_latest_version() -> str | None:
    version, _url = _try_api_latest_with_url()
    if version:
        return version
    return _try_redirect_version()


def _try_api_latest_with_url() -> tuple[str | None, str | None]:
    try:
        req = Request(
            _GITHUB_API_RELEASES_URL,
            headers={"Accept": "application/json", "User-Agent": "codewood"},
        )
        with urlopen(req, timeout=20, context=_get_ssl_context()) as response:
            data = json.loads(response.read().decode("utf-8"))

        version = data.get("tag_name", "")
        if not version:
            return None, None

        platform_target = _detect_platform_target()
        if platform_target is None:
            return None, None

        assets = data.get("assets", [])
        ext = _archive_extension()
        for asset in assets:
            name = asset.get("name", "")
            if platform_target in name and name.lower().endswith(ext):
                return version, asset.get("browser_download_url")

        return version, None
    except Exception:
        return None, None


def _try_redirect_version() -> str | None:
    try:
        req = Request(
            _GITHUB_LATEST_REDIRECT_URL,
            headers={"User-Agent": "codewood"},
        )
        with urlopen(req, timeout=15, context=_get_ssl_context()) as response:
            final_url = response.geturl()
            match = re.search(r"/tag/([^/]+?)(?:$|\?)", final_url)
            if match:
                return match.group(1)
    except Exception:
        pass
    return None


def _build_download_url(version: str) -> str | None:
    platform_target = _detect_platform_target()
    if platform_target is None:
        return None
    ext = _archive_extension()
    clean_version = version.lstrip("v")
    return (
        f"https://github.com/BurntSushi/ripgrep/releases/download/"
        f"{version}/ripgrep-{clean_version}-{platform_target}{ext}"
    )


def _resolve_download_info() -> tuple[str | None, str | None]:
    version, url = _try_api_latest_with_url()
    if url:
        return version, url

    if not version:
        version = _try_redirect_version()

    if not version:
        return None, None

    url = _build_download_url(version)
    if url:
        return version, url

    return None, None


def _extract_rg_from_zip(archive_path: Path, bin_dir: Path) -> bool:
    with zipfile.ZipFile(archive_path, "r") as zf:
        for member in zf.namelist():
            member_path = Path(member)
            if member_path.name.lower() in ("rg.exe", "rg") and len(member_path.parts) > 1:
                dest_path = bin_dir / _RG_BINARY_NAME
                with zf.open(member) as src, open(dest_path, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                if os.name != "nt":
                    os.chmod(dest_path, 0o755)
                return True
    return False


def _extract_rg_from_tar(archive_path: Path, bin_dir: Path) -> bool:
    with tarfile.open(archive_path, "r:gz") as tf:
        for member in tf.getmembers():
            member_path = Path(member.name)
            if member_path.name.lower() in ("rg.exe", "rg") and len(member_path.parts) > 1 and member.isfile():
                dest_path = bin_dir / _RG_BINARY_NAME
                with tf.extractfile(member) as src, open(dest_path, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                if os.name != "nt":
                    os.chmod(dest_path, 0o755)
                return True
    return False


def _download_and_extract_rg(bin_dir: Path, *, is_update: bool = False) -> bool:
    global _rg_status, _rg_status_message

    action = "update" if is_update else "download"
    try:
        _logger.info("Resolving ripgrep download for %s/%s", platform.system(), platform.machine())

        if _detect_platform_target() is None:
            _rg_status = RG_STATUS_FAILED
            _rg_status_message = f"ripgrep: unsupported platform {platform.system()}/{platform.machine()}"
            _logger.error(_rg_status_message)
            return False

        version, download_url = _resolve_download_info()
        if not download_url:
            _rg_status = RG_STATUS_FAILED
            _rg_status_message = (
                f"rg {action} failed: cannot reach GitHub to resolve the latest release"
            )
            _logger.error(
                "Failed to resolve ripgrep release info: %s", _rg_status_message
            )
            return False

        _rg_status = RG_STATUS_DOWNLOADING
        if is_update:
            _rg_status_message = "Updating rg..."
        else:
            _rg_status_message = "Downloading rg..."
        _logger.info("Downloading ripgrep %s from: %s", version or "latest", download_url)

        ext = _archive_extension()
        tmp_path = Path(tempfile.mktemp(suffix=ext))

        try:
            req = Request(download_url, headers={"User-Agent": "codewood"})
            with urlopen(req, timeout=120, context=_get_ssl_context()) as response:
                with open(tmp_path, "wb") as f:
                    shutil.copyfileobj(response, f)

            _rg_status = RG_STATUS_EXTRACTING
            if is_update:
                _rg_status_message = "Updating rg..."
            else:
                _rg_status_message = "Installing rg..."
            _logger.info("Extracting rg binary")

            bin_dir.mkdir(parents=True, exist_ok=True)

            if os.name == "nt":
                ok = _extract_rg_from_zip(tmp_path, bin_dir)
            else:
                ok = _extract_rg_from_tar(tmp_path, bin_dir)

            if ok:
                if version:
                    _write_local_version(bin_dir, version)
                _rg_status = RG_STATUS_SUCCESS
                if is_update:
                    _rg_status_message = "rg updated"
                else:
                    _rg_status_message = "rg installed"
                _logger.info("rg %s installed to %s", version or "latest", bin_dir / _RG_BINARY_NAME)
                _schedule_success_reset()
                return True
            else:
                _rg_status = RG_STATUS_FAILED
                _rg_status_message = f"rg {action} failed: invalid archive"
                _logger.error("Failed to extract rg from archive: %s", _rg_status_message)
                return False
        finally:
            try:
                tmp_path.unlink()
            except Exception:
                pass
    except Exception as e:
        _rg_status = RG_STATUS_FAILED
        _rg_status_message = f"rg {action} failed: {e}"
        _logger.error("Failed to %s rg: %s", action, e)
        return False


def _schedule_success_reset() -> None:
    def _reset():
        global _rg_status, _rg_status_message
        _rg_status = RG_STATUS_IDLE
        _rg_status_message = ""

    timer = threading.Timer(3.0, _reset)
    timer.daemon = True
    timer.start()


def is_rg_available(bin_dir: Path) -> bool:
    rg_path = bin_dir / _RG_BINARY_NAME
    return rg_path.is_file()


def ensure_rg_sync(bin_dir: Path) -> bool:
    """Blocking download of the ripgrep binary when it is missing.

    Used by the packaging scripts: guarantees that both ``rg`` and
    ``rg-version.txt`` exist in *bin_dir* before bundling, downloading the
    latest release (and writing the version file) when either file is
    absent. Returns ``True`` when both files are present afterwards.
    """
    if is_rg_available(bin_dir) and _read_local_version(bin_dir):
        return True
    return _download_and_extract_rg(bin_dir, is_update=False)


def ensure_rg_async(bin_dir: Path) -> None:
    global _download_in_progress, _download_complete, _rg_status, _rg_status_message

    with _download_lock:
        if _download_in_progress:
            return
        _download_in_progress = True

    def _download_thread():
        global _download_complete, _rg_status, _rg_status_message
        try:
            if is_rg_available(bin_dir):
                local_version = _read_local_version(bin_dir)
                if local_version:
                    _logger.info("Checking for ripgrep update (local: %s)", local_version)

                    latest_version = _resolve_latest_version()
                    if latest_version and latest_version != local_version:
                        _logger.info(
                            "ripgrep update available: %s -> %s", local_version, latest_version
                        )
                        _download_and_extract_rg(bin_dir, is_update=True)
                    else:
                        _rg_status = RG_STATUS_IDLE
                        _rg_status_message = ""
                        _logger.info("ripgrep is up to date (%s)", local_version)
                else:
                    _logger.info("No version file found; downloading latest ripgrep")
                    _download_and_extract_rg(bin_dir, is_update=False)
            else:
                _logger.info("rg not found; downloading ripgrep")
                _download_and_extract_rg(bin_dir, is_update=False)
        except Exception:
            _rg_status = RG_STATUS_FAILED
            _rg_status_message = "ripgrep download failed"
        finally:
            _download_complete = True

    thread = threading.Thread(target=_download_thread, daemon=True, name="rg-downloader")
    thread.start()
