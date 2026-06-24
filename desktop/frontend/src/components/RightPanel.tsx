import { useEffect, useMemo, useState } from "react";
import { useApp } from "../state/AppContext";
import { Icon } from "./Icon";
import { ContextMenu, type MenuItem } from "./ContextMenu";
import { PlanContent, usePlanCounts } from "./PlanPanel";
import { BrowserPanel } from "./BrowserPanel";
import {
  MANDATORY_TABS,
  RIGHT_PANEL_TABS,
  loadRightPanelPrefs,
  saveRightPanelPrefs,
  toggleTabVisibility,
  type RightPanelPrefs,
  type RightPanelTabId,
} from "../state/rightPanelTabs";

function tabLabelKey(id: RightPanelTabId): string {
  return `rightpanel.tab.${id}`;
}

/** Tabbed right-hand panel. The To-dos tab is mandatory and first; the Browser
 *  tab (and any future tabs) are toggled via the "+" menu. */
export function RightPanel() {
  const { planOpen, togglePlan, t } = useApp();
  const [prefs, setPrefs] = useState<RightPanelPrefs>(loadRightPanelPrefs);
  const [menuPos, setMenuPos] = useState<{ x: number; y: number } | null>(null);
  const { total } = usePlanCounts();

  const applyPrefs = (next: RightPanelPrefs) => {
    setPrefs(next);
    saveRightPanelPrefs(next);
  };

  // Re-read prefs when another part of the app changes them (e.g. the in-message
  // "Preview" button forces the Browser tab visible+active).
  useEffect(() => {
    const onChange = () => setPrefs(loadRightPanelPrefs());
    window.addEventListener("codewood.rightPanelTabs", onChange);
    return () => window.removeEventListener("codewood.rightPanelTabs", onChange);
  }, []);

  const menuItems: MenuItem[] = useMemo(
    () =>
      RIGHT_PANEL_TABS.map((id) => {
        const visible = prefs.visible.includes(id);
        const mandatory = (MANDATORY_TABS as readonly string[]).includes(id);
        return {
          id,
          label: `${visible ? "\u2713 " : "\u2003"}${t(tabLabelKey(id))}`,
          disabled: mandatory,
          onSelect: () => applyPrefs(toggleTabVisibility(prefs, id)),
        };
      }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [prefs, t],
  );

  if (!planOpen) {
    return null;
  }

  const setActive = (id: RightPanelTabId) =>
    applyPrefs({ ...prefs, active: id });

  return (
    <aside className="right-panel" aria-label={t("plan.title")}>
      <div className="right-panel-tabs">
        <div className="right-panel-tablist" role="tablist">
          {prefs.visible.map((id) => {
            const label =
              id === "todos"
                ? `${t(tabLabelKey(id))} (${total})`
                : t(tabLabelKey(id));
            return (
              <button
                key={id}
                role="tab"
                aria-selected={prefs.active === id}
                className={`right-panel-tab ${prefs.active === id ? "active" : ""}`}
                onClick={() => setActive(id)}
              >
                {label}
              </button>
            );
          })}
        </div>
        <button
          type="button"
          className="right-panel-tab-add"
          aria-label={t("rightpanel.tabsMenu")}
          title={t("rightpanel.tabsMenu")}
          onClick={(e) => setMenuPos({ x: e.clientX, y: e.clientY })}
        >
          <Icon name="dots" size={16} />
        </button>
        <button
          className="right-panel-close active"
          aria-label={t("rightpanel.close")}
          aria-pressed
          title={t("plan.toggle")}
          onClick={togglePlan}
        >
          <Icon name="panel-right" size={18} />
        </button>
      </div>

      <div className="right-panel-body">
        {prefs.active === "todos" && <PlanContent />}
        {prefs.active === "browser" && <BrowserPanel />}
      </div>

      {menuPos && (
        <ContextMenu
          x={menuPos.x}
          y={menuPos.y}
          items={menuItems}
          onClose={() => setMenuPos(null)}
        />
      )}
    </aside>
  );
}
