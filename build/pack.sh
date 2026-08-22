#!/usr/bin/env bash
# Build executable with PyInstaller for non-Windows platforms (Linux/macOS).
# Mirrors build/pack.bat. It prepares the Python virtual environment in
# <root>/.venv (creating it and installing requirements.txt if needed) before
# packaging. Run it from anywhere; the script resolves the project root itself:
#   bash build/pack.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

ENTRY_SCRIPT="cli/main.py"
VENV_DIR=".venv"
VENV_PYTHON="$VENV_DIR/bin/python"
REQ_FILE="requirements.txt"

# ---- Prepare the Python virtual environment so all build/runtime
# ---- dependencies (PyInstaller, pywebview, ...) are ready before packaging.
# ---- Mirrors bin/codewood.sh: create .venv if missing, then install
# ---- requirements.txt into it.
if [ ! -x "$VENV_PYTHON" ]; then
  if command -v python3 >/dev/null 2>&1; then
    PY_BOOTSTRAP="python3"
  elif command -v python >/dev/null 2>&1; then
    PY_BOOTSTRAP="python"
  else
    echo "Python executable not found. Please install Python or add it to PATH." >&2
    exit 127
  fi
  echo "Virtual environment not found. Creating \"$VENV_DIR\"..."
  "$PY_BOOTSTRAP" -m venv "$VENV_DIR" || { echo "Failed to create virtual environment." >&2; exit 1; }
fi

if [ ! -f "$REQ_FILE" ]; then
  echo "Requirements file not found: \"$REQ_FILE\"" >&2
  exit 1
fi
echo "Installing/updating dependencies from \"$REQ_FILE\"..."
"$VENV_PYTHON" -m pip install -r "$REQ_FILE" || { echo "Failed to install dependencies." >&2; exit 1; }

# ---- Ensure ripgrep (rg) and rg-version.txt exist so they can be bundled.
# ---- If either is missing, download the latest ripgrep release into bin/
# ---- (which also writes bin/rg-version.txt).
echo "Checking rg and rg-version.txt for bundling..."
if [ ! -f "bin/rg" ] || [ ! -f "bin/rg-version.txt" ]; then
  echo "rg or rg-version.txt not found in bin/. Downloading ripgrep..."
  if ! "$VENV_PYTHON" -c "import sys; sys.path.insert(0, '.'); from pathlib import Path; from cli.config.rg_downloader import ensure_rg_sync; sys.exit(0 if ensure_rg_sync(Path('bin')) else 1)"; then
    echo "Failed to download rg. Aborting packaging." >&2
    exit 1
  fi
fi
echo "rg and rg-version.txt ready for bundling."

# Locate the virtualenv site-packages (.venv/lib/pythonX.Y/site-packages).
VENV_PATH="$(ls -d "$VENV_DIR"/lib/python*/site-packages 2>/dev/null | head -n 1 || true)"
if [ -z "$VENV_PATH" ]; then
  echo "Error: site-packages not found under $VENV_DIR/lib/python*/." >&2
  exit 1
fi

# Prefer the venv's PyInstaller when available, otherwise fall back to PATH.
PYINSTALLER="$VENV_DIR/bin/pyinstaller"
[ -x "$PYINSTALLER" ] || PYINSTALLER="pyinstaller"

# Build the desktop GUI frontend bundle first.
( cd desktop/frontend && npm install && npm run build )

# Source paths are relative to --specpath (build/codewood), matching pack.bat.
# Unix uses ':' as the --add-data separator instead of ';'.
# Note: ripgrep (bin/rg + bin/rg-version.txt) is downloaded above if missing
# and bundled into the package via the --add-data flags below.
#
# One-dir is used (instead of one-file) so each process runs directly without
# an extra self-extracting bootloader process: the GUI then uses two processes
# (app + serve backend) and the terminal UI uses one. Output is a single
# shippable folder dist/codewood/ (codewood, codewood-gui, _internal/).
#
# 1) codewood carries ALL terminal-UI and GUI logic (frontend bundle +
#    pywebview host included). Default = terminal UI; "codewood app" = GUI.

# ---- Download the embedding model before building so PyInstaller can bundle it ----
echo "Checking embedding model for offline bundle..."
MODEL_NAME="all-MiniLM-L6-v2"
if [ ! -f "models/$MODEL_NAME/config.json" ]; then
    echo "Downloading embedding model..."
    if ! "$VENV_PYTHON" -c "from sentence_transformers import SentenceTransformer; m = SentenceTransformer('$MODEL_NAME', device='cpu'); m.save('models/$MODEL_NAME')"; then
        echo "WARNING: Could not download embedding model. The package will require online HF access."
    fi
