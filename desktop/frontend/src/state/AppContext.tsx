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
  HistoryTurn,
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
  historyTurns: HistoryTurn[];
  historyStart: number;
  historyTotal: number;
  historyLoading: boolean;
  busy: boolean;
  /** Per-chat busy flags so the sidebar can mark every running chat. */
  busyByChat: Record<string, boolean>;
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
  planOpen: boolean;
  togglePlan: () => void;
  t: (key: string) => string;
  setTheme: (theme: Theme) => void;
  sendInput: (text: string) => Promise<void>;
  runCommand: (command: string) => Promise<void>;
  interrupt: () => Promise<void>;
  answerConfirm: (answer: string) => Promise<void>;
  clearTurns: (chatId?: string) => void;
  switchToChat: (chatId: string, workspaceId?: string) => Promise<void>;
  selectWorkspace: (workspaceId: string) => Promise<void>;
  /** Enter compose mode for a new chat (created on first send). */
  newChat: (workspaceId?: string) => Promise<void>;
  /** True while composing a not-yet-created chat. */
  draftMode: boolean;
  /** Target workspace id for the pending draft chat. */
  draftWorkspaceId: string;
  /** Choose the workspace the pending draft chat will be created in. */
  setDraftWorkspace: (workspaceId: string) => void;
  /** Delete a chat (may leave the workspace chat-less, entering compose mode). */
  deleteChat: (chatId: string, workspaceId?: string) => Promise<void>;
  forkChat: (index: number) => Promise<void>;
  editChat: (index: number) => Promise<void>;
  loadOlderHistory: () => Promise<void>;
  openWorkspaceInExplorer: (id: string) => Promise<boolean>;
  toggleWorkspacePin: (id: string) => void;
  toggleChatPin: (id: string) => void;
  toggleChatArchive: (id: string) => void;
  archiveChats: (ids: string[]) => void;
  setModel: (selector: string) => Promise<void>;
  setReasoning: (level: string) => Promise<void>;
  getModelsConfig: () => Promise<unknown[]>;
  saveModelsConfig: (providers: unknown[]) => Promise<boolean>;
  fetchProviderModels: (params: {
    base_url: string;
    api_key?: string;
    api_mode?: string;
    port?: number;
  }) => Promise<{ ok: boolean; models?: string[]; error?: string }>;
  setExecutionPolicy: (policy: string) => Promise<void>;
  toggleWorkspaceExpanded: (id: string) => void;
  refreshWorkspaceChats: (id: string) => Promise<void>;
  openSettings: () => void;
  closeSettings: () => void;
  openAbout: () => void;
  closeAbout: () => void;
  pickFolder: () => Promise<string>;
  pickFiles: () => Promise<string[]>;
}

interface HostApiBridge {
  pick_folder?: () => string | Promise<string>;
  pick_files?: () => string[] | Promise<string[]>;
}

const AppContext = createContext<AppContextValue | null>(null);

