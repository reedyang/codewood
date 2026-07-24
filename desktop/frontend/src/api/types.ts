export interface WorkspaceSummary {
  id: string;
  name: string;
  root: string;
  active: boolean;
  isDefault?: boolean;
}

/** Per-model cumulative cache-hit statistics from the AI provider. */
export interface CacheStats {
  totalTokens: number;
  hitTokens: number;
  missTokens: number;
  hitRate: number;
  model: string;
  supported: boolean;
  /** False when the provider only reports total input tokens without
   *  cache-breakdown details (cache hits, misses, and hit rate are N/A). */
  hasBreakdown: boolean;
}

export interface IndexStatus {
  hidden: boolean;
  files_total: number;
  workspace_name: string;
  is_default_workspace: boolean;
  refresh_phase: string;
  refresh_progress_total: number;
  refresh_progress_done: number;
  refresh_progress_percent: number;
}

export interface ChatSummary {
  index: number;
  id: string;
  name: string;
  messageCount: number;
  updatedAt?: string;
  active: boolean;
  /** Per-chat model selector ("provider/model"); empty if unset. */
  model?: string;
  /** True while this chat's agent loop is actively streaming a turn. Lets the
   *  sidebar busy dot persist across focus changes and reloads. */
  running?: boolean;
  /** Sticky Plan-mode flag recorded on the chat record root, so the GUI can
   *  restore the per-chat compose mode after a restart instead of defaulting
   *  every chat to Agent mode. */
  planMode?: boolean;
  /** Whether the chat has been archived (hidden from the sidebar). */
  archived?: boolean;
  /** Per-chat file-change summaries (list of per-turn summaries, survives restarts via getState). */
  fileChanges?: FileChangeSummary[];
  /** Pending input queue: messages typed while the model was busy, waiting to be sent. */
  pendingInputs?: string[];
}

/** Chat summary as returned by GET /workspace-chats for any workspace. */
export interface WorkspaceChatSummary {
  id: string;
  name: string;
  updatedAt?: string;
  archived?: boolean;
}

/** General-runtime settings exposed by the GUI's General settings page. */
export interface GeneralConfig {
  auto_compact_trigger_percent: number;
  /** ``null`` means unlimited tool rounds. */
  max_tool_rounds: number | null;
  memory_enabled: boolean;
}

/** Per-server summary on the MCP settings page. */
export interface McpServerSummary {
  name: string;
  enabled: boolean;
  transport: string;
  state: string;
  lastError: string;
  toolsCount: number;
  promptsCount: number;
  disabledTools: string[];
  /** URL or data URI of the runtime-discovered server icon. */
  icon?: string;
}

/** Lazy-loaded tool/prompt catalog for a single MCP server. */
export interface McpServerDetails {
  ok: boolean;
  tools: { name: string; description: string }[];
  prompts: { name: string; description: string }[];
  disabledTools: string[];
  loading?: boolean;
}

/** Catalog used by the composer's slash popup. */
export interface CompletionCatalog {
  skills: { name: string; description: string }[];
  mcpTools: { server: string; name: string; description: string }[];
  mcpPrompts: { server: string; name: string; description: string }[];
}

/** Per-skill summary on the Skills settings page. */
export interface SkillSummary {
  skillId: string;
  name: string;
  description: string;
  source: string;
  enabled: boolean;
}

/** Editable MCP server entry. Matches the JSONC shape in mcp.jsonc; only the
 *  subset of fields the GUI exposes is enumerated, but ``[unknown: string]``
 *  is allowed so the editor round-trips fields it doesn't render. */
export interface McpServerConfigEntry {
  command?: string;
  args?: string[];
  env?: Record<string, string>;
  url?: string;
  headers?: Record<string, string>;
  skip_preload?: boolean;
  transport?: string;
  [extra: string]: unknown;
}

export interface AppState {
  app: { name: string; version: string };
  workspace: { name: string; id: string; root: string; workDirectory: string };
  workspaces: WorkspaceSummary[];
  chats: ChatSummary[];
  activeChatId: string;
  model: {
    current: string;
    available: string[];
    /** True only when a usable (non-template) model is configured. */
    ready?: boolean;
    reasoningEffort?: string;
    reasoningEfforts?: string[];
    /** Map of model selector -> reasoning-effort levels that model supports. */
    reasoningEffortsBySelector?: Record<string, string[]>;
  };
  /** Active chat's last-known context-window usage snapshot. */
  contextUsage?: {
    percent: number;
    tokens: number;
    window: number;
  };
  /** Cumulative cache-hit/miss statistics for the active chat's current model. */
  cacheStats?: CacheStats;
  language: string;
  theme?: string;
  uiPrefs?: {
    pinnedWorkspaceIds?: string[];
    pinnedChatIds?: string[];
  };
  /** GUI-only embedded-console options (font + scrollback buffer). */
  consoleOptions?: {
    fontFamily: string;
    bufferLines: number;
  };
  /** GUI-only background image presentation settings. */
  background?: {
    hasImage: boolean;
    /** Stored background file name (e.g. "bg.png"); empty when none. */
    fileName: string;
    /** Opacity percentage (0-100) applied to the background image layer. */
    opacity: number;
    /** Cache-busting version (image file mtime); changes when the image is replaced. */
    version: number;
  };
  plan?: PlanState;
  /** Pending ``request_user_input`` request persisted on the active chat record
   *  (may be set by a different backend process — e.g. the TUI — and
   *  surfaced here so the GUI re-renders the panel on chat load/refresh). */
  askMoreInfo?: AskMoreInfoRequest | null;
  executionPolicy: string;
}

