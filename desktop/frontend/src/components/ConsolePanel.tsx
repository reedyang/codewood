import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type MouseEvent,
} from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import "@xterm/xterm/css/xterm.css";
import { useApp } from "../state/AppContext";
import { Icon } from "./Icon";
import { ContextMenu, type MenuItem } from "./ContextMenu";
import { hostApi } from "../utils/hostApi";
import {
  addTab as addTabState,
  removeTab as removeTabState,
  selectTab as selectTabState,
  type ConsoleTab,
  type ConsoleTabState,
} from "../state/consoleTabs";

// Windows offers three concrete shells; Linux/macOS offer a single generic
// terminal (the user's $SHELL). The platform is resolved once at runtime.
const WINDOWS_SHELL_KINDS: { kind: string; labelKey: string }[] = [
  { kind: "powershell", labelKey: "console.new.powershell" },
  { kind: "cmd", labelKey: "console.new.cmd" },
  { kind: "gitbash", labelKey: "console.new.gitbash" },
];
const POSIX_SHELL_KINDS: { kind: string; labelKey: string }[] = [
  { kind: "shell", labelKey: "console.new.terminal" },
];

/** Standard ANSI palette tuned for light terminal backgrounds so that
 *  PowerShell / PSReadLine syntax highlighting stays readable. */
const ANSI_LIGHT = {
  black: "#1f2329",
  red: "#c41a16",
  green: "#007400",
  yellow: "#9c6500",
  blue: "#004ec2",
  magenta: "#a824a8",
  cyan: "#00727c",
  white: "#6a737d",
  brightBlack: "#959da5",
  brightRed: "#d1242f",
  brightGreen: "#128a1e",
  brightYellow: "#b87600",
  brightBlue: "#0366d6",
  brightMagenta: "#c72ec7",
  brightCyan: "#0096a8",
  brightWhite: "#24292e",
};

/** Standard ANSI palette tuned for dark terminal backgrounds. */
const ANSI_DARK = {
  black: "#2e3436",
  red: "#cc0000",
  green: "#4e9a06",
  yellow: "#c4a000",
  blue: "#3465a4",
  magenta: "#75507b",
  cyan: "#06989a",
  white: "#d3d7cf",
  brightBlack: "#555753",
  brightRed: "#ef2929",
  brightGreen: "#8ae234",
  brightYellow: "#fce94f",
  brightBlue: "#729fcf",
  brightMagenta: "#ad7fa8",
  brightCyan: "#34e2e2",
  brightWhite: "#eeeeec",
};

/** Copy text to the clipboard via a hidden textarea + execCommand. Used as a
 *  fallback when the async Clipboard API is unavailable or denied. */
function legacyCopy(text: string) {
  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    document.execCommand("copy");
    document.body.removeChild(ta);
  } catch {
    // ignore: nothing else we can do without a clipboard API
  }
}

/** Read the terminal colour scheme from the active theme. The xterm internal
 *  background is overridden by CSS (`.xterm-viewport` uses `var(--console-bg)`)
 *  so the JS theme handles the foreground, cursor, selection, and ANSI palette. */
function readConsoleTheme() {
  const isDark = document.documentElement.dataset.theme === "dark";
  return isDark
    ? {
        ...ANSI_DARK,
        background: "#1e1e1e",
        foreground: "#d4d4d4",
        cursor: "#ffffff",
        cursorAccent: "#1e1e1e",
        selectionBackground: "#264f78",
        selectionInactiveBackground: "#3a3a3a",
      }
    : {
        ...ANSI_LIGHT,
        background: "#ffffff",
        foreground: "#1f2329",
        cursor: "#1f2329",
        cursorAccent: "#ffffff",
        selectionBackground: "#c4d9f1",
        selectionInactiveBackground: "#e8e8e8",
      };
}

/** A single xterm.js instance wired to a backend console session over a
 *  WebSocket. Output frames are binary (raw PTY bytes); input/resize are sent
 *  as small JSON text frames. The terminal mounts once per session id. */
