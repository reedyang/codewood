import { useEffect, useRef, useState } from "react";
import { useApp } from "../state/AppContext";
import { DiffPreview, langFromPath } from "./DiffPreview";

/**
 * Inline execution-policy confirmation panel rendered at the bottom of the
 * transcript when the backend (under the ``confirmation`` policy or an
 * AI-flagged manual-confirm) asks the user to approve running a command.
 *
 * This intentionally mirrors the ``request_user_input`` panel (single-select
 * fixed options, click-to-submit) instead of a modal dialog. There is NO
 * freeform input — only the fixed choices Yes / No / (optionally) Always.
 *
 * The chosen option's index is sent back via ``answerConfirm``; the backend
 * maps it to y/n/a locally. The user's pick is never forwarded to the model.
 */
/** Pull the patched file path out of the apply_patch confirm prompt so the
 *  diff preview can infer a syntax-highlighting language from its extension. */
function extractPatchPath(prompt: string): string {
  const m = /:\s*(.+?)\s*\??$/.exec(prompt || "");
  return m ? m[1] : "";
}

export function ConfirmDialog() {
  const { confirmRequest, answerConfirm, t } = useApp();
  const panelRef = useRef<HTMLDivElement | null>(null);
  const rejectInputRef = useRef<HTMLTextAreaElement | null>(null);
  const [rejectOpen, setRejectOpen] = useState(false);
  const [rejectText, setRejectText] = useState("");
  const [rejectSubmitting, setRejectSubmitting] = useState(false);
  const promptId = confirmRequest?.id ?? "";

  // Scroll the dialog into view when it appears so the user sees all
  // options, even if the command preview or diff is tall.  Defer to rAF
  // so the browser has laid out the full content (command block, diff
  // preview) before we measure.
  useEffect(() => {
    if (!confirmRequest) return;
    requestAnimationFrame(() => {
      panelRef.current?.scrollIntoView({ block: "nearest", behavior: "auto" });
    });
  }, [confirmRequest]);

  useEffect(() => {
    // Each new confirm prompt starts collapsed and empty so a stale
    // reject-supplement draft from a previous question never leaks in.
    setRejectOpen(false);
    setRejectText("");
    setRejectSubmitting(false);
  }, [promptId]);

  useEffect(() => {
    if (rejectOpen && rejectInputRef.current) {
      rejectInputRef.current.focus();
    }
  }, [rejectOpen]);

  if (!confirmRequest) {
    return null;
  }

  // Prefer the backend-supplied fixed options; fall back to built-in
  // Yes/No(/Always) labels for older backends that don't send ``options``.
  const fallbackOptions = [
    t("confirm.yes"),
    t("confirm.no"),
    ...(confirmRequest.offerAlways ? [t("confirm.always")] : []),
  ];
  const options =
    confirmRequest.options && confirmRequest.options.length > 0
      ? confirmRequest.options
      : fallbackOptions;

  // Map each option index back to the y/n/a token the backend expects. When
  // the backend sent structured options we post the index directly (the
  // backend resolves it); for the fallback we map by position.
  const answerForIndex = (idx: number): string => {
    if (confirmRequest.options && confirmRequest.options.length > 0) {
      return String(idx);
    }
    if (idx === 0) return "y";
    if (confirmRequest.offerAlways && idx === options.length - 1) return "a";
    return "n";
  };

  const submitReject = async () => {
    const trimmed = rejectText.trim();
    if (!trimmed || rejectSubmitting) {
      return;
    }
    setRejectSubmitting(true);
    try {
      // The backend maps this JSON payload to a reject-with-supplement
      // confirm answer; the supplementary text lands in the role:tool
      // result of the tool that was waiting for approval.
      await answerConfirm(JSON.stringify({ reject_with_supplement: trimmed }));
    } finally {
      // The panel unmounts on a successful submit because confirmRequest
      // flips to null, so this only matters on a network error.
      setRejectSubmitting(false);
    }
  };

  return (
    <div
      ref={panelRef}
      className="ask-more-info-panel"
      role="region"
      aria-label={t("confirm.title")}
    >
      <div className="ask-more-info-header">
        <span className="ask-more-info-glyph" aria-hidden>
          !
        </span>
        <div className="ask-more-info-header-text">
          <span className="ask-more-info-question">{confirmRequest.prompt}</span>
          {confirmRequest.command ? (
            <pre className="confirm-command-code">
              <code>{confirmRequest.command}</code>
            </pre>
          ) : null}
          {confirmRequest.diffRows && confirmRequest.diffRows.length > 0 ? (
            <DiffPreview
              rows={confirmRequest.diffRows}
              lang={langFromPath(extractPatchPath(confirmRequest.prompt))}
            />
          ) : null}
          <span className="ask-more-info-mode-hint">
            {t("askMoreInfo.singleHint")}
          </span>
        </div>
      </div>
      <div className="ask-more-info-options">
        {options.map((label, idx) => (
          <button
            key={`confirm-opt-${idx}-${label}`}
            type="button"
            className="ask-more-info-option"
            onClick={() => void answerConfirm(answerForIndex(idx))}
          >
            <span className="ask-more-info-option-index">{idx + 1}</span>
            <span className="ask-more-info-option-label">{label}</span>
          </button>
        ))}
        {confirmRequest.rejectSupplement && (
          <button
            type="button"
            className="ask-more-info-option ask-more-info-option-other"
            disabled={rejectSubmitting}
            onClick={() => setRejectOpen((v) => !v)}
          >
            <span className="ask-more-info-option-index">{options.length + 1}</span>
            <span className="ask-more-info-option-label">
              {t("confirm.rejectSupplement")}
            </span>
          </button>
        )}
      </div>
      {rejectOpen && (
        <div className="ask-more-info-freeform">
          <textarea
            ref={rejectInputRef}
            className="ask-more-info-textarea"
            value={rejectText}
            placeholder={t("confirm.rejectSupplementPlaceholder")}
            rows={2}
            disabled={rejectSubmitting}
            onChange={(e) => setRejectText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                void submitReject();
              } else if (e.key === "Escape") {
                e.preventDefault();
                setRejectOpen(false);
                setRejectText("");
              }
            }}
          />
          <div className="ask-more-info-freeform-actions">
            <button
              type="button"
              className="btn"
              disabled={rejectSubmitting}
              onClick={() => {
                setRejectOpen(false);
                setRejectText("");
              }}
            >
              {t("askMoreInfo.cancel")}
            </button>
            <button
              type="button"
              className="btn btn-primary"
              disabled={rejectSubmitting || !rejectText.trim()}
              onClick={() => void submitReject()}
            >
              {t("confirm.submit")}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