else
    echo "Embedding model already cached in models/$MODEL_NAME."
fi

DATA_ARGS=(
  --add-data "../../skills:skills"
  --add-data "../../additional-subagents:additional-subagents"
  --add-data "../../cli:cli"
  --add-data "../../desktop/frontend/dist:frontend"
  --add-data "../../desktop/host:host"
  --add-data "../../models:models"
  --add-data "../../bin/rg:bin"
  --add-data "../../bin/rg-version.txt:bin"
  # pathex is resolved relative to the working dir (project root), unlike
  # --add-data sources which are relative to --specpath; so no "../../".
  --paths "$VENV_PATH"
  --collect-all webview
  # tiktoken loads encodings (e.g. cl100k_base) lazily via the tiktoken_ext
  # namespace plugin, which PyInstaller's static analysis cannot see; without
  # these the packaged app silently drops to the heuristic token counter
  # (openai/tiktoken#43, #469 — still no built-in hook).
  --collect-all tiktoken
  --hidden-import tiktoken_ext
  --hidden-import tiktoken_ext.openai_public
)

# App icon: macOS uses .icns; Linux ignores it, so only pass when present.
ICON_ARGS=()
if [ "$(uname -s)" = "Darwin" ] && [ -f "build/app_icon.icns" ]; then
  ICON_ARGS=(--icon "../../build/app_icon.icns")
fi

# 1) codewood (console, one-dir): carries ALL terminal-UI + serve + `app`
#    logic. dist/codewood/codewood is the CLI the user runs in the terminal.
"$PYINSTALLER" "${ICON_ARGS[@]}" --onedir --noconfirm --name codewood \
  --specpath "build/codewood" "${DATA_ARGS[@]}" "$ENTRY_SCRIPT"

# 2) codewood-gui (windowed BUNDLE): the desktop GUI. Built from the same entry
#    with --windowed so PyInstaller produces a proper macOS .app bundle
#    (Contents/MacOS + Contents/Frameworks/Python + Contents/Resources +
#    Info.plist). The window is created by this bundle's own main process, so
#    the Dock icon merges with "Code Wood"; it spawns itself in serve mode for
#    the backend. On Linux this windowed build also opens the GUI.
"$PYINSTALLER" "${ICON_ARGS[@]}" --windowed --noconfirm --name codewood-gui \
  --specpath "build/codewood-gui" "${DATA_ARGS[@]}" "$ENTRY_SCRIPT"

echo "PyInstaller build completed."
echo "  dist/codewood/codewood     - terminal UI (TUI) + serve backend (CLI)"
echo "  dist/codewood-gui.app      - the desktop GUI (.app bundle)"

# ---- Resolve the application version + platform tag so the portable archive
# ---- filename carries version + platform info (e.g.
# ---- CodeWood-0.0.1-linux-x86_64-portable.tar.gz).
APP_VERSION="$("$VENV_PYTHON" -c 'import sys; sys.path.insert(0, "cli"); from config.app_info import get_app_version; print(get_app_version())' 2>/dev/null || echo "0.0.0")"
[ -n "$APP_VERSION" ] || APP_VERSION="0.0.0"

case "$(uname -s)" in
  Darwin) OS_TAG="macos" ;;
  Linux)  OS_TAG="linux" ;;
  *)      OS_TAG="$(uname -s | tr '[:upper:]' '[:lower:]')" ;;
esac
ARCH_TAG="$(uname -m)"
# On Apple Silicon the shell may be running under Rosetta, where ``uname -m``
# reports x86_64 even though the build is truly arm64. Use the hardware arch so
# the artifact tag (and the PyInstaller target) is correct.
if [ "$(uname -s)" = "Darwin" ] && [ "$(sysctl -n hw.optional.arm64 2>/dev/null)" = "1" ]; then
  ARCH_TAG="arm64"
fi
PLATFORM_TAG="${OS_TAG}-${ARCH_TAG}"
echo "Packaging artifacts for version $APP_VERSION ($PLATFORM_TAG)."

