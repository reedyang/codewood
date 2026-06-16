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
}

/** Chat summary as returned by GET /workspace-chats for any workspace. */
export interface WorkspaceChatSummary {
  id: string;
  name: string;
  updatedAt?: string;
}

export interface AppState {
  app: { name: string; version: string };
  workspace: { name: string; id: string; root: string; workDirectory: string };
  workspaces: WorkspaceSummary[];
  chats: ChatSummary[];
  activeChatId: string;
  model: { current: string; available: string[] };
  language: string;
  executionPolicy: string;
}

export interface ConfirmRequest {
  id: string;
  prompt: string;
}

export type ServerEvent =
  | { event: "idle"; data: { state: AppState } }
  | { event: "turn_start"; data: { text: string } }
  | { event: "output"; data: { text: string } }
  | { event: "assistant"; data: { text: string } }
  | { event: "confirm"; data: ConfirmRequest }
  | { event: string; data: Record<string, unknown> };

/** A streamed segment within a turn: intermediate steps or the final answer. */
export type SegmentKind = "step" | "answer";

export interface TurnSegment {
  id: number;
  kind: SegmentKind;
  text: string;
}

/** One user request and the assistant's streamed response, split into segments. */
export interface Turn {
  id: number;
  userText: string;
  segments: TurnSegment[];
  startedAt: number;
  endedAt: number | null;
}
