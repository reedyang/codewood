import { useEffect, useState } from "react";
import { useApp } from "../state/AppContext";
import { Icon } from "./Icon";
import { PlanContent, usePlanCounts } from "./PlanPanel";
import { BrowserPanel } from "./BrowserPanel";
import {
  MANDATORY_TABS,
  loadRightPanelPrefs,
  saveRightPanelPrefs,
  type RightPanelPrefs,
  type RightPanelTabId,
} from "../state/rightPanelTabs";

function tabLabelKey(id: RightPanelTabId): string {
  return `rightpanel.tab.${id}`;
}

/** Tabbed right-hand panel. The To-dos tab is mandatory and first; the Browser
 *  tab is opened from the View menu and closed via its own "x" button (so no
 *  in-panel menu floats over the embedded browser's overlay window). */
export function RightPanel() {
  const { planOpen, togglePlan, hideBrowserTab, t } = useApp();
  const [prefs, setPrefs] = useState<RightPanelPrefs>(loadRightPanelPrefs);
  const { total } = usePlanCounts();

  const applyPrefs = (next: RightPanelPrefs) => {
    setPrefs(next);
    saveRightPanelPrefs(next);
  };

  // Re-read prefs when another part of the app changes them (e.g. the View
  // menu / in-message "Preview" button forces the Browser tab visible+active).
  useEffect(() => {
    const onChange = () => setPrefs(loadRightPanelPrefs());
    window.addEventListener("codewood.rightPanelTabs", onChange);
    return () => window.removeEventListener("codewood.rightPanelTabs", onChange);
  }, []);

  if (!planOpen) {
    return null;
  }

  const setActive = (id: RightPanelTabId) =>
    applyPrefs({ ...prefs, active: id });

  return (
    <aside className="right-panel" aria-label={t("plan.title")}>
      <div className="right-panel-tabs">
        <div className="right-panel-tablist">
          {prefs.visible.map((id) => {
            const label =
              id === "todos"
                ? `${t(tabLabelKey(id))} (${total})`
                : t(tabLabelKey(id));
            const closable = !(MANDATORY_TABS as readonly string[]).includes(id);
            const isActive = prefs.active === id;
            return (
              <span
                key={id}
                className={`right-panel-tab-wrap ${isActive ? "active" : ""}`}
              >
                <button
                  className={`right-panel-tab ${isActive ? "active" : ""}`}
                  onClick={() => setActive(id)}
                >
                  {label}
                </button>
                {closable && (
                  <button
                    type="button"
                    className="right-panel-tab-close"
                    aria-label={t("rightpanel.tab.close")}
                    title={t("rightpanel.tab.close")}
                    onClick={() => {
                      if (id === "browser") {
                        hideBrowserTab();
                      }
                    }}
                  >
                    <Icon name="win-close" size={11} />
                  </button>
                )}
              </span>
            );
          })}
        </div>
        <button
          className="right-panel-close"
          aria-label={t("rightpanel.close")}
          title={t("rightpanel.toggle")}
          onClick={togglePlan}
        >
          <Icon name="panel-right" size={18} />
        </button>
      </div>

      <div className="right-panel-body">
        {prefs.active === "todos" && <PlanContent />}
        {/* Keep the Browser tab mounted (just hidden) whenever it is open so
            switching to To-dos and back does not tear down the overlay browser
            and reload a blank page — the page keeps running in the background
            and reappears on tab switch. */}
        {prefs.visible.includes("browser") && (
          <div
            className="right-panel-tab-content"
            style={{
              display: prefs.active === "browser" ? "flex" : "none",
            }}
          >
            <BrowserPanel active={prefs.active === "browser"} />
          </div>
        )}
      </div>
    </aside>
  );
}
