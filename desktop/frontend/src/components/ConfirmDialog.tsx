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

  return (
    <div
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
      </div>
    </div>
  );
}
