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

export interface AppState {
  app: { name: string; version: string };
  workspace: { name: string; id: string; root: string; workDirectory: string };
  workspaces: WorkspaceSummary[];
  chats: ChatSummary[];
  activeChatId: string;
  model: {
    current: string;
    available: string[];
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
  plan?: PlanState;
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

export type ServerEvent =
  | { event: "idle"; data: { state: AppState } }
  | { event: "turn_start"; data: { text: string } }
  | { event: "round_start"; data: { chatId?: string } }
  | { event: "round_end"; data: { chatId?: string } }
  | { event: "output"; data: { text: string } }
  | { event: "assistant"; data: { text: string } }
  | { event: "confirm"; data: ConfirmRequest }
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
}

/** A previously-recorded turn loaded from chat history (already classified). */
export interface HistoryTurn {
  userText: string;
  rounds: HistoryRound[];
  timestamp?: string;
}

/** Paginated chat history response from GET /chat-history. */
export interface ChatHistoryPage {
  turns: HistoryTurn[];
  start: number;
  total: number;
}
