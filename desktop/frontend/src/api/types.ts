export interface WorkspaceSummary {
  id: string;
  name: string;
  root: string;
  active: boolean;
}

export interface ChatSummary {
  index: number;
  id: string;
  name: string;
  messageCount: number;
  active: boolean;
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
  | { event: "confirm"; data: ConfirmRequest }
  | { event: string; data: Record<string, unknown> };