# ---- Portable archive: a self-contained, no-install bundle the user can
# ---- extract and run directly. macOS ships zip; both platforms ship tar, so
# ---- we emit a .tar.gz which preserves the executable bit on codewood/.
PORTABLE_ARCHIVE="dist/CodeWood-${APP_VERSION}-${PLATFORM_TAG}-portable.tar.gz"
rm -f "$PORTABLE_ARCHIVE"
echo "Creating portable archive \"$PORTABLE_ARCHIVE\"..."
tar -czf "$PORTABLE_ARCHIVE" -C dist codewood

# Track produced installer artifacts for the final summary.
INSTALLER_NOTES=()

# ---------------------------------------------------------------------------
# macOS native installers (.dmg + .pkg)
# ---------------------------------------------------------------------------
# These rely on macOS-only tooling (hdiutil / pkgbuild / productbuild) and a
# .app bundle, so they are produced only when running on macOS. The GUI now
# lives in the windowed PyInstaller bundle dist/codewood-gui.app (which carries
# a proper Contents/Frameworks/Python and Info.plist), renamed to Code Wood.app,
# with the console `codewood` (TUI/serve CLI) shipped inside Resources so the
# .pkg can link /usr/local/bin/codewood to it.
build_macos_installers() {
  local app_name="Code Wood"
  # Stage the bundle in a temp dir so a stale, non-writable dist/Code Wood.app
  # (e.g. left root-owned by an earlier run) can't block assembly.
  local app_dir; app_dir="$(mktemp -d)/${app_name}.app"
  local gui_bundle="dist/codewood-gui.app"

  echo "Assembling ${app_dir} from ${gui_bundle}..."
  if [ ! -d "$gui_bundle" ]; then
    echo "WARNING: ${gui_bundle} not found; run the PyInstaller build first." >&2
    return
  fi
  cp -R "$gui_bundle" "$app_dir"

  # PyInstaller's BUNDLE writes CFBundleIdentifier "codewood-gui"; the Dock
  # merges the running app with the pinned icon keyed on this bundle id, so
  # overwrite it with the product identity and display name.
  cat > "$app_dir/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>${app_name}</string>
  <key>CFBundleDisplayName</key><string>${app_name}</string>
  <key>CFBundleIdentifier</key><string>us.zoom.codewood</string>
  <key>CFBundleVersion</key><string>${APP_VERSION}</string>
  <key>CFBundleShortVersionString</key><string>${APP_VERSION}</string>
  <key>CFBundleExecutable</key><string>codewood-gui</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleIconFile</key><string>app_icon.icns</string>
  <key>NSHighResolutionCapable</key><true/>
</dict></plist>
PLIST

  # Ship the console `codewood` (TUI/serve CLI) inside the bundle's Resources so
  # the .pkg can link /usr/local/bin/codewood to it, and so `codewood app` works
  # from the bundle path.
  if [ -d "dist/codewood" ]; then
    cp -R "dist/codewood" "$app_dir/Contents/Resources/codewood"
  fi

  # .dmg (drag-to-Applications of the GUI app). hdiutil is part of macOS.
  if command -v hdiutil >/dev/null 2>&1; then
    local dmg="dist/CodeWood-${APP_VERSION}-${PLATFORM_TAG}.dmg"
    rm -f "$dmg"
    local stage; stage="$(mktemp -d)"
    cp -R "$app_dir" "$stage/"
    ln -s /Applications "$stage/Applications"
    echo "Creating .dmg \"$dmg\"..."
    if hdiutil create -volname "$app_name" -srcfolder "$stage" -ov -format UDZO "$dmg" >/dev/null; then
      INSTALLER_NOTES+=("  $(basename "$dmg")  - macOS drag-to-install (GUI app)")
    else
      echo "WARNING: hdiutil failed; skipping .dmg." >&2
    fi
    rm -rf "$stage"
  else
    echo "WARNING: hdiutil not found; skipping .dmg." >&2
  fi

  # .pkg (guided installer): installs the GUI app to /Applications and links the
  # `codewood` TUI CLI to /usr/local/bin.
  if command -v pkgbuild >/dev/null 2>&1; then
    local pkg="dist/CodeWood-${APP_VERSION}-${PLATFORM_TAG}.pkg"
    rm -f "$pkg"
    local pkgroot; pkgroot="$(mktemp -d)"
    mkdir -p "$pkgroot/Applications" "$pkgroot/usr/local/bin"
    cp -R "$app_dir" "$pkgroot/Applications/"
    ln -s "../../../Applications/${app_name}.app/Contents/Resources/codewood/codewood" \
      "$pkgroot/usr/local/bin/codewood"
    echo "Creating .pkg \"$pkg\"..."
    if pkgbuild --root "$pkgroot" --identifier "us.zoom.codewood" \
        --version "$APP_VERSION" --install-location "/" "$pkg" >/dev/null; then
      INSTALLER_NOTES+=("  $(basename "$pkg")  - macOS installer (GUI app + codewood CLI)")
    else
      echo "WARNING: pkgbuild failed; skipping .pkg." >&2
    fi
    rm -rf "$pkgroot"
  else
    echo "WARNING: pkgbuild not found; skipping .pkg." >&2
  fi

  # Best-effort convenience copy of the assembled .app into dist/. Skips quietly
  # if dist/Code Wood.app is not removable/writable (e.g. root-owned leftover) —
  # the installable artifacts above are built from the staged copy regardless.
  rm -rf "dist/${app_name}.app" 2>/dev/null || true
  cp -R "$app_dir" "dist/${app_name}.app" 2>/dev/null || true
}

