import { useApp } from "../state/AppContext";
import { Icon } from "./Icon";
import appIcon from "../assets/app_icon.svg";

export function AboutDialog({ onClose }: { onClose: () => void }) {
  const { state, t } = useApp();
  const name = state?.app.name ?? "Code Wood";
  const version = state?.app.version ?? "";

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        className="modal about-modal"
        role="dialog"
        aria-modal="true"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="about-titlebar">
          <img className="about-titlebar-icon" src={appIcon} alt="" aria-hidden="true" />
          <span className="about-titlebar-title">{t("about.title")}</span>
          <button className="about-close" aria-label={t("settings.close")} onClick={onClose}>
            <Icon name="win-close" size={11} />
          </button>
        </div>
        <div className="about-content">
          <img className="about-icon" src={appIcon} alt={name} />
          <div className="about-name">{name}</div>
          {version && (
            <div className="about-version">
              {t("about.version")} {version}
            </div>
          )}
          <div className="about-copyright">{t("about.copyright")}</div>
        </div>
        <div className="about-footer">
          <button className="btn btn-primary" onClick={onClose}>
            {t("common.ok")}
          </button>
        </div>
      </div>
    </div>
  );
}
