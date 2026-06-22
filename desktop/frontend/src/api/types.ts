export interface WorkspaceSummary {
  id: string;
  name: string;
  root: string;
  active: boolean;
  isDefault?: boolean;
}

export interface ChatSummary {
  index: number;
  id: string;
  name: string;
  messageCount: number;
  updatedAt?: string;
  active: boolean;
  /** Per-chat model selector ("provider:name"); empty if unset. */
  model?: string;
  /** True while this chat's agent loop is actively streaming a turn. Lets the
   *  sidebar busy dot persist across focus changes and reloads. */
  running?: boolean;
  /** Sticky Plan-mode flag recorded on the chat record root, so the GUI can
   *  restore the per-chat compose mode after a restart instead of defaulting
   *  every chat to Agent mode. */
  planMode?: boolean;
}

/** Chat summary as returned by GET /workspace-chats for any workspace. */
export interface WorkspaceChatSummary {
  id: string;
  name: string;
  updatedAt?: string;
}

/** General-runtime settings exposed by the GUI's General settings page. */
export interface GeneralConfig {
  auto_compact_trigger_percent: number;
  /** ``null`` means unlimited tool rounds. */
  max_tool_rounds: number | null;
  memory_enabled: boolean;
  mcp_tools_enabled: boolean;
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
}

/** Lazy-loaded tool/prompt catalog for a single MCP server. */
export interface McpServerDetails {
  ok: boolean;
  tools: { name: string; description: string }[];
  prompts: { name: string; description: string }[];
  disabledTools: string[];
}

/** Catalog used by the composer's slash popup. */
export interface CompletionCatalog {
  skills: { name: string; description: string }[];
  mcpTools: { server: string; name: string; description: string }[];
  mcpPrompts: { server: string; name: string; description: string }[];
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
    reasoningLevel?: string;
    reasoningLevels?: string[];
  };
  /** Active chat's last-known context-window usage snapshot. */
  contextUsage?: {
    percent: number;
    tokens: number;
    window: number;
  };
  language: string;
  theme?: string;
  uiPrefs?: {
    pinnedWorkspaceIds?: string[];
    pinnedChatIds?: string[];
    archivedChatIds?: string[];
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
  /** Pending ``ask_more_info`` request persisted on the active chat record
   *  (may be set by a different backend process — e.g. the TUI — and
   *  surfaced here so the GUI re-renders the panel on chat load/refresh). */
  askMoreInfo?: AskMoreInfoRequest | null;
  executionPolicy: string;
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

export interface ConfirmRequest {
  id: string;
  prompt: string;
}

/** Pending ``ask_more_info`` prompt surfaced by the backend.
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

export type ServerEvent =
  | { event: "idle"; data: { state: AppState } }
  | { event: "turn_start"; data: { text: string } }
  | { event: "round_start"; data: { chatId?: string } }
  | { event: "round_end"; data: { chatId?: string } }
  | { event: "output"; data: { text: string } }
  | { event: "assistant"; data: { text: string } }
  | { event: "confirm"; data: ConfirmRequest }
  | { event: "ask_more_info"; data: AskMoreInfoRequest }
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
}

/** One user request and the assistant's streamed response, split into rounds. */
export interface Turn {
  id: number;
  userText: string;
  rounds: TurnRound[];
  startedAt: number;
  endedAt: number | null;
}

/** A previously-recorded model round loaded from chat history. */
export interface HistoryRound {
  waitSeconds: number;
  text: string;
  tools: string;
  /** A recorded ask_more_info selection, rendered as a left-side bubble
   *  (a reply to the agent's question, not a user-initiated turn). */
  selection?: string;
}

/** A previously-recorded turn loaded from chat history (already classified). */
export interface HistoryTurn {
  userText: string;
  rounds: HistoryRound[];
  timestamp?: string;
}

/** One configured sub-agent as surfaced by the config UI. */
export interface SubAgentConfig {
  name: string;
  description: string;
  instructions: string;
  /** ``provider:model`` selector; empty = reuse the main model. */
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

/** Paginated chat history response from GET /chat-history. */
export interface ChatHistoryPage {
  turns: HistoryTurn[];
  start: number;
  total: number;
}
