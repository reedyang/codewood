import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { ApiClient } from "../api/client";
import type {
  AppState,
  ConfirmRequest,
  SegmentKind,
  ServerEvent,
  Turn,
  WorkspaceChatSummary,
} from "../api/types";
import { normalizeLang, translate, type Lang } from "../i18n";
import {
  loadUiPrefs,
  saveUiPrefs,
  toggleId,
  type UiPrefs,
} from "./uiPrefs";

export type Theme = "light" | "dark" | "system";

function quoteArg(value: string): string {
  return `"${value.replace(/"/g, "")}"`;
}

interface AppContextValue {
  state: AppState | null;
  turns: Turn[];
  busy: boolean;
  connected: boolean;
  now: number;
  confirmRequest: ConfirmRequest | null;
  theme: Theme;
  lang: Lang;
  uiPrefs: UiPrefs;
  workspaceChats: Record<string, WorkspaceChatSummary[]>;
  expandedWorkspaceIds: string[];
  settingsOpen: boolean;
  aboutOpen: boolean;
  t: (key: string) => string;
  setTheme: (theme: Theme) => void;
  sendInput: (text: string) => Promise<void>;
  runCommand: (command: string) => Promise<void>;
  interrupt: () => Promise<void>;
  answerConfirm: (answer: string) => Promise<void>;
  clearTurns: () => void;
  openWorkspaceInExplorer: (id: string) => Promise<boolean>;
  toggleWorkspacePin: (id: string) => void;
  toggleChatPin: (id: string) => void;
  toggleChatArchive: (id: string) => void;
  archiveChats: (ids: string[]) => void;
  setModel: (selector: string) => Promise<void>;
  setExecutionPolicy: (policy: string) => Promise<void>;
  toggleWorkspaceExpanded: (id: string) => void;
  refreshWorkspaceChats: (id: string) => Promise<void>;
  openSettings: () => void;
  closeSettings: () => void;
  openAbout: () => void;
  closeAbout: () => void;
  pickFolder: () => Promise<string>;
}

interface HostApiBridge {
  pick_folder?: () => string | Promise<string>;
}

const AppContext = createContext<AppContextValue | null>(null);

const THEME_STORAGE_KEY = "codewood.theme";

function loadInitialTheme(): Theme {
  const stored = window.localStorage.getItem(THEME_STORAGE_KEY);
  if (stored === "light" || stored === "dark" || stored === "system") {
    return stored;
  }
  return "system";
}

function systemPrefersDark(): boolean {
  return Boolean(window.matchMedia?.("(prefers-color-scheme: dark)").matches);
}

function resolveTheme(theme: Theme): "light" | "dark" {
  if (theme === "system") {
    return systemPrefersDark() ? "dark" : "light";
  }
  return theme;
}

