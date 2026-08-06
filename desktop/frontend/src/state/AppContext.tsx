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
  ChatSummary,
  CompactNoticeData,
  ConfirmAllowlist,
  CompletionCatalog,
  ConfirmRequest,
  SecurityAuditConfig,
  FileChangeSummary,
  GeneralConfig,
  HistoryTurn,
  McpServerConfigEntry,
  McpServerDetails,
  McpServerSummary,
  RetryCountdownState,
  SegmentKind,
  ServerEvent,
  SkillSummary,
  SubAgentConfig,
  SubAgentMessage,
  SubAgentSession,
  SubAgentsOverview,
  Turn,
  TurnRound,
  WorkspaceChatSummary,
} from "../api/types";
import { normalizeLang, translate, type Lang } from "../i18n";
import { MODEL_PRESETS } from "../components/modelPresets";
import {
  buildFallbackToolRound,
  buildFallbackToolRoundFromCall,
} from "../utils/subagentToolRounds";
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
  type RightPanelTabId,
} from "./rightPanelTabs";
import { IMG_CLOSE, IMG_OPEN } from "../utils/imageRefs";
import { hostApi } from "../utils/hostApi";

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

function parseChatKey(key: string): { wsId: string; chatId: string } {
  const idx = key.indexOf("\0");
  if (idx > 0) {
    return { wsId: key.slice(0, idx), chatId: key.slice(idx + 1) };
  }
  return { wsId: "", chatId: key };
}

// Resolve the reasoning-effort levels a model supports from the per-selector
// map the backend ships in ``state.model.reasoningEffortsBySelector``. Returns
// ``null`` when unknown (e.g. backend predates the map or model isn't listed),
// so callers can leave the existing list untouched rather than clearing it.
function resolveReasoningEffortsForModel(
  selector: string,
  bySelector?: Record<string, string[]>,
): string[] | null {
  if (!bySelector || !selector) {
    return null;
  }
  const exact = bySelector[selector];
  if (Array.isArray(exact)) {
    return exact;
  }
  const lower = selector.toLowerCase();
  for (const [key, efforts] of Object.entries(bySelector)) {
    if (key.toLowerCase() === lower && Array.isArray(efforts)) {
      return efforts;
    }
  }
  return null;
}

// Build the optimistic ``model`` patch to apply when the user switches models:
// update ``current`` and swap the reasoning-effort list to the newly selected
// model's supported levels, dropping any selected effort the new model doesn't
// support. Returns null when the new model's efforts are unknown, so callers
// can leave the existing list untouched.
function buildModelChangePatch(
  selector: string,
  prevModel?: {
    current: string;
    reasoningEffort?: string;
    reasoningEfforts?: string[];
    reasoningEffortsBySelector?: Record<string, string[]>;
  },
): { current: string; reasoningEfforts: string[]; reasoningEffort: string } | null {
  const efforts = resolveReasoningEffortsForModel(
    selector,
    prevModel?.reasoningEffortsBySelector,
  );
  if (efforts === null) {
    return null;
  }
  const prevEffort = (prevModel?.reasoningEffort ?? "").trim().toLowerCase();
  const supported = efforts.map((e) => e.toLowerCase());
  const reasoningEffort = prevEffort && supported.includes(prevEffort)
    ? prevModel?.reasoningEffort ?? ""
    : "";
  return { current: selector, reasoningEfforts: efforts, reasoningEffort };
}

interface AppContextValue {
  state: AppState | null;
  activeWorkspaceId: string;
  activeChatId: string;
  activeChats: ChatSummary[];
  client: ApiClient;
  turns: Turn[];
  historyTurns: HistoryTurn[];
  historyStart: number;
  historyTotal: number;
  historyLoading: boolean;
  busy: boolean;
  /** Per-chat busy flags so the sidebar can mark every running chat. */
  busyByChat: Record<string, boolean>;
  /** Start time of each chat's currently running turn, keyed by chatKey. */
  runningChatStartedAtByChat: Record<string, number>;
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
  todoDockVisible: boolean;
  setTodoDockVisible: (v: boolean) => void;
  zoomLevel: number;
  setZoomLevel: (level: number) => void;
  t: (key: string, params?: Record<string, string | number>) => string;
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
  saveDroppedFile: (
    dataUrl: string,
    fileName: string,
  ) => Promise<{ path: string; name: string } | null>;
  chatImageUrl: (path: string, workspaceId?: string) => string;
  mcpIconUrl: (server: string, icon: string) => string;
  subscribeBrowserCommand: (
    handler: (cmd: Record<string, unknown>) => void,
  ) => () => void;
  sendBrowserResult: (
    requestId: string,
    result: Record<string, unknown>,
  ) => Promise<void>;
  previewHtml: (html: string) => Promise<{ url: string } | null>;
  previewHtmlInBrowser: (html: string) => Promise<boolean>;
  /** Show the embedded Browser tab (visible + active, panel open). */
  showBrowserTab: () => void;
  /** Hide the embedded Browser tab (its "x" close button). */
  hideBrowserTab: () => void;
  /** Whether the embedded console dock is open below the message area. */
  consoleOpen: boolean;
  /** Whether the embedded Browser tab is visible in the right panel. */
  browserOpen: boolean;
  /** Open the embedded console dock (View menu / Console options). */
  showConsole: () => void;
  /** Hide the embedded console dock. */
  hideConsole: () => void;
  /** Effective console options (font + buffer) from server state. */
  consoleOptions: { fontFamily: string; bufferLines: number };
  /** Subscribe to live SSE output for a console session. Returns an
   *  unsubscribe function. The callback receives base64-encoded PTY bytes. */
  subscribeConsoleOutput: (
    id: string,
    handler: (b64: string, end: boolean) => void,
  ) => () => void;
  /** Consume and clear the pending auto-opened console info (from
   *  console_open SSE). Returns null if none pending. */
  consumePendingAutoConsole: () => { id: string; title: string; kind: string } | null;
  /** Fetch a console session's retained output (base64) for repaint. */
  attachConsole: (id: string) => Promise<string>;
  /** Send input to a console session (HTTP POST; output arrives via SSE). */
  consoleInput: (id: string, data: string) => Promise<void>;
  /** Notify the backend PTY of a terminal resize. */
  consoleResize: (id: string, cols: number, rows: number) => Promise<void>;
  /** Console session lifecycle (REST; the WS only carries byte I/O). */
  openConsole: (
    kind: string,
  ) => Promise<{ id: string; title: string; kind: string } | null>;
  closeConsole: (id: string) => Promise<boolean>;
  activateConsole: (id: string) => Promise<boolean>;
  setConsoleOptions: (options: {
    fontFamily: string;
    bufferLines: number;
  }) => Promise<boolean>;
  resolveBackendUrl: (url: string) => string;
  sendInput: (text: string) => Promise<void>;
  runCommand: (command: string) => Promise<void>;
  interrupt: () => Promise<void>;
  /** Pending input queue for the active chat (waiting while model is busy). */
  pendingInputs: string[];
  /** Whether the pending queue is in auto-send mode (vs. manual-start-on-restart). */
  pendingAutoSend: boolean;
  /** Manually start sending the pending input queue (when auto-send is off). */
  startPendingInputs: () => Promise<void>;
  /** Cancel a pending input at the given index. Returns the removed text. */
  cancelPendingInput: (index: number) => string | null;
  /** Pause the running task and send the pending input at the given index
   * immediately, removing it from the queue. */
  sendPendingInputNow: (index: number) => Promise<void>;
  compactContext: () => Promise<{ ok: boolean; text?: string }>;
  compactNotice: CompactNoticeData | null;
  /** Live 429/503 retry countdown per chat (workspace-qualified keys). */
  retryCountdownByChat: Record<string, RetryCountdownState | null>;
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
  /** Report whether the draft composer currently holds content (text/attachments). */
  setDraftHasContent: (has: boolean) => void;
  /** Delete a chat (may leave the workspace chat-less, entering compose mode). */
  deleteChat: (chatId: string, workspaceId?: string) => Promise<void>;
  forkChat: (index: number) => Promise<void>;
  editChat: (index: number) => Promise<void>;
  loadOlderHistory: () => Promise<void>;
  openWorkspaceInExplorer: (id: string) => Promise<boolean>;
  deleteWorkspace: (id: string) => Promise<boolean>;
  toggleWorkspacePin: (id: string) => void;
  toggleChatPin: (id: string) => void;
  reorderWorkspace: (workspaceId: string, beforeId: string | null) => void;
  toggleChatArchive: (key: string) => Promise<void>;
  archiveChats: (keys: string[]) => Promise<void>;
  setModel: (selector: string) => Promise<void>;
  setReasoning: (level: string) => Promise<void>;
  getModelsConfig: () => Promise<unknown[]>;
  saveModelsConfig: (providers: unknown[]) => Promise<boolean>;
  fetchProviderModels: (params: {
    base_url: string;
    api_key?: string;
    api_mode?: string;
    port?: number;
    context_length_attr_name?: string;
  }) => Promise<{
    ok: boolean;
    models?: { name: string; context_window?: number }[];
    error?: string;
  }>;
  getModelPresets: () => Promise<unknown[]>;
  getGeneralConfig: () => Promise<GeneralConfig | null>;
  saveGeneralConfig: (general: Partial<GeneralConfig>) => Promise<boolean>;
  getSecurityAuditConfig: () => Promise<SecurityAuditConfig | null>;
  saveSecurityAuditConfig: (audit: Partial<SecurityAuditConfig>) => Promise<boolean>;
  getModelSelectors: () => Promise<string[]>;
  getConfirmAllowlist: () => Promise<ConfirmAllowlist | null>;
  saveConfirmAllowlist: (
    allowlist: ConfirmAllowlist,
  ) => Promise<{ ok: boolean; allowlist?: ConfirmAllowlist }>;
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
  getSkillsOverview: () => Promise<SkillSummary[]>;
  setSkillEnabled: (skillId: string, enabled: boolean) => Promise<boolean>;
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
  pickAndOpenFolder: () => Promise<void>;
  pickFiles: () => Promise<string[]>;
  /** The currently viewed sub-agent session (null = viewing main chat). */
  activeSubAgentSession: SubAgentSession | null;
  /** Whether a sub-agent session is being loaded. */
  subAgentSessionLoading: boolean;
  /** Enter a sub-agent session view. */
  enterSubAgentSession: (sessionId: string) => Promise<void>;
  /** Exit the sub-agent session view and return to main chat. */
  exitSubAgentSession: () => void;
  /** Sub-agent session ID pending auto-expand when returning to the main chat ("" = none). */
  pendingExpandSubAgentId: string;
}

interface OptimisticModelState {
  current: string;
  reasoningEffort?: string;
  reasoningEfforts?: string[];
}

interface HostApiBridge {
  pick_folder?: () => string | Promise<string>;
  pick_files?: (directory?: string) => string[] | Promise<string[]>;
  pick_image?: (directory?: string) => string | Promise<string>;
}

interface OptimisticChatFocus {
  wsId: string;
  chatId: string;
  name: string;
}

const AppContext = createContext<AppContextValue | null>(null);

const THEME_STORAGE_KEY = "codewood.theme";
const CONSOLE_OPEN_KEY = "codewood.consoleOpen";
// Stable empty reference so the exposed `turns` doesn't change identity when a
// chat has no live turns (avoids needless re-renders / effect churn).
const EMPTY_TURNS: Turn[] = [];
const CMD_PROMPT_BEGIN = "\uE004";
const CMD_PROMPT_END = "\uE005";
const CMD_OUTPUT_BEGIN = "\uE000";
const CMD_OUTPUT_END = "\uE001";
const DIFF_BEGIN = "\uE006";

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

