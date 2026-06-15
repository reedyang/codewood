"""Run a configured sub-agent in an isolated nested agentic loop.

The sub-agent uses its own model (optional selector), its own system
instructions (the markdown body), and a configurable tool allowlist. Its
message history is fully isolated: it never touches ``agent.conversation_history``
or ``agent.operation_results``. The final text answer is returned to the caller,
which feeds it back into the main loop's context.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..ai.ai_orchestrator import AIOrchestrator, AgentAIContext
from ..ai.ai_provider_clients import AICallContext, resolve_api_mode
from ..core.config.subagents_loader import SubAgentRecord
from ..core.localization import get_display_language, translate

# Default core tool set granted to a sub-agent when its frontmatter omits
# ``tools``. ``run_subagent`` is always excluded to prevent recursion.
DEFAULT_SUBAGENT_TOOLS = (
    "shell",
    "apply_patch",
    "read_image",
    "project_context_search",
    "update_plan",
    "request_skill_prompt",
)

_EXCLUDED_SUBAGENT_TOOLS = {"run_subagent"}


def _t(agent: Any, key: str, **kwargs: object) -> str:
    return translate(key, get_display_language(agent), **kwargs)


def _find_subagent(agent: Any, name: str) -> Optional[SubAgentRecord]:
    needle = str(name or "").strip().lower()
    if not needle:
        return None
    for rec in list(getattr(agent, "subagents", []) or []):
        if str(getattr(rec, "name", "") or "").strip().lower() == needle:
            return rec
    return None


def _build_orchestrator(agent: Any, record: SubAgentRecord) -> AIOrchestrator:
    """Build a throwaway orchestrator for the sub-agent.

    When the sub-agent declares a ``model`` selector, resolve it from the
    configured catalog (without mutating the main agent). Otherwise reuse the
    main agent's current model.
    """
    provider = str(getattr(agent, "provider", "") or "")
    model_name = str(getattr(agent, "model_name", "") or "")
    params = dict(getattr(agent, "params", {}) or {})

    selector = str(getattr(record, "model_selector", "") or "").strip()
    if selector:
        choice = agent._find_configured_model_choice(selector)
        if choice:
            provider = str(choice.get("provider") or "").strip() or provider
            model_name = str(choice.get("name") or "").strip() or model_name
            params = dict(choice.get("params") or {})
            if model_name:
                params["model"] = model_name

    api_mode = resolve_api_mode(params=params, provider=provider)
    openai_conf = None if api_mode == "ollama" else params

    def _noop_history_writer(role: str, content: str) -> None:  # isolated: no history
        return None

    def _unused_message_builder(user_input: str, context: str):  # never used (messages_override)
        return [], False

    return AIOrchestrator(
        AgentAIContext(
            provider=provider,
            model_name=model_name,
            model_params=params,
            openai_conf=openai_conf,
            work_directory=str(getattr(agent, "work_directory", "")),
            history_writer=_noop_history_writer,
            regular_message_builder=_unused_message_builder,
            ollama_importer=lambda: None,
            display_language=get_display_language(agent),
        )
    )


def _resolve_allowed_tool_schemas(agent: Any, record: SubAgentRecord) -> List[Dict[str, Any]]:
    allowlist = [str(x).strip() for x in (getattr(record, "tools", []) or []) if str(x).strip()]
    if not getattr(record, "tools_specified", False):
        # Frontmatter omitted ``tools`` entirely -> grant the default core set.
        allowlist = list(DEFAULT_SUBAGENT_TOOLS)
    # When ``tools_specified`` is True we honor ``allowlist`` exactly, so an
    # explicit empty ``tools: []`` yields no tools at all.
    allowed = {name.lower() for name in allowlist} - {x.lower() for x in _EXCLUDED_SUBAGENT_TOOLS}

    out: List[Dict[str, Any]] = []
    for spec in (getattr(agent, "tool_specs", []) or []):
        fn = (spec or {}).get("function", {})
        name = str(fn.get("name") or "").strip()
        if not name or name.lower() in _EXCLUDED_SUBAGENT_TOOLS:
            continue
        if name.lower() in allowed:
            out.append(spec)
    return out


_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff", ".tif"}


def _resolve_image_path(agent: Any, image: str) -> Optional[str]:
    """Resolve an image path the same way ``action_read_image`` does.

    Returns the resolved absolute path string, or ``None`` if it does not
    resolve to an existing supported image file.
    """
    raw = str(image or "").strip()
    if not raw:
        return None
    candidate = Path(raw)
    if not candidate.is_absolute():
        search_roots = [
            getattr(agent, "work_directory", None),
            getattr(agent, "ai_workspace_temp_dir", None),
            getattr(agent, "workspace_config_dir", None),
        ]
        resolved: Optional[Path] = None
        for root in search_roots:
            if root is None:
                continue
            probe = Path(root) / raw
            if probe.is_file():
                resolved = probe
                break
        if resolved is None:
            # Fall back to work_directory join so the caller gets a clear error.
            base = getattr(agent, "work_directory", None)
            candidate = (Path(base) / raw) if base is not None else candidate
        else:
            candidate = resolved
    if not candidate.is_file():
        return None
    if candidate.suffix.lower() not in _IMAGE_EXTS:
        return None
    return str(candidate)


def _extract_tool_call_id(message: Dict[str, Any], index: int) -> str:
    try:
        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, list) and index < len(tool_calls):
            entry = tool_calls[index]
            if isinstance(entry, dict):
                cid = str(entry.get("id") or "").strip()
                if cid:
                    return cid
    except Exception:
        pass
    return f"call_{index}"


def run_subagent(
    agent: Any,
    subagent_name: str,
    prompt: str,
    image: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute a sub-agent and return ``{success, output}`` (or error).

    When ``image`` is provided, it is attached to the sub-agent's own model
    calls so a multimodal sub-agent can analyze it directly (independent of the
    main agent's model).
    """
    # Recursion guard: forbid nesting.
    if int(getattr(agent, "_subagent_depth", 0) or 0) > 0:
        return {
            "success": False,
            "error": _t(agent, "subagents.error.nesting_forbidden"),
        }

    record = _find_subagent(agent, subagent_name)
    if record is None:
        available = ", ".join(
            sorted(str(getattr(r, "name", "")) for r in (getattr(agent, "subagents", []) or []))
        ) or "-"
        return {
            "success": False,
            "error": _t(agent, "subagents.error.not_found", name=subagent_name, available=available),
        }

    prompt_text = str(prompt or "").strip()
    if not prompt_text:
        return {"success": False, "error": _t(agent, "subagents.error.empty_prompt")}

    image_path: Optional[str] = None
    if image:
        image_path = _resolve_image_path(agent, image)
        if image_path is None:
            return {
                "success": False,
                "error": _t(agent, "subagents.error.image_not_found", path=str(image)),
                "subagent": record.name,
            }

    # Import here to avoid a circular import at module load time.
    from ..runtime.runtime_loop import _parse_tool_plans_from_model_message

    orchestrator = _build_orchestrator(agent, record)
    tool_schemas = _resolve_allowed_tool_schemas(agent, record)

    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": str(record.instructions or "")},
        {"role": "user", "content": prompt_text},
    ]

    max_rounds = int(getattr(record, "max_rounds", 20) or 20)
    last_assistant_text = ""

    agent._subagent_depth = int(getattr(agent, "_subagent_depth", 0) or 0) + 1
    try:
        for _round in range(max_rounds):
            call_ctx = AICallContext(
                user_input="",
                messages_override=list(messages),
                record_history_override=False,
                return_message=True,
                stream=False,
                tool_schemas=tool_schemas or None,
                tool_choice="auto" if tool_schemas else None,
                # Re-attach the image on every round: ``prepare_image_input``
                # injects it into the request payload only, never into the
                # stored ``messages``, so it must be supplied each call to stay
                # visible to the sub-agent's multimodal model.
                image_path=image_path,
            )
            message = orchestrator.call(call_ctx=call_ctx)

            if isinstance(message, str):
                # Provider returned an error string (no message dict).
                return {"success": False, "error": message, "subagent": record.name}
            if not isinstance(message, dict):
                return {
                    "success": False,
                    "error": _t(agent, "subagents.error.bad_response"),
                    "subagent": record.name,
                }

            content_text = str(message.get("content") or "").strip()
            if content_text:
                last_assistant_text = content_text

            plans = _parse_tool_plans_from_model_message(message)
            if not plans:
                # No tool calls -> final answer.
                return {
                    "success": True,
                    "output": content_text or last_assistant_text,
                    "subagent": record.name,
                }

            # Record the assistant turn (with its tool_calls) so the follow-up
            # tool messages are valid in the next request.
            messages.append(dict(message))

            for idx, (tool_name, args) in enumerate(plans):
                call_id = _extract_tool_call_id(message, idx)
                if str(tool_name).strip().lower() in _EXCLUDED_SUBAGENT_TOOLS:
                    tool_result: Dict[str, Any] = {
                        "success": False,
                        "error": _t(agent, "subagents.error.nesting_forbidden"),
                    }
                else:
                    try:
                        tool_result = agent.execute_tool_call(tool_name, args if isinstance(args, dict) else {})
                    except Exception as e:  # never let a tool crash the sub-agent loop
                        tool_result = {"success": False, "error": str(e)}
                try:
                    result_text = json.dumps(tool_result, ensure_ascii=False)
                except Exception:
                    result_text = str(tool_result)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "name": str(tool_name),
                        "content": result_text,
                    }
                )

        # max_rounds exhausted: return the last text we have.
        return {
            "success": True,
            "output": last_assistant_text or _t(agent, "subagents.error.max_rounds", rounds=max_rounds),
            "subagent": record.name,
            "max_rounds_reached": True,
        }
    finally:
        agent._subagent_depth = max(0, int(getattr(agent, "_subagent_depth", 1) or 1) - 1)
