import { useEffect, useState } from "react";
import { useApp } from "../state/AppContext";

export function ConfirmDialog() {
  const { confirmRequest, answerConfirm, t } = useApp();
  const [answer, setAnswer] = useState("");

  useEffect(() => {
    setAnswer("");
  }, [confirmRequest?.id]);

  if (!confirmRequest) {
    return null;
  }

  return (
    <div className="modal-backdrop">
      <div className="modal" role="dialog" aria-modal="true">
        <h3 className="modal-title">{t("confirm.title")}</h3>
        <pre className="confirm-prompt">{confirmRequest.prompt}</pre>
        <div className="confirm-actions">
          <button className="btn btn-primary" onClick={() => void answerConfirm("y")}>
            {t("confirm.yes")}
          </button>
          <button className="btn" onClick={() => void answerConfirm("n")}>
            {t("confirm.no")}
          </button>
          <button className="btn" onClick={() => void answerConfirm("a")}>
            {t("confirm.always")}
          </button>
        </div>
        <div className="confirm-freeform">
          <input
            className="text-input"
            value={answer}
            placeholder={t("confirm.answerPlaceholder")}
            onChange={(e) => setAnswer(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && answer.trim()) {
                void answerConfirm(answer.trim());
              }
            }}
          />
          <button
            className="btn"
            disabled={!answer.trim()}
            onClick={() => void answerConfirm(answer.trim())}
          >
            {t("confirm.submit")}
          </button>
        </div>
      </div>
    </div>
  );
}