# ---------------------------------------------------------------------------
# Linux native installers (AppImage + .deb)
# ---------------------------------------------------------------------------
# Render an SVG to a 256x256 PNG using whichever rasterizer is available.
# Returns 0 and writes "$2" on success; non-zero (no output) on failure.
render_svg_to_png() {
  local svg="$1" png="$2"
  [ -f "$svg" ] || return 1
  if command -v rsvg-convert >/dev/null 2>&1; then
    rsvg-convert -w 256 -h 256 "$svg" -o "$png" >/dev/null 2>&1 && [ -s "$png" ] && return 0
  fi
  if command -v inkscape >/dev/null 2>&1; then
    inkscape "$svg" --export-type=png -w 256 -h 256 -o "$png" >/dev/null 2>&1 && [ -s "$png" ] && return 0
  fi
  if command -v magick >/dev/null 2>&1; then
    magick -background none -density 384 "$svg" -resize 256x256 "$png" >/dev/null 2>&1 && [ -s "$png" ] && return 0
  fi
  if command -v convert >/dev/null 2>&1; then
    convert -background none -density 384 "$svg" -resize 256x256 "$png" >/dev/null 2>&1 && [ -s "$png" ] && return 0
  fi
  # Python fallbacks (cairosvg, then svglib) if a CLI rasterizer is absent.
  local py="${VENV_PYTHON:-python3}"
  command -v "$py" >/dev/null 2>&1 || py="python3"
  "$py" - "$svg" "$png" >/dev/null 2>&1 <<'PYSVG' && [ -s "$png" ] && return 0
import sys
svg, png = sys.argv[1], sys.argv[2]
try:
    import cairosvg
    cairosvg.svg2png(url=svg, write_to=png, output_width=256, output_height=256)
    sys.exit(0)
except Exception:
    pass
try:
    from svglib.svglib import svg2rlg
    from reportlab.graphics import renderPM
    drawing = svg2rlg(svg)
    if drawing is None:
        sys.exit(1)
    sx, sy = 256.0 / drawing.width, 256.0 / drawing.height
    drawing.width, drawing.height = 256, 256
    drawing.scale(sx, sy)
    renderPM.drawToFile(drawing, png, fmt="PNG")
    sys.exit(0)
except Exception:
    sys.exit(1)
PYSVG
  return 1
}

build_linux_installers() {
  # Map uname -m to Debian arch naming for the .deb control file.
  local deb_arch
  case "$ARCH_TAG" in
    x86_64)  deb_arch="amd64" ;;
    aarch64) deb_arch="arm64" ;;
    armv7l)  deb_arch="armhf" ;;
    *)       deb_arch="$ARCH_TAG" ;;
  esac

  # A small launcher placed on PATH (/usr/bin/codewood) that forwards to the
  # installed one-dir bundle under /opt/codewood. Shared by AppImage and .deb.
  # AppDir layout used for both AppImage and .deb staging.
  local appdir="dist/CodeWood.AppDir"
  echo "Assembling ${appdir}..."
  rm -rf "$appdir"
  mkdir -p "$appdir/opt/codewood" "$appdir/usr/bin" "$appdir/usr/share/applications" "$appdir/usr/share/icons/hicolor/256x256/apps"
  cp -R "dist/codewood/." "$appdir/opt/codewood/"

  # PATH launcher -> terminal UI by default ("codewood app" opens the GUI).
  cat > "$appdir/usr/bin/codewood" <<'LAUNCH'