function ConsoleTerminal({
  sessionId,
  active,
  visible,
  fontFamily,
  bufferLines,
}: {
  sessionId: string;
  active: boolean;
  visible: boolean;
  fontFamily: string;
  bufferLines: number;
}) {
  const { t, subscribeConsoleOutput, attachConsole, consoleInput, consoleResize } =
    useApp();
  const hostRef = useRef<HTMLDivElement | null>(null);
  const termRef = useRef<Terminal | null>(null);
  const fitRef = useRef<FitAddon | null>(null);
  const decoderRef = useRef<TextDecoder>(new TextDecoder());
  const [ctxMenu, setCtxMenu] = useState<{ x: number; y: number } | null>(null);

  // Copy the current xterm selection to the system clipboard. Falls back to the
  // legacy execCommand path because pywebview's WebView2 may deny the async
  // Clipboard API without focused-document permission.
  const copySelection = useCallback(() => {
    const term = termRef.current;
    if (!term) {
      return;
    }
    const text = term.getSelection();
    if (!text) {
      return;
    }
    if (navigator.clipboard?.writeText) {
      void navigator.clipboard.writeText(text).catch(() => {
        legacyCopy(text);
      });
    } else {
      legacyCopy(text);
    }
  }, []);

  const clearTerminal = useCallback(() => {
    termRef.current?.clear();
  }, []);

  // Create the terminal and wire it to the backend session. Console I/O rides
  // the SSE event stream (output) and plain HTTP POSTs (input/resize) because
  // WebView2 loads the GUI from a file:// origin, where Chromium forbids ws://.
  useEffect(() => {
    const host = hostRef.current;
    if (!host) {
      return;
    }
    const term = new Terminal({
      cursorBlink: true,
      scrollback: bufferLines,
      fontFamily:
        fontFamily ||
        'Consolas, "Cascadia Mono", "DejaVu Sans Mono", monospace',
      fontSize: 13,
      theme: readConsoleTheme(),
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(host);
    // Don't let a right-click clobber an existing selection; the custom
    // context menu handles selection-based actions instead.
    term.options.rightClickSelectsWord = false;
    termRef.current = term;
    fitRef.current = fit;
    try {
      fit.fit();
    } catch {
      // host may be 0-sized until laid out; the ResizeObserver retries.
    }

    let disposed = false;
    const decoder = decoderRef.current;
    const writeB64 = (b64: string) => {
      if (!b64) {
        return;
      }
      try {
        const bin = atob(b64);
        const bytes = new Uint8Array(bin.length);
        for (let i = 0; i < bin.length; i += 1) {
          bytes[i] = bin.charCodeAt(i);
        }
        term.write(decoder.decode(bytes, { stream: true }));
      } catch {
        // ignore an undecodable chunk
      }
    };

    // Subscribe to live output BEFORE fetching the retained snapshot so no
    // bytes are lost in between. Live chunks are queued until the snapshot has
    // been painted, so scrollback and live output stay in order.
    let snapshotDone = false;
    const pending: string[] = [];
    const unsubscribe = subscribeConsoleOutput(sessionId, (b64, end) => {
      if (disposed) {
        return;
      }
      if (end) {
        term.write("\r\n\x1b[90m[process exited]\x1b[0m\r\n");
        return;
      }
      if (!snapshotDone) {
        pending.push(b64);
        return;
      }
      writeB64(b64);
    });

    // Repaint existing scrollback (shell banner/prompt printed before mount).
    void attachConsole(sessionId).then((b64) => {
      if (disposed) {
        return;
      }
      writeB64(b64);
      snapshotDone = true;
      for (const chunk of pending.splice(0)) {
        writeB64(chunk);
      }
    });

    const sendResize = () => {
      void consoleResize(sessionId, term.cols, term.rows);
    };
    sendResize();

    const dataDisp = term.onData((data) => {
      void consoleInput(sessionId, data);
    });
    const resizeDisp = term.onResize(() => sendResize());

    const ro = new ResizeObserver(() => {
      try {
        fit.fit();
      } catch {
        // ignore transient layout errors
      }
    });
    ro.observe(host);

    // Intercept copy in the capture phase, BEFORE xterm processes the key.
    // xterm clears the selection as soon as a key is typed, so by the time
    // onData fires the selection is already gone; checking there would always
    // miss. With a capture listener on the host we can read the live selection
    // and stop the event from reaching xterm, so Ctrl/Cmd+C copies instead of
    // being forwarded to the shell as SIGINT.
    const onCopyKey = (e: KeyboardEvent) => {
      if (disposed) return;
      const isCopy =
        (e.ctrlKey || e.metaKey) &&
        !e.altKey &&
        (e.key === "c" || e.key === "C");
      if (isCopy && term.hasSelection()) {
        e.preventDefault();
        e.stopPropagation();
        copySelection();
      }
    };
    host.addEventListener("keydown", onCopyKey, true);

    // Keep terminal colours in sync with CSS variables (theme + background
    // image transparency). The MutationObserver catches attribute/style
    // changes on <html> that affect the computed --console-bg / --console-fg.
    const observer = new MutationObserver((mutations) => {
      if (disposed) return;
      for (const m of mutations) {
        if (m.type === "attributes") {
          term.options.theme = readConsoleTheme();
          break;
        }
      }
    });
    observer.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["data-theme", "data-has-bg", "style"],
    });

    return () => {
      disposed = true;
      observer.disconnect();
      unsubscribe();
      dataDisp.dispose();
      resizeDisp.dispose();
      ro.disconnect();
      host.removeEventListener("keydown", onCopyKey, true);
      term.dispose();
      termRef.current = null;
      fitRef.current = null;
    };
    // Intentionally bind once per session; live option changes are applied in
    // the effect below.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId, subscribeConsoleOutput, attachConsole, consoleInput, consoleResize]);

  // Apply live option changes without recreating the socket.
  useEffect(() => {
    const term = termRef.current;
    if (!term) {
      return;
    }
    term.options.fontFamily =
      fontFamily || 'Consolas, "Cascadia Mono", "DejaVu Sans Mono", monospace';
    term.options.scrollback = bufferLines;
    try {
      fitRef.current?.fit();
    } catch {
      // ignore
    }
  }, [fontFamily, bufferLines]);

  // Refit + refresh + focus whenever the active terminal becomes visible. This
  // covers both tab switches and the first time the dock is expanded: xterm
  // can mis-measure rows if it mounts while the dock is still height:0.
  useEffect(() => {
    if (!active || !visible) {
      return;
    }
    const run = () => {
      try {
        fitRef.current?.fit();
      } catch {
        // ignore
      }
      const term = termRef.current;
      if (term) {
        term.refresh(0, Math.max(0, term.rows - 1));
        term.focus();
      }
    };
    const raf1 = window.requestAnimationFrame(() => {
      run();
      window.requestAnimationFrame(run);
    });
    const id = window.setTimeout(() => {
      // The dock animates its height when opening; refit again after the
      // transition settles so the first prompt line lands on the correct row.
      run();
    }, 220);
    return () => {
      window.cancelAnimationFrame(raf1);
      window.clearTimeout(id);
    };
  }, [active, visible]);

  const openCtxMenu = useCallback(
    (e: MouseEvent) => {
      e.preventDefault();
      setCtxMenu({ x: e.clientX, y: e.clientY });
    },
    [],
  );

  const ctxItems: MenuItem[] = (() => {
    const hasSel = !!termRef.current?.hasSelection();
    const items: MenuItem[] = [];
    if (hasSel) {
      items.push({
        id: "copy",
        label: t("console.copy"),
        onSelect: copySelection,
      });
    }
    items.push({
      id: "clear",
      label: t("console.clear"),
      onSelect: clearTerminal,
    });
    return items;
  })();

  return (
    <div
      className="console-term"
      ref={hostRef}
      onContextMenu={openCtxMenu}
      style={{ display: active && visible ? "block" : "none" }}
    >
      {ctxMenu && (
        <ContextMenu
          x={ctxMenu.x}
          y={ctxMenu.y}
          items={ctxItems}
          onClose={() => setCtxMenu(null)}
        />
      )}
    </div>
  );
}

export function ConsolePanel({ dockOpen }: { dockOpen: boolean }) {
  const {
    t,
    consoleOptions,
    openConsole,
    closeConsole,
    activateConsole,
    hideConsole,
    openSettings,
    consumePendingAutoConsole,
  } = useApp();
  const [tabState, setTabState] = useState<ConsoleTabState>({
    tabs: [],
    activeId: "",
  });
  const { tabs, activeId } = tabState;
  const [menu, setMenu] = useState<{ x: number; y: number } | null>(null);
  const addBtnRef = useRef<HTMLButtonElement | null>(null);
  // Open one console automatically the first time the dock is shown.
  const bootstrappedRef = useRef(false);
  // Set to true when a backend auto-open (console_exec) already added a tab,
  // so the bootstrap effect doesn't create a second, duplicate terminal.
  const autoOpenedRef = useRef(false);
  // Windows exposes three shells; other platforms a single generic terminal.
  const [isWindows, setIsWindows] = useState<boolean | null>(null);

  useEffect(() => {
    let cancelled = false;
    const api = hostApi();
    if (!api?.host_platform) {
      // No host bridge (e.g. plain browser dev): assume Windows shells.
      setIsWindows(true);
      return;
    }
    void Promise.resolve(api.host_platform())
      .then((p) => {
        if (!cancelled) setIsWindows(String(p) === "win32");
      })
      .catch(() => {
        if (!cancelled) setIsWindows(true);
      });
    return () => {
      cancelled = true;
    };
  }, []);
  const shellKinds = isWindows ? WINDOWS_SHELL_KINDS : POSIX_SHELL_KINDS;
  const defaultKind = isWindows ? "powershell" : "shell";

  const addTab = useCallback(
    async (kind: string) => {
      const info = await openConsole(kind);
      if (!info) {
        return;
      }
      const tab: ConsoleTab = { id: info.id, title: info.title, kind: info.kind };
      setTabState((prev) => addTabState(prev, tab));
    },
    [openConsole],
  );

useEffect(() => {
    // Wait until the platform is known so the first auto-opened tab uses the
    // right shell kind (PowerShell on Windows, the default shell elsewhere).
    // Only bootstrap after the dock is actually visible; opening xterm inside
    // the collapsed height:0 dock can leave the first prompt mis-rendered.
    if (bootstrappedRef.current || isWindows === null || !dockOpen) {
      return;
    }
    bootstrappedRef.current = true;
    // If the backend auto-opened a console before the dock was open, consume
    // the pending info and add the tab without creating a new session.
    const pending = consumePendingAutoConsole();
    if (pending) {
      autoOpenedRef.current = true;
      const tab: ConsoleTab = { id: pending.id, title: pending.title, kind: pending.kind };
      setTabState((prev) => addTabState(prev, tab));
    } else if (!autoOpenedRef.current) {
      void addTab(defaultKind);
    }
}, [addTab, isWindows, defaultKind, dockOpen, consumePendingAutoConsole]);

  // Listen for codewood:console-open custom event (fired when the backend
  // auto-opens a console while the dock is already open). Consume the pending
  // info and add the tab without creating a duplicate session.
  useEffect(() => {
    const handler = () => {
      const pending = consumePendingAutoConsole();
      if (pending) {
        autoOpenedRef.current = true;
        const tab: ConsoleTab = { id: pending.id, title: pending.title, kind: pending.kind };
        setTabState((prev) => addTabState(prev, tab));
      }
    };
    window.addEventListener("codewood:console-open", handler);
    return () => window.removeEventListener("codewood:console-open", handler);
  }, [consumePendingAutoConsole]);

  const selectTab = useCallback(
    (id: string) => {
      setTabState((prev) => selectTabState(prev, id));
      void activateConsole(id);
    },
    [activateConsole],
  );

  const removeTab = useCallback(
    async (id: string) => {
      await closeConsole(id);
      setTabState((prev) => {
        const next = removeTabState(prev, id);
        if (next.activeId && next.activeId !== prev.activeId) {
          void activateConsole(next.activeId);
        }
        return next;
      });
    },
    [activateConsole, closeConsole],
  );

  const menuItems: MenuItem[] = [
    ...shellKinds.map((s) => ({
      id: s.kind,
      label: t(s.labelKey),
      onSelect: () => void addTab(s.kind),
    })),
    {
      id: "options",
      label: t("console.settings"),
      onSelect: () => openSettings("console"),
    },
  ];

  const openMenu = () => {
    const rect = addBtnRef.current?.getBoundingClientRect();
    if (rect) {
      setMenu({ x: rect.left, y: rect.bottom + 2 });
    }
  };

  return (
    <section className="console-panel" aria-label={t("menu.view.console")}>
      <div className="console-tabbar">
        <div className="console-tabs">
          {tabs.map((tabItem) => (
            <div
              key={tabItem.id}
              className={`console-tab ${tabItem.id === activeId ? "active" : ""}`}
            >
              <button
                className="console-tab-btn"
                onClick={() => selectTab(tabItem.id)}
                title={tabItem.title}
              >
                <Icon name="terminal" size={13} />
                <span className="console-tab-label">{tabItem.title}</span>
              </button>
              <button
                className="console-tab-close"
                aria-label={t("console.tab.close")}
                title={t("console.tab.close")}
                onClick={() => void removeTab(tabItem.id)}
              >
                <Icon name="win-close" size={11} />
              </button>
            </div>
          ))}
          <button
            ref={addBtnRef}
            className="console-tab-add"
            aria-label={t("console.new")}
            title={t("console.new")}
            onClick={openMenu}
          >
            <Icon name="plus" size={14} />
          </button>
        </div>
        <button
          className="console-close"
          aria-label={t("console.hide")}
          title={t("console.hide")}
          onClick={hideConsole}
        >
          <Icon name="win-close" size={11} />
        </button>
      </div>
      <div className="console-body">
        {tabs.length === 0 ? (
          <div className="console-empty">{t("console.empty")}</div>
        ) : (
          tabs.map((tabItem) => (
            <ConsoleTerminal
              key={tabItem.id}
              sessionId={tabItem.id}
              active={tabItem.id === activeId}
              visible={dockOpen}
              fontFamily={consoleOptions.fontFamily}
              bufferLines={consoleOptions.bufferLines}
            />
          ))
        )}
      </div>
      {menu && (
        <ContextMenu
          x={menu.x}
          y={menu.y}
          items={menuItems}
          onClose={() => setMenu(null)}
        />
      )}
    </section>
  );
}
