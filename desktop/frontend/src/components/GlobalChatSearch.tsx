import {
  useEffect,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode,
} from "react";
import type { ChatSearchHit } from "../api/types";
import { useApp } from "../state/AppContext";
import { Icon } from "./Icon";

const DEBOUNCE_MS = 150;

function formatChatTime(value: string): string {
  const m = /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})/.exec(value || "");
  if (!m) {
    return "";
  }
  return `${m[2]}-${m[3]} ${m[4]}:${m[5]}`;
}

/** Title-bar full-text search across all workspaces' chats. */
export function GlobalChatSearch() {
  const { t, client, openSearchHit, clearSearchHit } = useApp();
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [results, setResults] = useState<ChatSearchHit[]>([]);
  const [total, setTotal] = useState(0);
  const [activeIdx, setActiveIdx] = useState(-1);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const boxRef = useRef<HTMLDivElement | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Ctrl+K / Ctrl+F focuses the search box.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && (e.key === "k" || e.key === "f")) {
        e.preventDefault();
        inputRef.current?.focus();
        inputRef.current?.select();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // Close the dropdown when clicking elsewhere.
  useEffect(() => {
    if (!open) {
      return;
    }
    const onPointer = (e: MouseEvent) => {
      if (boxRef.current && !boxRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    window.addEventListener("mousedown", onPointer);
    return () => window.removeEventListener("mousedown", onPointer);
  }, [open]);

  useEffect(
    () => () => {
      abortRef.current?.abort();
      if (timerRef.current) {
        clearTimeout(timerRef.current);
      }
    },
    [],
  );

  const runSearch = async (q: string) => {
    abortRef.current?.abort();
    const trimmed = q.trim();
    if (!trimmed) {
      clearSearchHit();
      setResults([]);
      setTotal(0);
      setLoading(false);
      setActiveIdx(-1);
      return;
    }
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    setLoading(true);
    try {
      const res = await client.searchChats(trimmed, 20, ctrl.signal);
      if (ctrl.signal.aborted) {
        return;
      }
      setResults(res.results ?? []);
      setTotal(res.total ?? 0);
      setActiveIdx(res.results && res.results.length > 0 ? 0 : -1);
      setOpen(true);
    } catch {
      if (!ctrl.signal.aborted) {
        setResults([]);
        setTotal(0);
        setOpen(true);
      }
    } finally {
      if (!ctrl.signal.aborted) {
        setLoading(false);
      }
    }
  };

  const onInputChange = (value: string) => {
    setQuery(value);
    if (timerRef.current) {
      clearTimeout(timerRef.current);
    }
    timerRef.current = setTimeout(() => void runSearch(value), DEBOUNCE_MS);
  };

  const pick = (hit: ChatSearchHit) => {
    setOpen(false);
    void openSearchHit(hit);
  };

  const onKeyDown = (e: ReactKeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Escape") {
      e.preventDefault();
      setOpen(false);
      clearSearchHit();
      inputRef.current?.blur();
      return;
    }
    if (!open || results.length === 0) {
      return;
    }
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActiveIdx((i) => (i + 1) % results.length);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActiveIdx((i) => (i <= 0 ? results.length - 1 : i - 1));
    } else if (e.key === "Enter") {
      e.preventDefault();
      const hit = results[activeIdx >= 0 ? activeIdx : 0];
      if (hit) {
        pick(hit);
      }
    }
  };

  const renderSnippet = (hit: ChatSearchHit): ReactNode => {
    const ranges = hit.ranges ?? [];
    if (ranges.length === 0) {
      return <span className="chat-search-snippet-text">{hit.snippet}</span>;
    }
    const parts: ReactNode[] = [];
    let cursor = 0;
    ranges.forEach(([s, e], i) => {
      if (s > cursor) {
        parts.push(hit.snippet.slice(cursor, s));
      }
      parts.push(
        <mark key={i} className="search-term">
          {hit.snippet.slice(s, e)}
        </mark>,
      );
      cursor = e;
    });
    if (cursor < hit.snippet.length) {
      parts.push(hit.snippet.slice(cursor));
    }
    return <span className="chat-search-snippet-text">{parts}</span>;
  };

  return (
    <div className="chat-search" ref={boxRef}>
      <div className="chat-search-input-wrap">
        <Icon name="search" size={14} className="chat-search-icon" />
        <input
          ref={inputRef}
          className="chat-search-input"
          type="text"
          value={query}
          placeholder={t("search.placeholder")}
          onChange={(e) => onInputChange(e.target.value)}
          onFocus={() => {
            if (results.length > 0 || query.trim()) {
              setOpen(true);
            }
          }}
          onKeyDown={onKeyDown}
          spellCheck={false}
          autoComplete="off"
        />
        {loading && <Icon name="spinner" size={14} className="chat-search-spinner" />}
        {query && !loading && (
          <button
            type="button"
            className="chat-search-clear"
            aria-label={t("search.clear")}
            onClick={() => {
              setQuery("");
              setResults([]);
              setTotal(0);
              setOpen(false);
              clearSearchHit();
              inputRef.current?.focus();
            }}
          >
            ×
          </button>
        )}
      </div>
      {open && (
        <div className="chat-search-panel" role="listbox" aria-label={t("search.placeholder")}>
          {loading ? (
            <div className="chat-search-empty">{t("search.loading")}</div>
          ) : results.length === 0 ? (
            <div className="chat-search-empty">
              {query.trim() ? t("search.empty") : ""}
            </div>
          ) : (
            <>
              <div className="chat-search-summary">
                {t("search.results", { n: String(total) })}
              </div>
              {results.map((hit, i) => (
                <button
                  type="button"
                  key={`${hit.wsId}:${hit.chatId}:${hit.msgIdx}`}
                  className={`chat-search-row ${i === activeIdx ? "active" : ""}`}
                  role="option"
                  aria-selected={i === activeIdx}
                  onMouseEnter={() => setActiveIdx(i)}
                  onClick={() => pick(hit)}
                >
                  <div className="chat-search-row-title">
                    <span className="chat-search-chat-name">
                      {hit.chatName || t("chat.new")}
                    </span>
                    {hit.wsName && (
                      <span className="chat-search-ws-name">{hit.wsName}</span>
                    )}
                    <span className="chat-search-time">
                      {formatChatTime(hit.updatedAt)}
                    </span>
                  </div>
                  <div className="chat-search-snippet">{renderSnippet(hit)}</div>
                  {hit.keywords && hit.keywords.length > 0 && (
                    <div className="chat-search-kw">
                      {hit.keywords.map((kw) => (
                        <span key={kw} className="chat-search-kw-chip">
                          {kw}
                        </span>
                      ))}
                    </div>
                  )}
                </button>
              ))}
            </>
          )}
        </div>
      )}
    </div>
  );
}
