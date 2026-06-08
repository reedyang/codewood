"""Implementation of the `update_plan` tool.

The plan is a short, ordered list of steps with a status per step that the
model maintains while working on a task. It is stored on the active chat
record (`chat["plan"]`) and is not surfaced directly to the user; the host
only persists it for use as model context.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


PLAN_STATUS_PENDING = "pending"
PLAN_STATUS_IN_PROGRESS = "in_progress"
PLAN_STATUS_COMPLETED = "completed"
PLAN_STATUSES = (
    PLAN_STATUS_PENDING,
    PLAN_STATUS_IN_PROGRESS,
    PLAN_STATUS_COMPLETED,
)

MAX_PLAN_ITEMS = 32
MAX_STEP_CHARS = 200


class PlanValidationError(ValueError):
    """Raised when an inbound `update_plan` payload fails validation."""


class UpdatePlanTool:
    """Pure helper for parsing/applying `update_plan` calls.

    The class is intentionally stateless so it can be reused by the dispatch
    handler, by unit tests, and by any future caller that wants to write a
    plan without going through the tool dispatcher.
    """

    @staticmethod
    def _coerce_step_text(raw: Any) -> str:
        if raw is None:
            return ""
        text = str(raw).strip()
        if not text:
            return ""
        text = " ".join(text.split())
        if len(text) > MAX_STEP_CHARS:
            text = text[:MAX_STEP_CHARS].rstrip()
        return text

    @staticmethod
    def _coerce_status(raw: Any) -> str:
        text = str(raw or "").strip().lower()
        if text not in PLAN_STATUSES:
            raise PlanValidationError(
                f"plan step status must be one of {PLAN_STATUSES}, got '{raw}'"
            )
        return text

    @classmethod
    def validate_plan_items(cls, raw_plan: Any) -> List[Dict[str, str]]:
        if not isinstance(raw_plan, list):
            raise PlanValidationError("plan must be a list of {step, status} items")
        if not raw_plan:
            raise PlanValidationError("plan must contain at least one step")
        if len(raw_plan) > MAX_PLAN_ITEMS:
            raise PlanValidationError(
                f"plan has too many steps (max {MAX_PLAN_ITEMS})"
            )
        items: List[Dict[str, str]] = []
        for index, entry in enumerate(raw_plan):
            if not isinstance(entry, dict):
                raise PlanValidationError(
                    f"plan[{index}] must be an object with `step` and `status`"
                )
            step = cls._coerce_step_text(entry.get("step"))
            if not step:
                raise PlanValidationError(f"plan[{index}].step must be non-empty")
            status = cls._coerce_status(entry.get("status"))
            items.append({"step": step, "status": status})
        in_progress_count = sum(1 for it in items if it["status"] == PLAN_STATUS_IN_PROGRESS)
        if in_progress_count > 1:
            raise PlanValidationError(
                "plan must have at most one step with status 'in_progress'"
            )
        return items

    @classmethod
    def parse_args(cls, params: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(params, dict):
            raise PlanValidationError("update_plan arguments must be an object")
        items = cls.validate_plan_items(params.get("plan"))
        explanation = str(params.get("explanation") or "").strip()
        return {"plan": items, "explanation": explanation}

    @classmethod
    def apply(cls, agent: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        try:
            parsed = cls.parse_args(params)
        except PlanValidationError as exc:
            return {"success": False, "error": str(exc)}

        manager = getattr(agent, "_chat_state_manager", None)
        if manager is None or not hasattr(manager, "persist_active_chat_plan"):
            return {
                "success": False,
                "error": "chat state manager does not support plans",
            }

        try:
            persisted = manager.persist_active_chat_plan(
                parsed["plan"],
                explanation=parsed["explanation"],
            )
        except Exception as exc:  # noqa: BLE001 - surface as tool error
            return {"success": False, "error": f"failed to persist plan: {exc}"}

        if not persisted:
            return {
                "success": False,
                "error": "no active chat to attach the plan to",
            }

        in_progress = next(
            (it["step"] for it in parsed["plan"] if it["status"] == PLAN_STATUS_IN_PROGRESS),
            "",
        )
        return {
            "success": True,
            "plan": parsed["plan"],
            "in_progress_step": in_progress,
            "message": "Plan updated.",
        }

    @classmethod
    def current_plan(cls, agent: Any) -> Optional[Dict[str, Any]]:
        manager = getattr(agent, "_chat_state_manager", None)
        if manager is None or not hasattr(manager, "active_chat_plan"):
            return None
        try:
            return manager.active_chat_plan()
        except Exception:
            return None
