#!/usr/bin/env bash
# Linux packaging script. Prepares the Python virtual environment, runs the
# shared PyInstaller build, then produces AppImage and .deb installers.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
PACK_PLATFORM="linux"
COMMON="$SCRIPT_DIR/_pack-common.sh"
if [ ! -f "$COMMON" ]; then
  echo "Shared packaging script not found: $COMMON" >&2
  exit 1
fi
# shellcheck source=_pack-common.sh
source "$COMMON"

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

build_linux_installers
pack_print_summary