const THEME_STORAGE_KEY = "codewood.theme";
// Stable empty reference so the exposed `turns` doesn't change identity when a
// chat has no live turns (avoids needless re-renders / effect churn).
const EMPTY_TURNS: Turn[] = [];

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
  // Live (in-session) turns are tracked per chat so a chat's in-progress work
  // is preserved when the user switches to another chat. The active chat's
  // list is exposed as `turns` below.
  const [turnsByChat, setTurnsByChat] = useState<Record<string, Turn[]>>({});
  // Mirror of turnsByChat readable synchronously (e.g. while loading history we
  // must know whether a chat still has an in-progress live turn).
  const turnsByChatRef = useRef<Record<string, Turn[]>>({});
  const [historyTurns, setHistoryTurns] = useState<HistoryTurn[]>([]);
  const [historyStart, setHistoryStart] = useState(0);
  const [historyTotal, setHistoryTotal] = useState(0);
  const [historyLoading, setHistoryLoading] = useState(false);
  const historyChatRef = useRef<string>("\u0000");
  // Per-chat busy flags so each running chat shows its own state.
  const [busyByChat, setBusyByChat] = useState<Record<string, boolean>>({});
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
  const [planOpen, setPlanOpen] = useState(false);
  // Draft (compose) mode: "New Chat" shows the empty composer without creating
  // a chat yet; the chat is materialized only when the first message is sent.
  // ``draftWorkspaceId`` is the workspace the new chat will be created in.
  const [draftMode, setDraftMode] = useState(false);
  const [draftWorkspaceId, setDraftWorkspaceId] = useState<string>("");
  const draftModeRef = useRef(false);
  const draftWorkspaceIdRef = useRef<string>("");
  useEffect(() => {
    draftModeRef.current = draftMode;
  }, [draftMode]);
  useEffect(() => {
    draftWorkspaceIdRef.current = draftWorkspaceId;
  }, [draftWorkspaceId]);
  const nextIdRef = useRef(1);
  const seededExpandRef = useRef(false);
  const themeInitRef = useRef(false);
  const activeChatIdRef = useRef<string>("");
  const activeWorkspaceIdRef = useRef<string>("");
  const stateRef = useRef<AppState | null>(null);
  useEffect(() => {
    stateRef.current = state;
  }, [state]);

  const activeChatId = state?.activeChatId ?? "";
  useEffect(() => {
    activeChatIdRef.current = activeChatId;
  }, [activeChatId]);
  useEffect(() => {
    activeWorkspaceIdRef.current = state?.workspace.id ?? "";
  }, [state?.workspace.id]);

  useEffect(() => {
    turnsByChatRef.current = turnsByChat;
  }, [turnsByChat]);

  // The active chat's live turns / busy flag are what the chat view renders.
  const turns = turnsByChat[activeChatId] ?? EMPTY_TURNS;
  const busy = busyByChat[activeChatId] ?? false;
  // When set, the next `idle` event reloads chat history even if the active
  // chat id is unchanged (e.g. after `/chat edit` truncates the conversation).
  const pendingHistoryReloadRef = useRef(false);
  const reloadHistoryRef = useRef<() => void>(() => {});

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

  const setTheme = useCallback(
    (value: Theme) => {
      themeInitRef.current = true;
      setThemeState(value);
      window.localStorage.setItem(THEME_STORAGE_KEY, value);
      void client.setTheme(value);
    },
    [client],
  );

  // Adopt the theme persisted in config.jsonc once it arrives from the backend,
  // unless the user has already changed it this session.
  useEffect(() => {
    if (themeInitRef.current) {
      return;
    }
    const serverTheme = state?.theme;
    if (serverTheme === "light" || serverTheme === "dark" || serverTheme === "system") {
      themeInitRef.current = true;
      setThemeState(serverTheme);
      window.localStorage.setItem(THEME_STORAGE_KEY, serverTheme);
    }
  }, [state?.theme]);

  // Auto-expand the active workspace once on first load so its chats are
  // visible; afterwards the user fully controls which workspaces are open.
  useEffect(() => {
    const activeWsId = state?.workspace.id ?? "";
    if (seededExpandRef.current || !activeWsId) {
      return;
    }
    seededExpandRef.current = true;
    setExpandedWorkspaceIds((prev) => (prev.includes(activeWsId) ? prev : [...prev, activeWsId]));
  }, [state?.workspace.id]);

  // Mirror the active workspace's chats into the per-workspace cache. The
  // sidebar renders the active workspace from state.chats but other (expanded)
  // workspaces from this cache; without mirroring, switching away would leave
  // the previously-active workspace showing "No chats yet" until re-expanded.
  useEffect(() => {
    const activeWsId = state?.workspace.id ?? "";
    const chats = state?.chats;
    if (!activeWsId || !chats) {
      return;
    }
    setWorkspaceChats((prev) => ({ ...prev, [activeWsId]: chats }));
  }, [state?.workspace.id, state?.chats]);

  const updatePrefs = useCallback(
    (next: UiPrefs) => {
      setUiPrefs(next);
      saveUiPrefs(next);
      // Persist to the backend too so pin/archive survive a restart even when
      // the webview clears localStorage.
      void client.setUiPrefs(next);
    },
    [client],
  );

  // Adopt server-persisted pin/archive prefs once on first load; if the backend
  // has none yet, migrate any existing localStorage prefs up to it.
  const prefsInitRef = useRef(false);
  useEffect(() => {
    if (prefsInitRef.current) {
      return;
    }
    const sp = state?.uiPrefs;
    if (!sp) {
      return;
    }
    prefsInitRef.current = true;
    const ids = (v: unknown): string[] =>
      Array.isArray(v) ? v.filter((x): x is string => typeof x === "string" && !!x) : [];
    const server: UiPrefs = {
      pinnedWorkspaceIds: ids(sp.pinnedWorkspaceIds),
      pinnedChatIds: ids(sp.pinnedChatIds),
      archivedChatIds: ids(sp.archivedChatIds),
    };
    const hasServer =
      server.pinnedWorkspaceIds.length > 0 ||
      server.pinnedChatIds.length > 0 ||
      server.archivedChatIds.length > 0;
    if (hasServer) {
      setUiPrefs(server);
      saveUiPrefs(server);
      return;
    }
    const local = loadUiPrefs();
    if (
      local.pinnedWorkspaceIds.length > 0 ||
      local.pinnedChatIds.length > 0 ||
      local.archivedChatIds.length > 0
    ) {
      void client.setUiPrefs(local);
    }
  }, [state?.uiPrefs, client]);

  // Tick a 1s clock while busy so the active turn shows live elapsed time.
  useEffect(() => {
    if (!busy) {
      return;
    }
    setNow(Date.now());
    const handle = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(handle);
  }, [busy]);

  // Append a streamed delta to the current round of the active turn. Within a
  // round, consecutive same-kind deltas merge into one segment so model text
  // and tool output each stay contiguous while preserving arrival order.
  const appendSegment = useCallback(
    (kind: SegmentKind, text: string, chatId: string) => {
      if (!text || !chatId) {
        return;
      }
      setTurnsByChat((prev) => {
        const existing = prev[chatId];
        // Deltas only belong inside a turn opened by `turn_start`. Output that
        // arrives with no active turn is startup noise (the banner / tips
        // printed before the first user message) — drop it rather than
        // synthesizing an empty "Worked for 0s" turn.
        if (!existing || existing.length === 0) {
          return prev;
        }
        const next = [...existing];
        const turn = next[next.length - 1];
        const rounds = [...turn.rounds];
        let round = rounds[rounds.length - 1];
        if (!round) {
          round = {
            id: nextIdRef.current++,
            waitStartedAt: Date.now(),
            waitEndedAt: null,
            segments: [],
          };
          rounds.push(round);
        }
        const segments = [...round.segments];
        const last = segments[segments.length - 1];
        if (last && last.kind === kind) {
          segments[segments.length - 1] = { ...last, text: last.text + text };
        } else {
          segments.push({ id: nextIdRef.current++, kind, text });
        }
        rounds[rounds.length - 1] = { ...round, segments };
        next[next.length - 1] = { ...turn, rounds };
        return { ...prev, [chatId]: next };
      });
    },
    [],
  );

  const startTurn = useCallback((userText: string, chatId: string) => {
    if (!chatId) {
      return;
    }
    setTurnsByChat((prev) => ({
      ...prev,
      [chatId]: [
        ...(prev[chatId] ?? []),
        {
          id: nextIdRef.current++,
          userText,
          rounds: [],
          startedAt: Date.now(),
          endedAt: null,
        },
      ],
    }));
  }, []);

  // Open a model round's wait timer on the active turn. Consecutive rounds
  // that only issue tool calls (no natural-language reply yet) belong to one
  // collapsible tool group with a single running timer, so instead of starting
  // a new round we just reopen the previous round's timer when it carries tool
  // output but no answer. A new round starts only after a round produced an
  // answer (or at the turn's first round).
  const startRound = useCallback((chatId: string) => {
    if (!chatId) {
      return;
    }
    setTurnsByChat((prev) => {
      const next = prev[chatId] ? [...prev[chatId]] : [];
      let turn = next[next.length - 1];
      if (!turn) {
        turn = {
          id: nextIdRef.current++,
          userText: "",
          rounds: [],
          startedAt: Date.now(),
          endedAt: null,
        };
        next.push(turn);
      }
      const last = turn.rounds[turn.rounds.length - 1];
      const lastIsOpenToolGroup =
        last &&
        last.segments.some((s) => s.kind === "step") &&
        !last.segments.some((s) => s.kind === "answer");
      let rounds;
      if (lastIsOpenToolGroup) {
        // Keep the group's original start time; just resume its timer.
        rounds = [...turn.rounds];
        rounds[rounds.length - 1] = { ...last, waitEndedAt: null };
      } else {
        rounds = [
          ...turn.rounds,
          {
            id: nextIdRef.current++,
            waitStartedAt: Date.now(),
            waitEndedAt: null,
            segments: [],
          },
        ];
      }
      next[next.length - 1] = { ...turn, rounds };
      return { ...prev, [chatId]: next };
    });
  }, []);

  // Freeze the current round's wait timer (the model has fully responded).
  const endRound = useCallback((chatId: string) => {
    if (!chatId) {
      return;
    }
    setTurnsByChat((prev) => {
      const list = prev[chatId];
      if (!list || list.length === 0) {
        return prev;
      }
      const turn = list[list.length - 1];
      if (turn.rounds.length === 0) {
        return prev;
      }
      const lastRound = turn.rounds[turn.rounds.length - 1];
      if (lastRound.waitEndedAt !== null) {
        return prev;
      }
      const rounds = [...turn.rounds];
      rounds[rounds.length - 1] = { ...lastRound, waitEndedAt: Date.now() };
      const next = [...list];
      next[next.length - 1] = { ...turn, rounds };
      return { ...prev, [chatId]: next };
    });
  }, []);

  const endActiveTurn = useCallback((chatId: string) => {
    if (!chatId) {
      return;
    }
    setTurnsByChat((prev) => {
      const list = prev[chatId];
      if (!list || list.length === 0) {
        return prev;
      }
      const last = list[list.length - 1];
      if (last.endedAt !== null) {
        return prev;
      }
      // Freeze any still-open round so its timer stops with the turn.
      const rounds = last.rounds.map((r, i) =>
        i === last.rounds.length - 1 && r.waitEndedAt === null
          ? { ...r, waitEndedAt: Date.now() }
          : r,
      );
      const copy = [...list];
      copy[copy.length - 1] = { ...last, rounds, endedAt: Date.now() };
      return { ...prev, [chatId]: copy };
    });
  }, []);

  const setBusyForChat = useCallback((chatId: string, value: boolean) => {
    if (!chatId) {
      return;
    }
    setBusyByChat((prev) => {
      if ((prev[chatId] ?? false) === value) {
        return prev;
      }
      return { ...prev, [chatId]: value };
    });
  }, []);

  useEffect(() => {
    let source: EventSource | null = null;

    const handleEvent = (event: ServerEvent) => {
      const data = event.data as {
        state?: AppState;
        text?: string;
        chatId?: string;
      };
      // Every per-chat event carries the id of the chat it belongs to. We must
      // NOT fall back to the focused chat: with several chats running in
      // parallel that would mis-route a background chat's turn/output (and the
      // connection-priming idle, which has no chatId) into whichever chat the
      // user is currently viewing — surfacing a phantom second "Working..." and
      // stealing the real chat's render. Untagged events only sync state; the
      // turn/busy mutators below all no-op on an empty chatId.
      const chatId = String(data.chatId || "");
      switch (event.event) {
        case "idle": {
          const next = data.state;
          if (next) {
            setState(next);
          }
          endActiveTurn(chatId);
          setBusyForChat(chatId, false);
          if (pendingHistoryReloadRef.current) {
            pendingHistoryReloadRef.current = false;
            reloadHistoryRef.current();
          }
          break;
        }
        case "turn_start": {
          startTurn(String(data.text ?? ""), chatId);
          setBusyForChat(chatId, true);
          break;
        }
        case "round_start": {
          startRound(chatId);
          break;
        }
        case "round_end": {
          endRound(chatId);
          break;
        }
        case "output": {
          appendSegment("step", String(data.text ?? ""), chatId);
          break;
        }
        case "assistant": {
          appendSegment("answer", String(data.text ?? ""), chatId);
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
  }, [client, appendSegment, startTurn, startRound, endRound, endActiveTurn, setBusyForChat]);

  const sendInput = useCallback(
    async (text: string) => {
      const trimmed = text.trim();
      if (!trimmed) {
        return;
      }
      // In draft (compose) mode the chat hasn't been created yet. Materialize it
      // now: switch to the chosen workspace if needed, then create a fresh chat,
      // so the first message lands in a brand-new chat in the right workspace.
      let targetChatId = activeChatIdRef.current;
      if (draftModeRef.current) {
        const wsId = draftWorkspaceIdRef.current;
        if (wsId && wsId !== activeWorkspaceIdRef.current) {
          await client.selectChat("", wsId);
        }
        const newId = await client.newChat();
        if (newId) {
          targetChatId = newId;
          historyChatRef.current = newId;
        }
        setDraftMode(false);
        setDraftWorkspaceId("");
      }
      // Composer input is always a model prompt; the GUI never executes
      // built-in commands or "!" direct shell typed by the user. Route it to
      // the focused chat explicitly so it reaches that chat's loop even while
      // another chat is mid-task.
      await client.sendInput(trimmed, true, targetChatId);
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

  const clearLiveTurns = useCallback((chatId: string) => {
    if (!chatId) {
      return;
    }
    setTurnsByChat((prev) => {
      if (!(chatId in prev)) {
        return prev;
      }
      const next = { ...prev };
      delete next[chatId];
      return next;
    });
  }, []);

  const clearTurns = useCallback(
    (chatId?: string) => clearLiveTurns(chatId ?? activeChatIdRef.current),
    [clearLiveTurns],
  );

  // Drop a chat's settled (ended) live turns but keep any in-progress one. Used
  // when switching to a still-running chat: its completed turns are now in the
  // reloaded history, but the streaming turn is not yet persisted and must be
  // preserved so it keeps rendering live.
  const dropSettledLiveTurns = useCallback((chatId: string) => {
    if (!chatId) {
      return;
    }
    setTurnsByChat((prev) => {
      const list = prev[chatId];
      if (!list || list.length === 0) {
        return prev;
      }
      const active = list.filter((tt) => tt.endedAt === null);
      if (active.length === list.length) {
        return prev;
      }
      const next = { ...prev };
      if (active.length === 0) {
        delete next[chatId];
      } else {
        next[chatId] = active;
      }
      return next;
    });
  }, []);

  const INITIAL_HISTORY = 12;
  const HISTORY_PAGE = 8;

  // Load the most recent history page for the active chat. `forChatId` names
  // the chat being loaded (it may differ from the focused chat momentarily,
  // right after a switch before the idle state arrives); its live turns are
  // dropped since they are now part of the persisted history.
  const loadChatHistory = useCallback(
    async (forChatId?: string) => {
      const cid = forChatId ?? activeChatIdRef.current;
      setHistoryLoading(true);
      try {
        const page = await client.getChatHistory(undefined, INITIAL_HISTORY);
        // If the chat we're loading is still streaming a turn, that same
        // in-progress turn also appears as the trailing persisted history entry
        // (its user message, no final answer yet). Drop that trailing entry and
        // keep the live turn so the streaming content (and "Working…") survives
        // the switch; otherwise show full history and clear the settled turns.
        const live = turnsByChatRef.current[cid] ?? [];
        const hasActive = live.some((tt) => tt.endedAt === null);
        if (hasActive && page.turns.length > 0) {
          setHistoryTurns(page.turns.slice(0, -1));
          setHistoryStart(page.start);
          setHistoryTotal(page.total);
          dropSettledLiveTurns(cid);
        } else {
          setHistoryTurns(page.turns);
          setHistoryStart(page.start);
          setHistoryTotal(page.total);
          clearLiveTurns(cid);
        }
      } finally {
        setHistoryLoading(false);
      }
    },
    [client, clearLiveTurns, dropSettledLiveTurns],
  );

  // Keep a stable ref to the latest loadChatHistory so the SSE idle handler
  // (which closes over an old render) can trigger a reload on demand.
  useEffect(() => {
    reloadHistoryRef.current = () => {
      void loadChatHistory();
    };
  }, [loadChatHistory]);

  const loadOlderHistory = useCallback(async () => {
    if (historyLoading || historyStart <= 0) {
      return;
    }
    setHistoryLoading(true);
    try {
      const page = await client.getChatHistory(historyStart, HISTORY_PAGE);
      setHistoryTurns((prev) => [...page.turns, ...prev]);
      setHistoryStart(page.start);
      setHistoryTotal(page.total);
    } finally {
      setHistoryLoading(false);
    }
  }, [client, historyLoading, historyStart]);

  // Initial / external chat changes: (re)load history for the active chat.
  // Our own switchToChat/newChat update historyChatRef so this won't double-run.
  useEffect(() => {
    const cid = state?.activeChatId ?? "";
    if (!cid || cid === historyChatRef.current) {
      return;
    }
    historyChatRef.current = cid;
    void loadChatHistory(cid);
  }, [state?.activeChatId, loadChatHistory]);

  const switchToChat = useCallback(
    async (chatId: string, workspaceId = "") => {
      const ok = await client.selectChat(chatId, workspaceId);
      if (!ok) {
        return;
      }
      setDraftMode(false);
      setDraftWorkspaceId("");
      historyChatRef.current = chatId;
      await loadChatHistory(chatId);
    },
    [client, loadChatHistory],
  );

  const selectWorkspace = useCallback(
    async (workspaceId: string) => {
      const ok = await client.selectChat("", workspaceId);
      if (!ok) {
        return;
      }
      // The new workspace's active chat id arrives via the idle state event,
      // which triggers the history effect above to load its turns.
      historyChatRef.current = "\u0000";
      clearTurns();
    },
    [client, clearTurns],
  );

  // Enter draft (compose) mode instead of creating a chat immediately. The chat
  // is materialized on the first send. ``workspaceId`` selects the target
  // workspace: from the sidebar's top New Chat button it defaults to the current
  // chat's workspace; from a workspace row's New Chat icon it is that workspace.
  const newChat = useCallback(
    async (workspaceId?: string) => {
      const wsId =
        workspaceId ?? state?.workspace.id ?? draftWorkspaceIdRef.current ?? "";
      setDraftWorkspaceId(wsId);
      setDraftMode(true);
      historyChatRef.current = "\u0000";
      setHistoryTurns([]);
      setHistoryStart(0);
      setHistoryTotal(0);
    },
    [state?.workspace.id],
  );

  // Pick the target workspace for the pending draft chat (compose mode only).
  const setDraftWorkspace = useCallback((workspaceId: string) => {
    setDraftWorkspaceId(workspaceId);
  }, []);

  // Delete a chat. If it was the active chat and the workspace is now chat-less,
  // drop into compose (draft) mode for that workspace instead of auto-creating a
  // new chat. The idle event from the backend carries the post-delete state.
  const deleteChat = useCallback(
    async (chatId: string, workspaceId = "") => {
      const wasActive = chatId === activeChatIdRef.current;
      const wsId = workspaceId || activeWorkspaceIdRef.current;
      // Whether deleting this chat empties the active workspace. (Only the
      // active workspace's chats are present in ``state.chats``.)
      const inActiveWs = !workspaceId || workspaceId === activeWorkspaceIdRef.current;
      const willBeEmpty =
        inActiveWs && (stateRef.current?.chats?.length ?? 0) <= 1;
      const ok = await client.deleteChat(chatId, workspaceId);
      if (!ok) {
        return;
      }
      clearLiveTurns(chatId);
      if (wasActive && willBeEmpty) {
        // Chat-less workspace: enter compose mode so the user can type to create
        // a fresh chat instead of auto-creating one.
        setDraftWorkspaceId(wsId);
        setDraftMode(true);
        historyChatRef.current = "\u0000";
        setHistoryTurns([]);
        setHistoryStart(0);
        setHistoryTotal(0);
      }
    },
    [client, clearLiveTurns],
  );

  // Fork the chat at the given (negative, from-end) genuine-user index into a
  // new chat, mirroring the TUI `/chat fork` command. The backend switches the
  // active chat; the idle state carries the new id and the history effect
  // reloads it.
  const forkChat = useCallback(
    async (index: number) => {
      clearTurns();
      historyChatRef.current = "\u0000";
      await client.sendInput(`/chat fork ${index}`);
    },
    [client, clearTurns],
  );

  // Truncate the conversation at the given (negative, from-end) genuine-user
  // index, mirroring the TUI `/chat edit` command. The chat id is unchanged, so
  // the activeChatId effect won't refire; instead we flag the next idle event
  // to reload the (now shorter) history.
  const editChat = useCallback(
    async (index: number) => {
      clearTurns();
      pendingHistoryReloadRef.current = true;
      await client.sendInput(`/chat edit ${index}`);
    },
    [client, clearTurns],
  );

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
        // Route the model switch to the focused chat so it only changes that
        // chat's model (and persists onto its history), never another chat's.
        await client.sendInput(`/model ${value}`, false, activeChatIdRef.current);
      }
    },
    [client],
  );

  const setReasoning = useCallback(
    async (level: string) => {
      // Route to the focused chat, same as model switches.
      await client.sendInput(
        `/model reasoning ${level.trim()}`,
        false,
        activeChatIdRef.current,
      );
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

  const pickFiles = useCallback(async (): Promise<string[]> => {
    const api = (window as unknown as { pywebview?: { api?: HostApiBridge } })
      .pywebview?.api;
    if (!api?.pick_files) {
      return [];
    }
    try {
      const result = await api.pick_files();
      return Array.isArray(result) ? result.filter(Boolean) : [];
    } catch {
      return [];
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
    historyTurns,
    historyStart,
    historyTotal,
    historyLoading,
    busy,
    busyByChat,
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
    planOpen,
    togglePlan: () => setPlanOpen((v) => !v),
    t,
    setTheme,
    sendInput,
    runCommand,
    interrupt,
    answerConfirm,
    clearTurns,
    switchToChat,
    selectWorkspace,
    newChat,
    draftMode,
    draftWorkspaceId,
    setDraftWorkspace,
    deleteChat,
    forkChat,
    editChat,
    loadOlderHistory,
    openWorkspaceInExplorer,
    toggleWorkspacePin,
    toggleChatPin,
    toggleChatArchive,
    archiveChats,
    setModel,
    setReasoning,
    getModelsConfig: () => client.getModelsConfig(),
    saveModelsConfig: (providers: unknown[]) => client.saveModelsConfig(providers),
    fetchProviderModels: (params: {
      base_url: string;
      api_key?: string;
      api_mode?: string;
      port?: number;
    }) => client.fetchProviderModels(params),
    setExecutionPolicy,
    toggleWorkspaceExpanded,
    refreshWorkspaceChats,
    openSettings,
    closeSettings,
    openAbout,
    closeAbout,
    pickFolder,
    pickFiles,
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
