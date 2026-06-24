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
  AskMoreInfoRequest,
  CompletionCatalog,
  ConfirmRequest,
  GeneralConfig,
  HistoryTurn,
  McpServerConfigEntry,
  McpServerDetails,
  McpServerSummary,
  SegmentKind,
  ServerEvent,
  SubAgentConfig,
  SubAgentsOverview,
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
import {
  RIGHT_PANEL_TABS,
  loadRightPanelPrefs,
  saveRightPanelPrefs,
} from "./rightPanelTabs";

export type Theme = "light" | "dark" | "system";

// Workspace-qualified key for the live turn / busy / ask-more-info maps. Chat
// ids are only unique within a workspace, so these client-side maps must be
// keyed by ``workspaceId`` + ``chatId`` to keep a chat's live state from
// bleeding into a same-id chat in another workspace. An empty workspace id
// degrades to the bare chat id so single-workspace behavior is unchanged.
function chatKey(workspaceId: string, chatId: string): string {
  const cid = String(chatId || "");
  if (!cid) {
    return "";
  }
  const ws = String(workspaceId || "");
  return ws ? `${ws}\u0000${cid}` : cid;
}

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
  /** Chats with a completed turn the user hasn't opened yet (unread). */
  unreadChatIds: Record<string, boolean>;
  connected: boolean;
  now: number;
  confirmRequest: ConfirmRequest | null;
  /** Pending ``request_user_input`` clarification surfaced for the active chat. */
  askMoreInfo: AskMoreInfoRequest | null;
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
  setGuiLanguage: (language: string) => Promise<void>;
  /** Pick + set a new GUI background image (returns false if cancelled/failed). */
  setBackgroundImage: () => Promise<boolean>;
  clearBackgroundImage: () => Promise<boolean>;
  /** Persist the background opacity (0-100). Applies live via document styles. */
  setBackgroundOpacity: (opacity: number) => Promise<void>;
  /** Absolute URL of the current background image (token + cache-busting). */
  backgroundImageUrl: (version: number) => string;
  pasteImage: (
    dataUrl: string,
  ) => Promise<{ path: string; name: string } | null>;
  chatImageUrl: (path: string) => string;
  subscribeBrowserCommand: (
    handler: (cmd: Record<string, unknown>) => void,
  ) => () => void;
  sendBrowserResult: (
    requestId: string,
    result: Record<string, unknown>,
  ) => Promise<void>;
  previewHtml: (html: string) => Promise<{ url: string } | null>;
  previewHtmlInBrowser: (html: string) => Promise<boolean>;
  resolveBackendUrl: (url: string) => string;
  sendInput: (text: string) => Promise<void>;
  runCommand: (command: string) => Promise<void>;
  interrupt: () => Promise<void>;
  answerConfirm: (answer: string) => Promise<void>;
  /** Resolve the active ``request_user_input`` prompt with the user's answer. */
  answerAskMoreInfo: (answer: string) => Promise<void>;
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
  getGeneralConfig: () => Promise<GeneralConfig | null>;
  saveGeneralConfig: (general: Partial<GeneralConfig>) => Promise<boolean>;
  getMcpOverview: () => Promise<McpServerSummary[]>;
  getMcpServerDetails: (name: string) => Promise<McpServerDetails | null>;
  setMcpServerEnabled: (name: string, enabled: boolean) => Promise<boolean>;
  setMcpToolEnabled: (
    server: string,
    tool: string,
    enabled: boolean,
  ) => Promise<boolean>;
  setMcpToolsEnabled: (
    server: string,
    tools: string[],
    enabled: boolean,
  ) => Promise<boolean>;
  getCompletionCatalog: () => Promise<CompletionCatalog>;
  getMcpServerConfig: (name: string) => Promise<McpServerConfigEntry>;
  addMcpServer: (
    name: string,
    config: McpServerConfigEntry,
  ) => Promise<{ ok: boolean; error?: string }>;
  updateMcpServer: (
    originalName: string,
    name: string,
    config: McpServerConfigEntry,
  ) => Promise<{ ok: boolean; error?: string }>;
  deleteMcpServer: (name: string) => Promise<{ ok: boolean; error?: string }>;
  getSubAgentsOverview: () => Promise<SubAgentsOverview>;
  saveSubAgent: (
    payload: Partial<SubAgentConfig> & { originalName?: string },
  ) => Promise<{ ok: boolean; error?: string }>;
  deleteSubAgent: (name: string) => Promise<{ ok: boolean; error?: string }>;
  setSubAgentEnabled: (
    name: string,
    enabled: boolean,
  ) => Promise<{ ok: boolean; error?: string }>;
  setPlanMode: (enabled: boolean) => Promise<boolean>;
  searchWorkspaceFiles: (query: string, limit?: number) => Promise<string[]>;
  setExecutionPolicy: (policy: string) => Promise<void>;
  toggleWorkspaceExpanded: (id: string) => void;
  refreshWorkspaceChats: (id: string) => Promise<void>;
  /** Open the settings view, optionally landing directly on a given page. */
  openSettings: (page?: string) => void;
  /** The page the settings view should open on (consumed once on open). */
  settingsInitialPage: string | null;
  closeSettings: () => void;
  openAbout: () => void;
  closeAbout: () => void;
  pickFolder: () => Promise<string>;
  pickFiles: () => Promise<string[]>;
}

