import { useCallback, useEffect, useRef, useState } from "react";
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

/** A single xterm.js instance wired to a backend console session over a
 *  WebSocket. Output frames are binary (raw PTY bytes); input/resize are sent
 *  as small JSON text frames. The terminal mounts once per session id. */
function ConsoleTerminal({
  sessionId,
  active,
  fontFamily,
  bufferLines,
}: {
  sessionId: string;
  active: boolean;
  fontFamily: string;
  bufferLines: number;
}) {
  const { subscribeConsoleOutput, attachConsole, consoleInput, consoleResize } =
    useApp();
  const hostRef = useRef<HTMLDivElement | null>(null);
  const termRef = useRef<Terminal | null>(null);
  const fitRef = useRef<FitAddon | null>(null);
  const decoderRef = useRef<TextDecoder>(new TextDecoder());

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
      theme: { background: "#1e1e1e", foreground: "#d4d4d4" },
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(host);
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

    return () => {
      disposed = true;
      unsubscribe();
      dataDisp.dispose();
      resizeDisp.dispose();
      ro.disconnect();
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

  // Refit + focus when this tab becomes active (it was display:none before).
  useEffect(() => {
    if (!active) {
      return;
    }
    const id = window.setTimeout(() => {
      try {
        fitRef.current?.fit();
      } catch {
        // ignore
      }
      termRef.current?.focus();
    }, 0);
    return () => window.clearTimeout(id);
  }, [active]);

  return (
    <div
      className="console-term"
      ref={hostRef}
      style={{ display: active ? "block" : "none" }}
    />
  );
}

export function ConsolePanel() {
  const {
    t,
    consoleOptions,
    openConsole,
    closeConsole,
    activateConsole,
    hideConsole,
    openSettings,
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
    if (bootstrappedRef.current || isWindows === null) {
      return;
    }
    bootstrappedRef.current = true;
    void addTab(defaultKind);
  }, [addTab, isWindows, defaultKind]);

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
