import { useApp } from "../state/AppContext";
import { Icon, type IconName } from "./Icon";
import type { PlanStep } from "../api/types";

function statusIcon(status: string): { name: IconName; className: string } {
  if (status === "completed") {
    return { name: "check-circle", className: "plan-step-icon completed" };
  }
  if (status === "in_progress") {
    return { name: "circle-square", className: "plan-step-icon in-progress" };
  }
  return { name: "circle", className: "plan-step-icon pending" };
}

/** Number of completed/total steps for the active plan (used by the tab title). */
export function usePlanCounts(): { completed: number; total: number } {
  const { state } = useApp();
  const steps: PlanStep[] = state?.plan?.plan ?? [];
  return {
    completed: steps.filter((s) => s.status === "completed").length,
    total: steps.length,
  };
}

/** The To-dos panel body (no surrounding chrome). Rendered inside the tabbed
 *  RightPanel's content area. */
export function PlanContent() {
  const { state, t } = useApp();
  const steps: PlanStep[] = state?.plan?.plan ?? [];
  const completed = steps.filter((s) => s.status === "completed").length;
  const total = steps.length;
  const pct = total > 0 ? Math.round((completed / total) * 100) : 0;

  if (total === 0) {
    return <div className="plan-empty">{t("plan.empty")}</div>;
  }
  return (
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
  );
}
