import type {
  AppState,
  ChatHistoryPage,
  CompletionCatalog,
  GeneralConfig,
  IndexStatus,
  McpServerConfigEntry,
  McpServerDetails,
  McpServerSummary,
  ServerEvent,
  SkillSummary,
  SubAgentConfig,
  SubAgentSession,
  SubAgentsOverview,
  UndoReapplyResult,
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

  async fetchIndexStatus(): Promise<IndexStatus> {
    const res = await fetch(`${this.base}/index-status`, { headers: this.headers() });
    if (!res.ok) {
      return { hidden: true, files_total: 0, workspace_name: "", is_default_workspace: true, refresh_phase: "", refresh_progress_total: 0, refresh_progress_done: 0, refresh_progress_percent: 0 };
    }
    return (await res.json()) as IndexStatus;
  }

  async sendInput(text: string, asPrompt = false, chatId = ""): Promise<void> {
    await fetch(`${this.base}/input`, {
      method: "POST",
      headers: this.headers(),
      body: JSON.stringify({ text, asPrompt, chatId }),
    });
  }

  async savePendingInputs(chatId: string, inputs: string[], workspaceId = ""): Promise<boolean> {
    const res = await fetch(`${this.base}/save-pending-inputs`, {
      method: "POST",
      headers: this.headers(),
      body: JSON.stringify({ chatId, inputs, workspaceId }),
    });
    return res.ok;
  }

  async confirm(id: string, answer: string): Promise<void> {
    await fetch(`${this.base}/confirm`, {
      method: "POST",
      headers: this.headers(),
      body: JSON.stringify({ id, answer }),
    });
  }

  /** Answer a pending ``request_user_input`` prompt (clicked option or freeform). */
  async answerAskMoreInfo(id: string, answer: string): Promise<void> {
    await fetch(`${this.base}/answer-ask-more-info`, {
      method: "POST",
      headers: this.headers(),
      body: JSON.stringify({ id, answer }),
    });
  }

  async interrupt(): Promise<void> {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 5000);
    try {
      await fetch(`${this.base}/interrupt`, {
        method: "POST",
        headers: this.headers(),
        body: "{}",
        signal: controller.signal,
      });
    } catch {
      // Best-effort: the interrupt flag is already set optimistically in the UI.
    } finally {
      clearTimeout(timeout);
    }
  }

  async compactContext(): Promise<{ ok: boolean; text?: string }> {
    try {
      const res = await fetch(`${this.base}/compact`, {
        method: "POST",
        headers: this.headers(),
        body: "{}",
      });
      const data = (await res.json()) as { ok: boolean; text?: string };
      return { ok: data.ok === true, text: data.text };
    } catch {
      return { ok: false };
    }
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

  /** Open a folder as a workspace (File > Open Folder), returning its id. */
  async openFolder(folder: string): Promise<{ ok: boolean; id?: string; name?: string }> {
    const res = await fetch(`${this.base}/open-folder`, {
      method: "POST",
      headers: this.headers(),
      body: JSON.stringify({ folder }),
    });
    try {
      return (await res.json()) as { ok: boolean; id?: string; name?: string };
    } catch {
      return { ok: false };
    }
  }

  /** Export a chat transcript as markdown to a file path. */
  async exportChat(
    id: string,
    workspaceId: string,
    filePath: string,
  ): Promise<boolean> {
    const res = await fetch(`${this.base}/export-chat`, {
      method: "POST",
      headers: this.headers(),
      body: JSON.stringify({ id, workspaceId, filePath }),
    });
    return res.ok;
  }

  /** Toggle a chat's archived flag (GUI-only, persistent). */
  async toggleChatArchive(id: string, workspaceId = ""): Promise<boolean> {
    const res = await fetch(`${this.base}/toggle-chat-archive`, {
      method: "POST",
      headers: this.headers(),
      body: JSON.stringify({ id, workspaceId }),
    });
    return res.ok;
  }

  /** Delete a workspace (GUI-only), handling fallback when the active one is removed. */
  async deleteWorkspace(id: string): Promise<boolean> {
    const res = await fetch(`${this.base}/delete-workspace`, {
      method: "POST",
      headers: this.headers(),
      body: JSON.stringify({ id }),
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

  /** Upload a pasted clipboard bitmap (base64 data URL). Returns the saved
   *  absolute path + file name, or null on failure. */
  async pasteImage(
    chatId: string,
    dataUrl: string,
    workspaceId = "",
  ): Promise<{ path: string; name: string } | null> {
    try {
      const res = await fetch(`${this.base}/paste-image`, {
        method: "POST",
        headers: this.headers(),
        body: JSON.stringify({ chatId, dataUrl, workspaceId }),
      });
      if (!res.ok) return null;
      const data = (await res.json()) as {
        ok?: boolean;
        path?: string;
        name?: string;
      };
      if (!data.ok || !data.path) return null;
      return { path: data.path, name: data.name ?? "" };
    } catch {
      return null;
    }
  }

  /** Absolute URL serving a pasted image by its on-disk path (token-gated;
   *  backend validates the path lives under the chats/data dir). */
  chatImageUrl(path: string): string {
    const params = new URLSearchParams({ token: this.token, path });
    return `${this.base}/chat-image?${params.toString()}`;
  }

  /** Resolve an MCP icon source into a GUI-loadable URL. Remote HTTP(S) icons
   *  are proxied through the local backend because the desktop WebView CSP
   *  only allows loopback/data images. */
  mcpIconUrl(server: string, icon: string): string {
    const src = String(icon || "").trim();
    const name = String(server || "").trim();
    if (!src) return "";
    const lower = src.toLowerCase();
    if (lower.startsWith("data:")) {
      return src;
    }
    if (src.startsWith(`${this.base}/`)) {
      return src;
    }
    if (lower.startsWith("http://127.0.0.1:") || lower.startsWith("http://localhost:")) {
      return src;
    }
    const params = new URLSearchParams({ token: this.token, src, server: name });
    return `${this.base}/mcp-icon?${params.toString()}`;
  }

  /** Post the outcome of a backend-issued browser command back to the waiting
   *  tool call, keyed by its requestId. */
  async browserResult(
    requestId: string,
    result: Record<string, unknown>,
  ): Promise<void> {
    try {
      await fetch(`${this.base}/browser-result`, {
        method: "POST",
        headers: this.headers(),
        body: JSON.stringify({ requestId, result }),
      });
    } catch {
      // The tool call will time out on the backend if this never arrives.
    }
  }

  /** Undo file changes for the given files. */
  async undoFileChanges(
    chatId: string,
    ref: string,
    files: string[],
  ): Promise<UndoReapplyResult> {
    const res = await fetch(`${this.base}/undo-file-changes`, {
      method: "POST",
      headers: this.headers(),
      body: JSON.stringify({ chatId, ref, files }),
    });
    try {
      return (await res.json()) as UndoReapplyResult;
    } catch {
      return { results: {} };
    }
  }

  /** Reapply file changes for the given files. */
  async reapplyFileChanges(
    chatId: string,
    ref: string,
    files: string[],
  ): Promise<UndoReapplyResult> {
    const res = await fetch(`${this.base}/reapply-file-changes`, {
      method: "POST",
      headers: this.headers(),
      body: JSON.stringify({ chatId, ref, files }),
    });
    try {
      return (await res.json()) as UndoReapplyResult;
    } catch {
      return { results: {} };
    }
  }

  /** Persist an HTML snippet for in-browser preview; returns a loadable URL. */
  async previewHtml(
    chatId: string,
    html: string,
  ): Promise<{ url: string } | null> {
    try {
      const res = await fetch(`${this.base}/browser-preview-html`, {
        method: "POST",
        headers: this.headers(),
        body: JSON.stringify({ chatId, html }),
      });
      if (!res.ok) return null;
      const data = (await res.json()) as { ok?: boolean; path?: string };
      if (!data.ok || !data.path) return null;
      return { url: this.chatFileUrl(data.path) };
    } catch {
      return null;
    }
  }

  /** Absolute URL serving a saved chat-data file (html preview, etc.) by its
   *  on-disk path (token-gated; backend validates containment under
   *  chats/data and serves an appropriate content-type). */
  chatFileUrl(path: string): string {
    const params = new URLSearchParams({ token: this.token, path });
    return `${this.base}/chat-file?${params.toString()}`;
  }

  /** Resolve a possibly backend-relative URL ("/chat-file?...") to an absolute
   *  one against this client's base. Absolute URLs are returned unchanged. */
  absoluteUrl(url: string): string {
    const s = String(url || "");
    if (s.startsWith("/")) {
      return `${this.base}${s}`;
    }
    return s;
  }

  // --- Embedded console ---------------------------------------------------

  /** Open a new console session of the given shell kind. Returns its info. */
  async openConsole(
    kind: string,
  ): Promise<{ id: string; title: string; kind: string } | null> {
    try {
      const res = await fetch(`${this.base}/console-open`, {
        method: "POST",
        headers: this.headers(),
        body: JSON.stringify({ kind }),
      });
      if (!res.ok) return null;
      const data = (await res.json()) as {
        success?: boolean;
        id?: string;
        title?: string;
        kind?: string;
      };
      if (!data.success || !data.id) return null;
      return { id: data.id, title: data.title ?? "", kind: data.kind ?? kind };
    } catch {
      return null;
    }
  }

  async closeConsole(id: string): Promise<boolean> {
    try {
      const res = await fetch(`${this.base}/console-close`, {
        method: "POST",
        headers: this.headers(),
        body: JSON.stringify({ id }),
      });
      return res.ok;
    } catch {
      return false;
    }
  }

  async activateConsole(id: string): Promise<boolean> {
    try {
      const res = await fetch(`${this.base}/console-activate`, {
        method: "POST",
        headers: this.headers(),
        body: JSON.stringify({ id }),
      });
      return res.ok;
    } catch {
      return false;
    }
  }

  async setConsoleOptions(options: {
    fontFamily: string;
    bufferLines: number;
  }): Promise<boolean> {
    try {
      const res = await fetch(`${this.base}/set-console-options`, {
        method: "POST",
        headers: this.headers(),
        body: JSON.stringify({ options }),
      });
      return res.ok;
    } catch {
      return false;
    }
  }

  /** Fetch the retained raw output (base64) so a (re)mounted terminal can
   *  repaint existing scrollback. Live output then arrives via SSE. */
  async attachConsole(id: string): Promise<string> {
    try {
      const res = await fetch(`${this.base}/console-attach`, {
        method: "POST",
        headers: this.headers(),
        body: JSON.stringify({ id }),
      });
      if (!res.ok) {
        return "";
      }
      const data = (await res.json()) as { ok?: boolean; b64?: string };
      return data.ok ? String(data.b64 ?? "") : "";
    } catch {
      return "";
    }
  }

  /** Send keystrokes/text to a console session over plain HTTP (WebView2's
   *  file:// origin forbids ws:// connections, so console I/O uses POST + SSE). */
  async consoleInput(id: string, data: string): Promise<void> {
    try {
      await fetch(`${this.base}/console-input`, {
        method: "POST",
        headers: this.headers(),
        body: JSON.stringify({ id, data }),
      });
    } catch {
      // best-effort; the next keystroke will retry
    }
  }

  async consoleResize(id: string, cols: number, rows: number): Promise<void> {
    try {
      await fetch(`${this.base}/console-resize`, {
        method: "POST",
        headers: this.headers(),
        body: JSON.stringify({ id, cols, rows }),
      });
    } catch {
      // best-effort
    }
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

  /** Sync app-level presets into model_presets.json (merge), return merged list. */
  async syncModelPresets(presets: unknown[]): Promise<unknown[]> {
    const res = await fetch(`${this.base}/sync-model-presets`, {
      method: "POST",
      headers: this.headers(),
      body: JSON.stringify({ presets }),
    });
    if (!res.ok) return [];
    try {
      const data = (await res.json()) as { presets?: unknown[] };
      return Array.isArray(data.presets) ? data.presets : [];
    } catch {
      return [];
    }
  }

  /** Read current model_presets.json (already merged). */
  async getModelPresets(): Promise<unknown[]> {
    const res = await fetch(`${this.base}/model-presets`, {
      method: "POST",
      headers: this.headers(),
      body: "{}",
    });
    if (!res.ok) return [];
    try {
      const data = (await res.json()) as { presets?: unknown[] };
      return Array.isArray(data.presets) ? data.presets : [];
    } catch {
      return [];
    }
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

  async getSkillsOverview(): Promise<SkillSummary[]> {
    const res = await fetch(`${this.base}/skills-overview`, {
      method: "POST",
      headers: this.headers(),
      body: "{}",
    });
    if (!res.ok) return [];
    try {
      const data = (await res.json()) as { ok?: boolean; skills?: SkillSummary[] };
      return Array.isArray(data.skills) ? data.skills : [];
    } catch {
      return [];
    }
  }

  async setSkillEnabled(skillId: string, enabled: boolean): Promise<boolean> {
    const res = await fetch(`${this.base}/set-skill-enabled`, {
      method: "POST",
      headers: this.headers(),
      body: JSON.stringify({ skillId, enabled }),
    });
    return res.ok;
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

  async getSubAgentSessionHistory(
    sessionId: string,
    chatId: string,
  ): Promise<SubAgentSession | null> {
    try {
      const res = await fetch(`${this.base}/subagent-session-history`, {
        method: "POST",
        headers: this.headers(),
        body: JSON.stringify({ sessionId, chatId }),
      });
      if (!res.ok) return null;
      const data = (await res.json()) as { ok: boolean; session?: SubAgentSession };
      return data.ok && data.session ? data.session : null;
    } catch {
      return null;
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
    context_length_attr_name?: string;
  }): Promise<{
    ok: boolean;
    models?: { name: string; context_window?: number }[];
    error?: string;
  }> {
    const res = await fetch(`${this.base}/fetch-models`, {
      method: "POST",
      headers: this.headers(),
      body: JSON.stringify(params),
    });
    try {
      return (await res.json()) as {
        ok: boolean;
        models?: { name: string; context_window?: number }[];
        error?: string;
      };
    } catch {
      return { ok: false, error: "Bad response" };
    }
  }

  /** Silently create and activate a new chat; returns its id (or "").
   *  When ``workspaceId`` is given the backend switches to that workspace and
   *  creates the chat there in one atomic op (avoids a separate selectChat that
   *  could leave an extra empty chat behind). */
  async newChat(workspaceId = "", model = "", reasoning = ""): Promise<string> {
    const res = await fetch(`${this.base}/new-chat`, {
      method: "POST",
      headers: this.headers(),
      body: JSON.stringify(
        workspaceId || model || reasoning
          ? { workspaceId: workspaceId || undefined, model: model || undefined, reasoning: reasoning || undefined }
          : {}),
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
