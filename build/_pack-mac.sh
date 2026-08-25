#!/usr/bin/env bash
# macOS packaging script. Prepares the Python virtual environment, runs the
# shared PyInstaller build, then produces .dmg and .pkg installers.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
PACK_PLATFORM="macos"
COMMON="$SCRIPT_DIR/_pack-common.sh"
if [ ! -f "$COMMON" ]; then
  echo "Shared packaging script not found: $COMMON" >&2
  exit 1
fi
# shellcheck source=_pack-common.sh
source "$COMMON"

# ---------------------------------------------------------------------------
# macOS native installers (.dmg + .pkg)
# ---------------------------------------------------------------------------
# These rely on macOS-only tooling (hdiutil / pkgbuild) and a .app bundle.
# The GUI lives in the windowed PyInstaller bundle dist/codewood-gui.app
# (Contents/Frameworks/Python and Info.plist), renamed to Code Wood.app.
# The .pkg exposes `codewood` on /usr/local/bin via a thin launcher that
# re-execs the same Contents/MacOS binary in console mode.
build_macos_installers() {
  local app_name="Code Wood"
  local app_stage="" app_dir="" gui_bundle="dist/codewood-gui.app"

  cleanup_macos_installers() {
    trap - RETURN
    [ -n "${app_stage:-}" ] && rm -rf "$app_stage"
  }
  trap cleanup_macos_installers RETURN

  # Stage the bundle in a temp dir so a stale, non-writable dist/Code Wood.app
  # (e.g. left root-owned by an earlier run) can't block assembly.
  app_stage="$(mktemp -d)"
  app_dir="${app_stage}/${app_name}.app"

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

  # The console `codewood` CLI is NOT duplicated inside the bundle. It is exposed
  # as a thin launcher on /usr/local/bin (see the .pkg block below) that reuses
  # this same Contents/MacOS binary, so the .dmg/.pkg stay a single ~640MB payload
  # instead of carrying a second (600MB+) copy of the console build.

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

  # .pkg (guided installer): installs the GUI app to /Applications and exposes the
  # `codewood` TUI/serve CLI on /usr/local/bin via a thin launcher that re-execs
  # the bundle's same Contents/MacOS binary in console mode (CODEWOOD_CONSOLE_LAUNCH
  # opts out of the auto-open-GUI default). This avoids shipping a duplicate copy of
  # the ~640MB console payload.
  #
  # The .app is shipped as a compressed archive and unpacked by a postinstall
  # script (the same copy the working .dmg drag performs). Driving the copy with a
  # postinstall avoids pkgbuild auto-detecting the .app bundle AND its embedded
  # Python.framework as relocatable/strict-identifier bundles (the Installer then
  # mishandles them and leaves the app out of /Applications). A plain tarball in
  # the payload triggers no bundle detection, so the app lands deterministically.
  if command -v pkgbuild >/dev/null 2>&1; then
    local pkg="dist/CodeWood-${APP_VERSION}-${PLATFORM_TAG}.pkg"
    rm -f "$pkg"

    # Ship the assembled app as a compressed archive under a non-bundle path.
    local app_parent; app_parent="$(dirname "$app_dir")"
    local staging; staging="$(mktemp -d)"
    local app_tar="$staging/CodeWood.app.tar.gz"
    ( cd "$app_parent" && /usr/bin/tar -czf "$app_tar" "Code Wood.app" )

    # Payload root (install-location "/"): the launcher + the app archive. The
    # archive is extracted to /Applications by the postinstall script below.
    local pkgroot; pkgroot="$(mktemp -d)"
    mkdir -p "$pkgroot/usr/local/bin" "$pkgroot/usr/local/share/codewood"
    cat > "$pkgroot/usr/local/bin/codewood" <<'LAUNCH'
#!/bin/sh
# Code Wood console CLI - thin launcher reusing the GUI bundle's binary, so the
# .pkg need not ship a second copy of the console payload. The bundle's
# Contents/MacOS executable contains the TUI, serve and GUI entry points.
CODEWOOD_CONSOLE_LAUNCH=1
export CODEWOOD_CONSOLE_LAUNCH
exec "/Applications/Code Wood.app/Contents/MacOS/codewood-gui" "$@"
LAUNCH
    chmod +x "$pkgroot/usr/local/bin/codewood"
    cp "$app_tar" "$pkgroot/usr/local/share/codewood/CodeWood.app.tar.gz"

    # postinstall: unpack the app into /Applications (exactly like dragging the
    # .dmg), then drop the archive copy. $2 = target volume mount point.
    local scripts; scripts="$(mktemp -d)"
    cat > "$scripts/postinstall" <<'POST'
#!/bin/sh
set -e
TARGET="${2:-/}"
APP_TAR="$TARGET/usr/local/share/codewood/CodeWood.app.tar.gz"
APP_DEST="$TARGET/Applications/Code Wood.app"
if [ -f "$APP_TAR" ]; then
  echo "Installing Code Wood.app to $TARGET/Applications..."
  rm -rf "$APP_DEST"
  mkdir -p "$TARGET/Applications"
  /usr/bin/tar -xzf "$APP_TAR" -C "$TARGET/Applications"
  rm -rf "$TARGET/usr/local/share/codewood"
fi
exit 0
POST
    chmod +x "$scripts/postinstall"

    echo "Creating .pkg \"$pkg\"..."
    if pkgbuild --root "$pkgroot" --scripts "$scripts" \
        --identifier "us.zoom.codewood" --version "$APP_VERSION" \
        --install-location "/" --ownership recommended "$pkg" >/dev/null; then
      INSTALLER_NOTES+=("  $(basename "$pkg")  - macOS installer (GUI app -> /Applications, codewood CLI -> /usr/local/bin)")
    else
      echo "WARNING: pkgbuild failed; skipping .pkg." >&2
    fi
    rm -rf "$staging" "$pkgroot" "$scripts"
  else
    echo "WARNING: pkgbuild not found; skipping .pkg." >&2
  fi

  # Best-effort convenience copy of the assembled .app into dist/. Skips quietly
  # if dist/Code Wood.app is not removable/writable (e.g. root-owned leftover) —
  # the installable artifacts above are built from the staged copy regardless.
  rm -rf "dist/${app_name}.app" 2>/dev/null || true
  cp -R "$app_dir" "dist/${app_name}.app" 2>/dev/null || true
}

build_macos_installers
pack_print_summary
