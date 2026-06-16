import type {
  AppState,
  ChatHistoryPage,
  ServerEvent,
  WorkspaceChatSummary,
} from "./types";

/**
 * Thin client for the Code Wood backend serve API.
 *
 * Connection details (port + token) are passed by the host on the page
 * query string. The token is sent as a Bearer header for fetch calls and
 * as a query parameter for the EventSource stream (EventSource cannot set
 * custom headers).
 */
export class ApiClient {
  readonly base: string;
  private readonly token: string;

  constructor() {
    // Connection details arrive in the URL hash (so file:// loads work in
    // WebView2); fall back to the query string for http dev servers.
    const hash = window.location.hash.startsWith("#")
      ? window.location.hash.slice(1)
      : "";
    const params = new URLSearchParams(hash || window.location.search);
    const port = params.get("port") ?? "";
    this.token = params.get("token") ?? "";
    this.base = `http://127.0.0.1:${port}`;
  }

  private headers(): HeadersInit {
    return {
      "Content-Type": "application/json",
      Authorization: `Bearer ${this.token}`,
    };
  }

  async getState(): Promise<AppState> {
    const res = await fetch(`${this.base}/state`, { headers: this.headers() });
    if (!res.ok) {
      throw new Error(`Failed to load state (${res.status})`);
    }
    return (await res.json()) as AppState;
  }

  async sendInput(text: string): Promise<void> {
    await fetch(`${this.base}/input`, {
      method: "POST",
      headers: this.headers(),
      body: JSON.stringify({ text }),
    });
  }

  async confirm(id: string, answer: string): Promise<void> {
    await fetch(`${this.base}/confirm`, {
      method: "POST",
      headers: this.headers(),
      body: JSON.stringify({ id, answer }),
    });
  }

  async interrupt(): Promise<void> {
    await fetch(`${this.base}/interrupt`, {
      method: "POST",
      headers: this.headers(),
      body: "{}",
    });
  }

  /** List chats for any workspace by id (without switching to it). */
  async listWorkspaceChats(id: string): Promise<WorkspaceChatSummary[]> {
    const res = await fetch(
      `${this.base}/workspace-chats?id=${encodeURIComponent(id)}`,
      { headers: this.headers() },
    );
    if (!res.ok) {
      return [];
    }
    try {
      const data = (await res.json()) as { chats?: WorkspaceChatSummary[] };
      return Array.isArray(data.chats) ? data.chats : [];
    } catch {
      return [];
    }
  }

  /** Load a paginated slice of structured turns for the active chat. */
  async getChatHistory(before?: number, limit = 12): Promise<ChatHistoryPage> {
    const params = new URLSearchParams();
    if (typeof before === "number") {
      params.set("before", String(before));
    }
    params.set("limit", String(limit));
    const res = await fetch(`${this.base}/chat-history?${params.toString()}`, {
      headers: this.headers(),
    });
    if (!res.ok) {
      return { turns: [], start: 0, total: 0 };
    }
    try {
      return (await res.json()) as ChatHistoryPage;
    } catch {
      return { turns: [], start: 0, total: 0 };
    }
  }

  /** Silently switch the active chat/workspace (no command echo / replay). */
  async selectChat(id: string, workspaceId = ""): Promise<boolean> {
    const res = await fetch(`${this.base}/select-chat`, {
      method: "POST",
      headers: this.headers(),
      body: JSON.stringify({ id, workspaceId }),
    });
    return res.ok;
  }

  /** Persist the GUI theme preference to the backend config file. */
  async setTheme(theme: string): Promise<boolean> {
    const res = await fetch(`${this.base}/set-theme`, {
      method: "POST",
      headers: this.headers(),
      body: JSON.stringify({ theme }),
    });
    return res.ok;
  }

  /** Silently create and activate a new chat; returns its id (or ""). */
  async newChat(): Promise<string> {
    const res = await fetch(`${this.base}/new-chat`, {
      method: "POST",
      headers: this.headers(),
      body: "{}",
    });
    if (!res.ok) {
      return "";
    }
    try {
      const data = (await res.json()) as { id?: string };
      return data.id ?? "";
    } catch {
      return "";
    }
  }

  /** Ask the backend to open a workspace's root in the OS file manager. */
  async openWorkspaceInExplorer(id: string): Promise<boolean> {
    const res = await fetch(`${this.base}/open-workspace`, {
      method: "POST",
      headers: this.headers(),
      body: JSON.stringify({ id }),
    });
    if (!res.ok) {
      return false;
    }
    try {
      const data = (await res.json()) as { ok?: boolean };
      return Boolean(data.ok);
    } catch {
      return false;
    }
  }

  connectEvents(onEvent: (event: ServerEvent) => void): EventSource {
    const url = `${this.base}/events?token=${encodeURIComponent(this.token)}`;
    const source = new EventSource(url);
    source.onmessage = (message: MessageEvent<string>) => {
      try {
        onEvent(JSON.parse(message.data) as ServerEvent);
      } catch {
        // Ignore malformed events (e.g. heartbeat comments are not delivered here).
      }
    };
    return source;
  }
}
