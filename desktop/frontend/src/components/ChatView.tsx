import { useEffect, useRef, useState } from "react";
import { useApp } from "../state/AppContext";

export function ChatView() {
  const { transcript, busy, sendInput, interrupt, t } = useApp();
  const [draft, setDraft] = useState("");
  const scrollRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const el = scrollRef.current;
    if (el) {
      el.scrollTop = el.scrollHeight;
    }
  }, [transcript]);

  const submit = async () => {
    const text = draft.trim();
    if (!text) {
      return;
    }
    setDraft("");
    await sendInput(text);
  };

  return (
    <div className="chat-view">
      <div className="transcript" ref={scrollRef}>
        {transcript.length === 0 ? (
          <div className="transcript-empty">{t("chat.empty")}</div>
        ) : (
          transcript.map((entry) =>
            entry.kind === "input" ? (
              <div key={entry.id} className="entry entry-input">
                <span className="entry-label">{t("chat.you")}</span>
                <div className="entry-text">{entry.text}</div>
              </div>
            ) : (
              <pre key={entry.id} className="entry entry-output">
                {entry.text}
              </pre>
            ),
          )
        )}
      </div>

      <div className="composer">
        <textarea
          className="composer-input"
          value={draft}
          placeholder={t("chat.inputPlaceholder")}
          rows={3}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              void submit();
            }
          }}
        />
        <div className="composer-actions">
          {busy ? (
            <span className="busy-indicator">{t("chat.busy")}</span>
          ) : (
            <span />
          )}
          <div className="composer-buttons">
            <button className="btn" disabled={!busy} onClick={() => void interrupt()}>
              {t("chat.interrupt")}
            </button>
            <button className="btn btn-primary" disabled={!draft.trim()} onClick={() => void submit()}>
              {t("chat.send")}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
