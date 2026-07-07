// Known API platform presets. Selecting one only requires the user to enter an
// API key; the rest (base URL, api_mode) is filled in automatically. "custom"
// lets the user type everything by hand.

export type PresetKind = "openai" | "ollama" | "custom";

export interface ModelPreset {
  id: string;
  label: string;
  provider: string;
  base_url: string;
  api_mode: string;
  /** Distinguishes connection shape: OpenAI-compatible, Ollama, or custom. */
  kind: PresetKind;
}

export const MODEL_PRESETS: ModelPreset[] = [
  {
    id: "deepseek",
    label: "DeepSeek",
    provider: "DeepSeek",
    base_url: "https://api.deepseek.com",
    api_mode: "chat",
    kind: "openai",
  },
  {
    id: "openai",
    label: "OpenAI",
    provider: "OpenAI",
    base_url: "https://api.openai.com/v1",
    api_mode: "chat",
    kind: "openai",
  },
  {
    id: "zhipu",
    label: "智谱 (Zhipu)",
    provider: "Zhipu",
    base_url: "https://open.bigmodel.cn/api/paas/v4",
    api_mode: "chat",
    kind: "openai",
  },
  {
    id: "qwen",
    label: "Qwen (DashScope)",
    provider: "Qwen",
    base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1",
    api_mode: "chat",
    kind: "openai",
  },
  {
    id: "mimo",
    label: "Mimo (Xiaomi)",
    provider: "Mimo",
    base_url: "https://api.mimo.xiaomi.com/v1",
    api_mode: "chat",
    kind: "openai",
  },
  {
    id: "minimax",
    label: "MiniMax",
    provider: "MiniMax",
    base_url: "https://api.minimax.chat/v1",
    api_mode: "chat",
    kind: "openai",
  },
  {
    id: "doubao",
    label: "豆包 (Doubao)",
    provider: "Doubao",
    base_url: "https://ark.cn-beijing.volces.com/api/v3",
    api_mode: "chat",
    kind: "openai",
  },
  {
    id: "moonshot",
    label: "月之暗面 (Moonshot)",
    provider: "Moonshot",
    base_url: "https://api.moonshot.cn/v1",
    api_mode: "chat",
    kind: "openai",
  },
  {
    id: "sensenova",
    label: "SenseNova (商汤日日新)",
    provider: "SenseNova",
    base_url: "https://token.sensenova.cn/v1",
    api_mode: "chat",
    kind: "openai",
  },
  {
    id: "ollama",
    label: "Ollama",
    provider: "Ollama",
    base_url: "",
    api_mode: "ollama",
    kind: "ollama",
  },
  {
    id: "custom",
    label: "Custom API",
    provider: "",
    base_url: "",
    api_mode: "chat",
    kind: "custom",
  },
];

export function findPreset(id: string): ModelPreset | undefined {
  return MODEL_PRESETS.find((p) => p.id === id);
}

/** Pick the best-matching preset id for a loaded provider (by api_mode/base_url). */
export function presetIdForProvider(p: {
  api_mode: string;
  base_url: string;
}): string {
  if ((p.api_mode || "").toLowerCase() === "ollama") {
    return "ollama";
  }
  const base = (p.base_url || "").replace(/\/+$/, "");
  const match = MODEL_PRESETS.find(
    (preset) => preset.kind === "openai" && preset.base_url.replace(/\/+$/, "") === base,
  );
  return match ? match.id : "custom";
}

export interface EditorHeader {
  key: string;
  value: string;
}

// Editor-side representation of a configured provider.
export interface EditorModel {
  name: string;
  enabled: boolean;
  context_window?: string | number;
  multimodal?: boolean;
  /** Whether the model supports thinking/reasoning tokens. */
  thinking?: boolean;
  /** Whether streaming responses are enabled (default true). */
  streaming?: boolean;
  /** Reasoning-effort levels this model supports (subset of low/medium/high). */
  reasoning_effort: string[];
  /** Per-model custom request headers (OpenAI-compatible only). */
  extra_headers: EditorHeader[];
}

