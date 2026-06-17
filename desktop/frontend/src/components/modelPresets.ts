// Known API platform presets. Selecting one only requires the user to enter an
// API key; the rest (base URL, api_mode) is filled in automatically. "custom"
// lets the user type everything by hand.

export interface ModelPreset {
  id: string;
  label: string;
  provider: string;
  base_url: string;
  api_mode: string;
  /** When true the UI only asks for an API key. */
  presetFilled: boolean;
}

export const MODEL_PRESETS: ModelPreset[] = [
  {
    id: "deepseek",
    label: "DeepSeek",
    provider: "DeepSeek",
    base_url: "https://api.deepseek.com",
    api_mode: "chat",
    presetFilled: true,
  },
  {
    id: "openai",
    label: "OpenAI",
    provider: "OpenAI",
    base_url: "https://api.openai.com/v1",
    api_mode: "chat",
    presetFilled: true,
  },
  {
    id: "zhipu",
    label: "智谱 (Zhipu)",
    provider: "Zhipu",
    base_url: "https://open.bigmodel.cn/api/paas/v4",
    api_mode: "chat",
    presetFilled: true,
  },
  {
    id: "qwen",
    label: "Qwen (DashScope)",
    provider: "Qwen",
    base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1",
    api_mode: "chat",
    presetFilled: true,
  },
  {
    id: "mimo",
    label: "Mimo (Xiaomi)",
    provider: "Mimo",
    base_url: "https://api.mimo.xiaomi.com/v1",
    api_mode: "chat",
    presetFilled: true,
  },
  {
    id: "minimax",
    label: "MiniMax",
    provider: "MiniMax",
    base_url: "https://api.minimax.chat/v1",
    api_mode: "chat",
    presetFilled: true,
  },
  {
    id: "doubao",
    label: "豆包 (Doubao)",
    provider: "Doubao",
    base_url: "https://ark.cn-beijing.volces.com/api/v3",
    api_mode: "chat",
    presetFilled: true,
  },
  {
    id: "custom",
    label: "Custom API",
    provider: "",
    base_url: "",
    api_mode: "chat",
    presetFilled: false,
  },
];

export function findPreset(id: string): ModelPreset | undefined {
  return MODEL_PRESETS.find((p) => p.id === id);
}

// Editor-side representation of a configured provider.
export interface EditorModel {
  name: string;
  enabled: boolean;
  context_window?: string | number;
  multimodal?: boolean;
}

export interface EditorProvider {
  provider: string;
  api_key: string;
  base_url: string;
  api_mode: string;
  port?: number;
  models: EditorModel[];
  /** Auto-refresh + select-all on every app start when true. */
  auto_refresh?: boolean;
}

/** Convert a raw config provider object into the editor model. */
export function toEditorProvider(raw: unknown): EditorProvider {
  const obj = (raw ?? {}) as Record<string, unknown>;
  const params = (obj.params ?? {}) as Record<string, unknown>;
  const rawModels = Array.isArray(params.models) ? params.models : [];
  const models: EditorModel[] = rawModels.map((m) => {
    if (typeof m === "string") {
      return { name: m, enabled: true };
    }
    const mm = (m ?? {}) as Record<string, unknown>;
    return {
      name: String(mm.name ?? ""),
      enabled: true,
      context_window: mm.context_window as string | number | undefined,
      multimodal: mm.multimodal as boolean | undefined,
    };
  });
  return {
    provider: String(obj.provider ?? ""),
    api_key: String(params.api_key ?? ""),
    base_url: String(params.base_url ?? ""),
    api_mode: String(params.api_mode ?? "chat"),
    port: typeof params.port === "number" ? params.port : undefined,
    models,
    auto_refresh: Boolean(params.auto_refresh),
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
        return model;
      });
    return { provider: e.provider, params };
  });
}
