# Desktop GUI

**English** | [简体中文](../zh-CN/gui.md)

Code Wood ships a desktop GUI under `desktop/`. The terminal UI and the
GUI share the **same entry point and the same executable**: run Code Wood
normally for the terminal UI, or pass the `app` command to open the GUI.
The GUI renders a TypeScript (React) UI inside the platform's default web
engine (WebView2 on Windows) via
[pywebview](https://pywebview.flowspace.dev/), and drives the existing
agent through a headless backend "serve" mode over a localhost HTTP +
Server-Sent-Events API.

Features: AI chat with streaming output, all `/workspace` and `/chat`
commands, light/dark themes, English / Simplified Chinese localization,
and a settings screen (theme, language, model, execution policy).

## Launch the GUI

```bash
# Development
python cli/main.py app

# Packaged build
codewood app
```

Launching `app` opens the desktop window without a console window. In a
packaged build, `codewood app` starts the GUI in a detached process and
returns control to the command prompt immediately. The GUI process spawns
the backend by re-launching the same executable in `serve` mode
(development: `python cli/main.py serve`).

The packaged build is a single **one-dir folder** (`dist/codewood/`) holding
**two executables** plus a shared `_internal/` runtime:

- **`codewood.exe`** — the full console build. It contains *all* terminal-UI
  and GUI functionality.
  - No arguments in a terminal → terminal UI.
  - `codewood.exe app` → desktop GUI from anywhere.
  - Double-click → desktop GUI (a console may briefly flash because this is a
    console-subsystem binary).
- **`codewood-gui.exe`** — a tiny windowed launcher next to `codewood.exe`.
  Double-clicking it opens the GUI **with no console window at all**; it simply
  runs `codewood app` (the sibling `codewood.exe`) in a window-free process. It
  bundles only the Python standard library, so it stays small. For the cleanest
  GUI launch, use this executable (or a shortcut to it).

On non-Windows platforms the same folder with two binaries is produced
(`codewood` and `codewood-gui`).

The build uses PyInstaller **one-dir** mode rather than one-file: a one-file
binary self-extracts through a bootloader process, so every launch costs an
extra resident process. With one-dir each executable runs as a single process,
so the desktop GUI uses **two** processes (the GUI host + the `serve` backend)
and the terminal UI uses **one**.

## Architecture

- The GUI process launches a backend process and talks to it over
  `127.0.0.1` using a per-launch bearer token.
- The backend `serve` mode reuses the normal agent loop, so every GUI
  action maps to the same logic the terminal uses.

## Automatic updates

The GUI checks
[github.com/reedyang/codewood/releases](https://github.com/reedyang/codewood/releases)
at launch and then once an hour. When a newer release exists, the matching
installer is downloaded **silently** in the background into
`<config dir>/cache/`:

| Platform | Package |
| --- | --- |
| Windows | `…-windows-x64-setup.exe` |
| macOS | `…-macos-<arch>.pkg` |
| Linux | `…-linux-<arch>.AppImage` (the distro-agnostic, no-root format; a `.deb` is used as a fallback) |

Downloads are **resumable**: the payload is written to `<asset>.part` and an
HTTP `Range` request continues from the current offset, so quitting Code Wood
mid-download and starting it again later resumes instead of restarting. Only
the newest release's package is kept — a stale package (and its partial file)
is deleted before a new download begins.

The download runs silently — **no UI appears until it completes**. If the
origin download fails (GitHub's asset CDN is unreachable from some networks),
the file is retried through `https://gh-proxy.com/<url>`; set
`CODEWOOD_UPDATE_PROXY=0` to disable that fallback.

Once the package is complete an **Update** button appears at the right end of
the title bar (left of the minimize button on Windows/Linux) showing the
release version. Clicking it launches the installer and quits Code Wood so the
files are not locked while they are replaced. Set `CODEWOOD_UPDATE=0` to
disable the feature entirely.

## Backend serve mode

```bash
# Headless server for the GUI (ephemeral port by default).
python cli/main.py serve
python cli/main.py serve --host 127.0.0.1 --port 8765
```

On startup it prints a single JSON handshake line
(`{"port": ..., "token": ...}`) on stdout that the GUI reads to connect.

## Develop the GUI

```bash
# 1. Install dependencies (includes pywebview for the GUI)
pip install -r requirements.txt

# 2. Install and build the frontend (or run the Vite dev server)
cd desktop/frontend
npm install
npm run build           # produces desktop/frontend/dist used by the host

# 3. Launch the GUI
cd ../..
python cli/main.py app
```

For live frontend development, run `npm run dev` in `desktop/frontend`
and point the host at it with the `CODEWOOD_GUI_URL` environment
variable (for example `http://localhost:5173`) before launching
`python cli/main.py app`.

## Package

The GUI is bundled by the standard build scripts (the frontend is built
automatically). A single run produces the `dist/codewood/` folder containing
both `codewood` and `codewood-gui`:

```bash
# Windows
build\pack.bat

# macOS / Linux (auto-detect)
bash build/pack.sh

```

`codewood` runs the terminal UI by default and the desktop GUI when invoked
as `codewood app`. `codewood-gui` is a thin windowed launcher that opens the
GUI without a console window by delegating to `codewood app`. Ship the whole
`dist/codewood/` folder (the executables need the sibling `_internal/`).
