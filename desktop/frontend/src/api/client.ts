import type { AppState, ServerEvent } from "./types";

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
