import type {
  AppState,
  ChatHistoryPage,
  CompletionCatalog,
  GeneralConfig,
  McpServerConfigEntry,
  McpServerDetails,
  McpServerSummary,
  ServerEvent,
  SubAgentConfig,
  SubAgentsOverview,
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

  async sendInput(text: string, asPrompt = false, chatId = ""): Promise<void> {
    await fetch(`${this.base}/input`, {
      method: "POST",
      headers: this.headers(),
      body: JSON.stringify({ text, asPrompt, chatId }),
    });
  }

  async confirm(id: string, answer: string): Promise<void> {
    await fetch(`${this.base}/confirm`, {
      method: "POST",
      headers: this.headers(),
      body: JSON.stringify({ id, answer }),
    });
  }

  /** Answer a pending ``ask_more_info`` prompt (clicked option or freeform). */
  async answerAskMoreInfo(id: string, answer: string): Promise<void> {
    await fetch(`${this.base}/answer-ask-more-info`, {
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

  /** Delete a chat, allowing the workspace to become chat-less (GUI-only). */
  async deleteChat(id: string, workspaceId = ""): Promise<boolean> {
    const res = await fetch(`${this.base}/delete-chat`, {
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

  async setGuiLanguage(language: string): Promise<boolean> {
    const res = await fetch(`${this.base}/set-gui-language`, {
      method: "POST",
      headers: this.headers(),
      body: JSON.stringify({ language }),
    });
    return res.ok;
  }

  async setUiPrefs(prefs: unknown): Promise<boolean> {
    const res = await fetch(`${this.base}/set-ui-prefs`, {
      method: "POST",
      headers: this.headers(),
      body: JSON.stringify({ prefs }),
    });
    return res.ok;
  }

  /** Copy a chosen image into the config dir as the GUI background. */
  async setBackgroundImage(sourcePath: string): Promise<boolean> {
    const res = await fetch(`${this.base}/set-background-image`, {
      method: "POST",
      headers: this.headers(),
      body: JSON.stringify({ sourcePath }),
    });
    return res.ok;
  }

  /** Remove the current GUI background image. */
  async clearBackgroundImage(): Promise<boolean> {
    const res = await fetch(`${this.base}/clear-background-image`, {
      method: "POST",
      headers: this.headers(),
      body: "{}",
    });
    return res.ok;
  }

  /** Persist the background image opacity (0-100). */
  async setBackgroundOpacity(opacity: number): Promise<boolean> {
    const res = await fetch(`${this.base}/set-background-opacity`, {
      method: "POST",
      headers: this.headers(),
      body: JSON.stringify({ opacity }),
    });
    return res.ok;
  }

  /** Absolute URL of the current background image (token + cache-busting version). */
  backgroundImageUrl(version: number): string {
    const params = new URLSearchParams({
      token: this.token,
      v: String(version),
    });
    return `${this.base}/background-image?${params.toString()}`;
  }

  /** Read the raw (unresolved) model_providers list for editing. */
  async getModelsConfig(): Promise<unknown[]> {
    const res = await fetch(`${this.base}/models-config`, {
      method: "POST",
      headers: this.headers(),
      body: "{}",
    });
    if (!res.ok) {
      return [];
    }
    try {
      const data = (await res.json()) as { providers?: unknown[] };
      return Array.isArray(data.providers) ? data.providers : [];
    } catch {
      return [];
    }
  }

  /** Persist a new model_providers list; takes effect immediately. */
  async saveModelsConfig(providers: unknown[]): Promise<boolean> {
    const res = await fetch(`${this.base}/save-models-config`, {
      method: "POST",
      headers: this.headers(),
      body: JSON.stringify({ providers }),
    });
    return res.ok;
  }

  /** General-runtime settings (auto-compact / tool rounds / memory / mcp). */
  async getGeneralConfig(): Promise<GeneralConfig | null> {
    const res = await fetch(`${this.base}/general-config`, {
      method: "POST",
      headers: this.headers(),
      body: "{}",
    });
    if (!res.ok) {
      return null;
    }
    try {
      const data = (await res.json()) as { general?: GeneralConfig };
      return data.general ?? null;
    } catch {
      return null;
    }
  }

  async saveGeneralConfig(general: Partial<GeneralConfig>): Promise<boolean> {
    const res = await fetch(`${this.base}/save-general-config`, {
      method: "POST",
      headers: this.headers(),
      body: JSON.stringify({ general }),
    });
    return res.ok;
  }

  async getMcpOverview(): Promise<McpServerSummary[]> {
    const res = await fetch(`${this.base}/mcp-overview`, {
      method: "POST",
      headers: this.headers(),
      body: "{}",
    });
    if (!res.ok) return [];
    try {
      const data = (await res.json()) as { servers?: McpServerSummary[] };
      return Array.isArray(data.servers) ? data.servers : [];
    } catch {
      return [];
    }
  }

  async getMcpServerDetails(name: string): Promise<McpServerDetails | null> {
    const res = await fetch(`${this.base}/mcp-server-details`, {
      method: "POST",
      headers: this.headers(),
      body: JSON.stringify({ name }),
    });
    if (!res.ok) return null;
    try {
      const data = (await res.json()) as McpServerDetails;
      return data;
    } catch {
      return null;
    }
  }

  async setMcpServerEnabled(name: string, enabled: boolean): Promise<boolean> {
    const res = await fetch(`${this.base}/set-mcp-server-enabled`, {
      method: "POST",
      headers: this.headers(),
      body: JSON.stringify({ name, enabled }),
    });
    return res.ok;
  }

  async setMcpToolEnabled(server: string, tool: string, enabled: boolean): Promise<boolean> {
    const res = await fetch(`${this.base}/set-mcp-tool-enabled`, {
      method: "POST",
      headers: this.headers(),
      body: JSON.stringify({ server, tool, enabled }),
    });
    return res.ok;
  }

  async setMcpToolsEnabled(
    server: string,
    tools: string[],
    enabled: boolean,
  ): Promise<boolean> {
    const res = await fetch(`${this.base}/set-mcp-tools-enabled`, {
      method: "POST",
      headers: this.headers(),
      body: JSON.stringify({ server, tools, enabled }),
    });
    return res.ok;
  }

  async getMcpServerConfig(name: string): Promise<McpServerConfigEntry> {
    const res = await fetch(`${this.base}/mcp-server-config`, {
      method: "POST",
      headers: this.headers(),
      body: JSON.stringify({ name }),
    });
    if (!res.ok) return {};
    try {
      const data = (await res.json()) as { ok?: boolean; config?: McpServerConfigEntry };
      return data?.config ?? {};
    } catch {
      return {};
    }
  }

  async addMcpServer(
    name: string,
    config: McpServerConfigEntry,
  ): Promise<{ ok: boolean; error?: string }> {
    const res = await fetch(`${this.base}/add-mcp-server`, {
      method: "POST",
      headers: this.headers(),
      body: JSON.stringify({ name, config }),
    });
    try {
      return (await res.json()) as { ok: boolean; error?: string };
    } catch {
      return { ok: false, error: "network" };
    }
  }

  async updateMcpServer(
    originalName: string,
    name: string,
    config: McpServerConfigEntry,
  ): Promise<{ ok: boolean; error?: string }> {
    const res = await fetch(`${this.base}/update-mcp-server`, {
      method: "POST",
      headers: this.headers(),
      body: JSON.stringify({ originalName, name, config }),
    });
    try {
      return (await res.json()) as { ok: boolean; error?: string };
    } catch {
      return { ok: false, error: "network" };
    }
  }

  async deleteMcpServer(name: string): Promise<{ ok: boolean; error?: string }> {
    const res = await fetch(`${this.base}/delete-mcp-server`, {
      method: "POST",
      headers: this.headers(),
      body: JSON.stringify({ name }),
    });
    try {
      return (await res.json()) as { ok: boolean; error?: string };
    } catch {
      return { ok: false, error: "network" };
    }
  }

  async getSubAgentsOverview(): Promise<SubAgentsOverview> {
    const empty: SubAgentsOverview = { subagents: [], models: [], tools: [] };
    try {
      const res = await fetch(`${this.base}/subagents-overview`, {
        method: "POST",
        headers: this.headers(),
        body: "{}",
      });
      if (!res.ok) return empty;
      const data = (await res.json()) as Partial<SubAgentsOverview>;
      return {
        subagents: Array.isArray(data.subagents) ? data.subagents : [],
        models: Array.isArray(data.models) ? data.models : [],
        tools: Array.isArray(data.tools) ? data.tools : [],
      };
    } catch {
      return empty;
    }
  }

  async saveSubAgent(
    payload: Partial<SubAgentConfig> & { originalName?: string },
  ): Promise<{ ok: boolean; error?: string }> {
    try {
      const res = await fetch(`${this.base}/save-subagent`, {
        method: "POST",
        headers: this.headers(),
        body: JSON.stringify(payload),
      });
      return (await res.json()) as { ok: boolean; error?: string };
    } catch {
      return { ok: false, error: "network" };
    }
  }

  async deleteSubAgent(name: string): Promise<{ ok: boolean; error?: string }> {
    try {
      const res = await fetch(`${this.base}/delete-subagent`, {
        method: "POST",
        headers: this.headers(),
        body: JSON.stringify({ name }),
      });
      return (await res.json()) as { ok: boolean; error?: string };
    } catch {
      return { ok: false, error: "network" };
    }
  }

  async setSubAgentEnabled(
    name: string,
    enabled: boolean,
  ): Promise<{ ok: boolean; error?: string }> {
    try {
      const res = await fetch(`${this.base}/set-subagent-enabled`, {
        method: "POST",
        headers: this.headers(),
        body: JSON.stringify({ name, enabled }),
      });
      return (await res.json()) as { ok: boolean; error?: string };
    } catch {
      return { ok: false, error: "network" };
    }
  }

  async setPlanMode(enabled: boolean): Promise<boolean> {
    const res = await fetch(`${this.base}/set-plan-mode`, {
      method: "POST",
      headers: this.headers(),
      body: JSON.stringify({ enabled }),
    });
    return res.ok;
  }

  async searchWorkspaceFiles(
    query: string,
    workspaceId?: string,
    limit = 10,
  ): Promise<string[]> {
    try {
      const res = await fetch(`${this.base}/search-workspace-files`, {
        method: "POST",
        headers: this.headers(),
        body: JSON.stringify({ query, workspaceId: workspaceId ?? "", limit }),
      });
      if (!res.ok) return [];
      const data = (await res.json()) as { candidates?: unknown };
      return Array.isArray(data.candidates)
        ? data.candidates.filter((c): c is string => typeof c === "string")
        : [];
    } catch {
      return [];
    }
  }

  async getCompletionCatalog(): Promise<CompletionCatalog> {
    const empty: CompletionCatalog = { skills: [], mcpTools: [], mcpPrompts: [] };
    const res = await fetch(`${this.base}/completion-catalog`, {
      method: "POST",
      headers: this.headers(),
      body: "{}",
    });
    if (!res.ok) return empty;
    try {
      const data = (await res.json()) as Partial<CompletionCatalog>;
      return {
        skills: Array.isArray(data.skills) ? data.skills : [],
        mcpTools: Array.isArray(data.mcpTools) ? data.mcpTools : [],
        mcpPrompts: Array.isArray(data.mcpPrompts) ? data.mcpPrompts : [],
      };
    } catch {
      return empty;
    }
  }

  /** Fetch a provider's advertised model list from its /models endpoint. */
  async fetchProviderModels(params: {
    base_url: string;
    api_key?: string;
    api_mode?: string;
    port?: number;
  }): Promise<{ ok: boolean; models?: string[]; error?: string }> {
    const res = await fetch(`${this.base}/fetch-models`, {
      method: "POST",
      headers: this.headers(),
      body: JSON.stringify(params),
    });
    try {
      return (await res.json()) as { ok: boolean; models?: string[]; error?: string };
    } catch {
      return { ok: false, error: "Bad response" };
    }
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
