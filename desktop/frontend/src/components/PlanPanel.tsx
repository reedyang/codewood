import { useApp } from "../state/AppContext";
import { Icon, type IconName } from "./Icon";
import type { PlanStep } from "../api/types";

function statusIcon(status: string): { name: IconName; className: string } {
  if (status === "completed") {
    return { name: "check-circle", className: "plan-step-icon completed" };
  }
  if (status === "in_progress") {
    return { name: "spinner", className: "plan-step-icon in-progress" };
  }
  return { name: "circle", className: "plan-step-icon pending" };
}

export function PlanPanel() {
  const { state, planOpen, togglePlan, t } = useApp();
  if (!planOpen) {
    return null;
  }

  const steps: PlanStep[] = state?.plan?.plan ?? [];
  const completed = steps.filter((s) => s.status === "completed").length;
  const total = steps.length;
  const pct = total > 0 ? Math.round((completed / total) * 100) : 0;

  return (
    <aside className="plan-panel" aria-label={t("plan.title")}>
      <div className="plan-panel-head">
        <span className="plan-panel-title">{t("plan.title")}</span>
        <button
          className="plan-panel-close"
          aria-label={t("plan.close")}
          onClick={togglePlan}
        >
          <Icon name="win-close" size={14} />
        </button>
      </div>

      {total === 0 ? (
        <div className="plan-empty">{t("plan.empty")}</div>
      ) : (
        <>
          <div className="plan-progress">
            <div className="plan-progress-bar">
              <div className="plan-progress-fill" style={{ width: `${pct}%` }} />
            </div>
            <span className="plan-progress-label">
              {completed}/{total}
            </span>
          </div>
          <ol className="plan-steps">
            {steps.map((step, idx) => {
              const { name, className } = statusIcon(step.status);
              return (
                <li key={idx} className={`plan-step ${step.status}`}>
                  <Icon name={name} size={16} className={className} />
                  <span className="plan-step-text">{step.step}</span>
                </li>
              );
            })}
          </ol>
        </>
      )}
    </aside>
  );
}