export function AppProvider({ children }: { children: ReactNode }) {
  const clientRef = useRef<ApiClient | null>(null);
  if (clientRef.current === null) {
    clientRef.current = new ApiClient();
  }
  const client = clientRef.current;

  const [state, setState] = useState<AppState | null>(null);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [busy, setBusy] = useState(false);
  const [connected, setConnected] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  const [confirmRequest, setConfirmRequest] = useState<ConfirmRequest | null>(null);
  const [theme, setThemeState] = useState<Theme>(loadInitialTheme);
  const [uiPrefs, setUiPrefs] = useState<UiPrefs>(loadInitialUiPrefs);
  const [workspaceChats, setWorkspaceChats] = useState<
    Record<string, WorkspaceChatSummary[]>
  >({});
  const [expandedWorkspaceIds, setExpandedWorkspaceIds] = useState<string[]>([]);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [aboutOpen, setAboutOpen] = useState(false);
  const nextIdRef = useRef(1);

  const lang = useMemo(() => normalizeLang(state?.language), [state?.language]);
  const t = useCallback((key: string) => translate(lang, key), [lang]);

  // Apply the resolved theme, re-resolving when the OS preference changes
  // while in "system" mode.
  useEffect(() => {
    const apply = () =>
      document.documentElement.setAttribute("data-theme", resolveTheme(theme));
    apply();
    if (theme !== "system" || !window.matchMedia) {
      return;
    }
    const mql = window.matchMedia("(prefers-color-scheme: dark)");
    const onChange = () => apply();
    mql.addEventListener?.("change", onChange);
    return () => mql.removeEventListener?.("change", onChange);
  }, [theme]);

  const setTheme = useCallback((value: Theme) => {
    setThemeState(value);
    window.localStorage.setItem(THEME_STORAGE_KEY, value);
  }, []);

  const updatePrefs = useCallback((next: UiPrefs) => {
    setUiPrefs(next);
    saveUiPrefs(next);
  }, []);

  // Tick a 1s clock while busy so the active turn shows live elapsed time.
  useEffect(() => {
    if (!busy) {
      return;
    }
    setNow(Date.now());
    const handle = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(handle);
  }, [busy]);

  // Append text to the last segment of the active turn when it matches the
  // requested kind; otherwise open a new segment. This keeps streamed deltas
  // append-only (no full-block rewrites) and separates steps from the answer.
  const appendSegment = useCallback((kind: SegmentKind, text: string) => {
    if (!text) {
      return;
    }
    setTurns((prev) => {
      const next = prev.length > 0 ? [...prev] : [];
      let turn = next[next.length - 1];
      if (!turn) {
        turn = {
          id: nextIdRef.current++,
          userText: "",
          segments: [],
          startedAt: Date.now(),
          endedAt: null,
        };
        next.push(turn);
      }
      const segments = [...turn.segments];
      const last = segments[segments.length - 1];
      if (last && last.kind === kind) {
        segments[segments.length - 1] = { ...last, text: last.text + text };
      } else {
        segments.push({ id: nextIdRef.current++, kind, text });
      }
      next[next.length - 1] = { ...turn, segments };
      return next;
    });
  }, []);

  const startTurn = useCallback((userText: string) => {
    setTurns((prev) => [
      ...prev,
      {
        id: nextIdRef.current++,
        userText,
        segments: [],
        startedAt: Date.now(),
        endedAt: null,
      },
    ]);
  }, []);

  const endActiveTurn = useCallback(() => {
    setTurns((prev) => {
      if (prev.length === 0) {
        return prev;
      }
      const last = prev[prev.length - 1];
      if (last.endedAt !== null) {
        return prev;
      }
      const copy = [...prev];
      copy[copy.length - 1] = { ...last, endedAt: Date.now() };
      return copy;
    });
  }, []);

  useEffect(() => {
    let source: EventSource | null = null;

    const handleEvent = (event: ServerEvent) => {
      switch (event.event) {
        case "idle": {
          const next = (event.data as { state?: AppState }).state;
          if (next) {
            setState(next);
          }
          endActiveTurn();
          setBusy(false);
          break;
        }
        case "turn_start": {
          const text = String((event.data as { text?: string }).text ?? "");
          startTurn(text);
          setBusy(true);
          break;
        }
        case "output": {
          appendSegment("step", String((event.data as { text?: string }).text ?? ""));
          break;
        }
        case "assistant": {
          appendSegment("answer", String((event.data as { text?: string }).text ?? ""));
          break;
        }
        case "confirm": {
          setConfirmRequest(event.data as ConfirmRequest);
          break;
        }
        default:
          break;
      }
    };

    source = client.connectEvents(handleEvent);
    source.onopen = () => setConnected(true);
    source.onerror = () => setConnected(false);

    client
      .getState()
      .then((value) => {
        setState(value);
        setConnected(true);
      })
      .catch(() => setConnected(false));

    return () => {
      source?.close();
    };
  }, [client, appendSegment, startTurn, endActiveTurn]);

  const sendInput = useCallback(
    async (text: string) => {
      const trimmed = text.trim();
      if (!trimmed) {
        return;
      }
      await client.sendInput(trimmed);
    },
    [client],
  );

  const runCommand = useCallback(
    async (command: string) => {
      await client.sendInput(command);
    },
    [client],
  );

  const interrupt = useCallback(async () => {
    await client.interrupt();
  }, [client]);

  const answerConfirm = useCallback(
    async (answer: string) => {
      const current = confirmRequest;
      setConfirmRequest(null);
      if (current) {
        await client.confirm(current.id, answer);
      }
    },
    [client, confirmRequest],
  );

  const clearTurns = useCallback(() => setTurns([]), []);

  const openWorkspaceInExplorer = useCallback(
    (id: string) => client.openWorkspaceInExplorer(id),
    [client],
  );

  const toggleWorkspacePin = useCallback(
    (id: string) =>
      updatePrefs({
        ...uiPrefs,
        pinnedWorkspaceIds: toggleId(uiPrefs.pinnedWorkspaceIds, id),
      }),
    [uiPrefs, updatePrefs],
  );

  const toggleChatPin = useCallback(
    (id: string) =>
      updatePrefs({ ...uiPrefs, pinnedChatIds: toggleId(uiPrefs.pinnedChatIds, id) }),
    [uiPrefs, updatePrefs],
  );

  const toggleChatArchive = useCallback(
    (id: string) =>
      updatePrefs({
        ...uiPrefs,
        archivedChatIds: toggleId(uiPrefs.archivedChatIds, id),
      }),
    [uiPrefs, updatePrefs],
  );

  const archiveChats = useCallback(
    (ids: string[]) => {
      const merged = new Set(uiPrefs.archivedChatIds);
      for (const id of ids) {
        if (id) {
          merged.add(id);
        }
      }
      updatePrefs({ ...uiPrefs, archivedChatIds: Array.from(merged) });
    },
    [uiPrefs, updatePrefs],
  );

  const setModel = useCallback(
    async (selector: string) => {
      const value = selector.trim();
      if (value) {
        await client.sendInput(`/model ${value}`);
      }
    },
    [client],
  );

  const setExecutionPolicy = useCallback(
    async (policy: string) => {
      const value = policy.trim();
      if (value) {
        await client.sendInput(`/execution-policy ${value}`);
      }
    },
    [client],
  );

  const refreshWorkspaceChats = useCallback(
    async (id: string) => {
      const chats = await client.listWorkspaceChats(id);
      setWorkspaceChats((prev) => ({ ...prev, [id]: chats }));
    },
    [client],
  );

  const toggleWorkspaceExpanded = useCallback(
    (id: string) => {
      setExpandedWorkspaceIds((prev) => {
        if (prev.includes(id)) {
          return prev.filter((x) => x !== id);
        }
        void refreshWorkspaceChats(id);
        return [...prev, id];
      });
    },
    [refreshWorkspaceChats],
  );

  const openSettings = useCallback(() => setSettingsOpen(true), []);
  const closeSettings = useCallback(() => setSettingsOpen(false), []);
  const openAbout = useCallback(() => setAboutOpen(true), []);
  const closeAbout = useCallback(() => setAboutOpen(false), []);

  const pickFolder = useCallback(async (): Promise<string> => {
    const api = (window as unknown as { pywebview?: { api?: HostApiBridge } })
      .pywebview?.api;
    if (!api?.pick_folder) {
      return "";
    }
    try {
      return (await api.pick_folder()) || "";
    } catch {
      return "";
    }
  }, []);

  // Bridge native-menu actions (gui.py -> window.__codewoodMenu) to app state.
  useEffect(() => {
    const handler = (action: string, payload?: string) => {
      switch (action) {
        case "new-chat":
          clearTurns();
          void runCommand("/chat new");
          break;
        case "open-folder": {
          const path = String(payload ?? "").trim();
          if (path) {
            clearTurns();
            void runCommand(`/workspace create ${quoteArg(path)}`);
          }
          break;
        }
        case "settings":
          setSettingsOpen(true);
          break;
        case "about":
          setAboutOpen(true);
          break;
        default:
          break;
      }
    };
    (window as unknown as { __codewoodMenu?: typeof handler }).__codewoodMenu =
      handler;
    return () => {
      delete (window as unknown as { __codewoodMenu?: typeof handler })
        .__codewoodMenu;
    };
  }, [clearTurns, runCommand]);

  const value: AppContextValue = {
    state,
    turns,
    busy,
    connected,
    now,
    confirmRequest,
    theme,
    lang,
    uiPrefs,
    workspaceChats,
    expandedWorkspaceIds,
    settingsOpen,
    aboutOpen,
    t,
    setTheme,
    sendInput,
    runCommand,
    interrupt,
    answerConfirm,
    clearTurns,
    openWorkspaceInExplorer,
    toggleWorkspacePin,
    toggleChatPin,
    toggleChatArchive,
    archiveChats,
    setModel,
    setExecutionPolicy,
    toggleWorkspaceExpanded,
    refreshWorkspaceChats,
    openSettings,
    closeSettings,
    openAbout,
    closeAbout,
    pickFolder,
  };

  return <AppContext.Provider value={value}>{children}</AppContext.Provider>;
}

function loadInitialUiPrefs(): UiPrefs {
  return loadUiPrefs();
}

export function useApp(): AppContextValue {
  const ctx = useContext(AppContext);
  if (!ctx) {
    throw new Error("useApp must be used within AppProvider");
  }
  return ctx;
}