/** One embedded-console tab as reported by the backend. */
export interface ConsoleSessionInfo {
  id: string;
  kind: string;
  title: string;
  cwd: string;
  cols: number;
  rows: number;
  totalLines: number;
  alive: boolean;
  active?: boolean;
}

export type PlanStepStatus = "pending" | "in_progress" | "completed";

export interface PlanStep {
  step: string;
  status: PlanStepStatus | string;
}

export interface PlanState {
  plan: PlanStep[];
  explanation: string;
}

/** Pending execution-policy confirmation surfaced by the backend.
 *  ``options`` is the fixed list of choices (Yes / No / optionally Always);
 *  the user picks exactly one and the index is posted back. The pick is
 *  mapped to y/n/a on the backend locally and never sent to the model.
 *  Older backends omit ``options``; the panel then falls back to the
 *  built-in Yes/No(/Always) buttons. */
/** One row of a structured change preview (apply_patch diff). Mirrors the
 *  backend ``ChangePreviewFormatter.format_segments_structured`` output. */
export interface DiffRow {
  type: "context" | "del" | "add" | "change" | "omitted";
  oldNo: number | null;
  newNo: number | null;
  oldText: string;
  newText: string;
}

export interface ConfirmRequest {
  id: string;
  prompt: string;
  chatId?: string;
  /** The command/script to run, surfaced on its own syntax-highlighted line
   *  so it stands out from the surrounding confirmation prompt text. */
  command?: string;
  options?: string[];
  offerAlways?: boolean;
  /** Structured diff rows for an apply_patch change preview. When present the
   *  panel renders a responsive (side-by-side / inline) highlighted diff. */
  diffRows?: DiffRow[];
}

/** Pending ``request_user_input`` prompt surfaced by the backend.
 *  ``options`` is the model-supplied option list; the GUI always appends
 *  an additional "Other" choice that lets the user type a freeform answer.
 *  ``multiSelect`` switches between single-pick (one option, click to
 *  submit) and multi-pick (any subset, click Submit when done). */
export interface AskMoreInfoRequest {
  id: string;
  question: string;
  options: string[];
  multiSelect: boolean;
  chatId: string;
}

export interface CompactNoticeData {
  title: string;
  body: string;
  text: string;
  stage?: string;
  mode?: string;
}

export type ServerEvent =
  | { event: "idle"; data: { state: AppState } }
  | { event: "turn_start"; data: { text: string } }
  | { event: "round_start"; data: { chatId?: string } }
  | { event: "round_end"; data: { chatId?: string } }
  | { event: "compact_notice"; data: { title?: string; body?: string; text: string; stage?: string; mode?: string; chatId?: string; workspaceId?: string } }
  | { event: "output"; data: { text: string } }
  | { event: "assistant"; data: { text: string } }
  | { event: "thinking"; data: { text: string } }
  | { event: "confirm"; data: ConfirmRequest }
  | { event: "request_user_input"; data: AskMoreInfoRequest }
  | { event: "file_changes"; data: FileChangeSummary & { chatId?: string; workspaceId?: string; turnIndex?: number } }
  | { event: "sub_agent_start"; data: { sessionId: string; name: string; topic: string; description: string; prompt: string } }
  | { event: "sub_agent_assistant"; data: { sessionId: string; text: string } }
  | { event: "sub_agent_thinking"; data: { sessionId: string; text: string } }
  | { event: "sub_agent_thinking_end"; data: { sessionId: string; thinkingElapsedSeconds: number } }
  | { event: "sub_agent_tool_call"; data: { sessionId: string; toolName: string; args: Record<string, unknown>; thinkingElapsedSeconds?: number } }
  | { event: "sub_agent_output"; data: { sessionId: string; text: string; toolName: string } }
  | { event: "sub_agent_end"; data: { sessionId: string; output: string; success: boolean; max_rounds_reached?: boolean } }
  | { event: string; data: Record<string, unknown> };

/** A streamed segment within a round: model text ("answer") or tool output ("step"). */
export type SegmentKind = "step" | "answer";

export interface TurnSegment {
  id: number;
  kind: SegmentKind;
  text: string;
}

/** One model request/response within a turn: the wait timer plus the model
 *  text and tool output streamed for that round, kept in arrival order. */