#!/bin/sh
exec /opt/codewood/codewood "$@"
LAUNCH
  chmod +x "$appdir/usr/bin/codewood"

  # .desktop entry (used by both AppImage and the .deb menu integration).
  cat > "$appdir/usr/share/applications/codewood.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Code Wood
Comment=Code Wood AI Agent
Exec=codewood app
Icon=codewood
Terminal=false
Categories=Development;
DESKTOP

  # The icon is mandatory for AppImage: appimagetool aborts when the icon
  # named by the .desktop "Icon=" key is missing from the AppDir. Prefer a
  # shipped 256x256 PNG; otherwise render build/app_icon.svg with whatever
  # SVG rasterizer is available; only as a last resort fall back to a minimal
  # placeholder PNG so packaging never fails on a missing asset.
  local icon_dest="$appdir/usr/share/icons/hicolor/256x256/apps/codewood.png"
  if [ -f "build/app_icon.png" ]; then
    cp "build/app_icon.png" "$icon_dest"
  elif [ -f "build/app_icon.svg" ] && render_svg_to_png "build/app_icon.svg" "$icon_dest"; then
    echo "Rendered build/app_icon.svg -> $(basename "$icon_dest")"
  else
    echo "WARNING: no usable app icon; generating a placeholder icon." >&2
    # 1x1 transparent PNG (valid, minimal). Enough to satisfy appimagetool.
    printf '%s' \
'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M8AAAMBAQDJ/pLvAAAAAElFTkSuQmCC' \
      | base64 -d > "$icon_dest" 2>/dev/null || \
    printf '%s' \
'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M8AAAMBAQDJ/pLvAAAAAElFTkSuQmCC' \
      | base64 --decode > "$icon_dest"
  fi

  # ---- AppImage (universal, no root). Requires appimagetool + FUSE.
  if command -v appimagetool >/dev/null 2>&1; then
    # AppImage expects an AppRun entrypoint + top-level .desktop/.icon.
    cat > "$appdir/AppRun" <<'APPRUN'
