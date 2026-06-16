import { useApp } from "../state/AppContext";
import { Icon } from "./Icon";

export function AboutDialog({ onClose }: { onClose: () => void }) {
  const { state, t } = useApp();
  const name = state?.app.name ?? "Code Wood";
  const version = state?.app.version ?? "";

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal about-modal" role="dialog" aria-modal="true" onClick={(e) => e.stopPropagation()}>
        <div className="about-head">
          <Icon name="info" size={28} />
          <h3 className="modal-title">{t("about.title")}</h3>
        </div>
        <div className="about-body">
          <div className="about-name">{name}</div>
          {version && <div className="about-version">{t("about.version")} {version}</div>}
          <p className="about-desc">{t("about.description")}</p>
        </div>
        <div className="modal-actions">
          <button className="btn btn-primary" onClick={onClose}>
            {t("common.ok")}
          </button>
        </div>
      </div>
    </div>
  );
}