export interface TurnRound {
  id: number;
  waitStartedAt: number;
  waitEndedAt: number | null;
  segments: TurnSegment[];
  /** Accumulated model reasoning/thinking text for this round. */
  thinkingText?: string;
  /** When model reasoning/thinking started for this round. */
  thinkingStartedAt?: number;
  /** When model reasoning/thinking ended for this round. */
  thinkingEndedAt?: number;
  /** Backend-computed elapsed milliseconds for this round (from round_end SSE).
   *  When present, overrides the client-side thinking timer for consistency
   *  with the history-view ``waitSeconds``. */
  backendElapsedMs?: number;
}

/** One user request and the assistant's streamed response, split into rounds. */
export interface Turn {
  id: number;
  userText: string;
  rounds: TurnRound[];
  startedAt: number;
  endedAt: number | null;
  /** True for a turn the GUI opened optimistically (the first message of a
   *  freshly-created chat) before the backend's authoritative ``turn_start``
   *  event arrived. The matching ``turn_start`` reconciles it in place rather
   *  than appending a duplicate, and ``endActiveTurn`` won't settle it while it
   *  has no rounds yet (so a premature ``idle`` can't split it in two). */
  optimistic?: boolean;
  /** File changes emitted during this turn. */
  fileChanges?: FileChangeSummary;
}

/** A previously-recorded model round loaded from chat history. */
export interface HistoryRound {
  waitSeconds: number;
  text: string;
  tools: string;
  /** Compact notice banner title rendered between turns. */
  compactNoticeTitle?: string;
  /** Persisted compaction summary body shown below the compact notice banner. */
  compactNoticeBody?: string;
  /** A recorded request_user_input selection, rendered as a left-side bubble
   *  (a reply to the agent's question, not a user-initiated turn). */
  selection?: string;
  /** Model thinking/reasoning content for this round. */
  thinking?: string;
  /** Conversation-interrupted banner text, rendered outside the "Worked for"
   *  collapsible section so the user always sees the status message. */
  interrupted?: string;
}

/** A previously-recorded turn loaded from chat history (already classified). */
export interface HistoryTurn {
  userText: string;
  rounds: HistoryRound[];
  timestamp?: string;
  /** File changes emitted during this turn. */
  fileChanges?: FileChangeSummary;
}

/** One configured sub-agent as surfaced by the config UI. */
export interface SubAgentConfig {
  name: string;
  description: string;
  instructions: string;
  /** ``provider/model`` selector; empty = reuse the main model. */
  model: string;
  tools: string[];
  toolsSpecified: boolean;
  maxRounds: number;
  enabled: boolean;
  sourcePath: string;
  global: boolean;
}

/** Response from POST /subagents-overview: the configured sub-agents plus the
 *  option catalogs (selectable models and tools) for the editor dropdowns. */
export interface SubAgentsOverview {
  subagents: SubAgentConfig[];
  models: string[];
  tools: string[];
}

/** A message within a sub-agent session. */
export interface SubAgentToolCall {
  name?: string;
  args?: Record<string, unknown>;
  function?: {
    name?: string;
    arguments?: string | Record<string, unknown>;
  };
}

export interface SubAgentToolRoundRaw {
  tool?: string;
  args?: Record<string, unknown>;
  failed?: boolean;
  elapsed?: number | null;
  output?: string;
  marker?: string;
}

export interface SubAgentMessage {
  role: "system" | "user" | "assistant" | "tool";
  content: string;
  /** Accumulated model reasoning/thinking text for this assistant message
   *  (mirrors the main chat's ``_thinking`` field), surfaced in a collapsible
   *  Thinking block. */
  _thinking?: string;
  /** Wall-clock seconds the model spent reasoning for this message,
   *  persisted by the backend. Used for the "Thought for Xs" label. */
  _thinking_elapsed_seconds?: number;
  tool_calls?: SubAgentToolCall[];
  /** Rendered tool-round display text (main-chat envelope) attached to an
   *  assistant message that issued tool calls. When present it is rendered
   *  through StepsView instead of the raw tool_calls + tool messages. */
  tool_rounds?: string[];
  /** Structured raw tool-round records persisted by the backend. */
  _tool_rounds_raw?: SubAgentToolRoundRaw[];
  name?: string;
  tool_call_id?: string;
}

/** A sub-agent session with full conversation history. */
export interface SubAgentSession {
  id: string;
  name: string;
  topic: string;
  description: string;
  prompt: string;
  startedAt: string;
  endedAt: string | null;
  messages: SubAgentMessage[];
  output: string | null;
  success: boolean | null;
  maxRoundsReached: boolean;
}

/** Paginated chat history response from GET /chat-history. */
export interface ChatHistoryPage {
  turns: HistoryTurn[];
  start: number;
  total: number;
}

/** File change record for tracking modifications */
export interface FileChangeRecord {
  filePath: string;
  changeType: "create" | "modify" | "delete" | "rename";
  source: string;
  timestamp: string;
  addedLines: number;
  deletedLines: number;
  patch?: DiffRow[];
  backupPath?: string;
}

/** Summary of all file changes */
export interface FileChangeSummary {
  totalFiles: number;
  totalAdded: number;
  totalDeleted: number;
  files: FileChangeRecord[];
  /** Turn index (0-based) for per-turn association. */
  turnIndex?: number;
}
