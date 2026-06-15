import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { ApiClient } from "../api/client";
import type { AppState, ConfirmRequest, ServerEvent } from "../api/types";
import { normalizeLang, translate, type Lang } from "../i18n";

export type Theme = "light" | "dark";

export interface TranscriptEntry {
  id: number;
  kind: "input" | "output";
  text: string;
}

interface AppContextValue {
  state: AppState | null;
  transcript: TranscriptEntry[];
  busy: boolean;
  connected: boolean;
  confirmRequest: ConfirmRequest | null;
  theme: Theme;
  lang: Lang;
  t: (key: string) => string;
  setTheme: (theme: Theme) => void;
  sendInput: (text: string) => Promise<void>;
  runCommand: (command: string) => Promise<void>;
  interrupt: () => Promise<void>;
  answerConfirm: (answer: string) => Promise<void>;
  clearTranscript: () => void;
}

const AppContext = createContext<AppContextValue | null>(null);

const THEME_STORAGE_KEY = "codewood.theme";

function loadInitialTheme(): Theme {
  const stored = window.localStorage.getItem(THEME_STORAGE_KEY);
  if (stored === "light" || stored === "dark") {
    return stored;
  }
  const prefersDark = window.matchMedia?.("(prefers-color-scheme: dark)").matches;
  return prefersDark ? "dark" : "light";
}

export function AppProvider({ children }: { children: ReactNode }) {
  const clientRef = useRef<ApiClient | null>(null);
  if (clientRef.current === null) {
    clientRef.current = new ApiClient();
  }
  const client = clientRef.current;

  const [state, setState] = useState<AppState | null>(null);
  const [transcript, setTranscript] = useState<TranscriptEntry[]>([]);
  const [busy, setBusy] = useState(false);
  const [connected, setConnected] = useState(false);
  const [confirmRequest, setConfirmRequest] = useState<ConfirmRequest | null>(null);
  const [theme, setThemeState] = useState<Theme>(loadInitialTheme);
  const nextIdRef = useRef(1);

  const lang = useMemo(() => normalizeLang(state?.language), [state?.language]);
  const t = useCallback((key: string) => translate(lang, key), [lang]);

  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
  }, [theme]);

  const setTheme = useCallback((value: Theme) => {
    setThemeState(value);
    window.localStorage.setItem(THEME_STORAGE_KEY, value);
  }, []);

  const appendOutput = useCallback((text: string) => {
    setTranscript((prev) => {
      const last = prev[prev.length - 1];
      if (last && last.kind === "output") {
        const copy = prev.slice(0, -1);
        copy.push({ ...last, text: last.text + text });
        return copy;
      }
      return [...prev, { id: nextIdRef.current++, kind: "output", text }];
    });
  }, []);

  const pushInput = useCallback((text: string) => {
    setTranscript((prev) => [
      ...prev,
      { id: nextIdRef.current++, kind: "input", text },
    ]);
  }, []);

  useEffect(() => {
    let source: EventSource | null = null;

    const handleEvent = (event: ServerEvent) => {
      switch (event.event) {
        case "idle": {
          const next = (event.data as { state?: AppState }).state;
          if (next) {
            setState(next);
          }
          setBusy(false);
          break;
        }
        case "turn_start": {
          const text = String((event.data as { text?: string }).text ?? "");
          if (text) {
            pushInput(text);
          }
          setBusy(true);
          break;
        }
        case "output": {
          const text = String((event.data as { text?: string }).text ?? "");
          if (text) {
            appendOutput(text);
          }
          break;
        }
        case "confirm": {
          setConfirmRequest(event.data as ConfirmRequest);
          break;
        }
        default:
          break;
      }
    };

    source = client.connectEvents(handleEvent);
    source.onopen = () => setConnected(true);
    source.onerror = () => setConnected(false);

    client
      .getState()
      .then((value) => {
        setState(value);
        setConnected(true);
      })
      .catch(() => setConnected(false));

    return () => {
      source?.close();
    };
  }, [client, appendOutput, pushInput]);

  const sendInput = useCallback(
    async (text: string) => {
      const trimmed = text.trim();
      if (!trimmed) {
        return;
      }
      await client.sendInput(trimmed);
    },
    [client],
  );

  const runCommand = useCallback(
    async (command: string) => {
      await client.sendInput(command);
    },
    [client],
  );

  const interrupt = useCallback(async () => {
    await client.interrupt();
  }, [client]);

  const answerConfirm = useCallback(
    async (answer: string) => {
      const current = confirmRequest;
      setConfirmRequest(null);
      if (current) {
        await client.confirm(current.id, answer);
      }
    },
    [client, confirmRequest],
  );

  const clearTranscript = useCallback(() => setTranscript([]), []);

  const value: AppContextValue = {
    state,
    transcript,
    busy,
    connected,
    confirmRequest,
    theme,
    lang,
    t,
    setTheme,
    sendInput,
    runCommand,
    interrupt,
    answerConfirm,
    clearTranscript,
  };

  return <AppContext.Provider value={value}>{children}</AppContext.Provider>;
}

export function useApp(): AppContextValue {
  const ctx = useContext(AppContext);
  if (!ctx) {
    throw new Error("useApp must be used within AppProvider");
  }
  return ctx;
}
