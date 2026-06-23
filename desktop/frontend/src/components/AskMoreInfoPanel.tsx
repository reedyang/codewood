import { useEffect, useRef, useState } from "react";
import { useApp } from "../state/AppContext";

/**
 * Inline clarification panel rendered at the bottom of the transcript when
 * the model called ``request_user_input``. Two layouts:
 *
 *   * single-select (default) — each option is a button that submits
 *     immediately on click. "Other" expands a textarea + Send button.
 *   * multi-select — each option is a checkbox-style toggle; the user
 *     ticks any subset, optionally adds an "Other" freeform fragment,
 *     and clicks the bottom Submit button. The supplement sent back is
 *     the ticked labels (plus the freeform text if any) joined with
 *     "; ".
 *
 * The chosen answer is sent via ``answerAskMoreInfo`` and the panel
 * dismisses itself optimistically; the runtime loop resumes the same
 * turn using the answer as the user's supplement (NOT as a new user
 * message, by design — the transcript stays clean).
 */
export function AskMoreInfoPanel() {
  const { askMoreInfo, answerAskMoreInfo, t } = useApp();
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [otherOpen, setOtherOpen] = useState(false);
  const [freeform, setFreeform] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const inputRef = useRef<HTMLTextAreaElement | null>(null);
  const promptId = askMoreInfo?.id ?? "";
  const multi = Boolean(askMoreInfo?.multiSelect);

  useEffect(() => {
    // Each new prompt starts collapsed and empty; without this a stale
    // freeform draft or stale ticked checkboxes from the previous
    // question would leak into the next one if both arrived in the
    // same chat.
    setSelected(new Set());
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

  const submitMulti = async () => {
    if (submitting || !askMoreInfo) {
      return;
    }
    // Preserve the option list's source order so the model sees picks
    // in a deterministic shape regardless of click sequence.
    const parts: string[] = [];
    askMoreInfo.options.forEach((label, idx) => {
      if (selected.has(idx)) {
        parts.push(label);
      }
    });
    const freeformTrimmed = freeform.trim();
    if (otherOpen && freeformTrimmed) {
      parts.push(freeformTrimmed);
    }
    if (parts.length === 0) {
      return;
    }
    await submit(parts.join("; "));
  };

  const toggleSelected = (idx: number) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(idx)) {
        next.delete(idx);
      } else {
        next.add(idx);
      }
      return next;
    });
  };

  const canSubmitMulti =
    multi &&
    !submitting &&
    (selected.size > 0 || (otherOpen && freeform.trim().length > 0));

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
        <div className="ask-more-info-header-text">
          <span className="ask-more-info-question">{askMoreInfo.question}</span>
          <span className="ask-more-info-mode-hint">
            {multi ? t("askMoreInfo.multiHint") : t("askMoreInfo.singleHint")}
          </span>
        </div>
      </div>
      <div className="ask-more-info-options">
        {askMoreInfo.options.map((label, idx) => {
          const isSelected = selected.has(idx);
          const optionClass = [
            "ask-more-info-option",
            multi ? "ask-more-info-option-multi" : "",
            multi && isSelected ? "is-selected" : "",
          ]
            .filter(Boolean)
            .join(" ");
          return (
            <button
              key={`opt-${idx}-${label}`}
              type="button"
              className={optionClass}
              disabled={submitting}
              {...(multi ? { "aria-pressed": isSelected } : {})}
              onClick={() =>
                multi ? toggleSelected(idx) : void submit(label)
              }
            >
              <span className="ask-more-info-option-index">
                {multi ? (
                  <span
                    className={`ask-more-info-check${
                      isSelected ? " is-on" : ""
                    }`}
                    aria-hidden
                  >
                    {isSelected ? "\u2713" : ""}
                  </span>
                ) : (
                  idx + 1
                )}
              </span>
              <span className="ask-more-info-option-label">{label}</span>
            </button>
          );
        })}
        {!otherOpen && (
          <button
            type="button"
            className="ask-more-info-option ask-more-info-option-other"
            disabled={submitting}
            onClick={() => setOtherOpen(true)}
          >
            <span className="ask-more-info-option-index">
              {multi ? (
                <span className="ask-more-info-check" aria-hidden>
                  +
                </span>
              ) : (
                askMoreInfo.options.length + 1
              )}
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
              if (e.key === "Enter" && !e.shiftKey && !multi) {
                // Single-select: Enter submits the freeform answer
                // immediately. In multi-select we don't want Enter to
                // bypass the user's chance to also tick options, so
                // they use the bottom Submit button instead.
                e.preventDefault();
                void submit(freeform);
              } else if (e.key === "Escape") {
                e.preventDefault();
                setOtherOpen(false);
                setFreeform("");
              }
            }}
          />
          {!multi && (
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
          )}
        </div>
      )}
      {multi && (
        <div className="ask-more-info-multi-actions">
          <span className="ask-more-info-multi-count">
            {t("askMoreInfo.selectedLabel")}{" "}
            {selected.size + (otherOpen && freeform.trim() ? 1 : 0)}
          </span>
          <button
            type="button"
            className="btn btn-primary"
            disabled={!canSubmitMulti}
            onClick={() => void submitMulti()}
          >
            {t("askMoreInfo.submit")}
          </button>
        </div>
      )}
    </div>
  );
}