interface HostApiBridge {
  pick_folder?: () => string | Promise<string>;
  pick_files?: (directory?: string) => string[] | Promise<string[]>;
  pick_image?: (directory?: string) => string | Promise<string>;
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
  // Chats whose turn finished while the user was looking at a different chat.
  // They stay flagged as unread (blue dot in the sidebar) until opened.
  const [unreadChatIds, setUnreadChatIds] = useState<Record<string, boolean>>(
    {},
  );
  const [connected, setConnected] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  const [confirmRequest, setConfirmRequest] = useState<ConfirmRequest | null>(null);
  // Keyed per chat so a clarifying prompt fired in one chat stays visible
  // there even if the user temporarily switches away; the panel for the
  // active chat is derived in the value below.
  const [askMoreInfoByChat, setAskMoreInfoByChat] = useState<
    Record<string, AskMoreInfoRequest>
  >({});
  const [theme, setThemeState] = useState<Theme>(loadInitialTheme);
  const [uiPrefs, setUiPrefs] = useState<UiPrefs>(loadInitialUiPrefs);
  const [workspaceChats, setWorkspaceChats] = useState<
    Record<string, WorkspaceChatSummary[]>
  >({});
  const [expandedWorkspaceIds, setExpandedWorkspaceIds] = useState<string[]>([]);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settingsInitialPage, setSettingsInitialPage] = useState<string | null>(null);
  const [aboutOpen, setAboutOpen] = useState(false);
  const [planOpen, setPlanOpen] = useState(false);
  // Subscribers for backend-originated browser commands (the BrowserPanel
  // registers one while mounted). A Set so mount/unmount add/remove cleanly.
  const browserCommandHandlersRef = useRef<
    Set<(cmd: Record<string, unknown>) => void>
  >(new Set());
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

  // Local focus override applied when the GUI materializes a brand-new chat
  // from draft mode. The backend's authoritative ``activeChatId`` only arrives
  // via an SSE ``idle`` event, and EVERY incoming event calls ``setState`` —
  // so an optimistic ``setState({activeChatId})`` can be clobbered by an
  // in-flight/stale event, leaving the view stuck on the old/empty chat (the
  // "slow switch / message not shown" symptom). This override forces the view
  // onto the new chat immediately and is cleared once ``state`` catches up.
  const [focusOverride, setFocusOverride] = useState<{
    chatId: string;
    wsId: string;
  } | null>(null);
  useEffect(() => {
    if (focusOverride && state?.activeChatId === focusOverride.chatId) {
      setFocusOverride(null);
    }
  }, [state?.activeChatId, focusOverride]);

  const activeChatId = focusOverride?.chatId ?? state?.activeChatId ?? "";
  const activeChatWsId = focusOverride?.wsId ?? state?.workspace.id ?? "";
  useEffect(() => {
    activeChatIdRef.current = activeChatId;
    // Opening (or switching to) a chat clears its unread marker. The unread
    // set is keyed by the workspace-qualified bucket so we clear only the
    // focused workspace's chat, never a same-id chat in another workspace.
    const key = chatKey(activeChatWsId, activeChatId);
    if (key) {
      setUnreadChatIds((prev) =>
        prev[key]
          ? Object.fromEntries(
              Object.entries(prev).filter(([id]) => id !== key),
            )
          : prev,
      );
    }
  }, [activeChatId, activeChatWsId]);

  // Auto-open the plan panel when the active chat has a plan and either
  // (a) the plan just went from empty to non-empty (new plan), or
  // (b) we switched to a chat that already has a saved plan.
  const planAutoOpenRef = useRef<{ chatId: string; planLen: number }>({
    chatId: "",
    planLen: 0,
  });
  const activePlanLen = state?.plan?.plan?.length ?? 0;
  useEffect(() => {
    const prev = planAutoOpenRef.current;
    const chatChanged = prev.chatId !== activeChatId;
    const grewFromEmpty = !chatChanged && prev.planLen === 0 && activePlanLen > 0;
    if (activePlanLen > 0 && (chatChanged || grewFromEmpty)) {
      setPlanOpen(true);
    }
    planAutoOpenRef.current = { chatId: activeChatId, planLen: activePlanLen };
  }, [activeChatId, activePlanLen]);
  useEffect(() => {
    activeWorkspaceIdRef.current = state?.workspace.id ?? "";
  }, [state?.workspace.id]);

  useEffect(() => {
    turnsByChatRef.current = turnsByChat;
  }, [turnsByChat]);

  // Live turn / busy / ask maps are keyed by a WORKSPACE-QUALIFIED composite
  // (``workspaceId\x00chatId``), not the bare chat id: chat ids repeat across
  // workspaces (``chat-1``, ``chat-2``, … per workspace), so a bare-id bucket
  // would make a chat in one workspace render another workspace's same-id
  // chat's turns/busy/messages after a focus switch. ``chatKey`` builds the
  // composite; an empty workspace id degrades to the bare id (single-workspace
  // / legacy behavior unchanged).
  const activeWorkspaceId = focusOverride?.wsId ?? state?.workspace.id ?? "";
  const activeKey = chatKey(activeWorkspaceId, activeChatId);
  // The active chat's live turns / busy flag are what the chat view renders.
  const turns = turnsByChat[activeKey] ?? EMPTY_TURNS;
  const busy = busyByChat[activeKey] ?? false;
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

  const setGuiLanguage = useCallback(
    async (value: string) => {
      // GUI language is a presentation-only override stored in the GUI config
      // file; the agent's display_language (used by the TUI) is intentionally
      // left untouched.
      await client.setGuiLanguage(value);
    },
    [client],
  );

  // Live opacity override so the slider updates the background instantly while
  // dragging, before the backend state snapshot round-trips back.
  const [bgOpacityOverride, setBgOpacityOverride] = useState<number | null>(null);

  const setBackgroundImage = useCallback(async (): Promise<boolean> => {
    const api = (window as unknown as { pywebview?: { api?: HostApiBridge } })
      .pywebview?.api;
    if (!api) {
      return false;
    }
    let chosen = "";
    try {
      if (api.pick_image) {
        // Preferred: native single-select image picker.
        chosen = (await api.pick_image()) || "";
      } else if (api.pick_files) {
        // Fallback for older hosts without ``pick_image``: use the generic
        // multi-select picker and take the first selection. The backend still
        // validates the extension, so a non-image pick is rejected safely.
        const picked = (await api.pick_files()) || [];
        chosen = Array.isArray(picked) ? String(picked[0] ?? "") : "";
      }
    } catch {
      chosen = "";
    }
    if (!chosen) {
      return false;
    }
    const ok = await client.setBackgroundImage(chosen);
    if (ok) {
      // Drop the optimistic override so the freshly-pushed state opacity wins.
      setBgOpacityOverride(null);
    }
    return ok;
  }, [client]);

  const clearBackgroundImage = useCallback(async (): Promise<boolean> => {
    return client.clearBackgroundImage();
  }, [client]);

  const backgroundImageUrl = useCallback(
    (version: number) => client.backgroundImageUrl(version),
    [client],
  );

  const pasteImage = useCallback(
    (dataUrl: string) => client.pasteImage(activeChatIdRef.current, dataUrl),
    [client],
  );

  const chatImageUrl = useCallback(
    (path: string) => client.chatImageUrl(path),
    [client],
  );

  const resolveBackendUrl = useCallback(
    (url: string) => client.absoluteUrl(url),
    [client],
  );

  const subscribeBrowserCommand = useCallback(
    (handler: (cmd: Record<string, unknown>) => void) => {
      browserCommandHandlersRef.current.add(handler);
      return () => {
        browserCommandHandlersRef.current.delete(handler);
      };
    },
    [],
  );

  const sendBrowserResult = useCallback(
    (requestId: string, result: Record<string, unknown>) =>
      client.browserResult(requestId, result),
    [client],
  );

  const previewHtml = useCallback(
    (html: string) => client.previewHtml(activeChatIdRef.current, html),
    [client],
  );

  // Locally fan a command out to the mounted BrowserPanel (no backend / no
  // requestId): used by the in-message "Preview" button to drive the browser.
  const emitBrowserCommandLocal = useCallback(
    (cmd: Record<string, unknown>) => {
      for (const handler of browserCommandHandlersRef.current) {
        try {
          handler(cmd);
        } catch {
          // ignore a misbehaving handler
        }
      }
    },
    [],
  );

  // Render an HTML snippet in the embedded browser: persist it, ensure the
  // Browser tab is visible+active and the right panel is open, then navigate.
  const previewHtmlInBrowser = useCallback(
    async (html: string) => {
      const saved = await client.previewHtml(activeChatIdRef.current, html);
      if (!saved) {
        return false;
      }
      try {
        const prefs = loadRightPanelPrefs();
        const visible = prefs.visible.includes("browser")
          ? prefs.visible
          : [...prefs.visible, "browser"];
        const ordered = RIGHT_PANEL_TABS.filter((id) => visible.includes(id));
        saveRightPanelPrefs({ visible: ordered, active: "browser" });
        window.dispatchEvent(new Event("codewood.rightPanelTabs"));
      } catch {
        // localStorage may be unavailable; the panel just won't switch tabs.
      }
      setPlanOpen(true);
      // Defer the fan-out so a just-mounted BrowserPanel (the Browser tab may
      // have been hidden/closed until now) has registered its subscriber
      // before the command is delivered — otherwise the very first Preview
      // click only opens the tab and the page never loads.
      window.setTimeout(() => {
        emitBrowserCommandLocal({ action: "open_preview", url: saved.url });
      }, 120);
      return true;
    },
    [client, emitBrowserCommandLocal],
  );

  const setBackgroundOpacity = useCallback(
    async (opacity: number) => {
      const clamped = Math.max(0, Math.min(100, Math.round(opacity)));
      setBgOpacityOverride(clamped);
      await client.setBackgroundOpacity(clamped);
    },
    [client],
  );

  // Apply the GUI background image as a fixed, full-window layer behind all
  // content (including the settings view). We drive it through document-level
  // CSS variables so a single ``::before`` layer in the stylesheet renders it
  // without distortion (cover) at the configured opacity.
  const bgState = state?.background;
  const bgHasImage = Boolean(bgState?.hasImage);
  const bgVersion = bgState?.version ?? 0;
  const bgServerOpacity = bgState?.opacity ?? 60;
  useEffect(() => {
    const root = document.documentElement;
    if (bgHasImage) {
      const url = client.backgroundImageUrl(bgVersion);
      root.style.setProperty("--app-bg-image", `url("${url}")`);
      root.setAttribute("data-has-bg", "true");
    } else {
      root.style.removeProperty("--app-bg-image");
      root.removeAttribute("data-has-bg");
    }
  }, [client, bgHasImage, bgVersion]);

  useEffect(() => {
    // The slider value is the *image transparency* the user asked for (default
    // 80 = faint). The background image layer itself is always fully painted;
    // we instead control how much of it shows through by setting the opacity of
    // the app surfaces stacked on top via ``--app-surface-alpha`` (0-1). A
    // higher transparency keeps the surfaces more opaque, so the image stays
    // faint; transparency 0 makes the surfaces fully opaque (image hidden) and
    // 100 makes them fully transparent (image fully revealed). Using this
    // single dimming lever avoids the previous compounding-opacity bug where a
    // faint image behind semi-opaque panels became effectively invisible.
    const transparency = Math.max(0, Math.min(100, bgOpacityOverride ?? bgServerOpacity));
    // Map the slider (image transparency %) to the surface opacity with a gentle
    // ease so the image is revealed progressively across the whole range instead
    // of only near 0. A linear mapping made surfaces feel too opaque past ~50%
    // (e.g. 60% transparency still mostly hid the image). The exponent (<1)
    // lowers surface alpha faster as transparency drops:
    //   t=80 -> ~0.62 (faint, default)   t=60 -> ~0.42   t=40 -> ~0.25   t=0 -> 0
    const surfaceAlpha = Math.pow(transparency / 100, 1.6);
    document.documentElement.style.setProperty(
      "--app-surface-alpha",
      String(surfaceAlpha),
    );
  }, [bgOpacityOverride, bgServerOpacity]);

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
        } else if (
          kind === "answer" &&
          round.segments.some((s) => s.kind === "step") &&
          !round.segments.some((s) => s.kind === "answer")
        ) {
          // The model finished its tool calls for this round and is now
          // replying. Freeze the tool group's timer and open a fresh round so
          // the answer (and the live "Working" timer that follows for the next
          // tool call) sits BELOW the tools, in natural order — instead of the
          // tool group's running timer hovering above the just-streamed reply.
          rounds[rounds.length - 1] = {
            ...round,
            waitEndedAt: round.waitEndedAt ?? Date.now(),
          };
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
    setTurnsByChat((prev) => {
      const existing = prev[chatId] ?? [];
      // Reconcile the optimistic first-message turn opened by ``sendInput`` for
      // a brand-new chat: adopt the backend's authoritative user text in place
      // instead of appending a SECOND turn (which would duplicate the message).
      const last = existing[existing.length - 1];
      if (last && last.optimistic && last.rounds.length === 0) {
        const merged = [...existing];
        merged[merged.length - 1] = {
          ...last,
          userText: userText || last.userText,
          optimistic: false,
        };
        return { ...prev, [chatId]: merged };
      }
      return {
        ...prev,
        [chatId]: [
          ...existing,
          {
            id: nextIdRef.current++,
            userText,
            rounds: [],
            startedAt: Date.now(),
            endedAt: null,
          },
        ],
      };
    });
  }, []);

  // Optimistically open a turn for the user's message before the backend's
  // ``turn_start`` event lands. Used when materializing a brand-new chat from
  // draft mode so the first message echoes immediately (no SSE round-trip lag);
  // ``startTurn`` reconciles it in place when the authoritative event arrives.
  const startOptimisticTurn = useCallback(
    (userText: string, chatId: string) => {
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
            optimistic: true,
          },
        ],
      }));
    },
    [],
  );

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
      // An optimistic turn with no rounds yet is the first message of a
      // just-created chat still waiting for its authoritative ``turn_start``.
      // The ``new_chat`` path emits an ``idle`` snapshot before that input is
      // processed; settling the turn here would split it (the reply would open a
      // SECOND turn, duplicating the user message). Leave it open.
      if (last.optimistic && last.rounds.length === 0) {
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
        workspaceId?: string;
      };
      // Every per-chat event carries the id of the chat it belongs to. We must
      // NOT fall back to the focused chat: with several chats running in
      // parallel that would mis-route a background chat's turn/output (and the
      // connection-priming idle, which has no chatId) into whichever chat the
      // user is currently viewing — surfacing a phantom second "Working..." and
      // stealing the real chat's render. Untagged events only sync state; the
      // turn/busy mutators below all no-op on an empty chatId.
      const chatId = String(data.chatId || "");
      // Chat ids repeat across workspaces (``chat-1``, ``chat-2``, … are
      // reassigned per workspace), so a background chat running in another
      // workspace can share an id with a chat in the focused workspace. Route
      // every per-chat mutation into a WORKSPACE-QUALIFIED bucket keyed by the
      // EVENT's workspace, never the focused one: that way a background WS-A
      // turn keeps accumulating into WS-A's bucket while the user views WS-B,
      // and switching back to WS-A shows the live, still-streaming turn. The
      // active selectors read the bucket for the focused workspace+chat.
      //
      // ``eventWsId`` falls back to the focused workspace when the backend
      // doesn't tag the event (older backend / untagged priming events), so
      // single-workspace behavior is unchanged.
      const activeWsId = String(stateRef.current?.workspace?.id || "");
      const eventWsId = String(data.workspaceId || "") || activeWsId;
      const eventKey = chatKey(eventWsId, chatId);
      switch (event.event) {
        case "idle": {
          const next = data.state;
          if (next) {
            setState(next);
          }
          // An ``idle`` event can be emitted by paths other than a genuine
          // turn completion — most importantly the focus-switch broadcast
          // (``select_chat``) and any state refresh that reuses the ``idle``
          // channel. The snapshot's per-chat ``running`` flag is the durable
          // truth (it mirrors the backend runtime's busy state and survives
          // focus changes). When the chat this ``idle`` is attributed to is
          // STILL running on the backend, treating it as "finished" would
          // wrongly call ``endActiveTurn``/clear-busy — wiping the chat's
          // in-progress reply and the sidebar busy/blue dot with no
          // ``turn_start`` to restore them when the user switches back. So we
          // only terminate the turn when the backend agrees the chat is idle.
          // The snapshot's per-chat ``running`` flag only describes the chats
          // of the workspace that owns this ``idle`` event. Resolve it against
          // that workspace: if the event isn't for the focused workspace the
          // freshly-applied ``next`` snapshot is for a DIFFERENT workspace, so
          // we can't trust its chat list for the event's chat. In that case
          // fall back to whether a live turn is still open in the event's
          // bucket (it stays open until a real terminal idle for that chat).
          const idleForFocused =
            !eventWsId || !activeWsId || eventWsId === activeWsId;
          const stillRunning = idleForFocused
            ? Boolean(
                chatId &&
                  next?.chats?.some(
                    (c) => String(c.id) === chatId && Boolean(c.running),
                  ),
              )
            : false;
          if (!stillRunning) {
            endActiveTurn(eventKey);
            setBusyForChat(eventKey, false);
            // A turn that finishes in a chat the user isn't currently viewing
            // (different chat, or a chat in another workspace) leaves an unread
            // marker until they open it. The unread set is keyed by the
            // workspace-qualified bucket so it can't bleed across workspaces.
            const focusedKey = chatKey(activeWsId, activeChatIdRef.current);
            if (eventKey && eventKey !== focusedKey) {
              setUnreadChatIds((prev) =>
                prev[eventKey] ? prev : { ...prev, [eventKey]: true },
              );
            }
          }
          if (pendingHistoryReloadRef.current) {
            pendingHistoryReloadRef.current = false;
            reloadHistoryRef.current();
          }
          break;
        }
        case "state": {
          // State-only refresh fired mid-turn (e.g. when the agent calls
          // ``update_plan``). Update the snapshot so the plan panel can
          // re-render but DO NOT close the active turn or clear the busy
          // flag — the model is still streaming its reply.
          const next = data.state;
          if (next) {
            setState(next);
          }
          break;
        }
        case "turn_start": {
          startTurn(String(data.text ?? ""), eventKey);
          setBusyForChat(eventKey, true);
          break;
        }
        case "round_start": {
          startRound(eventKey);
          break;
        }
        case "round_end": {
          endRound(eventKey);
          break;
        }
        case "output": {
          appendSegment("step", String(data.text ?? ""), eventKey);
          break;
        }
        case "assistant": {
          appendSegment("answer", String(data.text ?? ""), eventKey);
          break;
        }
        case "confirm": {
          setConfirmRequest(event.data as ConfirmRequest);
          break;
        }
        case "browser_command": {
          // Backend tool wants the embedded browser to do something (navigate,
          // refresh, read a preview page, ...). For commands that show a page,
          // make sure the Browser tab is visible+active and the right panel is
          // open so the BrowserPanel is mounted to receive the command.
          const action = String(
            (event.data as Record<string, unknown>).action || "",
          );
          const cmdData = event.data as Record<string, unknown>;
          const fanOut = () => {
            for (const handler of browserCommandHandlersRef.current) {
              try {
                handler(cmdData);
              } catch {
                // A misbehaving handler must not break event dispatch.
              }
            }
          };
          if (action === "open" || action === "open_preview") {
            try {
              const prefs = loadRightPanelPrefs();
              const visible = prefs.visible.includes("browser")
                ? prefs.visible
                : [...prefs.visible, "browser"];
              const ordered = RIGHT_PANEL_TABS.filter((id) =>
                visible.includes(id),
              );
              saveRightPanelPrefs({ visible: ordered, active: "browser" });
              window.dispatchEvent(new Event("codewood.rightPanelTabs"));
            } catch {
              // localStorage may be unavailable; panel just won't switch tabs.
            }
            setPlanOpen(true);
            // Defer the fan-out so a just-mounted BrowserPanel has registered
            // its subscriber before the command is delivered (otherwise the
            // requestId-bearing command would be dropped and the tool times
            // out). A short delay is enough for React to flush the mount.
            window.setTimeout(fanOut, 120);
          } else {
            fanOut();
          }
          break;
        }
        case "request_user_input": {
          // Bucket per workspace-qualified chat so switching chats (or
          // workspaces) while one is pending doesn't drop the panel; the value
          // selector below picks the entry for the focused workspace+chat.
          const req = event.data as AskMoreInfoRequest;
          const ownerChat = String(req.chatId || chatId || "");
          if (!ownerChat) {
            break;
          }
          const ownerKey = chatKey(eventWsId, ownerChat);
          // Older backends may not send ``multiSelect``; default to
          // single-choice so the panel doesn't get stuck waiting for a
          // Submit click that the user has no reason to expect.
          const normalized: AskMoreInfoRequest = {
            ...req,
            chatId: ownerChat,
            multiSelect: Boolean(req.multiSelect),
          };
          setAskMoreInfoByChat((prev) => ({
            ...prev,
            [ownerKey]: normalized,
          }));
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
      // now in the chosen workspace as a SINGLE atomic backend op (switch +
      // create). Doing the workspace switch and chat creation as two separate
      // calls previously left an extra empty chat behind in the target
      // workspace, so we let ``newChat`` take the workspace id directly.
      let targetChatId = activeChatIdRef.current;
      if (draftModeRef.current) {
        const wsId = draftWorkspaceIdRef.current;
        const switchWs = Boolean(wsId && wsId !== activeWorkspaceIdRef.current);
        const newId = await client.newChat(switchWs ? wsId : "");
        if (newId) {
          targetChatId = newId;
          historyChatRef.current = newId;
          // Optimistically focus the new chat AND echo the user's message right
          // away instead of waiting for the backend's ``idle`` / ``turn_start``
          // SSE round-trip. Without the optimistic focus the view keeps
          // rendering the old/empty chat's bucket (looks like it "stays on the
          // welcome screen"); without the optimistic echo the message vanishes
          // until ``turn_start`` lands. ``startTurn`` reconciles the echo in
          // place when the authoritative event arrives, so there is no
          // duplicate. The optimistic bucket uses the SAME workspace id the
          // backend will tag the events with (the one we just switched to, or
          // the current one), so the keys match and reconciliation works.
          const echoWsId = switchWs
            ? wsId
            : wsId || activeWorkspaceIdRef.current;
          activeChatIdRef.current = newId;
          activeWorkspaceIdRef.current = echoWsId;
          // Force the view onto the new chat via the focus override (survives
          // any racing SSE ``setState``), then echo the message into its bucket.
          setFocusOverride({ chatId: newId, wsId: echoWsId });
          startOptimisticTurn(trimmed, chatKey(echoWsId, newId));
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
    [client, startOptimisticTurn],
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

  const answerAskMoreInfo = useCallback(
    async (answer: string) => {
      // Snapshot the active chat's pending request, dismiss the panel
      // optimistically, then forward the answer to the backend. We dismiss
      // first so a slow network round-trip doesn't leave a "live" panel
      // that the user could double-click.
      const key = chatKey(activeWorkspaceIdRef.current, activeChatId);
      const current = key ? askMoreInfoByChat[key] : undefined;
      if (!current) {
        return;
      }
      setAskMoreInfoByChat((prev) => {
        if (!prev[key]) {
          return prev;
        }
        const next = { ...prev };
        delete next[key];
        return next;
      });
      try {
        await client.answerAskMoreInfo(current.id, answer);
      } catch {
        // Network errors are swallowed; the backend will eventually
        // drain the queue on shutdown (delivering an empty answer that
        // pauses the task) and the user can re-issue the request.
      }
    },
    [askMoreInfoByChat, client, activeChatId],
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

  // Clears the live turns for a chat. The optional ``chatId`` is a BARE chat
  // id (public API used by views/commands); it's resolved against the focused
  // workspace into the composite bucket key. Defaults to the focused chat.
  const clearTurns = useCallback(
    (chatId?: string) =>
      clearLiveTurns(
        chatKey(
          activeWorkspaceIdRef.current,
          chatId ?? activeChatIdRef.current,
        ),
      ),
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
      // ``getChatHistory`` always returns the focused chat's history, so the
      // live-turn bucket to reconcile is the focused workspace's composite
      // key for ``cid``.
      const key = chatKey(activeWorkspaceIdRef.current, cid);
      setHistoryLoading(true);
      try {
        const page = await client.getChatHistory(undefined, INITIAL_HISTORY);
        // If the chat we're loading is still streaming a turn, that same
        // in-progress turn also appears as the trailing persisted history entry
        // (its user message, no final answer yet). Drop that trailing entry and
        // keep the live turn so the streaming content (and "Working…") survives
        // the switch; otherwise show full history and clear the settled turns.
        const live = turnsByChatRef.current[key] ?? [];
        const hasActive = live.some((tt) => tt.endedAt === null);
        const hasSettledLive = live.some((tt) => tt.endedAt !== null);
        if (hasActive) {
          // An in-progress turn is streaming for this chat (or an optimistic
          // first-message turn just opened for a freshly materialized draft
          // chat). It is not yet fully persisted, so keep the live turn and,
          // when the persisted page already carries its trailing duplicate,
          // drop that tail. A brand-new chat has no persisted history yet
          // (empty page) — keep the live turn untouched so the optimistically
          // echoed user message survives this reload instead of being cleared.
          setHistoryTurns(page.turns.length > 0 ? page.turns.slice(0, -1) : page.turns);
          setHistoryStart(page.start);
          setHistoryTotal(page.total);
          dropSettledLiveTurns(key);
        } else if (hasSettledLive && page.turns.length === 0) {
          // A background turn finished in this chat while it was unfocused —
          // its full output is captured in the live bucket — but the persisted
          // history came back empty (its disk flush hasn't landed yet, or a
          // cross-workspace persistence race). Keep the live turns visible
          // instead of clearing them, so the user still sees the reply on
          // switch-back rather than a blank chat. The next history reload
          // (after persistence settles) reconciles them.
          setHistoryStart(0);
          setHistoryTotal(0);
        } else {
          setHistoryTurns(page.turns);
          setHistoryStart(page.start);
          setHistoryTotal(page.total);
          clearLiveTurns(key);
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

  // Hydrate the per-chat ``askMoreInfo`` bucket from the snapshot the
  // backend serializes in ``state.askMoreInfo``. This lets the GUI
  // render the selection panel on chat load (or on refresh) even when
  // the original ``request_user_input`` SSE event was missed — most
  // importantly when a *different* backend process (e.g. the TUI)
  // triggered the prompt and this GUI's backend was never asked.
  useEffect(() => {
    const cid = state?.activeChatId ?? "";
    if (!cid) {
      return;
    }
    const persisted = state?.askMoreInfo;
    setAskMoreInfoByChat((prev) => {
      const existing = prev[cid];
      if (!persisted) {
        if (!existing) {
          return prev;
        }
        // Backend says no pending prompt for this chat -> drop stale.
        const next = { ...prev };
        delete next[cid];
        return next;
      }
      if (
        existing &&
        existing.id === persisted.id &&
        existing.question === persisted.question &&
        existing.options.length === persisted.options.length &&
        existing.options.every((o, i) => o === persisted.options[i]) &&
        existing.multiSelect === Boolean(persisted.multiSelect)
      ) {
        return prev;
      }
      return {
        ...prev,
        [cid]: {
          id: String(persisted.id || ""),
          question: String(persisted.question || ""),
          options: Array.isArray(persisted.options)
            ? persisted.options.map((o) => String(o))
            : [],
          multiSelect: Boolean(persisted.multiSelect),
          chatId: cid,
        },
      };
    });
  }, [state?.activeChatId, state?.askMoreInfo]);

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
      clearLiveTurns(chatKey(wsId, chatId));
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
      // Editing the last user message deletes that turn (and any pending
      // request_user_input clarification it spawned). Drop the chat's pending
      // request_user_input bucket up front so the selection panel doesn't flash
      // back in while the backend processes the edit and clears its own
      // pending state.
      if (activeChatId) {
        const activeBucket = chatKey(
          activeWorkspaceIdRef.current,
          activeChatId,
        );
        const pending = askMoreInfoByChat[activeBucket];
        setAskMoreInfoByChat((prev) => {
          if (!(activeBucket in prev)) {
            return prev;
          }
          const next = { ...prev };
          delete next[activeBucket];
          return next;
        });
        // The turn that surfaced the request_user_input prompt is still blocked on
        // the backend reply queue. Clearing the panel alone leaves that turn
        // hung with ``busy`` set — so the composer button would revert to the
        // interrupt/stop affordance even though the user is now just editing.
        // Drain the prompt with an empty answer so the orphaned turn unwinds
        // and emits ``idle``, then optimistically clear busy here so the
        // button flips back to "send" without waiting for the round-trip.
        if (pending) {
          setBusyForChat(activeBucket, false);
          try {
            await client.answerAskMoreInfo(pending.id, "");
          } catch {
            // Best-effort: the backend drains pending prompts on shutdown.
          }
        }
      }
      pendingHistoryReloadRef.current = true;
      await client.sendInput(`/chat edit ${index}`);
    },
    [client, clearTurns, activeChatId, askMoreInfoByChat, setBusyForChat],
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

  const openSettings = useCallback((page?: string) => {
    setSettingsInitialPage(page ?? null);
    setSettingsOpen(true);
  }, []);
  const closeSettings = useCallback(() => {
    setSettingsOpen(false);
    setSettingsInitialPage(null);
  }, []);
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
    // Anchor the native file picker at the active workspace's root so the user
    // doesn't see pywebview's last-remembered (often unrelated) location after
    // switching workspaces. The dialog itself still lets the user navigate
    // anywhere; this is just the starting directory.
    const workspaceRoot = stateRef.current?.workspace?.root ?? "";
    try {
      const result = await api.pick_files(workspaceRoot);
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
    unreadChatIds,
    connected,
    now,
    confirmRequest,
    askMoreInfo: activeKey ? (askMoreInfoByChat[activeKey] ?? null) : null,
    theme,
    lang,
    uiPrefs,
    workspaceChats,
    expandedWorkspaceIds,
    settingsOpen,
    settingsInitialPage,
    aboutOpen,
    planOpen,
    togglePlan: () => setPlanOpen((v) => !v),
    t,
    setTheme,
    setGuiLanguage,
    setBackgroundImage,
    clearBackgroundImage,
    setBackgroundOpacity,
    backgroundImageUrl,
    pasteImage,
    chatImageUrl,
    subscribeBrowserCommand,
    sendBrowserResult,
    previewHtml,
    previewHtmlInBrowser,
    resolveBackendUrl,
    sendInput,
    runCommand,
    interrupt,
    answerConfirm,
    answerAskMoreInfo,
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
    getGeneralConfig: () => client.getGeneralConfig(),
    saveGeneralConfig: (general: Partial<GeneralConfig>) =>
      client.saveGeneralConfig(general),
    getMcpOverview: () => client.getMcpOverview(),
    getMcpServerDetails: (name: string) => client.getMcpServerDetails(name),
    setMcpServerEnabled: (name: string, enabled: boolean) =>
      client.setMcpServerEnabled(name, enabled),
    setMcpToolEnabled: (server: string, tool: string, enabled: boolean) =>
      client.setMcpToolEnabled(server, tool, enabled),
    setMcpToolsEnabled: (server: string, tools: string[], enabled: boolean) =>
      client.setMcpToolsEnabled(server, tools, enabled),
    getCompletionCatalog: () => client.getCompletionCatalog(),
    getMcpServerConfig: (name: string) => client.getMcpServerConfig(name),
    addMcpServer: (name: string, config: McpServerConfigEntry) =>
      client.addMcpServer(name, config),
    updateMcpServer: (
      originalName: string,
      name: string,
      config: McpServerConfigEntry,
    ) => client.updateMcpServer(originalName, name, config),
    deleteMcpServer: (name: string) => client.deleteMcpServer(name),
    getSubAgentsOverview: () => client.getSubAgentsOverview(),
    saveSubAgent: (payload) => client.saveSubAgent(payload),
    deleteSubAgent: (name: string) => client.deleteSubAgent(name),
    setSubAgentEnabled: (name: string, enabled: boolean) =>
      client.setSubAgentEnabled(name, enabled),
    setPlanMode: (enabled: boolean) => client.setPlanMode(enabled),
    searchWorkspaceFiles: (query: string, limit = 10) =>
      client.searchWorkspaceFiles(query, activeWorkspaceIdRef.current, limit),
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