#!/bin/sh
HERE="$(dirname "$(readlink -f "$0")")"
exec "$HERE/opt/codewood/codewood" "$@"
APPRUN
    chmod +x "$appdir/AppRun"
    cp "$appdir/usr/share/applications/codewood.desktop" "$appdir/codewood.desktop"
    [ -f "$appdir/usr/share/icons/hicolor/256x256/apps/codewood.png" ] && \
      cp "$appdir/usr/share/icons/hicolor/256x256/apps/codewood.png" "$appdir/codewood.png"
    local appimage="dist/CodeWood-${APP_VERSION}-${PLATFORM_TAG}.AppImage"
    rm -f "$appimage"
    echo "Creating AppImage \"$appimage\"..."

    # Build on the native Linux filesystem and without FUSE. Under WSL the
    # project often lives on a Windows mount (DrvFs) that ignores chmod/owner
    # bits, and FUSE is frequently unavailable — both make appimagetool fail.
    # Staging in $TMPDIR/tmp restores POSIX permissions, and
    # APPIMAGE_EXTRACT_AND_RUN=1 lets the (AppImage-packaged) appimagetool run
    # without mounting itself via FUSE. The finished image is copied to dist/.
    local aistage aiappdir aiout ai_staged
    aistage="$(mktemp -d "${TMPDIR:-/tmp}/codewood-appimage.XXXXXX")"
    aiappdir="$aistage/CodeWood.AppDir"
    cp -R "$appdir" "$aiappdir"
    find "$aiappdir" -type d -exec chmod 0755 {} +
    find "$aiappdir" -type f -exec chmod 0644 {} +
    chmod 0755 "$aiappdir/AppRun" "$aiappdir/usr/bin/codewood"
    chmod 0755 "$aiappdir/opt/codewood/codewood" 2>/dev/null || true
    chmod 0755 "$aiappdir/opt/codewood/codewood-gui" 2>/dev/null || true
    ai_staged="$aistage/$(basename "$appimage")"
    aiout="$aistage/appimagetool.log"
    if ARCH="$ARCH_TAG" APPIMAGE_EXTRACT_AND_RUN=1 appimagetool "$aiappdir" "$ai_staged" >"$aiout" 2>&1; then
      cp -f "$ai_staged" "$appimage"
      chmod +x "$appimage"
      INSTALLER_NOTES+=("  $(basename "$appimage")  - Linux universal, no-install (chmod +x then run)")
    else
      echo "WARNING: appimagetool failed; skipping AppImage. Details:" >&2
      sed 's/^/  /' "$aiout" >&2 || true
    fi
    rm -rf "$aistage"
  else
    echo "WARNING: appimagetool not found; skipping AppImage." >&2
  fi

  # ---- .deb (Debian/Ubuntu). Requires dpkg-deb.
  if command -v dpkg-deb >/dev/null 2>&1; then
    # Stage on the native Linux filesystem rather than under dist/. When the
    # project lives on a Windows-mounted path in WSL (DrvFs, e.g. /mnt/d/...),
    # the mount ignores chmod and forces 0777 on every file, which dpkg-deb
    # rejects ("control directory has bad permissions 777"). A directory under
    # $TMPDIR/tmp honors POSIX permissions, so we build the package there and
    # copy the finished .deb back into dist/.
    local debstage
    debstage="$(mktemp -d "${TMPDIR:-/tmp}/codewood-deb.XXXXXX")"
    local debroot="$debstage/codewood-deb"
    rm -rf "$debroot"
    mkdir -p "$debroot/opt/codewood" "$debroot/usr/bin" "$debroot/usr/share/applications" "$debroot/DEBIAN"
    cp -R "dist/codewood/." "$debroot/opt/codewood/"
    cp "$appdir/usr/bin/codewood" "$debroot/usr/bin/codewood"
    chmod +x "$debroot/usr/bin/codewood"
    cp "$appdir/usr/share/applications/codewood.desktop" "$debroot/usr/share/applications/codewood.desktop"
    if [ -f "$appdir/usr/share/icons/hicolor/256x256/apps/codewood.png" ]; then
      mkdir -p "$debroot/usr/share/icons/hicolor/256x256/apps"
      cp "$appdir/usr/share/icons/hicolor/256x256/apps/codewood.png" "$debroot/usr/share/icons/hicolor/256x256/apps/codewood.png"
    fi
    cat > "$debroot/DEBIAN/control" <<CONTROL
Package: codewood
Version: ${APP_VERSION}
Section: devel
Priority: optional
Architecture: ${deb_arch}
Maintainer: Reed Yang
Description: Code Wood AI Agent
 Code Wood terminal UI and desktop GUI.
CONTROL
    # Normalize permissions. On WSL/Windows-mounted filesystems staged files
    # default to 0777, which dpkg-deb rejects ("control directory has bad
    # permissions 777"). Force standard dirs=0755, files=0644, then restore
    # the executable bit on the launcher binary.
    find "$debroot" -type d -exec chmod 0755 {} +
    find "$debroot" -type f -exec chmod 0644 {} +
    chmod 0755 "$debroot/usr/bin/codewood"
    chmod 0755 "$debroot/opt/codewood/codewood" 2>/dev/null || true
    chmod 0755 "$debroot/opt/codewood/codewood-gui" 2>/dev/null || true

    local deb="dist/CodeWood-${APP_VERSION}-${PLATFORM_TAG}.deb"
    local deb_staged="$debstage/$(basename "$deb")"
    rm -f "$deb"
    echo "Creating .deb \"$deb\"..."
    if dpkg-deb --build --root-owner-group "$debroot" "$deb_staged" >/dev/null; then
      cp -f "$deb_staged" "$deb"
      INSTALLER_NOTES+=("  $(basename "$deb")  - Debian/Ubuntu package (installs to /opt/codewood, links /usr/bin/codewood)")
    else
      echo "WARNING: dpkg-deb failed; skipping .deb." >&2
    fi
    rm -rf "$debstage"
  else
    echo "WARNING: dpkg-deb not found; skipping .deb." >&2
  fi
}

case "$(uname -s)" in
  Darwin) build_macos_installers ;;
  Linux)  build_linux_installers ;;
esac

echo ""
echo "Packaging completed. Artifacts in \"dist/\":"
echo "  CodeWood-${APP_VERSION}-${PLATFORM_TAG}-portable.tar.gz  - portable, no-install bundle"
for note in "${INSTALLER_NOTES[@]}"; do
  echo "$note"
done
echo "Note: the Windows .exe installer is produced by build/pack.bat on Windows."
