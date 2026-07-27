import json
import logging
import os
import platform
import re
import shutil
import tarfile
import tempfile
import threading
import zipfile
from pathlib import Path
from urllib.request import Request, urlopen

_logger = logging.getLogger(__name__)
_logger.propagate = False

_GITHUB_API_RELEASES_URL = "https://api.github.com/repos/BurntSushi/ripgrep/releases/latest"
_GITHUB_LATEST_REDIRECT_URL = "https://github.com/BurntSushi/ripgrep/releases/latest"

_RG_BINARY_NAME = "rg.exe" if os.name == "nt" else "rg"

_download_lock = threading.Lock()
_download_in_progress = False
_download_complete = False

RG_STATUS_IDLE = "idle"
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


def _reset_status() -> None:
    global _rg_status, _rg_status_message
    _rg_status = RG_STATUS_IDLE
    _rg_status_message = ""


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


def _try_api_latest() -> tuple[str | None, str | None]:
    try:
        req = Request(
            _GITHUB_API_RELEASES_URL,
            headers={"Accept": "application/json", "User-Agent": "codewood"},
        )
        with urlopen(req, timeout=20) as response:
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
        with urlopen(req, timeout=15) as response:
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
    version, url = _try_api_latest()
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


def _download_and_extract_rg(bin_dir: Path) -> bool:
    global _rg_status, _rg_status_message

    try:
        _logger.info("Resolving ripgrep download for %s/%s", platform.system(), platform.machine())

        version, download_url = _resolve_download_info()
        if not download_url:
            _rg_status = RG_STATUS_FAILED
            _rg_status_message = f"ripgrep: unsupported platform {platform.system()}/{platform.machine()}"
            _logger.error(_rg_status_message)
            return False

        _rg_status = RG_STATUS_DOWNLOADING
        _rg_status_message = "Downloading ripgrep..."
        _logger.info("Downloading ripgrep %s from: %s", version or "latest", download_url)

        ext = _archive_extension()
        tmp_path = Path(tempfile.mktemp(suffix=ext))

        try:
            req = Request(download_url, headers={"User-Agent": "codewood"})
            with urlopen(req, timeout=120) as response:
                with open(tmp_path, "wb") as f:
                    shutil.copyfileobj(response, f)

            _rg_status = RG_STATUS_EXTRACTING
            _rg_status_message = "Installing ripgrep..."
            _logger.info("Extracting rg binary")

            bin_dir.mkdir(parents=True, exist_ok=True)

            if os.name == "nt":
                ok = _extract_rg_from_zip(tmp_path, bin_dir)
            else:
                ok = _extract_rg_from_tar(tmp_path, bin_dir)

            if ok:
                _rg_status = RG_STATUS_SUCCESS
                _rg_status_message = "ripgrep installed"
                _logger.info("rg installed to %s", bin_dir / _RG_BINARY_NAME)
                return True
            else:
                _rg_status = RG_STATUS_FAILED
                _rg_status_message = "ripgrep download failed"
                _logger.error("Failed to extract rg from archive")
                return False
        finally:
            try:
                tmp_path.unlink()
            except Exception:
                pass
    except Exception as e:
        _rg_status = RG_STATUS_FAILED
        _rg_status_message = "ripgrep download failed"
        _logger.error("Failed to download rg: %s", e)
        return False


def is_rg_available(bin_dir: Path) -> bool:
    rg_path = bin_dir / _RG_BINARY_NAME
    return rg_path.is_file()


def ensure_rg_async(bin_dir: Path) -> None:
    global _download_in_progress, _download_complete, _rg_status

    if _download_complete or is_rg_available(bin_dir):
        _download_complete = True
        _rg_status = RG_STATUS_IDLE
        return

    with _download_lock:
        if _download_in_progress:
            return
        _download_in_progress = True

    def _download_thread():
        global _download_complete
        try:
            _download_and_extract_rg(bin_dir)
            _download_complete = True
        except Exception:
            _rg_status = RG_STATUS_FAILED
            _rg_status_message = "ripgrep download failed"

    thread = threading.Thread(target=_download_thread, daemon=True, name="rg-downloader")
    thread.start()
