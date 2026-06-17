import { useEffect, useRef, useState } from "react";
import { useApp } from "../state/AppContext";

/**
 * Inline clarification panel rendered at the bottom of the transcript when
 * the model called ``ask_more_info``. Shows the model's question, the
 * provided options as buttons, and a final "Other (type your own)"
 * affordance that expands into a freeform input.
 *
 * The chosen answer is sent via ``answerAskMoreInfo`` and the panel
 * dismisses itself optimistically; the runtime loop resumes the same turn
 * using the answer as the user's supplement (not as a new user message,
 * by design — the transcript stays clean).
 */
export function AskMoreInfoPanel() {
  const { askMoreInfo, answerAskMoreInfo, t } = useApp();
  const [otherOpen, setOtherOpen] = useState(false);
  const [freeform, setFreeform] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const inputRef = useRef<HTMLTextAreaElement | null>(null);
  const promptId = askMoreInfo?.id ?? "";

  useEffect(() => {
    // Each new prompt starts collapsed and empty; without this a stale
    // freeform draft from the previous question would leak into the
    // next one if both arrived in the same chat.
    setOtherOpen(false);
    setFreeform("");
    setSubmitting(false);
  }, [promptId]);

  useEffect(() => {
    if (otherOpen && inputRef.current) {
      inputRef.current.focus();
    }
  }, [otherOpen]);

  if (!askMoreInfo) {
    return null;
  }

  const submit = async (answer: string) => {
    const trimmed = answer.trim();
    if (!trimmed || submitting) {
      return;
    }
    setSubmitting(true);
    try {
      await answerAskMoreInfo(trimmed);
    } finally {
      // The panel unmounts on a successful submit because askMoreInfo
      // flips to null, so this only matters on a network error.
      setSubmitting(false);
    }
  };

  return (
    <div
      className="ask-more-info-panel"
      role="region"
      aria-label={t("askMoreInfo.regionLabel")}
    >
      <div className="ask-more-info-header">
        <span className="ask-more-info-glyph" aria-hidden>
          ?
        </span>
        <span className="ask-more-info-question">{askMoreInfo.question}</span>
      </div>
      <div className="ask-more-info-options">
        {askMoreInfo.options.map((label, idx) => (
          <button
            key={`opt-${idx}-${label}`}
            type="button"
            className="ask-more-info-option"
            disabled={submitting}
            onClick={() => void submit(label)}
          >
            <span className="ask-more-info-option-index">{idx + 1}</span>
            <span className="ask-more-info-option-label">{label}</span>
          </button>
        ))}
        {!otherOpen && (
          <button
            type="button"
            className="ask-more-info-option ask-more-info-option-other"
            disabled={submitting}
            onClick={() => setOtherOpen(true)}
          >
            <span className="ask-more-info-option-index">
              {askMoreInfo.options.length + 1}
            </span>
            <span className="ask-more-info-option-label">
              {t("askMoreInfo.other")}
            </span>
          </button>
        )}
      </div>
      {otherOpen && (
        <div className="ask-more-info-freeform">
          <textarea
            ref={inputRef}
            className="ask-more-info-textarea"
            value={freeform}
            placeholder={t("askMoreInfo.otherPlaceholder")}
            rows={2}
            disabled={submitting}
            onChange={(e) => setFreeform(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                void submit(freeform);
              } else if (e.key === "Escape") {
                e.preventDefault();
                setOtherOpen(false);
                setFreeform("");
              }
            }}
          />
          <div className="ask-more-info-freeform-actions">
            <button
              type="button"
              className="btn"
              disabled={submitting}
              onClick={() => {
                setOtherOpen(false);
                setFreeform("");
              }}
            >
              {t("askMoreInfo.cancel")}
            </button>
            <button
              type="button"
              className="btn btn-primary"
              disabled={submitting || !freeform.trim()}
              onClick={() => void submit(freeform)}
            >
              {t("askMoreInfo.submit")}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