export interface EditorProvider {
  provider: string;
  /** Optional human label; required to disambiguate duplicate providers. */
  display_name: string;
  api_key: string;
  base_url: string;
  api_mode: string;
  port?: number;
  models: EditorModel[];
  /** Auto-refresh + select-all on every app start when true. */
  auto_refresh?: boolean;
  /** Selected platform preset id (drives which fields are shown). */
  presetId: string;
}

/** Convert a raw config provider object into the editor model. */
export function toEditorProvider(raw: unknown): EditorProvider {
  const obj = (raw ?? {}) as Record<string, unknown>;
  const params = (obj.params ?? {}) as Record<string, unknown>;
  const rawModels = Array.isArray(params.models) ? params.models : [];
  const models: EditorModel[] = rawModels.map((m) => {
    if (typeof m === "string") {
      return { name: m, enabled: true, reasoning_effort: [], extra_headers: [] };
    }
    const mm = (m ?? {}) as Record<string, unknown>;
    const re = Array.isArray(mm.reasoning_effort)
      ? mm.reasoning_effort.map((x) => String(x).trim().toLowerCase()).filter(Boolean)
      : [];
    const headersObj =
      mm.extra_headers && typeof mm.extra_headers === "object"
        ? (mm.extra_headers as Record<string, unknown>)
        : {};
    const headers: EditorHeader[] = Object.entries(headersObj).map(([key, value]) => ({
      key: String(key),
      value: String(value ?? ""),
    }));
    return {
      name: String(mm.name ?? ""),
      enabled: true,
      context_window: mm.context_window as string | number | undefined,
      multimodal: mm.multimodal as boolean | undefined,
      thinking: mm.thinking as boolean | undefined,
      streaming: mm.streaming as boolean | undefined,
      reasoning_effort: re,
      extra_headers: headers,
    };
  });
  const api_mode = String(params.api_mode ?? "chat");
  const base_url = String(params.base_url ?? "");
  return {
    provider: String(obj.provider ?? ""),
    display_name: String(obj.display_name ?? ""),
    api_key: String(params.api_key ?? ""),
    base_url,
    api_mode,
    port: typeof params.port === "number" ? params.port : undefined,
    models,
    auto_refresh: Boolean(params.auto_refresh),
    presetId: presetIdForProvider({ api_mode, base_url }),
  };
}

/** Serialize editor providers back into the raw config_providers shape. */
export function toConfigProviders(editors: EditorProvider[]): unknown[] {
  return editors.map((e) => {
    const params: Record<string, unknown> = {
      api_mode: e.api_mode || "chat",
    };
    if (e.api_key) params.api_key = e.api_key;
    if (e.base_url) params.base_url = e.base_url;
    if (typeof e.port === "number") params.port = e.port;
    if (e.auto_refresh) params.auto_refresh = true;
    params.models = e.models
      .filter((m) => m.enabled && m.name.trim())
      .map((m) => {
        const model: Record<string, unknown> = { name: m.name.trim() };
        if (m.context_window !== undefined && m.context_window !== "") {
          model.context_window = m.context_window;
        }
        if (m.multimodal !== undefined) {
          model.multimodal = m.multimodal;
        }
        if (m.streaming !== undefined) {
          model.streaming = m.streaming;
        }
        if (m.thinking !== undefined) {
          model.thinking = m.thinking;
        }
        // Ollama doesn't support reasoning effort or custom headers; only
        // serialize them for OpenAI-compatible providers.
        if ((e.api_mode || "").toLowerCase() !== "ollama") {
          const re = (m.reasoning_effort || [])
            .map((x) => String(x).trim().toLowerCase())
            .filter(Boolean);
          if (re.length > 0) {
            model.reasoning_effort = re;
          }
          const headers: Record<string, string> = {};
          for (const h of m.extra_headers || []) {
            const k = h.key.trim();
            if (k) headers[k] = h.value;
          }
          if (Object.keys(headers).length > 0) {
            model.extra_headers = headers;
          }
        }
        return model;
      });
    const entry: Record<string, unknown> = { provider: e.provider, params };
    if (e.display_name && e.display_name.trim()) {
      entry.display_name = e.display_name.trim();
    }
    return entry;
  });
}