function buildCompactNoticeData(
  titleOrText: string,
  body = "",
  extras?: Pick<CompactNoticeData, "stage" | "mode">,
): CompactNoticeData {
  const title = String(titleOrText || "").trim();
  const detail = String(body || "").trim();
  return {
    title,
    body: detail,
    text: detail ? `${title}\n\n${detail}` : title,
    stage: extras?.stage,
    mode: extras?.mode,
  };
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

  // Keep a lightweight, authenticated renderer-to-app-log channel available
  // for future GUI diagnostics.  A single startup marker verifies that the
  // loaded frontend and the backend endpoint are from the same launch.
  useEffect(() => {
    void (client as any).logFrontendTrace?.("frontend-started", { source: "desktop-gui" });
  }, [client]);

  const [state, setState] = useState<AppState | null>(null);
  // Live (in-session) turns are tracked per chat so a chat's in-progress work
  // is preserved when the user switches to another chat. The active chat's
  // list is exposed as `turns` below.
  const [_turnsByChat, _rawSetTurnsByChat] = useState<Record<string, Turn[]>>({});
  // Mirror of turnsByChat readable synchronously (e.g. while loading history we
  // must know whether a chat still has an in-progress live turn).  Updated
  // synchronously inside every state mutation so async callbacks always see
  // the committed render's snapshot — never a stale pre-render snapshot.
  const turnsByChatRef = useRef<Record<string, Turn[]>>({});
  const setTurnsByChat = useCallback(
    (arg: React.SetStateAction<Record<string, Turn[]>>) => {
      if (typeof arg === "function") {
        _rawSetTurnsByChat((prev) => {
          const next = arg(prev);
          turnsByChatRef.current = next;
          return next;
        });
      } else {
        _rawSetTurnsByChat(arg);
        turnsByChatRef.current = arg;
      }
    },
    [],
  );
  const turnsByChat = _turnsByChat;
  const [historyTurns, setHistoryTurns] = useState<HistoryTurn[]>([]);
  const [historyStart, setHistoryStart] = useState(0);
  const [historyTotal, setHistoryTotal] = useState(0);
  const [historyLoading, setHistoryLoading] = useState(false);
  const historyChatRef = useRef<string>("\u0000");
  // Per-chat busy flags so each running chat shows its own state.
  const [busyByChat, setBusyByChat] = useState<Record<string, boolean>>({});
  const busyByChatRef = useRef<Record<string, boolean>>({});
  // Per-chat pending inputs: messages typed while the model was busy, waiting
  // to be sent when the current turn finishes. Keyed by workspace-qualified key.
  const [pendingInputsByChat, setPendingInputsByChat] = useState<Record<string, string[]>>({});
  const pendingInputsByChatRef = useRef<Record<string, string[]>>({});
  // Per-chat auto-send flag: true when pending inputs should be auto-dequeued
  // on idle (normal operation). False on restart (user must manually start).
  const [pendingAutoSendByChat, setPendingAutoSendByChat] = useState<Record<string, boolean>>({});
  const pendingAutoSendByChatRef = useRef<Record<string, boolean>>({});
  // When a message is jump-sent the running task is paused; the idle that
  // closes THAT paused turn must NOT drain the queue — the remaining messages
  // wait until the jumped task finishes, then auto-send resumes one per turn.
  // Keyed by workspace-qualified chat key; consumed by the first idle after
  // the jump (no state needed — read/written by the SSE handler only).
  const suppressAutoSendOnceRef = useRef<Record<string, boolean>>({});
  useEffect(() => {
    pendingInputsByChatRef.current = pendingInputsByChat;
    pendingAutoSendByChatRef.current = pendingAutoSendByChat;
  }, [pendingInputsByChat, pendingAutoSendByChat]);
  // Chats whose turn finished while the user was looking at a different chat.
  // They stay flagged as unread (blue dot in the sidebar) until opened. The
  // flag is persisted by the backend (``hasUnread`` on the chat summary) and
  // set only when a task completes in a non-viewed chat, so we DERIVE this set
  // from the server snapshot instead of computing it from SSE event timing —
  // the old heuristic could flag a chat the user was watching when a late idle
  // event arrived after they had already switched away. Keyed by the
  // workspace-qualified composite (same convention as ``busyByChat``).
  const [connected, setConnected] = useState(false);
  // Bumped whenever the ApiClient is rebuilt against a new backend endpoint
  // (crash-restart); re-runs the event-stream effect with the fresh client.
  const [connGeneration, setConnGeneration] = useState(0);
  const [now, setNow] = useState(() => Date.now());
  const [confirmRequestByChat, setConfirmRequestByChat] = useState<
    Record<string, ConfirmRequest>
  >({});
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
  const workspaceChatsRef = useRef<Record<string, WorkspaceChatSummary[]>>({});
  useEffect(() => {
    workspaceChatsRef.current = workspaceChats;
  }, [workspaceChats]);
  // Unread blue dots, derived from the backend-persisted ``hasUnread`` flags
  // on the active workspace's state snapshot and the per-workspace chat cache.
  // The ACTIVE workspace is sourced ONLY from ``state.chats`` (authoritative);
  // its ``workspaceChats`` copy is a mirrored cache that a late
  // refreshWorkspaceChats response could overwrite with a stale ``true``, which
  // would otherwise resurrect a dot the user already cleared. Other workspaces
  // have no live snapshot, so they keep using the cache.
  const unreadChatIds = useMemo<Record<string, boolean>>(() => {
    const map: Record<string, boolean> = {};
    const activeWsId = state?.workspace?.id ?? "";
    if (state && activeWsId && Array.isArray(state.chats)) {
      for (const ch of state.chats) {
        if (ch && ch.hasUnread) {
          map[chatKey(activeWsId, ch.id)] = true;
        }
      }
    }
    for (const [wsId, list] of Object.entries(workspaceChats)) {
      if (wsId === activeWsId) {
        continue;
      }
      for (const ch of list) {
        if (ch && ch.hasUnread) {
          map[chatKey(wsId, ch.id)] = true;
        }
      }
    }
    return map;
  }, [state, workspaceChats]);
  const [expandedWorkspaceIds, setExpandedWorkspaceIds] = useState<string[]>([]);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settingsInitialPage, setSettingsInitialPage] = useState<string | null>(null);
  const [aboutOpen, setAboutOpen] = useState(false);
  const [zoomLevel, setZoomLevel] = useState<number>(() => {
    try { const v = Number(window.localStorage.getItem("codewood.zoomLevel")); return v > 0 ? v : 1; } catch { return 1; }
  });
  const [planOpen, setPlanOpen] = useState(false);
  const [todoDockVisible, setTodoDockVisible] = useState(false);
  const [consoleOpen, setConsoleOpen] = useState<boolean>(() => {
    try {
      return window.localStorage.getItem(CONSOLE_OPEN_KEY) === "1";
    } catch {
      return false;
    }
  });
  const [browserOpen, setBrowserOpen] = useState<boolean>(() => {
    try {
      return loadRightPanelPrefs().visible.includes("browser");
    } catch {
      return false;
    }
  });
  useEffect(() => {
    const onChange = () => {
      try {
        setBrowserOpen(loadRightPanelPrefs().visible.includes("browser"));
      } catch {
        setBrowserOpen(false);
      }
    };
    window.addEventListener("codewood.rightPanelTabs", onChange);
    return () => window.removeEventListener("codewood.rightPanelTabs", onChange);
  }, []);
  // Subscribers for backend-originated browser commands (the BrowserPanel
  // registers one while mounted). A Set so mount/unmount add/remove cleanly.
  const browserCommandHandlersRef = useRef<
    Set<(cmd: Record<string, unknown>) => void>
  >(new Set());
  // Subscribers for backend console output, keyed by session id. Each mounted
  // ConsoleTerminal registers one so live PTY output (delivered over SSE since
  // WebView2's file:// origin forbids ws://) reaches the right terminal.
  const consoleOutputHandlersRef = useRef<
    Map<string, Set<(b64: string, end: boolean) => void>>
  >(new Map());
  // Holds the last auto-opened console info from a backend console_open SSE
  // event. The ConsolePanel checks and consumes this when the dock opens.
  const pendingAutoConsoleRef = useRef<{
    id: string;
    title: string;
    kind: string;
  } | null>(null);
  // Draft (compose) mode: "New Chat" shows the empty composer without creating
  // a chat yet; the chat is materialized only when the first message is sent.
  // ``draftWorkspaceId`` is the workspace the new chat will be created in.
  const [draftMode, setDraftMode] = useState(false);
  const [draftWorkspaceId, setDraftWorkspaceId] = useState<string>("");
  const draftModeRef = useRef(false);
  const draftWorkspaceIdRef = useRef<string>("");
  // Model the user chose while in draft (compose) mode, applied when the chat
  // is materialized on first send. ``""`` means "inherit from last chat".
  const draftModelRef = useRef<string>("");
  // Reasoning effort the user chose while in draft (compose) mode, applied
  // when the chat is materialized on first send. ``""`` means "inherit".
  const draftReasoningRef = useRef<string>("");
  // Whether the pending draft (compose) session currently holds any typed
  // content (text segments or attachments). Kept in a ref (no rendering
  // impact) and reported by ChatView so ``newChat`` can distinguish "returning
  // to an existing draft" (preserve its model/reasoning) from "starting a
  // fresh draft" (reset to inherit from the last chat).
  const draftHasContentRef = useRef(false);
  // Reset the pending draft model/reasoning when leaving draft mode so they
  // never leak into a later compose session.
  const resetDraftSelectionRef = useCallback(() => {
    draftModelRef.current = "";
    draftReasoningRef.current = "";
  }, []);
  // ChatView reports whether the draft composer holds any content so ``newChat``
  // can decide whether a re-entered draft is a fresh session.
  const setDraftHasContent = useCallback((has: boolean) => {
    draftHasContentRef.current = has;
  }, []);
  // Apply the draft session's selected model/reasoning to the local model
  // state (mirrors materializeDraftChat's application). When ``clear`` is true
  // the refs are also reset so the selection is consumed exactly once.
  const applyDraftSelectionToState = useCallback((clear: boolean) => {
    const draftModel = draftModelRef.current;
    const draftReasoning = draftReasoningRef.current;
    if (clear) {
      draftModelRef.current = "";
      draftReasoningRef.current = "";
    }
    if (!draftModel && !draftReasoning) {
      return;
    }
    setState((prev) => {
      if (!prev) return prev;
      let nextModel = { ...prev.model };
      if (draftModel) {
        const patch = buildModelChangePatch(draftModel, prev.model);
        nextModel = {
          ...nextModel,
          current: draftModel,
          ...(patch
            ? {
                reasoningEfforts: patch.reasoningEfforts,
                reasoningEffort: patch.reasoningEffort,
              }
            : {}),
        };
      }
      if (draftReasoning) {
        nextModel = { ...nextModel, reasoningEffort: draftReasoning };
      }
      return { ...prev, model: nextModel };
    });
  }, []);
  // Sub-agent session viewer state
  const [activeSubAgentSession, setActiveSubAgentSession] = useState<SubAgentSession | null>(null);
  const [subAgentSessionLoading, setSubAgentSessionLoading] = useState(false);
  const activeSubAgentSessionRef = useRef<SubAgentSession | null>(null);
  const subAgentViewingRef = useRef(false);
  const subAgentCacheRef = useRef<Record<string, SubAgentSession>>({});
  const subAgentThinkingStartRef = useRef<Record<string, number>>({});
  const [pendingExpandSubAgentId, setPendingExpandSubAgentId] = useState<string>("");

  // Helper: apply a sub-agent session update from SSE handlers. Always updates
  // the live ref and cache; only pushes to React state when the user is viewing.
  const applySubAgentSession = useCallback((updated: SubAgentSession) => {
    activeSubAgentSessionRef.current = updated;
    subAgentCacheRef.current[updated.id] = updated;
    if (subAgentViewingRef.current) {
      setActiveSubAgentSession(updated);
    }
  }, []);
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
  const pendingModelConfigRef = useRef<Promise<void>>(Promise.resolve());
  const stateRef = useRef<AppState | null>(null);
  // Track the most recently requested focus workspace so idle/state events
  // from it can bypass the background-event guard during a focus switch.
  const pendingFocusWsIdRef = useRef<string>("");
  // Track the composite key of the currently streaming chat so idle/state
  // events during a streaming turn can be blocked (prevents React re-render
  // side effects from disrupting in-progress content accumulation).
  const streamingKeyRef = useRef<string>("");
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
  const [optimisticModelByChat, setOptimisticModelByChat] = useState<
    Record<string, OptimisticModelState>
  >({});
  const optimisticModelByChatRef = useRef<Record<string, OptimisticModelState>>({});
  useEffect(() => {
    optimisticModelByChatRef.current = optimisticModelByChat;
  }, [optimisticModelByChat]);
  const [optimisticChatFocus, setOptimisticChatFocus] =
    useState<OptimisticChatFocus | null>(null);
  const [compactNoticeState, setCompactNoticeState] = useState<{
    chatKey: string;
    notice: CompactNoticeData | null;
    version: number;
  }>({ chatKey: "", notice: null, version: 0 });
  // Live 429/503 retry countdown keyed by workspace-qualified chat key. The
  // backend publishes one tick per second while it backs off before the next
  // retry (3s, then 2^n capped at 60s, forever); the UI renders the latest
  // tick below the last message and clears it on the ``done`` tick.
  const [retryCountdownByChat, setRetryCountdownByChat] = useState<
    Record<string, RetryCountdownState | null>
  >({});
  useEffect(() => {
    const wsReady = (state?.workspace.id ?? "") === (focusOverride?.wsId ?? "");
    const chatReady =
      (state?.activeChatId ?? "") === (focusOverride?.chatId ?? "") &&
      Boolean(state?.chats?.some((chat) => chat.id === focusOverride?.chatId));
    if (focusOverride && wsReady && chatReady) {
      setFocusOverride(null);
    }
  }, [state?.workspace.id, state?.activeChatId, state?.chats, focusOverride]);
  useEffect(() => {
    if (!optimisticChatFocus) {
      return;
    }
    const wsReady = (state?.workspace.id ?? "") === optimisticChatFocus.wsId;
    const chatReady =
      (state?.activeChatId ?? "") === optimisticChatFocus.chatId &&
      Boolean(state?.chats?.some((chat) => chat.id === optimisticChatFocus.chatId));
    if (wsReady && chatReady) {
      setOptimisticChatFocus(null);
    }
  }, [state?.workspace.id, state?.activeChatId, state?.chats, optimisticChatFocus]);

  const selectedWorkspaceId =
    focusOverride?.wsId ?? optimisticChatFocus?.wsId ?? state?.workspace.id ?? "";
  const selectedChatId =
    focusOverride?.chatId ?? optimisticChatFocus?.chatId ?? state?.activeChatId ?? "";
  const activeChatId = selectedChatId;
  useEffect(() => {
    activeChatIdRef.current = activeChatId;
  }, [activeChatId]);

  // Auto-show the todo dock only when the plan content actually changes
  // (a new plan was generated) within the same chat.  Comparing a hash of
  // the plan array avoids a false trigger when the plan is temporarily
  // cleared and restored during a round transition without a real
  // update_plan call.
  const planAutoOpenRef = useRef<{ chatId: string; hash: string }>({
    chatId: "",
    hash: "",
  });
  const planHash = useMemo(
    () => JSON.stringify(state?.plan?.plan ?? []),
    [state?.plan?.plan],
  );
  const activePlanLen = state?.plan?.plan?.length ?? 0;
  useEffect(() => {
    const prev = planAutoOpenRef.current;
    const sameChat = prev.chatId === activeChatId;
    const planChanged = prev.hash !== planHash;
    const hasPlan = activePlanLen > 0;
    const initialLoad = !prev.chatId;
    if ((sameChat || initialLoad) && planChanged && hasPlan) {
      setTodoDockVisible(true);
    }
    planAutoOpenRef.current = { chatId: activeChatId, hash: planHash };
  }, [activeChatId, activePlanLen, planHash]);
  useEffect(() => {
    activeWorkspaceIdRef.current = state?.workspace.id ?? "";
  }, [state?.workspace.id]);

  // Live turn / busy / ask maps are keyed by a WORKSPACE-QUALIFIED composite
  // (``workspaceId\x00chatId``), not the bare chat id: chat ids repeat across
  // workspaces (``chat-1``, ``chat-2``, … per workspace), so a bare-id bucket
  // would make a chat in one workspace render another workspace's same-id
  // chat's turns/busy/messages after a focus switch. ``chatKey`` builds the
  // composite; an empty workspace id degrades to the bare id (single-workspace
  // / legacy behavior unchanged).
  const activeWorkspaceId = selectedWorkspaceId;
  const activeKey = chatKey(activeWorkspaceId, activeChatId);
  const optimisticModel = activeKey ? optimisticModelByChat[activeKey] : undefined;
  const displayState = useMemo(() => {
    if (draftMode || !state) {
      return state;
    }
    const selectedChatModel = (() => {
      const sourceChats =
        selectedWorkspaceId === (state.workspace.id ?? "")
          ? state.chats
          : (workspaceChats[selectedWorkspaceId] ?? []);
      const selected = sourceChats.find((chat) => chat.id === activeChatId) as
        | (Partial<ChatSummary> & { id: string })
        | undefined;
      return selected?.model;
    })();
    // The displayed model always follows the focused chat's recorded model
    // when one exists. This is the authoritative per-chat truth (the backend's
    // ``model.current`` is itself derived from the same chat record), and it
    // keeps the composer consistent even when a state event for a busy/streaming
    // chat is stale or dropped; only when the chat has no recorded model do we
    // fall back to the backend's current selector.
    const currentModel =
      optimisticModel?.current ??
      (selectedChatModel || state.model.current);
    const currentReasoningEffort =
      optimisticModel?.reasoningEffort !== undefined
        ? optimisticModel.reasoningEffort
        : state.model.reasoningEffort;
    const currentReasoningEfforts =
      optimisticModel?.reasoningEfforts ?? state.model.reasoningEfforts;
    return {
      ...state,
      model: {
        ...state.model,
        current: currentModel,
        ...(currentReasoningEffort !== undefined
          ? { reasoningEffort: currentReasoningEffort }
          : {}),
        ...(currentReasoningEfforts
          ? { reasoningEfforts: currentReasoningEfforts }
          : {}),
      },
      chats: state.chats.map((chat) =>
        chat.id === activeChatId
          ? { ...chat, model: currentModel }
          : chat,
      ),
    };
  }, [state, optimisticModel, activeChatId, draftMode, selectedWorkspaceId, workspaceChats]);
  const compactNotice =
    activeKey && compactNoticeState.chatKey === activeKey
      ? compactNoticeState.notice
      : null;
  // The active chat's live turns / busy flag are what the chat view renders.
  const turns = turnsByChat[activeKey] ?? EMPTY_TURNS;
  const busy = busyByChat[activeKey] ?? false;
  const pendingInputs = pendingInputsByChat[activeKey] ?? [];
  const pendingAutoSend = pendingAutoSendByChat[activeKey] ?? false;
  const runningChatStartedAtByChat = useMemo(() => {
    const out: Record<string, number> = {};
    for (const [key, list] of Object.entries(turnsByChat)) {
      const last = list[list.length - 1];
      if (last && last.endedAt === null) {
        out[key] = last.startedAt;
      }
    }
    return out;
  }, [turnsByChat]);
  const anyBusy = useMemo(
    () => Object.values(busyByChat).some(Boolean),
    [busyByChat],
  );
  // When set, the next `idle` event reloads chat history even if the active
  // chat id is unchanged (e.g. after `/chat edit` truncates the conversation).
  const pendingHistoryReloadRef = useRef(false);
  const reloadHistoryRef = useRef<() => void>(() => {});

  const lang = useMemo(() => normalizeLang(state?.language), [state?.language]);
  const t = useCallback(
    (key: string, params?: Record<string, string | number>) =>
      translate(lang, key, params),
    [lang],
  );
  const applyOptimisticModelOverride = useCallback((next: AppState | null | undefined) => {
    if (!next) {
      return next ?? null;
    }
    const key = chatKey(next.workspace?.id ?? "", next.activeChatId ?? "");
    const optimistic = key ? optimisticModelByChatRef.current[key] : undefined;
    if (!optimistic) {
      return next;
    }
    return {
      ...next,
      model: {
        ...next.model,
        current: optimistic.current,
        ...(optimistic.reasoningEffort !== undefined
          ? { reasoningEffort: optimistic.reasoningEffort }
          : {}),
        ...(optimistic.reasoningEfforts
          ? { reasoningEfforts: optimistic.reasoningEfforts }
          : {}),
      },
      chats: Array.isArray(next.chats)
        ? next.chats.map((chat) =>
            chat.id === next.activeChatId
              ? { ...chat, model: optimistic.current }
              : chat,
          )
        : next.chats,
    };
  }, []);
  const clearOptimisticModelOverrideIfAcknowledged = useCallback(
    (next: AppState | null | undefined) => {
      if (!next) {
        return;
      }
      const key = chatKey(next.workspace?.id ?? "", next.activeChatId ?? "");
      const optimistic = key ? optimisticModelByChatRef.current[key] : undefined;
      if (!optimistic) {
        return;
      }
      const nextReasoning = String(next.model.reasoningEffort ?? "");
      const optimisticReasoning = String(optimistic.reasoningEffort ?? "");
      if (
        next.model.current === optimistic.current &&
        nextReasoning === optimisticReasoning
      ) {
        setOptimisticModelByChat((prev) => {
          if (!(key in prev)) {
            return prev;
          }
          const updated = { ...prev };
          delete updated[key];
          return updated;
        });
      }
    },
    [],
  );
  const selectedChats = useMemo(() => {
    if (!state || !selectedWorkspaceId) {
      return [] as ChatSummary[];
    }
    const baseChats =
      selectedWorkspaceId === (state.workspace.id ?? "")
        ? state.chats
        : (workspaceChats[selectedWorkspaceId] ?? []);
    const sortedChats = [...baseChats].sort((a, b) => {
      const aTime = a.updatedAt ? new Date(a.updatedAt).getTime() : 0;
      const bTime = b.updatedAt ? new Date(b.updatedAt).getTime() : 0;
      return bTime - aTime;
    });
    const targetName =
      optimisticChatFocus?.chatId === selectedChatId
        ? optimisticChatFocus.name
        : t("chat.new");
    let foundTarget = false;
    const chats: ChatSummary[] = sortedChats.map((chat, index) => {
      const summary = chat as Partial<ChatSummary>;
      const isTarget = chat.id === selectedChatId;
      if (isTarget) {
        foundTarget = true;
      }
      return {
        index: typeof summary.index === "number" ? summary.index : index,
        id: chat.id,
        name: chat.name,
        messageCount:
          typeof summary.messageCount === "number" ? summary.messageCount : 0,
        updatedAt: chat.updatedAt,
        active: isTarget,
        model: summary.model,
        running: summary.running,
        planMode: summary.planMode,
        archived: Boolean(chat.archived),
      };
    });
    if (selectedChatId && !foundTarget && (focusOverride || optimisticChatFocus)) {
      chats.unshift({
        index: 0,
        id: selectedChatId,
        name: targetName,
        messageCount: 0,
        updatedAt: new Date().toISOString(),
        active: true,
        model: undefined,
        running: undefined,
        planMode: undefined,
        archived: false,
      });
      for (let i = 1; i < chats.length; i += 1) {
        chats[i] = { ...chats[i], index: i };
      }
    }
    return chats;
  }, [
    state,
    workspaceChats,
    selectedWorkspaceId,
    selectedChatId,
    focusOverride,
    optimisticChatFocus,
    t,
  ]);

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

  const materializeDraftChat = useCallback(async () => {
    if (!draftModeRef.current) {
      return {
        chatId: activeChatIdRef.current,
        workspaceId: activeWorkspaceIdRef.current,
      };
    }
    const wsId = draftWorkspaceIdRef.current;
    const switchWs = Boolean(wsId && wsId !== activeWorkspaceIdRef.current);
    if (switchWs) {
      pendingFocusWsIdRef.current = wsId;
    }
    const draftModel = draftModelRef.current;
    const draftReasoning = draftReasoningRef.current;
    const newId = await client.newChat(switchWs ? wsId : "", draftModel, draftReasoning);
    if (!newId) {
      if (switchWs && pendingFocusWsIdRef.current === wsId) {
        pendingFocusWsIdRef.current = "";
      }
      return null;
    }
    const targetWsId = switchWs ? wsId : wsId || activeWorkspaceIdRef.current;
    historyChatRef.current = chatKey(targetWsId, newId);
    const optimisticName = translate(
      normalizeLang(stateRef.current?.language),
      "chat.new",
    );
    activeChatIdRef.current = newId;
    activeWorkspaceIdRef.current = targetWsId;
    // Clear stale history that may have been populated by a stray SSE event
    // during draft mode so the new chat doesn't display another chat's turns.
    setHistoryTurns([]);
    setHistoryStart(0);
    setHistoryTotal(0);
    setFocusOverride({ chatId: newId, wsId: targetWsId });
    setOptimisticChatFocus({
      chatId: newId,
      wsId: targetWsId,
      name: optimisticName,
    });
    setDraftMode(false);
    setDraftWorkspaceId("");
    // The backend already applied the model atomically (passed in newChat body).
    // Mirror it locally and consume the draft selections.
    applyDraftSelectionToState(true);
    return { chatId: newId, workspaceId: targetWsId };
  }, [client, applyDraftSelectionToState]);

  const pasteImage = useCallback(
    async (dataUrl: string) => {
      if (draftModeRef.current) {
        // Composing a brand-new chat: stage the bitmap in the workspace cache
        // WITHOUT materializing a chat yet. It moves into the chat's data dir
        // when the user actually sends (see sendInput's draft branch).
        const wsId =
          draftWorkspaceIdRef.current || activeWorkspaceIdRef.current;
        return client.saveDraftAttachment(dataUrl, "", wsId);
      }
      const target = await materializeDraftChat();
      if (!target?.chatId) {
        return null;
      }
      return client.pasteImage(target.chatId, dataUrl, target.workspaceId);
    },
    [client, materializeDraftChat],
  );

  const saveDroppedFile = useCallback(
    async (dataUrl: string, fileName: string) => {
      if (draftModeRef.current) {
        // Same staging behavior as pasteImage: no chat is created yet.
        const wsId =
          draftWorkspaceIdRef.current || activeWorkspaceIdRef.current;
        return client.saveDraftAttachment(dataUrl, fileName, wsId);
      }
      const target = await materializeDraftChat();
      if (!target?.chatId) {
        return null;
      }
      return client.saveDroppedFile(target.chatId, dataUrl, fileName, target.workspaceId);
    },
    [client, materializeDraftChat],
  );

  const chatImageUrl = useCallback(
    (path: string, workspaceId?: string) => {
      // Draft attachments live under the workspace cache; pass the target
      // workspace so the backend can resolve (and validate) that dir.
      const wsId =
        workspaceId ||
        draftWorkspaceIdRef.current ||
        activeWorkspaceIdRef.current;
      return client.chatImageUrl(path, wsId);
    },
    [client],
  );

  const mcpIconUrl = useCallback(
    (server: string, icon: string) => client.mcpIconUrl(server, icon),
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

  const subscribeConsoleOutput = useCallback(
    (id: string, handler: (b64: string, end: boolean) => void) => {
      const map = consoleOutputHandlersRef.current;
      let set = map.get(id);
      if (!set) {
        set = new Set();
        map.set(id, set);
      }
      set.add(handler);
      return () => {
        const s = consoleOutputHandlersRef.current.get(id);
        if (s) {
          s.delete(handler);
          if (s.size === 0) {
            consoleOutputHandlersRef.current.delete(id);
          }
        }
      };
    },
    [],
  );

  const consumePendingAutoConsole = useCallback(() => {
    const info = pendingAutoConsoleRef.current;
    pendingAutoConsoleRef.current = null;
    return info;
  }, []);

  const previewHtml = useCallback(
    (html: string) =>
      client.previewHtml(
        activeChatIdRef.current,
        html,
        activeWorkspaceIdRef.current,
      ),
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

  // Make the Browser tab visible + active and open the right panel (invoked
  // from the View menu). Idempotent.
  const showBrowserTab = useCallback(() => {
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
  }, []);

  // Listen for preview-path link clicks from Steps.tsx and open the returned
  // URL through the normal BrowserPanel command flow (show tab + open_preview).
  // Mirror the 120 ms deferral used by the SSE browser_command fan-out so
  // the just-mounted BrowserPanel has time to subscribe before the command
  // fires (otherwise the first click after a cold open is lost).
  useEffect(() => {
    const handler = (e: Event) => {
      const detail = (e as CustomEvent).detail as
        | { url: string }
        | undefined;
      if (detail?.url) {
        showBrowserTab();
        window.setTimeout(() => {
          emitBrowserCommandLocal({ action: "open_preview", url: detail.url });
        }, 120);
      }
    };
    window.addEventListener("codewood:browser-open-preview", handler);
    return () =>
      window.removeEventListener("codewood:browser-open-preview", handler);
  }, [showBrowserTab, emitBrowserCommandLocal]);

  // Remove the Browser tab from the visible set (its "x" close button). Falls
  // back to the To-dos tab.
  const hideBrowserTab = useCallback(() => {
    try {
      const prefs = loadRightPanelPrefs();
      const visible: RightPanelTabId[] = prefs.visible.filter(
        (id): id is RightPanelTabId => id !== "browser",
      );
      const ordered: RightPanelTabId[] = RIGHT_PANEL_TABS.filter((id) =>
        visible.includes(id),
      );
      const active: RightPanelTabId =
        ordered.indexOf(prefs.active) >= 0 ? prefs.active : (ordered[0] ?? "dashboard");
      saveRightPanelPrefs({ visible: ordered, active });
      window.dispatchEvent(new Event("codewood.rightPanelTabs"));
    } catch {
      // localStorage may be unavailable.
    }
  }, []);

  const consoleOptions = useMemo(
    () => ({
      fontFamily: state?.consoleOptions?.fontFamily ?? "",
      bufferLines: state?.consoleOptions?.bufferLines ?? 1000,
    }),
    [state?.consoleOptions?.fontFamily, state?.consoleOptions?.bufferLines],
  );

  const showConsole = useCallback(() => {
    setConsoleOpen(true);
    try {
      window.localStorage.setItem(CONSOLE_OPEN_KEY, "1");
    } catch {
      // localStorage may be unavailable; the dock just won't persist.
    }
  }, []);

  const hideConsole = useCallback(() => {
    setConsoleOpen(false);
    try {
      window.localStorage.setItem(CONSOLE_OPEN_KEY, "0");
    } catch {
      // localStorage may be unavailable.
    }
  }, []);

  const attachConsole = useCallback((id: string) => client.attachConsole(id), []);
  const consoleInput = useCallback(
    (id: string, data: string) => client.consoleInput(id, data),
    [],
  );
  const consoleResize = useCallback(
    (id: string, cols: number, rows: number) => client.consoleResize(id, cols, rows),
    [],
  );
  const openConsole = useCallback((kind: string) => client.openConsole(kind), []);
  const closeConsole = useCallback((id: string) => client.closeConsole(id), []);
  const activateConsole = useCallback(
    (id: string) => client.activateConsole(id),
    [],
  );
  const setConsoleOptions = useCallback(
    (options: { fontFamily: string; bufferLines: number }) =>
      client.setConsoleOptions(options),
    [],
  );

  // Render an HTML snippet in the embedded browser: persist it, ensure the
  // Browser tab is visible+active and the right panel is open, then navigate.
  const previewHtmlInBrowser = useCallback(
    async (html: string) => {
      const saved = await client.previewHtml(
        activeChatIdRef.current,
        html,
        activeWorkspaceIdRef.current,
      );
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
  const bgServerOpacity = bgState?.opacity ?? 85;
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
    // 85 = faint). The background image layer itself is always fully painted;
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
    //   t=85 -> ~0.77 (faint, default)   t=60 -> ~0.44   t=40 -> ~0.23   t=0 -> 0
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
    // During a cross-workspace focus switch, ``state`` can briefly contain a
    // stale workspace id while its chat list was produced by the background
    // runtime in the other workspace.  Never cache that unconfirmed snapshot:
    // chat ids repeat per workspace, so it would make A's running chat appear
    // as a real row under B until the next full reload.
    const expectedWsId = focusOverride?.wsId ?? optimisticChatFocus?.wsId ?? "";
    if (expectedWsId && activeWsId !== expectedWsId) {
      return;
    }
    setWorkspaceChats((prev) => ({ ...prev, [activeWsId]: chats }));
  }, [
    state?.workspace.id,
    state?.chats,
    focusOverride?.wsId,
    optimisticChatFocus?.wsId,
  ]);

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
      workspaceOrder: ids(sp.workspaceOrder),
    };
    const hasServer =
      server.pinnedWorkspaceIds.length > 0 ||
      server.pinnedChatIds.length > 0;
    if (hasServer) {
      setUiPrefs(server);
      saveUiPrefs(server);
      return;
    }
    const local = loadUiPrefs();
    if (
      local.pinnedWorkspaceIds.length > 0 ||
      local.pinnedChatIds.length > 0
    ) {
      void client.setUiPrefs(local);
    }
  }, [state?.uiPrefs, client]);

  // Tick a 1s clock while busy so the active turn shows live elapsed time.
  useEffect(() => {
    if (!anyBusy) {
      return;
    }
    setNow(Date.now());
    const handle = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(handle);
  }, [anyBusy]);

  // Whether a round's step text contains a command-output block that has been
  // opened (CMD_OUTPUT_BEGIN) but not yet closed (CMD_OUTPUT_END). Used to
  // detect streaming continuations that belong to the round.
  const roundHasOpenCmdBlock = useCallback((round: TurnRound | undefined) => {
    if (!round) {
      return false;
    }
    const text = round.segments
      .filter((segment) => segment.kind === "step")
      .map((segment) => segment.text)
      .join("");
    return text.lastIndexOf(CMD_OUTPUT_BEGIN) > text.lastIndexOf(CMD_OUTPUT_END);
  }, []);

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
        // When output arrives before `turn_start` (rare race under fast
        // consecutive tool calls), synthesize a placeholder turn so the
        // segment is not lost. The subsequent `turn_start` will append a
        // proper turn alongside it, which is harmless — the placeholder
        // already carries the tool output.
        if (!existing || existing.length === 0) {
          const placeholder = {
            id: nextIdRef.current++,
            userText: "",
            rounds: [{
              id: nextIdRef.current++,
              waitStartedAt: Date.now(),
              waitEndedAt: null,
              segments: [{ id: nextIdRef.current++, kind, text }],
            }],
            startedAt: Date.now(),
            endedAt: null,
          };
          return { ...prev, [chatId]: [placeholder] };
        }
        const next = [...existing];
        const turn = next[next.length - 1];
        const rounds = [...turn.rounds];
        let round = rounds[rounds.length - 1];
        // Command output is live-streamed in raw chunks; only the first chunk
        // carries the CMD_OUTPUT_BEGIN sentinel and the final one the END
        // sentinel. When the tool round closes (``round_end``) or a new model
        // round starts while chunks are still draining, a continuation chunk
        // (no BEGIN sentinel) must keep appending to the most recent round that
        // holds an open command block — otherwise the block is split across
        // rounds and the tail renders as orphaned raw text.
        let openCmdRound: TurnRound | undefined;
        for (let i = rounds.length - 1; i >= 0; i -= 1) {
          if (roundHasOpenCmdBlock(rounds[i])) {
            openCmdRound = rounds[i];
            break;
          }
        }
        const isCmdContinuation =
          kind === "step" &&
          !text.startsWith(CMD_OUTPUT_BEGIN) &&
          !text.startsWith(CMD_PROMPT_BEGIN) &&
          !text.startsWith(DIFF_BEGIN) &&
          Boolean(openCmdRound);
        if (isCmdContinuation) {
          // Keep the stream contiguous: append to the round that owns the open
          // command block without touching its frozen wait timer. Drop any
          // empty trailing rounds (opened by ``round_start``) that belong to
          // the still-streaming command; leave non-empty rounds (e.g. an early
          // model reply) in place.
          while (rounds.length > 0 && rounds[rounds.length - 1] !== openCmdRound) {
            const trailing = rounds[rounds.length - 1];
            if (trailing && trailing.segments.length === 0 && !trailing.thinkingText) {
              rounds.pop();
            } else {
              break;
            }
          }
          round = openCmdRound as TurnRound;
        } else if (!round) {
          round = {
            id: nextIdRef.current++,
            waitStartedAt: Date.now(),
            waitEndedAt: null,
            segments: [],
          };
          rounds.push(round);
        } else if (round.waitEndedAt !== null) {
          // If the last round is closed (timer ended), open a fresh round
          // rather than appending new visible output to an earlier model pass.
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
          // If answer text lands after tool output without an intervening
          // ``round_start`` event, still open a fresh round so the reply gets
          // its own Thinking/timer block instead of being merged into the
          // previous tool-only pass.
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
        } else if (
          kind === "step" &&
          round.segments.some((s) => s.kind === "answer" && s.text.trim().length > 0)
        ) {
          // If tool output starts after visible assistant text without an
          // explicit ``round_start``, split the round so the answer doesn't
          // remain in a second running "Working..." block beside the tools.
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
        // Freeze the current round's thinking timer on the first visible
        // content so that later model passes can open their own Thinking block.
        const roundIndex = Math.max(0, rounds.indexOf(round));
        if (round.thinkingText && !round.thinkingEndedAt) {
          round = { ...round, thinkingEndedAt: Date.now() };
        }
        const segments = [...round.segments];
        const last = segments[segments.length - 1];
        if (last && last.kind === kind) {
          segments[segments.length - 1] = { ...last, text: last.text + text };
        } else {
          segments.push({ id: nextIdRef.current++, kind, text });
        }
        const mergedRound = { ...round, segments };
        // A tool round's output is fully streamed the moment its
        // command-output block closes (CMD_OUTPUT_END). Settle the round
        // immediately so the GUI stops the tool spinner and flips to the
        // "Working..." wait indicator right away — instead of staying
        // "running" until the backend's round_end, which for shell only
        // arrives after heavy post-processing (workspace file-diff
        // detection) has completed. The block-close check keeps the spinner
        // running for the whole command execution, matching the
        // "spin stops while shell command are still not done" fix.
        if (
          kind === "step" &&
          mergedRound.waitEndedAt === null &&
          roundHasOpenCmdBlock(round) &&
          !roundHasOpenCmdBlock(mergedRound)
        ) {
          mergedRound.waitEndedAt = Date.now();
        }
        rounds[roundIndex] = mergedRound;
        next[next.length - 1] = { ...turn, rounds };
        return { ...prev, [chatId]: next };
      });
    },
    [roundHasOpenCmdBlock],
  );

  const repaintLastToolPrompt = useCallback((text: string, chatId: string) => {
    if (!text || !chatId) {
      return;
    }
    setTurnsByChat((prev) => {
      const existing = prev[chatId];
      if (!existing || existing.length === 0) {
        return prev;
      }
      const next = [...existing];
      const turn = next[next.length - 1];
      if (!turn) {
        return prev;
      }
      const rounds = [...turn.rounds];
      const round = rounds[rounds.length - 1];
      if (!round) {
        return prev;
      }
      const segments = [...round.segments];
      let replaced = false;
      for (let i = segments.length - 1; i >= 0; i -= 1) {
        const seg = segments[i];
        if (!seg || seg.kind !== "step") {
          continue;
        }
        const end = seg.text.lastIndexOf(CMD_PROMPT_END);
        if (end < 0) {
          continue;
        }
        const begin = seg.text.lastIndexOf(CMD_PROMPT_BEGIN, end);
        if (begin < 0) {
          continue;
        }
        segments[i] = {
          ...seg,
          text: `${seg.text.slice(0, begin)}${text}${seg.text.slice(end + CMD_PROMPT_END.length)}`,
        };
        replaced = true;
        break;
      }
      if (!replaced) {
        return prev;
      }
      rounds[rounds.length - 1] = { ...round, segments };
      next[next.length - 1] = { ...turn, rounds };
      return { ...prev, [chatId]: next };
    });
  }, []);

  const appendThinking = useCallback((text: string, chatId: string) => {
    if (!text || !chatId) {
      return;
    }
    setTurnsByChat((prev) => {
      const existing = prev[chatId];
      if (!existing || existing.length === 0) {
        const placeholder = {
          id: nextIdRef.current++,
          userText: "",
          rounds: [{
            id: nextIdRef.current++,
            waitStartedAt: Date.now(),
            waitEndedAt: null,
            segments: [],
            thinkingText: text,
            thinkingStartedAt: Date.now(),
          }],
          startedAt: Date.now(),
          endedAt: null,
        };
        return { ...prev, [chatId]: [placeholder] };
      }
      const next = [...existing];
      const turn = next[next.length - 1];
      const rounds = [...turn.rounds];
      let round = rounds[rounds.length - 1];
      const roundHasVisibleContent = Boolean(
        round?.segments.some((segment) => segment.text.trim().length > 0),
      );
      if (
        !round ||
        round.waitEndedAt !== null ||
        roundHasVisibleContent ||
        round.thinkingEndedAt !== undefined
      ) {
        round = {
          id: nextIdRef.current++,
          waitStartedAt: Date.now(),
          waitEndedAt: null,
          segments: [],
        };
        rounds.push(round);
      }
      const prevText = round.thinkingText ?? "";
      // Record the moment thinking first started so the UI can show a live timer.
      const thinkingStartedAt = round.thinkingStartedAt ?? (!prevText ? Date.now() : undefined);
      rounds[rounds.length - 1] = {
        ...round,
        thinkingText: prevText + text,
        thinkingStartedAt,
      };
      next[next.length - 1] = { ...turn, rounds };
      return { ...prev, [chatId]: next };
    });
  }, []);

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
      // Reconcile a placeholder turn created by ``appendSegment`` (tool output
      // that arrived before the ``turn_start`` event). Fill in the user text.
      if (last && !last.userText && last.rounds.length > 0 && last.rounds.some((r) => r.segments.length > 0)) {
        const merged = [...existing];
        merged[merged.length - 1] = { ...last, userText: userText || last.userText };
        return { ...prev, [chatId]: merged };
      }
      if (last && last.optimistic && last.rounds.length === 0) {
        const merged = [...existing];
        merged[merged.length - 1] = {
          ...last,
          userText: userText || last.userText,
          optimistic: false,
        };
        return { ...prev, [chatId]: merged };
      }
      // SSE is normally at-most-once, but a reconnect or overlapping event
      // subscription can occasionally deliver ``turn_start`` twice.  The
      // second event belongs to the already-open turn, not a new user action:
      // one chat cannot run two turns concurrently.  Ignore it so subsequent
      // streamed tool output does not get split into a duplicate transcript
      // row.  Keep the comparison exact here; this is event de-duplication,
      // not the more permissive persisted-history reconciliation below.
      if (last && last.endedAt === null && last.userText === userText) {
        return prev;
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

  // Open a model round's wait timer on the active turn. Each backend
  // ``round_start`` becomes its own UI round so repeated tool-call loops can
  // render distinct Thinking blocks instead of merging later reasoning into the
  // first one.
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
      let rounds = [...turn.rounds];
      if (
        !last ||
        last.waitEndedAt !== null ||
        last.segments.length > 0 ||
        last.thinkingText
      ) {
        if (last && last.waitEndedAt === null) {
          rounds[rounds.length - 1] = {
            ...last,
            waitEndedAt: Date.now(),
          };
        }
        rounds = [
          ...rounds,
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
  const endRound = useCallback((chatId: string, backendElapsedMs?: number) => {
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
      const updated: TurnRound = { ...lastRound, waitEndedAt: Date.now() };
      if (typeof backendElapsedMs === "number" && backendElapsedMs > 0) {
        updated.backendElapsedMs = backendElapsedMs;
      }
      rounds[rounds.length - 1] = updated;
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
      const next = { ...prev, [chatId]: value };
      busyByChatRef.current = next;
      return next;
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
      // The chat the user is currently viewing can never be unread. The backend
      // may race a stale completion snapshot past select_chat's clear (or a
      // spurious mark could ride an internal-command idle), so force the viewed
      // chat's hasUnread to false on any incoming snapshot — the frontend knows
      // what the user is looking at and wins here.
      const clearViewedUnread = (
        snap: AppState | null | undefined,
      ): AppState | null | undefined => {
        if (!snap) {
          return snap;
        }
        const chats = snap.chats;
        if (!Array.isArray(chats)) {
          return snap;
        }
        const viewedKey = chatKey(activeWorkspaceIdRef.current, activeChatIdRef.current);
        if (!viewedKey) {
          return snap;
        }
        const wsId = String(snap.workspace?.id || "");
        let changed = false;
        const mapped = chats.map((c) => {
          if (c && c.hasUnread && chatKey(wsId, String(c.id || "")) === viewedKey) {
            changed = true;
            return { ...c, hasUnread: false };
          }
          return c;
        });
        return changed ? { ...snap, chats: mapped } : snap;
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
      // When a workspace switch is pending, only accept events from the
      // target workspace — old-workspace events must NOT pass, otherwise
      // their setState() would clobber the new workspace's state before
      // React flushes the focus override.
      const pendingWs = pendingFocusWsIdRef.current;
      switch (event.event) {
        case "idle": {
          const rawNext = data.state;
          clearOptimisticModelOverrideIfAcknowledged(rawNext);
          const next = clearViewedUnread(applyOptimisticModelOverride(rawNext));
          const idleForFocused =
            !eventWsId || !activeWsId ||
            (pendingWs
              ? eventWsId === pendingWs
              : eventWsId === activeWsId);
          // Check whether this idle's chat is still running on the backend
          // BEFORE we apply setState (which can trigger side effects). When
          // the running chat is the same one that is currently streaming in
          // the GUI, we must keep its live turns intact and avoid a React
          // re-render that could disrupt content accumulation.
          const stillRunning = idleForFocused
            ? Boolean(
                chatId &&
                  next?.chats?.some(
                    (c) => String(c.id) === chatId && Boolean(c.running),
                  ),
              )
            : false;
          const isStreamingChat = !!streamingKeyRef.current &&
            eventKey === streamingKeyRef.current;
          if (!isStreamingChat || !stillRunning) {
            // Apply state update as normal (idle is terminal or from a
            // different chat). Consume pending focus if applicable.
            if (next && idleForFocused) {
              // When the idle event is NOT from the focused chat AND a
              // workspace switch is NOT pending (a background chat finished
              // in the same workspace), only merge chat list & plan updates
              // — a full state replacement would override activeChatId and
              // reload the wrong chat's history, blanking the focused chat's
              // transcript. When a workspace switch IS pending, the full
              // replacement is required to complete the switch.
              const focusedKey = chatKey(activeWsId, activeChatIdRef.current);
              const isPendingSwitch = eventWsId === pendingWs;
              if (chatId && eventKey && eventKey !== focusedKey && !isPendingSwitch) {
                // Only merge chat lists from the same workspace. Background
                // idle events can carry state from a different workspace when
                // _build_state reads the focused (not runtime) workspace id.
                const nextChatWs = String(next?.workspace?.id || "");
                setState((prev) => {
                  if (!prev) return prev;
                  const merged: any = { ...prev };
                  // A background runtime's SSE envelope is authoritative for
                  // its workspace.  Never merge a snapshot whose embedded
                  // workspace disagrees with that envelope: during a
                  // cross-workspace switch the shared agent can briefly build
                  // a state object with B's workspace metadata but A's chat
                  // records.  Since chat ids repeat per workspace, merging it
                  // by bare id would overwrite B's chat name/list with A's.
                  if (
                    next.chats &&
                    nextChatWs === eventWsId &&
                    nextChatWs === String(prev.workspace?.id || "")
                  ) {
                    const nextChats = Array.isArray(next.chats) ? next.chats : [];
                    // ``next.chats`` is the authoritative full chat list for
                    // this workspace, so a chat absent from it (deleted on the
                    // backend) must be dropped — otherwise the deleted chat
                    // lingers in the sidebar after a delete.
                    const nextChatIds = new Set(nextChats.map((nc: any) => String(nc.id)));
                    merged.chats = prev.chats
                      .filter((c) => nextChatIds.has(String(c.id)))
                      .map((c) => {
                        const updated = nextChats.find((nc: any) => String(nc.id) === String(c.id));
                        return updated ? { ...c, ...updated } : c;
                      });
                  }
                  if (next.plan) {
                    merged.plan = next.plan;
                  }
                  return merged;
                });
              } else {
                setState(next);
              }
              if (eventWsId === pendingFocusWsIdRef.current) {
                pendingFocusWsIdRef.current = "";
              }
            }
            if (isStreamingChat && !stillRunning) {
              streamingKeyRef.current = "";
            }
          } else if (next && idleForFocused) {
            // The focused chat is STILL streaming: a full state replacement
            // would clobber its live turns and disrupt content accumulation,
            // but the snapshot may carry fresh UI-only preferences (background
            // image / opacity, theme, language, console options, execution
            // policy) changed via the settings pages while the task runs.
            // Merge just those fields so e.g. a background image chosen during
            // a task still appears (and its opacity slider enables) without
            // touching the running turn's state.
            setState((prev) => {
              if (!prev) return next;
              return {
                ...prev,
                ...(next.background !== undefined
                  ? { background: next.background }
                  : {}),
                ...(next.theme !== undefined ? { theme: next.theme } : {}),
                ...(next.language !== undefined
                  ? { language: next.language }
                  : {}),
                ...(next.uiPrefs !== undefined
                  ? { uiPrefs: next.uiPrefs }
                  : {}),
                ...(next.consoleOptions !== undefined
                  ? { consoleOptions: next.consoleOptions }
                  : {}),
                ...(next.executionPolicy !== undefined
                  ? { executionPolicy: next.executionPolicy }
                  : {}),
              };
            });
          }
          if (!stillRunning) {
            endActiveTurn(eventKey);
            setBusyForChat(eventKey, false);
            // The unread blue-dot flag is decided by the BACKEND when the turn
            // finishes: a chat that completed while the user was elsewhere gets
            // ``hasUnread`` set (persisted), the viewed chat gets it cleared.
            // The sidebar derives its dots from the server snapshot, so a late
            // idle event can no longer falsely flag a chat the user watched
            // complete. When a BACKGROUND workspace's chat finished, refresh
            // that workspace's list so its persisted flag reaches the sidebar.
            if (!idleForFocused && eventWsId) {
              refreshWorkspaceChatsRef.current?.(eventWsId);
            }
            const focusedKey = chatKey(activeWsId, activeChatIdRef.current);
            // When the focused chat's turn finishes, reload its history from
            // the server so the GUI always reflects the full persisted content
            // — even when live-streaming events were partially lost.
            if (eventKey === focusedKey) {
              if (pendingHistoryReloadRef.current) {
                pendingHistoryReloadRef.current = false;
              }
              reloadHistoryRef.current();
            }
            // Auto-send next pending input if auto-send is enabled for this chat.
            if (eventKey && pendingAutoSendByChatRef.current[eventKey]) {
              const pending = pendingInputsByChatRef.current[eventKey];
              // A jump paused the current turn; the idle that closes THAT turn
              // must not drain the queue. Consume the marker here and let the
              // queue resume on the NEXT idle (after the jumped task finishes),
              // so the remaining messages stay queued behind the jumped one.
              const suppressDrain = Boolean(suppressAutoSendOnceRef.current[eventKey]);
              if (suppressDrain) {
                delete suppressAutoSendOnceRef.current[eventKey];
              }
              if (!suppressDrain && pending && pending.length > 0) {
                setTimeout(() => {
                  sendNextPendingRef.current(eventKey);
                }, 200);
              }
            }
          }
          // When the focused workspace changes to one with no active chat,
          // enter draft mode (show empty composer). This covers:
          //   - File > Open Folder (workspace create)
          //   - Auto-opening a workspace with no chats (startup / delete+fallback)
          //   - Any other path that lands on a chatless workspace
          if (idleForFocused) {
            const prevWsId = stateRef.current?.workspace?.id;
            if (
              prevWsId &&
              next?.workspace?.id &&
              prevWsId !== next.workspace.id &&
              !next.activeChatId
            ) {
              setDraftMode(true);
              setDraftWorkspaceId(next.workspace.id);
              historyChatRef.current = "\u0000";
              setHistoryTurns([]);
              setHistoryStart(0);
              setHistoryTotal(0);
            }
          }
          break;
        }
        case "state": {
          // State-only refresh fired mid-turn (e.g. when the agent calls
          // ``update_plan``). Update the snapshot so the plan panel can
          // re-render but DO NOT close the active turn or clear the busy
          // flag — the model is still streaming its reply.
          // Guard: only apply state updates when the event belongs to the
          // focused workspace AND the streaming chat is not currently
          // accumulating turn content (to avoid React re-render side
          // effects that can disrupt segment accumulation).
          const rawNext = data.state;
          clearOptimisticModelOverrideIfAcknowledged(rawNext);
          const next = clearViewedUnread(applyOptimisticModelOverride(rawNext));
          const isStreamingChat = !!streamingKeyRef.current &&
            eventKey === streamingKeyRef.current;
          const stateForFocused =
            !eventWsId || !activeWsId ||
            (pendingWs
              ? eventWsId === pendingWs
              : eventWsId === activeWsId);
          if (next && stateForFocused) {
            if (isStreamingChat) {
              // During streaming, only apply chat list updates (e.g. auto-
              // generated name), plan changes (e.g. from update_plan), and the
              // model + dashboard snapshots (a model switch / focus switch while
              // the chat streams must still update the composer selector and the
              // cache/output stats — dropping them leaves the UI showing a stale
              // model or another chat's numbers). Full state replacement is
              // avoided to keep from disrupting turn content accumulation.
              setState((prev) => {
                if (!prev) return prev;
                const merged: any = { ...prev };
                // A streaming ``state`` event is an incremental update, but
                // chat ids repeat per workspace.  Never merge its list by bare
                // id unless all three identities agree.  Otherwise an A/chat-1
                // plan or usage update can rename B/chat-1 in the currently
                // rendered B list for one frame, until the next full B snapshot
                // arrives (the observed same-name flash on the second switch).
                const nextChatWs = String(next.workspace?.id || "");
                const prevChatWs = String(prev.workspace?.id || "");
                if (
                  next.chats &&
                  nextChatWs === eventWsId &&
                  nextChatWs === prevChatWs
                ) {
                  merged.chats = prev.chats.map((c) => {
                    const updated = next.chats?.find((nc: any) => nc.id === c.id);
                    return updated ? { ...c, ...updated } : c;
                  });
                }
                if (next.plan) {
                  merged.plan = next.plan;
                }
                if (next.model) {
                  merged.model = next.model;
                }
                if (next.cacheStats !== undefined) {
                  merged.cacheStats = next.cacheStats;
                }
                if (next.tokenStats !== undefined) {
                  merged.tokenStats = next.tokenStats;
                }
                if (next.contextUsage !== undefined) {
                  merged.contextUsage = next.contextUsage;
                }
                return merged;
              });
            } else {
              setState(next);
            }
          }
          break;
        }
        case "turn_start": {
          startTurn(String(data.text ?? ""), eventKey);
          setBusyForChat(eventKey, true);
          streamingKeyRef.current = eventKey;
          // A new turn supersedes any stale retry countdown for this chat.
          setRetryCountdownByChat((prev) => {
            if (!prev[eventKey]) return prev;
            const next = { ...prev };
            delete next[eventKey];
            return next;
          });
          break;
        }
        case "round_start": {
          startRound(eventKey);
          break;
        }
        case "round_end": {
          // The model round completed: any 429/503 retry countdown is over.
          setRetryCountdownByChat((prev) => {
            if (!prev[eventKey]) return prev;
            const next = { ...prev };
            delete next[eventKey];
            return next;
          });
          const roundMeta = event.data as Record<string, unknown>;
          const backendElapsedS = typeof roundMeta.thinkingElapsedSeconds === "number"
            ? roundMeta.thinkingElapsedSeconds as number : undefined;
          endRound(eventKey, backendElapsedS != null ? Math.round(backendElapsedS * 1000) : undefined);
          // Refresh context-usage ring and cache/output token stats from the
          // round_end payload so they stay live during a multi-round task
          // instead of freezing until the terminal idle event.
          const cu = roundMeta.contextUsage as
            | { percent?: number; tokens?: number; window?: number }
            | undefined;
          const cs = roundMeta.cacheStats as
            | { totalTokens?: number; hitTokens?: number; missTokens?: number; hitRate?: number; supported?: boolean }
            | undefined;
          const ts = roundMeta.tokenStats as
            | { outputTokens?: number; reasoningTokens?: number; hasOutputTokens?: boolean; hasReasoningTokens?: boolean; includesReasoning?: boolean }
            | undefined;
          if (cu || cs || ts) {
            setState((prev) => {
              if (!prev) return prev;
              const next = { ...prev };
              if (cu) next.contextUsage = cu as AppState["contextUsage"];
              if (cs) next.cacheStats = cs as AppState["cacheStats"];
              if (ts) next.tokenStats = ts as AppState["tokenStats"];
              return next;
            });
          }
          break;
        }
        case "retry_countdown": {
          // Backend ticks once per second while it backs off before the next
          // 429/503 retry. Each tick replaces the previous one so the UI shows
          // a single live countdown line below the last message; the final
          // tick carries ``done`` and removes it. Routing follows the event's
          // own workspace+chat so background chats stay independent.
          const d = event.data as Extract<ServerEvent, { event: "retry_countdown" }>["data"];
          const ownerChat = String(d.chatId || "");
          if (!ownerChat) break;
          const ownerKey = chatKey(eventWsId, ownerChat);
          const done = Boolean(d.done);
          setRetryCountdownByChat((prev) => {
            if (done) {
              if (!prev[ownerKey]) return prev;
              const next = { ...prev };
              delete next[ownerKey];
              return next;
            }
            return {
              ...prev,
              [ownerKey]: {
                code: Number(d.code) || 0,
                retryNumber: Number(d.retryNumber) || 0,
                waitSeconds: Number(d.waitSeconds) || 0,
                remainingSeconds: Number(d.remainingSeconds) || 0,
                modelName: String(d.modelName || ""),
                message: String(d.message || ""),
                updatedAt: Date.now(),
              },
            };
          });
          break;
        }
        case "compact_notice": {
          const compactData = event.data as Extract<ServerEvent, { event: "compact_notice" }>["data"];
          const text = String(compactData.text ?? "");
          const title = String(compactData.title ?? "") || text;
          const body = String(compactData.body ?? "");
          if (!eventKey || !title) {
            break;
          }
          // History and streaming turns are separate lists.  Anchor this
          // streamed summary to the live turn that was current on arrival, so
          // it remains after its user entry instead of above it.
          const liveTurns = turnsByChatRef.current[eventKey] ?? EMPTY_TURNS;
          const anchorTurnId = liveTurns.length > 0
            ? liveTurns[liveTurns.length - 1].id
            : undefined;
          setCompactNoticeState((state) => ({
            chatKey: eventKey,
            notice: {
              ...buildCompactNoticeData(title, body, {
                stage: String(compactData.stage ?? "") || undefined,
                mode: String(compactData.mode ?? "") || undefined,
              }),
              // Stream chunks update the body but retain the original anchor.
              anchorTurnId:
                state.chatKey === eventKey && state.notice?.anchorTurnId !== undefined
                  ? state.notice.anchorTurnId
                  : anchorTurnId,
              // The final notice replaces its streamed predecessors. Keep the
              // original slot so a later user turn cannot jump above it.
              createdAt:
                state.chatKey === eventKey && state.notice?.createdAt !== undefined
                  ? state.notice.createdAt
                  : Date.now(),
            },
            version: state.version + 1,
          }));
          break;
        }
        case "output": {
          const stepText = String(data.text ?? "");
          // Route tool/step output into the EVENT's own workspace+chat bucket
          // (like ``assistant`` / ``thinking``), never only the focused chat.
          // Dropping a background chat's output while the user views another
          // chat would lose the tool-call description (the collapsible block
          // opener), so when the user switches back the streaming command
          // output renders bare instead of inside the tool-call block. The
          // backend tags every event with chatId + workspaceId (see the
          // ``_OutputBridge`` chat getters), so the bucket is always correct.
          if (!eventKey || !String(data.chatId ?? "")) break;
          appendSegment("step", stepText, eventKey);
          break;
        }
        case "tool_feedback_repaint": {
          const stepText = String(data.text ?? "");
          repaintLastToolPrompt(stepText, eventKey);
          break;
        }
        case "assistant": {
          appendSegment("answer", String(data.text ?? ""), eventKey);
          break;
        }
        case "thinking": {
          appendThinking(String(data.text ?? ""), eventKey);
          break;
        }
        case "request_user_input_answer": {
          const selectionData = event.data as { answer?: string };
          const answer = String(selectionData.answer ?? "").trim();
          if (!answer || !eventKey) break;
          // Keep the live transcript's choice as its own visual round. This
          // matches the persisted-history ``ask-selection`` row instead of
          // appending a plain line to the tool-output block.
          setTurnsByChat((prev) => {
            const turns = prev[eventKey];
            if (!turns || turns.length === 0) return prev;
            const next = [...turns];
            const turn = next[next.length - 1];
            const now = Date.now();
            const previousRounds = [...turn.rounds];
            const previous = previousRounds[previousRounds.length - 1];
            // The Ask model pass is complete once the user has selected an
            // answer.  Freeze it before adding the selection row so its
            // tool activity no longer appears to be spinning.
            if (previous && previous.waitEndedAt === null) {
              previousRounds[previousRounds.length - 1] = {
                ...previous,
                waitEndedAt: now,
              };
            }
            next[next.length - 1] = {
              ...turn,
              rounds: [...previousRounds, {
                id: nextIdRef.current++,
                waitStartedAt: now,
                waitEndedAt: now,
                segments: [],
                selection: answer,
              }],
            };
            return { ...prev, [eventKey]: next };
          });
          break;
        }
        case "confirm": {
          const req = event.data as ConfirmRequest;
          const ownerChat = String(req.chatId || chatId || "");
          if (!ownerChat) break;
          const ownerKey = chatKey(eventWsId, ownerChat);
          setConfirmRequestByChat((prev) => ({
            ...prev,
            [ownerKey]: { ...req, chatId: ownerChat },
          }));
          break;
        }
        case "console_output": {
          const d = event.data as Record<string, unknown>;
          const id = String(d.id || "");
          const b64 = String(d.b64 || "");
          const end = Boolean(d.end);
          const handlers = consoleOutputHandlersRef.current.get(id);
          if (handlers) {
            for (const handler of handlers) {
              try {
                handler(b64, end);
              } catch {
                // A misbehaving handler must not break event dispatch.
              }
            }
          }
          break;
        }
        case "console_open": {
          const d = event.data as Record<string, unknown>;
          const id = String(d.id || "");
          if (!id) break;
          pendingAutoConsoleRef.current = {
            id,
            title: String(d.title || ""),
            kind: String(d.kind || ""),
          };
          setConsoleOpen(true);
          try {
            window.dispatchEvent(new CustomEvent("codewood:console-open"));
          } catch {
            // Custom event may not be supported; the bootstrap fallback
            // in ConsolePanel will pick it up on next dock open.
          }
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
        case "sub_agent_start": {
          const d = event.data as { sessionId: string; name: string; topic: string; description: string; prompt: string };
          const sessionId = String(d.sessionId || "");
          if (!sessionId) break;
          // If we're already viewing this session, update it
          const current = activeSubAgentSessionRef.current;
          if (current && current.id === sessionId) {
            const updated: SubAgentSession = {
              ...current,
              name: String(d.name || current.name),
              topic: String(d.topic || current.topic || ""),
              description: String(d.description || current.description),
              prompt: String(d.prompt || current.prompt),
            };
            applySubAgentSession(updated);
          }
          break;
        }
        case "sub_agent_assistant": {
          const d = event.data as { sessionId: string; text: string };
          const sessionId = String(d.sessionId || "");
          const current = activeSubAgentSessionRef.current;
          if (current && current.id === sessionId) {
            const msgs = [...current.messages];
            const text = String(d.text || "");
            const last = msgs[msgs.length - 1] as SubAgentMessage | undefined;
            if (
              last &&
              last.role === "assistant" &&
              !(last.tool_calls && last.tool_calls.length > 0)
            ) {
              const hadOnlyThinking = !!(last as unknown as { _thinking?: string })._thinking && !(last.content || "");
              msgs[msgs.length - 1] = {
                ...last,
                content: (last.content || "") + text,
              };
              if (hadOnlyThinking && !(msgs[msgs.length - 1] as any)._thinking_elapsed_seconds) {
                const startedAt = subAgentThinkingStartRef.current[sessionId];
                if (typeof startedAt === "number") {
                  (msgs[msgs.length - 1] as any)._thinking_elapsed_seconds = Math.round((Date.now() - startedAt) / 100) / 10;
                }
              }
            } else {
              // A fresh assistant turn (first delta of a round, or a round that
              // had no prior text-only assistant message).
              msgs.push({ role: "assistant", content: text });
            }
            const updated: SubAgentSession = { ...current, messages: msgs };
            applySubAgentSession(updated);
          }
          break;
        }
        case "sub_agent_thinking": {
          const d = event.data as { sessionId: string; text: string };
          const sessionId = String(d.sessionId || "");
          const current = activeSubAgentSessionRef.current;
          if (current && current.id === sessionId) {
            const msgs = [...current.messages];
            const text = String(d.text || "");
            const lastMsg = msgs[msgs.length - 1] as SubAgentMessage | undefined;
            const lastIsTextAssistant =
              !!lastMsg &&
              lastMsg.role === "assistant" &&
              !(lastMsg.tool_calls && lastMsg.tool_calls.length > 0);
            const hadThinking = lastIsTextAssistant && !!((lastMsg as unknown as { _thinking?: string })._thinking);
            if (lastIsTextAssistant) {
              msgs[msgs.length - 1] = {
                ...lastMsg,
                _thinking: ((lastMsg as unknown as { _thinking?: string })._thinking || "") + text,
              };
            } else {
              const newMsg: SubAgentMessage = { role: "assistant", content: "", _thinking: text };
              // If the previous message is a tool-call placeholder (created by
              // sub_agent_tool_call before thinking arrived), insert the thinking
              // message BEFORE it so the display order is: thought → tool calls.
              if (lastMsg?.role === "assistant" && lastMsg.tool_calls?.length) {
                msgs.splice(msgs.length - 1, 0, newMsg);
              } else {
                msgs.push(newMsg);
              }
            }
            if (!hadThinking && !subAgentThinkingStartRef.current[sessionId]) {
              subAgentThinkingStartRef.current[sessionId] = Date.now();
            }
            const updated: SubAgentSession = { ...current, messages: msgs };
            applySubAgentSession(updated);
          }
          break;
        }
        case "sub_agent_thinking_end": {
          const d = event.data as { sessionId: string; thinkingElapsedSeconds: number };
          const sessionId = String(d.sessionId || "");
          const current = activeSubAgentSessionRef.current;
          if (current && current.id === sessionId && typeof d.thinkingElapsedSeconds === "number" && d.thinkingElapsedSeconds > 0) {
            const msgs = current.messages ? [...current.messages] : [];
            for (let i = msgs.length - 1; i >= 0; i--) {
              const m = msgs[i] as SubAgentMessage;
              if (m.role === "assistant" && (m as any)._thinking) {
                msgs[i] = { ...m, _thinking_elapsed_seconds: d.thinkingElapsedSeconds };
                break;
              }
            }
            delete subAgentThinkingStartRef.current[sessionId];
            const updated: SubAgentSession = { ...current, messages: msgs };
            applySubAgentSession(updated);
          }
          break;
        }
        case "sub_agent_tool_call": {
          const d = event.data as { sessionId: string; toolName: string; args: Record<string, unknown>; thinkingElapsedSeconds?: number };
          const sessionId = String(d.sessionId || "");
          const current = activeSubAgentSessionRef.current;
          if (current && current.id === sessionId) {
            const msgs = current.messages ? [...current.messages] : [];
            // Backfill the backend-computed thinking elapsed on the previous
            // assistant message that has _thinking. Overwrites any client-side
            // estimate with the authoritative server measurement.
            if (typeof d.thinkingElapsedSeconds === "number" && d.thinkingElapsedSeconds > 0) {
              for (let i = msgs.length - 1; i >= 0; i--) {
                const m = msgs[i] as SubAgentMessage;
                if (m.role === "assistant" && (m as any)._thinking) {
                  msgs[i] = { ...m, _thinking_elapsed_seconds: d.thinkingElapsedSeconds };
                  break;
                }
              }
            }
            delete subAgentThinkingStartRef.current[sessionId];
            msgs.push({
              role: "assistant",
              content: "",
              tool_calls: [{ name: String(d.toolName || ""), args: d.args || {} }],
            });
            const updated: SubAgentSession = {
              ...current,
              messages: msgs,
            };
            applySubAgentSession(updated);
          }
          break;
        }
        case "sub_agent_output": {
          const d = event.data as { sessionId: string; text: string; toolName: string; toolRound?: string };
          const sessionId = String(d.sessionId || "");
          const current = activeSubAgentSessionRef.current;
          if (current && current.id === sessionId) {
            const msgs = [...current.messages];
            const outputToolName = String(d.toolName || "");
            // Walk backwards to find the assistant message whose next
            // pending tool call name matches this output's toolName.
            let matchedIdx = -1;
            for (let i = msgs.length - 1; i >= 0; i--) {
              const msg = msgs[i];
              if (!msg || msg.role !== "assistant") {
                continue;
              }
              const existing = msg.tool_rounds || [];
              const nextCall = Array.isArray(msg.tool_calls)
                ? msg.tool_calls[existing.length]
                : undefined;
              const callName = String(nextCall?.function?.name || nextCall?.name || "");
              if (callName && callName === outputToolName) {
                matchedIdx = i;
                break;
              }
            }
            if (matchedIdx >= 0) {
              const msg = msgs[matchedIdx];
              const existing = msg.tool_rounds || [];
              const nextCall = Array.isArray(msg.tool_calls)
                ? msg.tool_calls[existing.length]
                : undefined;
              const nextRound =
                d.toolRound ||
                (
                  nextCall
                    ? buildFallbackToolRoundFromCall(nextCall, String(d.text || ""), { lang })
                    : buildFallbackToolRound(String(d.toolName || ""), {}, String(d.text || ""), "", { lang })
                );
              if (nextRound) {
                msgs[matchedIdx] = { ...msg, tool_rounds: [...existing, nextRound] };
              }
            } else {
              // No matching pending tool call exists — the output arrived
              // before its tool_call event, or the tool_call was already
              // resolved. Push a new standalone assistant message so the
              // output never leaks onto an unrelated tool call.
              const standaloneRound =
                d.toolRound ||
                buildFallbackToolRound(String(d.toolName || ""), {}, String(d.text || ""), "", { lang });
              msgs.push({
                role: "assistant",
                content: "",
                tool_calls: [{ name: String(d.toolName || "") }],
                tool_rounds: standaloneRound ? [standaloneRound] : [],
              });
            }
            msgs.push({
              role: "tool" as const,
              name: String(d.toolName || ""),
              content: String(d.text || ""),
            });
            const updated: SubAgentSession = { ...current, messages: msgs };
            applySubAgentSession(updated);
          }
          break;
        }
        case "sub_agent_end": {
          const d = event.data as { sessionId: string; output: string; success: boolean; max_rounds_reached?: boolean };
          const sessionId = String(d.sessionId || "");
          const current = activeSubAgentSessionRef.current;
          if (current && current.id === sessionId) {
            const updated: SubAgentSession = {
              ...current,
              output: String(d.output || ""),
              success: Boolean(d.success),
              maxRoundsReached: Boolean(d.max_rounds_reached),
              endedAt: new Date().toISOString(),
            };
            applySubAgentSession(updated);
          }
          break;
        }
        case "file_changes": {
          const fileChangesData = event.data as FileChangeSummary & { chatId?: string; workspaceId?: string; turnIndex?: number };
          if (fileChangesData && fileChangesData.totalFiles > 0) {
            // Store file changes on the current turn so they persist in the message list
            setTurnsByChat((prev) => {
              const turns = prev[eventKey];
              if (!turns || turns.length === 0) return prev;
              const lastTurn = { ...turns[turns.length - 1], fileChanges: fileChangesData };
              const newTurns = [...turns];
              newTurns[newTurns.length - 1] = lastTurn;
              return { ...prev, [eventKey]: newTurns };
            });
          }
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
        // Hydrate pending inputs from persisted state. On restart the auto-send
        // flag stays false so the user must manually trigger the queue.
        if (value.chats) {
          const wsId = String(value.workspace?.id ?? activeWorkspaceId ?? "");
          const hydratedInputs: Record<string, string[]> = {};
          for (const ch of value.chats) {
            if (ch.pendingInputs && Array.isArray(ch.pendingInputs) && ch.pendingInputs.length > 0) {
              hydratedInputs[chatKey(wsId, ch.id)] = ch.pendingInputs;
            }
          }
          if (Object.keys(hydratedInputs).length > 0) {
            setPendingInputsByChat((prev) => ({ ...prev, ...hydratedInputs }));
          }
        }
        // Sync model presets to backend on startup (fire-and-forget).
        client.syncModelPresets(MODEL_PRESETS.filter((p) => p.id !== "custom")).catch(() => {});
      })
      .catch(() => setConnected(false));

    return () => {
      source?.close();
    };
  }, [client, connGeneration, appendSegment, startTurn, startRound, endRound, endActiveTurn, setBusyForChat]);

  // Backend crash recovery: the host restarts the serve process on a fresh
  // port/token and publishes the new endpoint. While disconnected we poll the
  // host bridge (and listen for its push notification) and rebuild the
  // ApiClient once the endpoint changes, which re-runs the event-stream
  // effect above and reconnects. No-op in a plain browser (no host to poll).
  useEffect(() => {
    const api = hostApi();
    if (!api || typeof api.backend_info !== "function") {
      return;
    }
    let cancelled = false;

    const applyInfo = (port: number, token: string) => {
      const cur = clientRef.current;
      if (!cur) {
        return;
      }
      if (String(port) === cur.port && token === cur.token) {
        return;
      }
      clientRef.current = new ApiClient(String(port), token);
      setConnGeneration((g) => g + 1);
    };

    const onPush = (e: Event) => {
      const detail = (e as CustomEvent<{ port: number; token: string }>).detail;
      if (detail && typeof detail.port === "number") {
        applyInfo(detail.port, detail.token);
      }
    };

    const check = async () => {
      if (cancelled) {
        return;
      }
      try {
        const info = await api.backend_info!();
        if (!cancelled && info && typeof info.port === "number") {
          applyInfo(info.port, info.token);
        }
      } catch {
        // Host bridge busy/unavailable; retry on the next tick.
      }
    };

    window.addEventListener("codewood:backend-restarted", onPush);
    if (!connected) {
      void check();
      const timer = setInterval(() => void check(), 1500);
      return () => {
        cancelled = true;
        clearInterval(timer);
        window.removeEventListener("codewood:backend-restarted", onPush);
      };
    }
    return () => {
      cancelled = true;
      window.removeEventListener("codewood:backend-restarted", onPush);
    };
  }, [connected, client, connGeneration]);

  const persistPendingInputs = useCallback(
    async (chatId: string, wsId: string, inputs: string[]) => {
      try {
        await client.savePendingInputs(chatId, inputs, wsId);
      } catch {
        // Best-effort persistence
      }
    },
    [client],
  );

  const sendNextPending = useCallback(
    async (chatKeyVal: string) => {
      const inputs = pendingInputsByChatRef.current[chatKeyVal];
      if (!inputs || inputs.length === 0) {
        return;
      }
      const next = inputs[0];
      const remaining = inputs.slice(1);
      setPendingInputsByChat((prev) => {
        const nextState = { ...prev, [chatKeyVal]: remaining };
        if (remaining.length === 0) {
          delete nextState[chatKeyVal];
        }
        return nextState;
      });
      const { wsId, chatId } = parseChatKey(chatKeyVal);
      if (chatId) {
        void persistPendingInputs(chatId, wsId, remaining);
      }
      if (remaining.length === 0) {
        setPendingAutoSendByChat((prev) => {
          const next = { ...prev };
          delete next[chatKeyVal];
          return next;
        });
      }
      setTodoDockVisible(false);
      await client.sendInput(next, true, chatId, wsId);
    },
    [client, persistPendingInputs],
  );
  const sendNextPendingRef = useRef(sendNextPending);
  sendNextPendingRef.current = sendNextPending;

  const queueModelConfigUpdate = useCallback((run: () => Promise<void>) => {
    const next = pendingModelConfigRef.current
      .catch(() => undefined)
      .then(run);
    pendingModelConfigRef.current = next.catch(() => undefined);
    return next;
  }, []);

  // When a draft chat is materialized on first send, any staged attachments
  // (workspace cache ``draft-attachments``) must move into the new chat's
  // side-data dir, and every path reference inside the message (the file
  // ATTACH envelope + the image-ref tokens) must be rewritten to the new
  // location so the model still resolves them with ``read``.
  const migrateDraftAttachmentPaths = useCallback(
    async (text: string, chatId: string, wsId: string): Promise<string> => {
      const attachRe = /\uE100ATTACH:([^\uE100\uE101\r\n]+)\uE101/g;
      const imgRe = new RegExp(
        `${IMG_OPEN}([^${IMG_OPEN}${IMG_CLOSE}\\r\\n]+)${IMG_CLOSE}`,
        "g",
      );
      const paths: string[] = [];
      const seen = new Set<string>();
      const collect = (m: RegExpExecArray | null) => {
        if (m && m[1] && !seen.has(m[1])) {
          seen.add(m[1]);
          paths.push(m[1]);
        }
      };
      let m: RegExpExecArray | null;
      attachRe.lastIndex = 0;
      while ((m = attachRe.exec(text))) collect(m);
      imgRe.lastIndex = 0;
      while ((m = imgRe.exec(text))) collect(m);
      if (paths.length === 0) {
        return text;
      }
      const mapping = await client.materializeDraftAttachments(
        chatId,
        paths,
        wsId,
      );
      if (!mapping) {
        return text;
      }
      let out = text;
      for (const [oldPath, newPath] of Object.entries(mapping)) {
        if (oldPath === newPath) continue;
        out = out
          .split(`\uE100ATTACH:${oldPath}\uE101`)
          .join(`\uE100ATTACH:${newPath}\uE101`);
        out = out
          .split(`${IMG_OPEN}${oldPath}${IMG_CLOSE}`)
          .join(`${IMG_OPEN}${newPath}${IMG_CLOSE}`);
      }
      return out;
    },
    [client],
  );

  const sendInput = useCallback(
    async (text: string) => {
      const trimmed = text.trim();
      if (!trimmed) {
        return;
      }
      // Diagnostic command: send straight to the server (no optimistic turn,
      // no pending-queue buffering) so it reaches the backend even while the
      // chat is busy/stuck. The server always handles it as a health dump.
      if (trimmed === "/server-health") {
        await client.sendInput(
          trimmed,
          true,
          activeChatIdRef.current,
          activeWorkspaceIdRef.current,
        );
        return;
      }
      let targetChatId = activeChatIdRef.current;
      let targetWsId = activeWorkspaceIdRef.current;
      if (draftModeRef.current) {
        const target = await materializeDraftChat();
        if (!target?.chatId) {
          return;
        }
        targetChatId = target.chatId;
        targetWsId = target.workspaceId;
        // Move staged draft attachments into the new chat's data dir and
        // rewrite their paths inside the message before sending.
        const finalText = await migrateDraftAttachmentPaths(
          trimmed,
          targetChatId,
          targetWsId,
        );
        startOptimisticTurn(finalText, chatKey(targetWsId, targetChatId));
        setBusyForChat(chatKey(targetWsId, targetChatId), true);
        await client.sendInput(finalText, true, targetChatId, targetWsId);
        return;
      }
      const key = chatKey(targetWsId, targetChatId);
      const isBusy = busyByChatRef.current[key] ?? false;
      if (isBusy && targetChatId) {
        setPendingInputsByChat((prev) => {
          const existing = prev[key] ?? [];
          const next = { ...prev, [key]: [...existing, trimmed] };
          return next;
        });
        setPendingAutoSendByChat((prev) => ({ ...prev, [key]: true }));
        void persistPendingInputs(
          targetChatId,
          targetWsId,
          [...(pendingInputsByChatRef.current[key] ?? []), trimmed],
        );
        return;
      }
      // When there are pending items with auto-send off (e.g. restart with
      // unsent queue) and the user sends a new message instead of clicking
      // the queue's send button, re-enable auto-send so the queue continues
      // after the new message's turn finishes.
      if (!isBusy && targetChatId) {
        const existingPending = pendingInputsByChatRef.current[key];
        if (existingPending && existingPending.length > 0) {
          setPendingAutoSendByChat((prev) => ({ ...prev, [key]: true }));
        }
      }
      await pendingModelConfigRef.current;
      await client.sendInput(trimmed, true, targetChatId, targetWsId);
    },
    [client, materializeDraftChat, migrateDraftAttachmentPaths, startOptimisticTurn, setBusyForChat, persistPendingInputs],
  );

  const startPendingInputs = useCallback(async () => {
    const key = chatKey(activeWorkspaceIdRef.current, activeChatIdRef.current);
    if (!key) {
      return;
    }
    const inputs = pendingInputsByChatRef.current[key];
    if (!inputs || inputs.length === 0) {
      return;
    }
    setPendingAutoSendByChat((prev) => ({ ...prev, [key]: true }));
    await sendNextPendingRef.current(key);
  }, []);

  const cancelPendingInput = useCallback(
    (index: number): string | null => {
      const key = chatKey(activeWorkspaceIdRef.current, activeChatIdRef.current);
      if (!key) {
        return null;
      }
      const inputs = pendingInputsByChatRef.current[key];
      if (!inputs || index < 0 || index >= inputs.length) {
        return null;
      }
      const removed = inputs[index];
      const remaining = inputs.filter((_, i) => i !== index);
      setPendingInputsByChat((prev) => {
        if (remaining.length === 0) {
          const next = { ...prev };
          delete next[key];
          return next;
        }
        return { ...prev, [key]: remaining };
      });
      if (remaining.length === 0) {
        setPendingAutoSendByChat((prev) => {
          const next = { ...prev };
          delete next[key];
          return next;
        });
      }
      const { wsId, chatId } = parseChatKey(key);
      if (chatId) {
        void persistPendingInputs(chatId, wsId, remaining);
      }
      return removed;
    },
    [persistPendingInputs],
  );

  const sendPendingInputNow = useCallback(
    async (index: number): Promise<void> => {
      const key = chatKey(activeWorkspaceIdRef.current, activeChatIdRef.current);
      if (!key) {
        return;
      }
      const inputs = pendingInputsByChatRef.current[key];
      if (!inputs || index < 0 || index >= inputs.length) {
        return;
      }
      const text = inputs[index];
      const { wsId, chatId } = parseChatKey(key);
      if (!chatId) {
        return;
      }
      // The idle that closes the PAUSED turn must not drain the queue: the
      // remaining messages stay queued until the jumped task finishes, then
      // auto-send resumes (one per turn). Set the marker BEFORE pausing so it
      // is already in place if the paused turn's idle races the POST response.
      if (busyByChatRef.current[key]) {
        suppressAutoSendOnceRef.current[key] = true;
      }
      // Pause the running task first: the backend interrupts the current turn
      // (same cooperative mechanism as Stop) while keeping the interrupted
      // shell call's notice as "interrupted to add more information" instead
      // of a plain cancel.
      await client.pause(chatId, wsId);
      // Drop this message from the pending queue now that it is being sent.
      const remaining = inputs.filter((_, i) => i !== index);
      setPendingInputsByChat((prev) => {
        const next = { ...prev, [key]: remaining };
        if (remaining.length === 0) {
          delete next[key];
        }
        return next;
      });
      if (remaining.length === 0) {
        setPendingAutoSendByChat((prev) => {
          const next = { ...prev };
          delete next[key];
          return next;
        });
      }
      void persistPendingInputs(chatId, wsId, remaining);
      setTodoDockVisible(false);
      // Send the jumped message immediately. The backend queues it until the
      // paused turn unwinds, then processes it before any auto-sent follow-up.
      await client.sendInput(text, true, chatId, wsId);
    },
    [client, persistPendingInputs],
  );

  const runCommand = useCallback(
    async (command: string) => {
      await client.sendInput(
        command,
        false,
        activeChatIdRef.current,
        activeWorkspaceIdRef.current,
      );
    },
    [client],
  );

  const interrupt = useCallback(async () => {
    const key = chatKey(activeWorkspaceIdRef.current, activeChatIdRef.current);
    if (key) {
      // Scope the interrupt to the chat whose stop button was clicked so a
      // different chat's running task is never aborted by mistake.
      await client.interrupt(
        activeChatIdRef.current,
        activeWorkspaceIdRef.current,
      );
    }
    // Stop auto-send for the pending queue: clicking Stop means the user
    // wants to halt everything, not just the current turn. The pending list
    // re-shows its send button so the user can manually resume.
    if (key) {
      setPendingAutoSendByChat((prev) => {
        if (!prev[key]) {
          return prev;
        }
        const next = { ...prev };
        delete next[key];
        return next;
      });
    }
  }, [client]);

  const answerConfirm = useCallback(
    async (answer: string) => {
      const key = chatKey(activeWorkspaceIdRef.current, activeChatIdRef.current);
      const current = confirmRequestByChat[key];
      if (current) {
        setConfirmRequestByChat((prev) => {
          const next = { ...prev };
          delete next[key];
          return next;
        });
        await client.confirm(current.id, answer);
      }
    },
    [client, confirmRequestByChat],
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

  const trimHistoryTurnsForEdit = useCallback(
    (index: number) => {
      if (index >= 0 || historyTurns.length === 0) {
        return;
      }
      const activeKey = chatKey(
        activeWorkspaceIdRef.current,
        activeChatIdRef.current,
      );
      const live = turnsByChatRef.current[activeKey] ?? EMPTY_TURNS;
      const histUserCount = historyTurns.reduce(
        (count, turn) => count + (turn.userText ? 1 : 0),
        0,
      );
      const liveUserCount = live.reduce(
        (count, turn) => count + (turn.userText ? 1 : 0),
        0,
      );
      const totalUserCount = histUserCount + liveUserCount;
      const targetOrdinal = totalUserCount + index + 1;
      if (targetOrdinal <= 0 || targetOrdinal > histUserCount) {
        return;
      }
      let seenUsers = 0;
      let keepCount = historyTurns.length;
      for (let i = 0; i < historyTurns.length; i += 1) {
        if (!historyTurns[i].userText) {
          continue;
        }
        seenUsers += 1;
        if (seenUsers === targetOrdinal) {
          keepCount = i;
          break;
        }
      }
      setHistoryTurns(historyTurns.slice(0, keepCount));
      setHistoryTotal(historyStart + keepCount);
    },
    [historyTurns, historyStart],
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

  // Like ``dropSettledLiveTurns`` but only drops settled turns whose user text
  // is already represented in the just-fetched history page. Settled turns that
  // are MISSING from the page are kept so a completed task never vanishes from
  // the transcript: the page can be stale when the reload's response races the
  // persistence flush, or when a NEWER turn started streaming before the
  // response arrived (e.g. an auto-sent pending message right after the
  // previous turn's idle event). The next history reload reconciles them.
  const dropSettledLiveTurnsNotInHistory = useCallback(
    (chatId: string, pageTurns: HistoryTurn[]) => {
      // User text alone is not a turn identity: a user can legitimately send
      // the same prompt twice.  Associate each archived text with its message
      // timestamp, so a stale page containing an *older* identical prompt
      // cannot make us discard the just-finished live turn (the regression
      // that briefly hid the latest task after command completion).
      const archivedAtByText = new Map<string, number[]>();
      for (const turn of pageTurns) {
        const text = String(turn.userText || "").trim();
        if (!text) continue;
        const timestamp = Date.parse(String(turn.timestamp || "").replace(" ", "T"));
        const timestamps = archivedAtByText.get(text) ?? [];
        timestamps.push(timestamp);
        archivedAtByText.set(text, timestamps);
      }
      setTurnsByChat((prev) => {
        const list = prev[chatId];
        if (!list || list.length === 0) {
          return prev;
        }
        const filtered = list.filter((tt) => {
          if (tt.endedAt === null) {
            return true;
          }
          // Placeholder turns created by early tool output have no user text
          // and nothing to pin to history — drop them once settled.
          if (!String(tt.userText || "").trim()) {
            return false;
          }
          // Persisted copies may normalize whitespace / line endings, so match
          // on the trimmed user text: a settled turn that is already archived
          // must be dropped instead of lingering at the tail (which reorders
          // the transcript around newer history turns).
          const archivedAt = archivedAtByText.get(String(tt.userText || "").trim());
          const liveOutput = tt.rounds
            .flatMap((round) => round.segments.map((segment) => segment.text))
            .join("")
            .trim();
          const archivedOutput = pageTurns
            .filter((turn) => String(turn.userText || "").trim() === String(tt.userText || "").trim())
            .flatMap((turn) => turn.rounds ?? [])
            .map((round) => `${round.tools || ""}${round.text || ""}${round.thinking || ""}`)
            .join("");
          // Structured history records the user's send time, while the live
          // turn records when its matching SSE start arrived.  They should be
          // seconds apart.  Keep a generous window for a paused UI thread,
          // but never treat an hours-old identical prompt as this live turn.
          const represented = archivedAt?.some((timestamp) =>
            !Number.isFinite(timestamp) || Math.abs(timestamp - tt.startedAt) <= 120_000,
          ) || Boolean(liveOutput && archivedOutput.includes(liveOutput));
          if (represented) {
            return false;
          }
          return true;
        });
        if (filtered.length === list.length) {
          return prev;
        }
        const next = { ...prev };
        if (filtered.length === 0) {
          delete next[chatId];
        } else {
          next[chatId] = filtered;
        }
        return next;
      });
    },
    [],
  );

  const INITIAL_HISTORY = 12;
  const HISTORY_PAGE = 8;

  // Load the most recent history page for the active chat. `forChatId` names
  // the chat being loaded (it may differ from the focused chat momentarily,
  // right after a switch before the idle state arrives); its live turns are
  // dropped since they are now part of the persisted history.
  const loadChatHistory = useCallback(
    async (target?: { chatId?: string; wsId?: string }) => {
      const cid = target?.chatId ?? activeChatIdRef.current;
      const wsId = target?.wsId ?? activeWorkspaceIdRef.current;
      // ``getChatHistory`` always returns the focused chat's history, so the
      // live-turn bucket to reconcile is the focused workspace's composite
      // key for ``cid``.
      const key = chatKey(wsId, cid);
      // Capture the expected composite key at start; if it has changed by the
      // time the async response arrives the user switched chats and we must
      // discard the stale result (e.g. a slow response for an intermediate
      // workspace-switch state event can race past a fast empty-chat response
      // from the new-chat idle event and overwrite the correct history).
      const expectedKey = historyChatRef.current;
      setHistoryLoading(true);
      // When history reloads (e.g. after a turn finishes), the persisted
      // compact summary is already part of the returned structured turns.
      // Clear the live compact notice so it doesn't duplicate the history turn.
      setCompactNoticeState((state) =>
        state.notice
          ? { chatKey: "", notice: null, version: state.version + 1 }
          : state,
      );
      try {
        const page = await client.getChatHistory(undefined, INITIAL_HISTORY, cid, wsId);
        // The user switched to a different chat while we were fetching.
        if (historyChatRef.current !== expectedKey) {
          return;
        }
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
          // when the persisted page already carries its trailing duplicate
          // (the user message of the SAME in-progress turn, no answer yet),
          // drop that tail. A brand-new chat has no persisted history yet
          // (empty page) — keep the live turn untouched so the optimistically
          // echoed user message survives this reload instead of being cleared.
          //
          // IMPORTANT: only drop the tail when it really IS that duplicate.
          // The reload may race a NEW turn's start: the idle event that
          // triggered this fetch belongs to the PREVIOUS turn, and the new
          // turn (e.g. an auto-sent pending message) can begin streaming
          // before the response arrives. In that case the trailing persisted
          // turn is the COMPLETED previous turn, not the streaming one —
          // dropping it (and then dropping the settled live turns) would erase
          // the completed task's user message and steps from the transcript
          // until the next chat switch.
          const activeTurns = live.filter((tt) => tt.endedAt === null);
          const tail = page.turns[page.turns.length - 1];
          const tailTimestamp = Date.parse(
            String(tail?.timestamp || "").replace(" ", "T"),
          );
          const tailIsStreamingDuplicate = !!tail && activeTurns.some((turn) => {
            if (String(turn.userText || "").trim() !== String(tail.userText || "").trim()) {
              return false;
            }
            // A history reload after switching away and back may already have
            // replayed the running turn's tool rounds.  It is still the same
            // live turn and must not render a second time.  Match its user
            // send time to avoid mistaking an older, identical prompt for the
            // active one; retain the old no-round fallback for legacy history
            // entries that have no parseable timestamp.
            const liveOutput = turn.rounds
              .flatMap((round) => round.segments.map((segment) => segment.text))
              .join("")
              .trim();
            const tailOutput = (tail.rounds ?? [])
              .map((round) => `${round.tools || ""}${round.text || ""}${round.thinking || ""}`)
              .join("");
            return Number.isFinite(tailTimestamp)
              ? Math.abs(tailTimestamp - turn.startedAt) <= 120_000 ||
                Boolean(liveOutput && tailOutput.includes(liveOutput))
              : Boolean(liveOutput && tailOutput.includes(liveOutput)) ||
                (!Array.isArray(tail.rounds) || tail.rounds.length === 0);
          });
          const keptTurns = tailIsStreamingDuplicate
            ? page.turns.slice(0, -1)
            : page.turns;
          setHistoryTurns(keptTurns);
          setHistoryStart(page.start);
          setHistoryTotal(page.total);
          dropSettledLiveTurnsNotInHistory(key, keptTurns);
          if (tailIsStreamingDuplicate) {
            // ``idle`` can settle the live turn in the same event batch that
            // started this fetch.  In that narrow window this branch preserves
            // the live copy and hides its history copy, but no later event is
            // guaranteed to reconcile them.  Recheck after React has applied
            // the settlement; if the turn is now done, the normal history path
            // replaces it with the archived copy.
            window.setTimeout(() => {
              if (
                historyChatRef.current === expectedKey &&
                !(turnsByChatRef.current[key] ?? []).some((turn) => turn.endedAt === null)
              ) {
                reloadHistoryRef.current();
              }
            }, 100);
          }
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
          // All turns are settled — the backend's structured-turn builder
          // already attaches fileChanges to each turn via [FILE_CHANGE_REF]
          // messages in the conversation history. Settled live turns are only
          // dropped when the page actually represents them (a stale page from
          // a persistence race must leave them visible instead of blanking a
          // just-finished task).
          setHistoryTurns(page.turns);
          setHistoryStart(page.start);
          setHistoryTotal(page.total);
          dropSettledLiveTurnsNotInHistory(key, page.turns);
        }
      } finally {
        setHistoryLoading(false);
      }
    },
    [client, dropSettledLiveTurnsNotInHistory],
  );

  // Keep a stable ref to the latest loadChatHistory so the SSE idle handler
  // (which closes over an old render) can trigger a reload on demand.
  useEffect(() => {
    reloadHistoryRef.current = () => {
      void loadChatHistory({
        chatId: activeChatIdRef.current,
        wsId: activeWorkspaceIdRef.current,
      });
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
    const wsId = state?.workspace.id ?? "";
    if (!cid) {
      return;
    }
    const persisted = state?.askMoreInfo;
    const bucketKey = chatKey(wsId, cid);
    setAskMoreInfoByChat((prev) => {
      const existing = prev[bucketKey];
      if (!persisted) {
        if (!existing) {
          return prev;
        }
        // Backend says no pending prompt for this chat -> drop stale.
        const next = { ...prev };
        delete next[bucketKey];
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
        [bucketKey]: {
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
  }, [state?.workspace.id, state?.activeChatId, state?.askMoreInfo]);

  const loadOlderHistory = useCallback(async () => {
    if (historyLoading || historyStart <= 0) {
      return;
    }
    setHistoryLoading(true);
    try {
      const page = await client.getChatHistory(
        historyStart,
        HISTORY_PAGE,
        activeChatIdRef.current,
        activeWorkspaceIdRef.current,
      );
      setHistoryTurns((prev) => [...page.turns, ...prev]);
      setHistoryStart(page.start);
      setHistoryTotal(page.total);
    } finally {
      setHistoryLoading(false);
    }
  }, [client, historyLoading, historyStart]);

  // Auto-load all remaining history after the initial page loads so the
  // minimap can show the final stable line count without visible intermediate
  // decreases. The minimap stays hidden while historyStart > 0.
  const loadOlderHistoryRef = useRef(loadOlderHistory);
  loadOlderHistoryRef.current = loadOlderHistory;
  useEffect(() => {
    if (historyStart > 0 && !historyLoading) {
      void loadOlderHistoryRef.current();
    }
  }, [historyStart, historyLoading]);

  // Initial / external chat changes: (re)load history for the active chat.
  // Our own switchToChat/newChat update historyChatRef so this won't double-run.
  useEffect(() => {
    const cid = state?.activeChatId ?? "";
    const wsId = state?.workspace.id ?? "";
    const nextKey = chatKey(wsId, cid);
    if (!cid || !nextKey || nextKey === historyChatRef.current) {
      return;
    }
    // If the user has an explicit focus override (e.g. from a just-created
    // draft chat), only accept SSE events whose activeChatId matches the
    // user's intended focus.  Stale/delayed events (e.g. a workspace-switch
    // state event that arrives after the task already completed on the new
    // chat) are silently dropped so the correct history is preserved.
    const focusedId = activeChatIdRef.current;
    const focusedWs = activeWorkspaceIdRef.current;
    if (focusedId && (cid !== focusedId || wsId !== focusedWs)) {
      return;
    }
    // If the previously active chat still has a turn that hasn't finished
    // streaming, the supposedly new activeChatId is almost certainly an
    // artifact of a state or idle event that briefly clobbered the app
    // state (e.g. during a same-workspace update_plan mid-turn).
    // Loading history at this point would call clearLiveTurns and destroy
    // the in-progress streaming content.  Defer the reload until the
    // streaming turn settles (the idle handler will retry via
    // pendingHistoryReloadRef).
    const prevKey = historyChatRef.current;
    if (turnsByChatRef.current[prevKey]?.some((t) => t.endedAt === null)) {
      pendingHistoryReloadRef.current = true;
      return;
    }
    historyChatRef.current = nextKey;
    void loadChatHistory({ chatId: cid, wsId });
  }, [state?.workspace.id, state?.activeChatId, loadChatHistory]);

  const switchToChat = useCallback(
    async (chatId: string, workspaceId = "") => {
      // If we're viewing a sub-agent session, return to the main chat first.
      const currentSub = activeSubAgentSessionRef.current;
      if (currentSub) {
        subAgentCacheRef.current[currentSub.id] = currentSub;
        subAgentViewingRef.current = false;
        setActiveSubAgentSession(null);
        activeSubAgentSessionRef.current = null;
      }
      const prevKey = chatKey(activeWorkspaceIdRef.current, activeChatIdRef.current);
      const prevWsId = activeWorkspaceIdRef.current;
      const targetWsId = workspaceId || activeWorkspaceIdRef.current;
      const targetChatName = (() => {
        const activeWsId = stateRef.current?.workspace.id ?? "";
        const activeList = stateRef.current?.chats ?? [];
        const otherList = workspaceChatsRef.current[targetWsId] ?? [];
        const source = targetWsId === activeWsId ? activeList : otherList;
        const hit = source.find((chat) => chat.id === chatId);
        return hit?.name || t("chat.new");
      })();
      setFocusOverride(null);
      setOptimisticChatFocus(null);
      setCompactNoticeState((state) =>
        state.notice
          ? { chatKey: "", notice: null, version: state.version + 1 }
          : state,
      );
      setDraftMode(false);
      setDraftWorkspaceId("");
      historyChatRef.current = chatKey(targetWsId, chatId);
      activeWorkspaceIdRef.current = targetWsId;
      activeChatIdRef.current = chatId;
      setFocusOverride({ chatId, wsId: targetWsId });
      setOptimisticChatFocus({
        chatId,
        wsId: targetWsId,
        name: targetChatName,
      });
      setHistoryTurns([]);
      setHistoryStart(0);
      setHistoryTotal(0);
      setHistoryLoading(true);
      // When switching to a different workspace, record the target so the
      // subsequent idle/state SSE event from that workspace can bypass the
      // background-event guard (stateRef still has the old workspace ID).
      // Compare against the workspace we were on BEFORE this switch — the ref
      // was already advanced to the target above.
      if (workspaceId && workspaceId !== prevWsId) {
        pendingFocusWsIdRef.current = workspaceId;
      }
      const ok = await client.selectChat(chatId, workspaceId);
      if (!ok) {
        setHistoryLoading(false);
        setFocusOverride(null);
        setOptimisticChatFocus(null);
        return;
      }
      // Preserve any still-running turn in the chat we just left so its
      // background timer and live transcript keep updating in the sidebar /
      // switch-back view. Settled live turns are safe to drop because they are
      // already represented in persisted history.
      dropSettledLiveTurns(prevKey);
      // Switching to a still-running chat emits a mid-stream ``state`` event
      // that we intentionally ignore to protect segment accumulation, so carry
      // an optimistic focus override until the backend's terminal idle snapshot
      // catches up.
      await loadChatHistory({ chatId, wsId: targetWsId });
    },
    [client, dropSettledLiveTurns, loadChatHistory, t],
  );

  const selectWorkspace = useCallback(
    async (workspaceId: string) => {
      const currentSub = activeSubAgentSessionRef.current;
      if (currentSub) {
        subAgentCacheRef.current[currentSub.id] = currentSub;
        subAgentViewingRef.current = false;
        setActiveSubAgentSession(null);
        activeSubAgentSessionRef.current = null;
      }
      setFocusOverride(null);
      setOptimisticChatFocus(null);
      setCompactNoticeState((state) =>
        state.notice
          ? { chatKey: "", notice: null, version: state.version + 1 }
          : state,
      );
      pendingFocusWsIdRef.current = workspaceId;
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
      setFocusOverride(null);
      setOptimisticChatFocus(null);
      setCompactNoticeState((state) =>
        state.notice
          ? { chatKey: "", notice: null, version: state.version + 1 }
          : state,
      );
      setDraftWorkspaceId(wsId);
      setDraftMode(true);
      // Returning to a draft that still holds content (e.g. the user typed a
      // message, switched to another chat, then clicked New Chat again) must
      // keep the model/reasoning they chose for it, alongside the content.
      // Only a genuinely fresh draft resets to "inherit from last chat".
      if (draftHasContentRef.current) {
        // Re-apply the preserved selection to local state so the composer
        // shows it again (state.model currently reflects the chat we left).
        applyDraftSelectionToState(false);
      } else {
        // Clear any selection left over from a previous draft session.
        resetDraftSelectionRef();
      }
      historyChatRef.current = "\u0000";
      setHistoryTurns([]);
      setHistoryStart(0);
      setHistoryTotal(0);
      setState((prev) => {
        if (!prev) return prev;
        return { ...prev, contextUsage: undefined, plan: undefined, cacheStats: undefined, tokenStats: undefined };
      });
    },
    [state?.workspace.id, applyDraftSelectionToState],
  );

  // Pick the target workspace for the pending draft chat (compose mode only).
  const setDraftWorkspace = useCallback((workspaceId: string) => {
    setDraftWorkspaceId(workspaceId);
  }, []);

  // Delete a chat. If it was the active chat always drop into compose (draft)
  // mode for that workspace so the UI shows a clean New Chat view — no sidebar
  // highlight, no title-bar name. The idle event from the backend carries the
  // post-delete state but draft mode keeps the UI consistent.
  const deleteChat = useCallback(
    async (chatId: string, workspaceId = "") => {
      const wasActive = chatId === activeChatIdRef.current;
      const wsId = workspaceId || activeWorkspaceIdRef.current;
      // Whether deleting this chat empties the active workspace. (Only the
      // active workspace's chats are present in ``state.chats``.)
      const inActiveWs = !workspaceId || workspaceId === activeWorkspaceIdRef.current;
      const ok = await client.deleteChat(chatId, workspaceId);
      if (!ok) {
        return;
      }
      clearLiveTurns(chatKey(wsId, chatId));
      if (inActiveWs) {
        // Optimistically drop the deleted chat from the sidebar list so it
        // disappears immediately, without waiting for the SSE idle event.
        setState((prev) => {
          if (!prev || String(prev.workspace?.id || "") !== wsId) return prev;
          return { ...prev, chats: prev.chats.filter((c) => c.id !== chatId) };
        });
        if (wasActive) {
          // Always enter draft mode (New Chat) so the sidebar doesn't
          // high-light a sibling chat and the title bar shows no name.
          // The backend auto-switches to another chat via SSE on delete,
          // but draft mode keeps the UI showing a clean New Chat view.
          setDraftWorkspaceId(wsId);
          setDraftMode(true);
          resetDraftSelectionRef();
          historyChatRef.current = "\u0000";
          setHistoryTurns([]);
          setHistoryStart(0);
          setHistoryTotal(0);
          // The deleted chat is no longer active; clear the stale ref so
          // subsequent SSE events (output guard, idle merge) don't route to it.
          activeChatIdRef.current = "";
          setState((prev) => {
            if (!prev) return prev;
            return { ...prev, contextUsage: undefined, plan: undefined, cacheStats: undefined, tokenStats: undefined };
          });
        }
      } else {
        // Non-active workspace: optimistically remove the deleted chat from
        // the workspaceChats cache so the sidebar updates immediately.
        setWorkspaceChats((prev) => {
          const list = prev[wsId];
          if (!list) return prev;
          return { ...prev, [wsId]: list.filter((c) => c.id !== chatId) };
        });
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
      await client.sendInput(
        `/chat fork ${index}`,
        false,
        activeChatIdRef.current,
        activeWorkspaceIdRef.current,
      );
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
      trimHistoryTurnsForEdit(index);
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
      // Route the edit to the exact chat (and workspace) being edited so the
      // backend scopes its interrupt to that chat instead of the active one.
      await client.sendInput(
        `/chat edit ${index}`,
        false,
        activeChatId,
        activeWorkspaceIdRef.current,
      );
    },
    [client, clearTurns, trimHistoryTurnsForEdit, activeChatId, askMoreInfoByChat, setBusyForChat],
  );

  const openWorkspaceInExplorer = useCallback(
    (id: string) => client.openWorkspaceInExplorer(id),
    [client],
  );

  /** Delete a workspace (GUI-only), using the dedicated /delete-workspace
   *  endpoint that bypasses the chat runtime to avoid session-bleed. */
  const deleteWorkspaceViaApi = useCallback(
    async (id: string) => {
      return await client.deleteWorkspace(id);
    },
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

  const reorderWorkspace = useCallback(
    (workspaceId: string, beforeId: string | null) => {
      const order = uiPrefs.workspaceOrder;
      const newOrder = order.filter((id) => id !== workspaceId);
      if (beforeId === null) {
        newOrder.push(workspaceId);
      } else {
        const idx = newOrder.indexOf(beforeId);
        if (idx >= 0) {
          newOrder.splice(idx, 0, workspaceId);
        } else {
          newOrder.push(workspaceId);
        }
      }
      // Append any workspace not yet in the order list (e.g. newly added
      // workspaces) so they don't disappear from the display.
      const allIds = (state?.workspaces ?? []).map((w) => w.id);
      for (const id of allIds) {
        if (!newOrder.includes(id)) {
          newOrder.push(id);
        }
      }
      updatePrefs({
        ...uiPrefs,
        workspaceOrder: newOrder,
      });
    },
    [uiPrefs, updatePrefs, state?.workspaces],
  );

  const toggleChatPin = useCallback(
    (id: string) =>
      updatePrefs({ ...uiPrefs, pinnedChatIds: toggleId(uiPrefs.pinnedChatIds, id) }),
    [uiPrefs, updatePrefs],
  );

  const refreshWorkspaceChats = useCallback(
    async (id: string) => {
      const chats = await client.listWorkspaceChats(id);
      // The ACTIVE workspace's list is authoritative from ``state.chats`` (the
      // mirror effect keeps ``workspaceChats[activeWsId]`` in sync). A refresh
      // response may have been built BEFORE a select_chat cleared a chat's
      // unread, so letting it overwrite the cache would resurrect a cleared
      // dot the moment this workspace becomes non-active again. Skip the
      // active workspace; its live snapshot always wins.
      if (id === activeWorkspaceIdRef.current) {
        return;
      }
      setWorkspaceChats((prev) => ({ ...prev, [id]: chats }));
    },
    [client],
  );
  // Stable ref so the SSE idle handler (which closes over an old render) can
  // re-fetch a background workspace's list when one of its chats finishes.
  const refreshWorkspaceChatsRef = useRef<(id: string) => void>(() => {});
  useEffect(() => {
    refreshWorkspaceChatsRef.current = (id: string) => {
      void refreshWorkspaceChats(id);
    };
  }, [refreshWorkspaceChats]);

  const toggleChatArchive = useCallback(
    async (key: string) => {
      const { wsId, chatId } = parseChatKey(key);
      const ok = await client.toggleChatArchive(chatId, wsId);
      if (!ok) return;
      const wasActive =
        chatId === activeChatIdRef.current &&
        (!wsId || wsId === activeWorkspaceIdRef.current);
      if (wasActive) {
        clearLiveTurns(key);
        setDraftWorkspaceId(wsId);
        setDraftMode(true);
        resetDraftSelectionRef();
        historyChatRef.current = "\u0000";
        setHistoryTurns([]);
        setHistoryStart(0);
        setHistoryTotal(0);
      }
      if (wsId && wsId !== stateRef.current?.workspace?.id) {
        await refreshWorkspaceChats(wsId);
      }
    },
    [client, refreshWorkspaceChats, clearLiveTurns],
  );

  const archiveChats = useCallback(
    async (keys: string[]) => {
      for (const key of keys) {
        await toggleChatArchive(key);
      }
    },
    [toggleChatArchive],
  );

  const setModel = useCallback(
    async (selector: string) => {
      const value = selector.trim();
      if (!value) {
        return;
      }
      const targetChatId =
        selectedChatId || stateRef.current?.activeChatId || activeChatIdRef.current;
      if (draftModeRef.current) {
        // In draft mode just record the preference — don't materialize the
        // chat yet.  The selected model will be applied when the user sends
        // their first message (materializeDraftChat applies it afterwards).
        draftModelRef.current = value;
        // Switching models resets the reasoning-effort selection: the level
        // (if any) belonged to the previous model and must be re-picked for
        // the new one. Keep the composer dropdown and the materialized draft
        // in sync — otherwise the UI can show an effort the chat never got.
        draftReasoningRef.current = "";
        setState((prev) => {
          if (!prev) return prev;
          const patch = buildModelChangePatch(value, prev.model);
          return {
            ...prev,
            model: {
              ...prev.model,
              current: value,
              reasoningEffort: "",
              ...(patch ? { reasoningEfforts: patch.reasoningEfforts } : {}),
            },
          };
        });
        return;
      }
      const targetKey = chatKey(
        selectedWorkspaceId || stateRef.current?.workspace.id || activeWorkspaceIdRef.current,
        selectedChatId || stateRef.current?.activeChatId || activeChatIdRef.current,
      );
      if (targetKey) {
        const patch = buildModelChangePatch(value, stateRef.current?.model);
        setOptimisticModelByChat((prev) => ({
          ...prev,
          [targetKey]: {
            current: value,
            reasoningEffort: patch
              ? patch.reasoningEffort
              : stateRef.current?.model.reasoningEffort ?? "",
            reasoningEfforts: patch
              ? patch.reasoningEfforts
              : stateRef.current?.model.reasoningEfforts,
          },
        }));
      }
      setState((prev) => {
        if (!prev) return prev;
        const patch = buildModelChangePatch(value, prev.model);
        return {
          ...prev,
          model: {
            ...prev.model,
            current: value,
            ...(patch
              ? {
                  reasoningEfforts: patch.reasoningEfforts,
                  reasoningEffort: patch.reasoningEffort,
                }
              : {}),
          },
        };
      });
      await queueModelConfigUpdate(() =>
        client.setChatModel(
          targetChatId,
          value,
          selectedWorkspaceId || stateRef.current?.workspace.id || activeWorkspaceIdRef.current,
        ).then(() => undefined),
      );
    },
    [client, queueModelConfigUpdate, selectedWorkspaceId, selectedChatId],
  );

  const setReasoning = useCallback(
    async (level: string) => {
      // Empty ``level`` means "Default" (clear the selected effort). Unlike
      // model selection, Default is a valid choice and must not be dropped.
      const value = level.trim();
      const targetChatId =
        selectedChatId || stateRef.current?.activeChatId || activeChatIdRef.current;
      if (draftModeRef.current) {
        // In draft mode just record the preference — don't materialize the
        // chat yet. The selected reasoning effort will be applied when the
        // user sends their first message (materializeDraftChat applies it
        // afterwards). Mirrors setModel's draft-mode handling.
        draftReasoningRef.current = value;
        setState((prev) => {
          if (!prev) return prev;
          return { ...prev, model: { ...prev.model, reasoningEffort: value } };
        });
        return;
      }
      const targetKey = chatKey(
        selectedWorkspaceId || stateRef.current?.workspace.id || activeWorkspaceIdRef.current,
        selectedChatId || stateRef.current?.activeChatId || activeChatIdRef.current,
      );
      if (targetKey) {
        setOptimisticModelByChat((prev) => ({
          ...prev,
          [targetKey]: {
            current: prev[targetKey]?.current ?? stateRef.current?.model.current ?? "",
            reasoningEffort: value,
            reasoningEfforts:
              prev[targetKey]?.reasoningEfforts ?? stateRef.current?.model.reasoningEfforts,
          },
        }));
      }
      // Optimistically update local state so the dropdown reflects the change
      // immediately, even during active task execution. The backend applies the
      // change right away (_set_reasoning_effort is called directly), but SSE
      // "state" events are suppressed for the streaming chat to avoid disrupting
      // segment accumulation, so without this optimistic update the UI would
      // stay stuck on the old value until the task finishes.
      setState((prev) => {
        if (!prev) return prev;
        return { ...prev, model: { ...prev.model, reasoningEffort: value } };
      });
      await queueModelConfigUpdate(() =>
        client.setChatReasoning(
          targetChatId,
          value,
          selectedWorkspaceId || stateRef.current?.workspace.id || activeWorkspaceIdRef.current,
        ).then(() => undefined),
      );
    },
    [client, queueModelConfigUpdate, selectedWorkspaceId, selectedChatId],
  );

  const setExecutionPolicy = useCallback(
    async (policy: string) => {
      const value = policy.trim();
      if (value) {
        // Optimistically update local state so the dropdown reflects the change
        // immediately, even during active task execution. The backend applies the
        // change right away (agent.execution_policy is set on the fly), but SSE
        // "state" events are suppressed for the streaming chat to avoid disrupting
        // segment accumulation, so without this optimistic update the UI would
        // stay stuck on the old value until the task finishes.
        setState((prev) => {
          if (!prev) return prev;
          return { ...prev, executionPolicy: value };
        });
        await client.sendInput(`/execution-policy ${value}`);
      }
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

  const persistZoomLevel = useCallback((level: number) => {
    setZoomLevel(level);
    try {
      window.localStorage.setItem("codewood.zoomLevel", String(level));
    } catch {
      // localStorage may be unavailable; zoom won't persist across restarts.
    }
  }, []);

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

  /** Pick a folder via the native dialog and open it as a workspace.
   *  Uses the dedicated /open-folder endpoint that bypasses the chat
   *  runtime, preventing session-bleed from the old chat. */
  const pickAndOpenFolder = useCallback(async () => {
    const path = await pickFolder();
    if (path) {
      const result = await client.openFolder(path);
      if (result?.id) {
        pendingFocusWsIdRef.current = result.id;
        // Re-fetch state because the SSE idle event from /open-folder
        // may have arrived before pendingFocusWsIdRef was set (race),
        // causing the SSE handler to discard it (idleForFocused=false).
        try {
          const next = await client.getState();
          setState(next);
          if (!next.activeChatId) {
            setDraftMode(true);
            setDraftWorkspaceId(next.workspace.id);
            historyChatRef.current = "\u0000";
            setHistoryTurns([]);
            setHistoryStart(0);
            setHistoryTotal(0);
          }
        } catch {
          // getState failure is non-fatal; the workspace was created.
        }
      }
    }
  }, [pickFolder, client]);

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

  const enterSubAgentSession = useCallback(async (sessionId: string) => {
    // Check the live cache first — survives chat switches & workspace changes.
    const cached = subAgentCacheRef.current[sessionId];
    if (cached && !cached.endedAt) {
      activeSubAgentSessionRef.current = cached;
      subAgentViewingRef.current = true;
      setActiveSubAgentSession(cached);
      setSubAgentSessionLoading(false);
      return;
    }
    const live = activeSubAgentSessionRef.current;
    if (live && live.id === sessionId && !live.endedAt) {
      subAgentViewingRef.current = true;
      setActiveSubAgentSession(live);
      setSubAgentSessionLoading(false);
      return;
    }
    setSubAgentSessionLoading(true);
    try {
      const chatId = stateRef.current?.activeChatId || "";
      const session = await client.getSubAgentSessionHistory(sessionId, chatId);
      if (session) {
        activeSubAgentSessionRef.current = session;
        subAgentCacheRef.current[session.id] = session;
        subAgentViewingRef.current = true;
        setActiveSubAgentSession(session);
      }
    } catch {
      // Session load failure is non-fatal
    } finally {
      setSubAgentSessionLoading(false);
    }
  }, [client]);

  const exitSubAgentSession = useCallback(() => {
    const sessionId = activeSubAgentSessionRef.current?.id || "";
    setPendingExpandSubAgentId(sessionId);
    subAgentViewingRef.current = false;
    setActiveSubAgentSession(null);
    // Keep the ref + cache intact so SSE handlers continue to update the live
    // session while the user is in the main chat. On re-enter, enterSubAgentSession
    // prefers the in-memory data over a server reload.
  }, []);

  // Clear the pending auto-expand target shortly after returning to main chat,
  // once the transcript has had a chance to read it and expand the relevant rows.
  useEffect(() => {
    if (!pendingExpandSubAgentId) return;
    const timer = setTimeout(() => setPendingExpandSubAgentId(""), 1200);
    return () => clearTimeout(timer);
  }, [pendingExpandSubAgentId]);

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
            client.openFolder(path).then(async (r) => {
              if (r?.id) {
                pendingFocusWsIdRef.current = r.id;
                try {
                  const next = await client.getState();
                  setState(next);
                  if (!next.activeChatId) {
                    setDraftMode(true);
                    setDraftWorkspaceId(next.workspace.id);
                    historyChatRef.current = "\u0000";
                    setHistoryTurns([]);
                    setHistoryStart(0);
                    setHistoryTotal(0);
                  }
                } catch {
                  // non-fatal
                }
              }
            });
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
    client,
    state: displayState,
    activeWorkspaceId: selectedWorkspaceId,
    activeChatId: selectedChatId,
    activeChats: selectedChats,
    turns,
    historyTurns,
    historyStart,
    historyTotal,
    historyLoading,
    busy,
    busyByChat,
    runningChatStartedAtByChat,
    unreadChatIds,
    connected,
    now,
    confirmRequest: activeKey ? (confirmRequestByChat[activeKey] ?? null) : null,
    askMoreInfo: activeKey ? (askMoreInfoByChat[activeKey] ?? null) : null,
    theme,
    lang,
    uiPrefs,
    workspaceChats,
    expandedWorkspaceIds,
    settingsOpen,
    settingsInitialPage,
    aboutOpen,
    zoomLevel,
    setZoomLevel: persistZoomLevel,
    planOpen,
    togglePlan: () => setPlanOpen((v) => !v),
    todoDockVisible,
    setTodoDockVisible,
    t,
    setTheme,
    setGuiLanguage,
    setBackgroundImage,
    clearBackgroundImage,
    setBackgroundOpacity,
    backgroundImageUrl,
    pasteImage,
    saveDroppedFile,
    chatImageUrl,
    mcpIconUrl,
    subscribeBrowserCommand,
    sendBrowserResult,
    previewHtml,
    previewHtmlInBrowser,
    showBrowserTab,
    hideBrowserTab,
    consoleOpen,
    browserOpen,
    showConsole,
    hideConsole,
    consoleOptions,
    subscribeConsoleOutput,
    consumePendingAutoConsole,
    attachConsole,
    consoleInput,
    consoleResize,
    openConsole,
    closeConsole,
    activateConsole,
    setConsoleOptions,
    resolveBackendUrl,
    sendInput,
    runCommand,
    interrupt,
    pendingInputs,
    pendingAutoSend,
    startPendingInputs,
    cancelPendingInput,
    sendPendingInputNow,
    compactContext: async () => {
      setCompactNoticeState((state) =>
        state.notice
          ? { chatKey: "", notice: null, version: state.version + 1 }
          : state,
      );
      const result = await client.compactContext(
        activeChatIdRef.current,
        activeWorkspaceIdRef.current,
      );
      if (!result.ok && result.text && activeKey) {
        setCompactNoticeState((state) => ({
          chatKey: activeKey,
          notice: buildCompactNoticeData(result.text ?? ""),
          version: state.version + 1,
        }));
      }
      return result;
    },
    compactNotice,
    retryCountdownByChat,
    answerConfirm,
    answerAskMoreInfo,
    clearTurns,
    switchToChat,
    selectWorkspace,
    newChat,
    draftMode,
    draftWorkspaceId,
    setDraftWorkspace,
    setDraftHasContent,
    deleteChat,
    forkChat,
    editChat,
    loadOlderHistory,
    openWorkspaceInExplorer,
    deleteWorkspace: deleteWorkspaceViaApi,
    toggleWorkspacePin,
    reorderWorkspace,
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
    getModelPresets: () => client.getModelPresets(),
    getGeneralConfig: () => client.getGeneralConfig(),
    saveGeneralConfig: (general: Partial<GeneralConfig>) =>
      client.saveGeneralConfig(general),
    getSecurityAuditConfig: () => client.getSecurityAuditConfig(),
    saveSecurityAuditConfig: (audit: Partial<SecurityAuditConfig>) =>
      client.saveSecurityAuditConfig(audit),
    getModelSelectors: () => client.getModelSelectors(),
    getConfirmAllowlist: () => client.getConfirmAllowlist(),
    saveConfirmAllowlist: (allowlist: ConfirmAllowlist) =>
      client.saveConfirmAllowlist(allowlist),
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
    getSkillsOverview: () => client.getSkillsOverview(),
    setSkillEnabled: (skillId: string, enabled: boolean) =>
      client.setSkillEnabled(skillId, enabled),
    getSubAgentsOverview: () => client.getSubAgentsOverview(),
    saveSubAgent: (payload) => client.saveSubAgent(payload),
    deleteSubAgent: (name: string) => client.deleteSubAgent(name),
    setSubAgentEnabled: (name: string, enabled: boolean) =>
      client.setSubAgentEnabled(name, enabled),
    setPlanMode: (enabled: boolean) =>
      client.setPlanMode(
        enabled,
        activeChatIdRef.current,
        activeWorkspaceIdRef.current,
      ),
    searchWorkspaceFiles: (query: string, limit = 10) =>
      client.searchWorkspaceFiles(
        query,
        draftWorkspaceIdRef.current || activeWorkspaceIdRef.current,
        limit,
      ),
    setExecutionPolicy,
    toggleWorkspaceExpanded,
    refreshWorkspaceChats,
    openSettings,
    closeSettings,
    openAbout,
    closeAbout,
    pickFolder,
    pickAndOpenFolder,
    pickFiles,
    activeSubAgentSession,
    subAgentSessionLoading,
    enterSubAgentSession,
    exitSubAgentSession,
    pendingExpandSubAgentId,
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
