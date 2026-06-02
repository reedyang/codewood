import json
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from ..config.app_info import get_app_config_dirname, get_app_logger_root, get_app_runtime_attr_name
from ..core.config.model_providers import (
    DEFAULT_CONTEXT_WINDOW,
    SIMPLE_CHAT_SYSTEM_PROMPT_MIN_CONTEXT_WINDOW,
    parse_context_window,
)
from ..core.console_utils import _ansi_gray
from ..core.logging.app_logging import get_logger

MEMORY_RETRIEVAL_ROUNDS = 3
MEMORY_RETRIEVAL_MSG_MAX_CHARS = 400
MEMORY_RETRIEVAL_QUERY_MAX_CHARS = 2000
MEMORY_FALLBACK_MIN_RAW_SCORE = 4.0
MEMORY_EXPANSION_MAX_KEYWORD_CHARS = 600
MEMORY_IDENTITY_CLUSTER_TYPES = frozenset({"preference", "identity"})
SESSION_SUMMARY_ROLLING_MAX_CHARS = 600
SESSION_SUMMARY_MSG_SNIPPET = 120
SESSION_SUMMARY_LLM_INTERVAL_PAIRS = 6
SESSION_SUMMARY_LLM_MAX_CHARS = 1200
SESSION_SUMMARY_LLM_HISTORY_MSGS = 16
CHAT_RECENT_MESSAGES = 10
CONTEXT_OUTPUT_RESERVE_RATIO = 0.20
CONTEXT_OUTPUT_RESERVE_MIN = 512
CONTEXT_OUTPUT_RESERVE_MAX = 8192
CONTEXT_SAFETY_MARGIN_RATIO = 0.10
CONTEXT_SAFETY_MARGIN_MIN = 256
SYSTEM_BUCKET_RATIO = 0.45
HISTORY_BUCKET_RATIO = 0.35
OP_CONTEXT_BUCKET_RATIO = 0.12

SMALL_CTX_MAX = 16_000
MEDIUM_CTX_MAX = 64_000
AGGRESSIVE_COMPRESS_TRIGGER_PCT = 80
AGGRESSIVE_COMPRESS_TARGET_PCT = 20
AUTO_COMPACT_TRIGGER_PCT = 60
AUTO_COMPACT_TAIL_WINDOW_RATIO = 0.05
CONTEXT_COMPACTION_SUMMARY_PREFIX = "[CONTEXT_COMPACTION_SUMMARY]"
CONTEXT_COMPACTION_NOTICE_PREFIX = "[CONTEXT_COMPACTION_NOTICE]"


class SessionMemoryService:
    def __init__(self, agent: Any) -> None:
        self.agent = agent
        self._builtin_token_counter: Optional[Callable[[str], int]] = None
        self._builtin_token_counter_init_done = False
        self._token_counter_warmup_started = False
        self._token_counter_lock = threading.Lock()
        self._context_usage_refresh_lock = threading.Lock()
        self._context_usage_refresh_inflight = False
        self._context_usage_refresh_pending: Optional[Dict[str, str]] = None
        self._context_compaction_lock = threading.Lock()
        self._start_token_counter_warmup()

    def _model_visible_path_text(self, raw_path: Any) -> str:
        fallback = "(hidden internal runtime directory)"
        if not raw_path:
            return fallback
        try:
            resolved = Path(raw_path).resolve()
        except Exception:
            return fallback
        repo_root = getattr(self.agent, "_self_repo_root", None)
        if repo_root:
            try:
                if resolved.relative_to(Path(repo_root).resolve()) is not None:
                    return fallback
            except Exception:
                pass
        return str(resolved)

    def _model_visible_workspace_directory_text(self) -> str:
        workspace_root = getattr(self.agent, "workspace_root", None)
        if workspace_root:
            visible = self._model_visible_path_text(workspace_root)
            if visible != "(hidden internal runtime directory)":
                return visible
        workspace_config_dir = getattr(self.agent, "workspace_config_dir", None)
        if workspace_config_dir:
            visible = self._model_visible_path_text(workspace_config_dir)
            if visible != "(hidden internal runtime directory)":
                return visible
        return "(hidden internal runtime directory)"

    def _context_usage_state_key(self) -> str:
        chat_id = str(getattr(self.agent, "active_chat_id", "") or "").strip()
        task_id = str(getattr(self.agent, "_active_runtime_task_id", "") or "").strip()
        hist = list(getattr(self.agent, "conversation_history", None) or [])
        size = len(hist)
        last_role = ""
        last_content = ""
        last_task_id = ""
        if hist:
            try:
                last = hist[-1] if isinstance(hist[-1], dict) else {}
            except Exception:
                last = {}
            if isinstance(last, dict):
                last_role = str(last.get("role") or "").strip().lower()
                last_content = str(last.get("content") or "")
                if len(last_content) > 120:
                    last_content = last_content[-120:]
                last_task_id = str(last.get("task_id") or "").strip()
        return f"{chat_id}|{task_id}|{size}|{last_role}|{last_task_id}|{last_content}"

    def _start_token_counter_warmup(self) -> None:
        with self._token_counter_lock:
            if self._token_counter_warmup_started:
                return
            self._token_counter_warmup_started = True

        def _run() -> None:
            try:
                import tiktoken  # type: ignore

                enc = tiktoken.get_encoding("cl100k_base")
                self._builtin_token_counter = lambda s: len(enc.encode(str(s or "")))
            except Exception:
                self._builtin_token_counter = None
            finally:
                self._builtin_token_counter_init_done = True

        threading.Thread(
            target=_run,
            daemon=True,
            name=f"{get_app_logger_root()}-token-counter-warmup",
        ).start()

    def append_chat_message(self, role: str, content: str) -> None:
        r = str(role or "").strip().lower()
        if r not in ("user", "assistant"):
            return
        self.agent.conversation_history.append(
            {
                "role": r,
                "content": str(content or ""),
                "task_id": str(getattr(self.agent, "_active_runtime_task_id", "") or "").strip(),
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
        self.agent._sync_active_chat_messages()
        if r == "user":
            self.agent._maybe_schedule_auto_chat_name()

    def build_context_compaction_summary_content(
        self,
        *,
        summary: str,
        mode: str,
        covered_message_count: int,
    ) -> str:
        payload = {
            "kind": "context_compaction_summary",
            "summary": str(summary or "").strip(),
            "mode": str(mode or "").strip().lower() or "manual",
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "covered_message_count": max(0, int(covered_message_count or 0)),
        }
        return CONTEXT_COMPACTION_SUMMARY_PREFIX + json.dumps(payload, ensure_ascii=False)

    def parse_context_compaction_summary_content(self, content: str) -> Optional[Dict[str, Any]]:
        text = str(content or "")
        if not text.startswith(CONTEXT_COMPACTION_SUMMARY_PREFIX):
            return None
        body = text[len(CONTEXT_COMPACTION_SUMMARY_PREFIX):].strip()
        if not body:
            return None
        try:
            payload = json.loads(body)
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        if str(payload.get("kind") or "").strip() != "context_compaction_summary":
            return None
        return payload

    def is_context_compaction_summary_message(self, msg: Any) -> bool:
        if not isinstance(msg, dict):
            return False
        if str(msg.get("role") or "").strip().lower() != "assistant":
            return False
        return isinstance(
            self.parse_context_compaction_summary_content(str(msg.get("content") or "")),
            dict,
        )

    def build_context_compaction_notice_content(self, message: str = "Context automatically compacted") -> str:
        payload = {
            "kind": "context_compaction_notice",
            "message": str(message or "Context automatically compacted").strip(),
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        return CONTEXT_COMPACTION_NOTICE_PREFIX + json.dumps(payload, ensure_ascii=False)

    def parse_context_compaction_notice_content(self, content: str) -> Optional[Dict[str, Any]]:
        text = str(content or "")
        if not text.startswith(CONTEXT_COMPACTION_NOTICE_PREFIX):
            return None
        body = text[len(CONTEXT_COMPACTION_NOTICE_PREFIX):].strip()
        if not body:
            return None
        try:
            payload = json.loads(body)
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        if str(payload.get("kind") or "").strip() != "context_compaction_notice":
            return None
        return payload

    def is_context_compaction_notice_message(self, msg: Any) -> bool:
        if not isinstance(msg, dict):
            return False
        if str(msg.get("role") or "").strip().lower() != "assistant":
            return False
        return isinstance(
            self.parse_context_compaction_notice_content(str(msg.get("content") or "")),
            dict,
        )

    def _context_compaction_summary_for_model(self, content: str) -> str:
        payload = self.parse_context_compaction_summary_content(content)
        if not isinstance(payload, dict):
            return ""
        summary = str(payload.get("summary") or "").strip()
        if not summary:
            return ""
        created_at = str(payload.get("created_at") or "").strip()
        mode = str(payload.get("mode") or "").strip()
        header = "【上下文摘要】"
        meta: List[str] = []
        if mode:
            meta.append(f"mode={mode}")
        if created_at:
            meta.append(f"created_at={created_at}")
        if meta:
            header += " " + "; ".join(meta)
        return header + "\n" + summary

    def _is_excluded_user_message_for_model_context(self, msg: Dict[str, Any]) -> bool:
        if not isinstance(msg, dict):
            return False
        role = str(msg.get("role") or "").strip().lower()
        if role != "user":
            return False
        return bool(msg.get("exclude_from_model_context", False))

    def _is_internal_assistant_history_message(self, content: str) -> bool:
        raw = str(content or "")
        if not raw:
            return False
        if self.parse_context_compaction_notice_content(raw) is not None:
            return True
        if self.parse_context_compaction_summary_content(raw) is not None:
            return True
        parse_slash_result = getattr(self.agent, "_parse_internal_slash_result_history_content", None)
        if callable(parse_slash_result):
            try:
                if isinstance(parse_slash_result(raw), dict):
                    return True
            except Exception:
                pass
        parse_worked_summary = getattr(self.agent, "_parse_task_worked_summary_history_content", None)
        if callable(parse_worked_summary):
            try:
                if isinstance(parse_worked_summary(raw), dict):
                    return True
            except Exception:
                pass
        parse_direct_result = getattr(self.agent, "_parse_direct_shell_result_history_content", None)
        if callable(parse_direct_result):
            try:
                if isinstance(parse_direct_result(raw), dict):
                    return True
            except Exception:
                pass
        parse_interrupted = getattr(self.agent, "_parse_conversation_interrupted_history_content", None)
        if callable(parse_interrupted):
            try:
                if isinstance(parse_interrupted(raw), dict):
                    return True
            except Exception:
                pass
        return False

    def mark_cancelled_task_unanswered_user_messages(self, task_id: str) -> int:
        """
        Mark user messages in a cancelled task that have no later assistant reply in the same task,
        so they will not enter model context or token budgeting in following rounds.
        """
        tid = str(task_id or "").strip()
        if not tid:
            return 0
        hist = list(getattr(self.agent, "conversation_history", None) or [])
        if not hist:
            return 0
        marked_indexes: List[int] = []
        seen_assistant_after = False
        for idx in range(len(hist) - 1, -1, -1):
            msg = hist[idx]
            if not isinstance(msg, dict):
                continue
            if str(msg.get("task_id") or "").strip() != tid:
                continue
            role = str(msg.get("role") or "").strip().lower()
            if role == "assistant":
                if self._is_internal_assistant_history_message(str(msg.get("content") or "")):
                    continue
                seen_assistant_after = True
                continue
            if role != "user":
                continue
            if self._is_builtin_slash_user_message(role, str(msg.get("content") or "")):
                continue
            if seen_assistant_after:
                continue
            marked_indexes.append(idx)
        if not marked_indexes:
            fallback_idx = self._latest_unanswered_user_message_index_global()
            if fallback_idx >= 0:
                marked_indexes.append(fallback_idx)
            else:
                return 0
        marked_count = 0
        for idx in marked_indexes:
            try:
                msg_obj = self.agent.conversation_history[idx]
            except Exception:
                continue
            if not isinstance(msg_obj, dict):
                continue
            if bool(msg_obj.get("exclude_from_model_context", False)):
                continue
            msg_obj["exclude_from_model_context"] = True
            marked_count += 1
        if marked_count > 0:
            try:
                self.agent._sync_active_chat_messages()
            except Exception:
                pass
        return marked_count

    def mark_latest_unanswered_user_message_for_cancel(self) -> int:
        """
        Fallback marker used when task-id-based marking cannot be applied.
        Marks the latest unanswered user message globally.
        """
        idx = self._latest_unanswered_user_message_index_global()
        if idx < 0:
            return 0
        try:
            msg_obj = self.agent.conversation_history[idx]
        except Exception:
            return 0
        if not isinstance(msg_obj, dict):
            return 0
        if str(msg_obj.get("role") or "").strip().lower() != "user":
            return 0
        if bool(msg_obj.get("exclude_from_model_context", False)):
            return 0
        msg_obj["exclude_from_model_context"] = True
        try:
            self.agent._sync_active_chat_messages()
        except Exception:
            pass
        return 1

    def _latest_unanswered_user_message_index_global(self) -> int:
        """
        Fallback selector: find the latest user message that has no later *real* assistant
        reply in history, ignoring slash/internal bookkeeping entries.
        """
        hist = list(getattr(self.agent, "conversation_history", None) or [])
        if not hist:
            return -1
        seen_assistant_after = False
        for idx in range(len(hist) - 1, -1, -1):
            msg = hist[idx]
            if not isinstance(msg, dict):
                continue
            role = str(msg.get("role") or "").strip().lower()
            if role == "assistant":
                if self._is_internal_assistant_history_message(str(msg.get("content") or "")):
                    continue
                seen_assistant_after = True
                continue
            if role != "user":
                continue
            if self._is_excluded_user_message_for_model_context(msg):
                continue
            raw = str(msg.get("content") or "")
            if self._is_builtin_slash_user_message(role, raw):
                continue
            if seen_assistant_after:
                continue
            return idx
        return -1

    def _is_builtin_slash_user_message(self, role: str, content: str) -> bool:
        norm_role = str(role or "").strip().lower()
        if norm_role != "user":
            return False
        raw = str(content or "")
        parse_slash_user = getattr(self.agent, "_parse_internal_slash_user_history_content", None)
        cmd = ""
        if callable(parse_slash_user):
            try:
                cmd = str(parse_slash_user(raw) or "").strip()
            except Exception:
                cmd = ""
        text = cmd if cmd else raw
        return str(text or "").strip().startswith("/")

    def _context_eligible_history(self) -> List[Dict[str, Any]]:
        hist = list(getattr(self.agent, "conversation_history", None) or [])

        def _context_eligible_messages(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            out: List[Dict[str, Any]] = []
            for item in rows:
                if not isinstance(item, dict):
                    continue
                role = str(item.get("role") or "").strip().lower()
                if role not in ("user", "assistant"):
                    continue
                if role == "user" and self._is_excluded_user_message_for_model_context(item):
                    continue
                if self._is_builtin_slash_user_message(role, str(item.get("content") or "")):
                    continue
                out.append(item)
            return out

        try:
            chat = self.agent._find_chat_by_id(getattr(self.agent, "active_chat_id", ""))
        except Exception:
            chat = None
        if not isinstance(chat, dict):
            return _context_eligible_messages(hist)
        chat_messages = chat.get("messages")
        if not isinstance(chat_messages, list) or not chat_messages:
            return _context_eligible_messages(hist)
        return _context_eligible_messages(chat_messages)

    def _context_eligible_history_with_indices(self) -> List[Tuple[int, Dict[str, Any]]]:
        hist = list(getattr(self.agent, "conversation_history", None) or [])
        out: List[Tuple[int, Dict[str, Any]]] = []
        for idx, item in enumerate(hist):
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "").strip().lower()
            if role not in ("user", "assistant"):
                continue
            if role == "user" and self._is_excluded_user_message_for_model_context(item):
                continue
            if self._is_builtin_slash_user_message(role, str(item.get("content") or "")):
                continue
            out.append((idx, item))
        return out

    def latest_compaction_summary_index(self, history: Optional[List[Dict[str, Any]]] = None) -> int:
        rows = list(history if history is not None else self._context_eligible_history())
        for idx in range(len(rows) - 1, -1, -1):
            if self.is_context_compaction_summary_message(rows[idx]):
                return idx
        return -1

    def history_for_regular_context(self) -> List[Dict[str, Any]]:
        rows = self._context_eligible_history()
        idx = self.latest_compaction_summary_index(rows)
        if idx >= 0:
            return rows[idx:]
        return rows

    def _history_with_indices_for_regular_context(self) -> List[Tuple[int, Dict[str, Any]]]:
        rows = self._context_eligible_history_with_indices()
        for idx in range(len(rows) - 1, -1, -1):
            if self.is_context_compaction_summary_message(rows[idx][1]):
                return rows[idx:]
        return rows

    def _normalize_history_content_for_model(self, role: str, content: str) -> str:
        text = str(content or "")
        norm_role = str(role or "").strip().lower()
        if not text:
            return ""
        if norm_role == "assistant":
            compact_summary = self._context_compaction_summary_for_model(text)
            if compact_summary:
                return compact_summary
        parse_user_cmd = getattr(self.agent, "_parse_direct_shell_user_history_content", None)
        parse_direct_result = getattr(self.agent, "_parse_direct_shell_result_history_content", None)
        is_direct_aborted = getattr(self.agent, "_is_direct_shell_result_aborted", None)
        normalize_aborted = getattr(self.agent, "_normalize_aborted_direct_shell_stdout_for_history", None)
        parse_interrupted = getattr(self.agent, "_parse_conversation_interrupted_history_content", None)

        if norm_role == "user" and callable(parse_user_cmd):
            try:
                cmd = str(parse_user_cmd(text) or "").strip()
            except Exception:
                cmd = ""
            if cmd:
                return f"[用户直接执行命令] !{cmd}"

        if norm_role == "assistant" and callable(parse_direct_result):
            try:
                payload = parse_direct_result(text)
            except Exception:
                payload = None
            if isinstance(payload, dict):
                executed = str(payload.get("executed_command") or "").strip()
                rc_raw = payload.get("return_code")
                try:
                    rc_num = int(rc_raw)
                    rc_text = str(rc_num)
                except Exception:
                    rc_num = None
                    rc_text = str(rc_raw)
                out = str(payload.get("stdout") or "")
                err = str(payload.get("stderr") or "")
                merged = out + err
                aborted = False
                if callable(is_direct_aborted):
                    try:
                        aborted = bool(is_direct_aborted(payload))
                    except Exception:
                        aborted = False
                if aborted and callable(normalize_aborted):
                    try:
                        merged = str(normalize_aborted(merged) or "")
                    except Exception:
                        merged = out + err
                success = False
                if not aborted:
                    if rc_num is None:
                        success = False
                    else:
                        success = rc_num == 0
                status = "interrupted_by_user=true" if aborted else "interrupted_by_user=false"
                success_text = "true" if success else "false"
                header = (
                    "[命令执行结果] "
                    f"command={executed or '<empty>'}; return_code={rc_text}; "
                    f"executed_success={success_text}; {status}"
                )
                body = merged.strip("\r\n")
                return f"{header}\n{body}" if body else header

        if norm_role == "assistant" and callable(parse_interrupted):
            try:
                payload2 = parse_interrupted(text)
            except Exception:
                payload2 = None
            if isinstance(payload2, dict):
                interrupted_kind = str(payload2.get("interrupted_kind") or "task").strip()
                reason = str(payload2.get("reason") or "user_interrupt").strip()
                detail = str(payload2.get("detail") or "").strip()
                msg = (
                    f"[会话中断事件] kind={interrupted_kind}; reason={reason}; "
                    "任务被用户中断，除非用户明确要求继续，否则不要自动续跑。"
                )
                if detail:
                    msg += f"\n被中断的任务: {detail}"
                return msg

        return text

    def _is_internal_slash_history_message(self, role: str, content: str) -> bool:
        norm_role = str(role or "").strip().lower()
        text = str(content or "")
        if norm_role == "user":
            parse_slash_user = getattr(self.agent, "_parse_internal_slash_user_history_content", None)
            if callable(parse_slash_user):
                try:
                    return bool(str(parse_slash_user(text) or "").strip())
                except Exception:
                    return False
            return False
        if norm_role == "assistant":
            if self.parse_context_compaction_notice_content(text) is not None:
                return True
            if self.parse_context_compaction_summary_content(text) is not None:
                return True
            parse_slash_result = getattr(self.agent, "_parse_internal_slash_result_history_content", None)
            if callable(parse_slash_result):
                try:
                    payload = parse_slash_result(text)
                except Exception:
                    payload = None
                if isinstance(payload, dict):
                    return True
            parse_model_tool_result = getattr(self.agent, "_parse_model_tool_result_history_content", None)
            if callable(parse_model_tool_result):
                try:
                    model_payload = parse_model_tool_result(text)
                except Exception:
                    model_payload = None
                if isinstance(model_payload, dict):
                    return True
            parse_worked_summary = getattr(self.agent, "_parse_task_worked_summary_history_content", None)
            if callable(parse_worked_summary):
                try:
                    worked_payload = parse_worked_summary(text)
                except Exception:
                    worked_payload = None
                if isinstance(worked_payload, dict):
                    return True
            return False
        return False

    def _latest_interruption_context_line(
        self, source_history: Optional[List[Dict[str, Any]]] = None
    ) -> str:
        rows = list(source_history if source_history is not None else self._context_eligible_history())
        parse_direct_result = getattr(self.agent, "_parse_direct_shell_result_history_content", None)
        is_direct_aborted = getattr(self.agent, "_is_direct_shell_result_aborted", None)
        parse_interrupted = getattr(self.agent, "_parse_conversation_interrupted_history_content", None)
        for msg in reversed(rows):
            role = str(msg.get("role") or "").strip().lower()
            if role != "assistant":
                continue
            content = str(msg.get("content") or "")
            if callable(parse_interrupted):
                try:
                    evt = parse_interrupted(content)
                except Exception:
                    evt = None
                if isinstance(evt, dict):
                    detail = str(evt.get("detail") or "").strip()
                    line = "最近一次任务执行被用户中断（ESC）。除非用户明确要求，禁止自动续跑被中断任务。"
                    if detail:
                        line += f" 被中断任务: {detail}"
                    return line
            if callable(parse_direct_result):
                try:
                    dr = parse_direct_result(content)
                except Exception:
                    dr = None
                if isinstance(dr, dict):
                    aborted = False
                    if callable(is_direct_aborted):
                        try:
                            aborted = bool(is_direct_aborted(dr))
                        except Exception:
                            aborted = False
                    if aborted:
                        cmd = str(dr.get("executed_command") or "").strip()
                        rc = dr.get("return_code")
                        return (
                            "最近一次直接命令执行被用户强制终止；"
                            f"command={cmd or '<empty>'}; return_code={rc}。"
                            "不要将该命令视为已完整成功执行。"
                        )
        return ""

    def update_session_summary_rolling(self) -> None:
        hist = list(getattr(self.agent, "conversation_history", None) or [])
        chunks: List[str] = []
        snip = SESSION_SUMMARY_MSG_SNIPPET
        for msg in hist[-8:]:
            role = (msg.get("role") or "").strip().lower()
            if role not in ("user", "assistant"):
                continue
            c_raw = str(msg.get("content") or "")
            if self._is_internal_slash_history_message(role, c_raw):
                continue
            c = c_raw.replace("\n", " ").strip()
            if len(c) > snip:
                c = c[: max(1, snip - 1)] + "…"
            tag = "U" if role == "user" else "A"
            if c:
                chunks.append(f"{tag}:{c}")
        s = " | ".join(chunks)
        maxc = SESSION_SUMMARY_ROLLING_MAX_CHARS
        if len(s) > maxc:
            s = s[-maxc:]
        self.agent._session_summary_rolling = s

    def session_summary_for_retrieval(self) -> str:
        llm = (self.agent._session_summary_llm or "").strip()
        if llm:
            cap = min(800, SESSION_SUMMARY_LLM_MAX_CHARS)
            return f"[会话摘要]\n{llm[:cap]}"
        roll = (self.agent._session_summary_rolling or "").strip()
        if roll:
            return f"[会话摘录]\n{roll}"
        return ""

    def maybe_refresh_session_summary_llm(self) -> None:
        all_hist = list(getattr(self.agent, "conversation_history", None) or [])
        filtered_hist = [
            m for m in all_hist
            if not self._is_internal_slash_history_message(
                str(m.get("role") or "").strip().lower(),
                str(m.get("content") or ""),
            )
        ]
        pairs = len(filtered_hist) // 2
        if pairs < SESSION_SUMMARY_LLM_INTERVAL_PAIRS:
            return
        if self.agent._last_llm_summary_pair_count > 0:
            if pairs - self.agent._last_llm_summary_pair_count < SESSION_SUMMARY_LLM_INTERVAL_PAIRS:
                return
        hist = filtered_hist[-SESSION_SUMMARY_LLM_HISTORY_MSGS:]
        lines: List[str] = []
        for msg in hist:
            role = (msg.get("role") or "").strip().lower()
            if role not in ("user", "assistant"):
                continue
            tag = "用户" if role == "user" else "助手"
            c = str(msg.get("content") or "")[:500].replace("\n", " ")
            lines.append(f"{tag}: {c}")
        blob = "\n".join(lines)
        if not blob.strip():
            return
        try:
            raw = self.agent.call_ai(
                "以下是本会话近期消息摘录，请按系统指令输出摘要。\n\n" + blob,
                context="",
                stream=False,
                session_summary_mode=True,
            )
        except Exception:
            return
        if not isinstance(raw, str):
            return
        text = raw.strip()
        if text.startswith("❌") or text.startswith("调用大模型"):
            return
        text = text.replace("```", "").strip()
        if not text:
            return
        self.agent._session_summary_llm = text[:SESSION_SUMMARY_LLM_MAX_CHARS]
        self.agent._last_llm_summary_pair_count = pairs

    def build_memory_retrieval_query(self, user_input: str) -> str:
        def _clip(text: str, n: int) -> str:
            t = (text or "").strip()
            if not t:
                return ""
            if len(t) <= n:
                return t
            return t[: max(1, n - 1)] + "…"

        return _clip((user_input or "").strip(), MEMORY_RETRIEVAL_QUERY_MAX_CHARS)

    @staticmethod
    def memory_row_sort_key(r: Dict[str, Any]) -> Tuple[int, float, int]:
        mt = (r.get("memory_type") or "").lower()
        tier = (r.get("tier") or "").lower()
        cluster = 0 if mt in MEMORY_IDENTITY_CLUSTER_TYPES else 1
        tr = 0 if tier == "durable" else (1 if tier == "episodic" else 2)
        try:
            ca = float(r.get("created_at") or 0)
        except (TypeError, ValueError):
            ca = 0.0
        return (cluster, -ca, tr)

    @staticmethod
    def user_input_emphasizes_memory_or_identity(user_input: str) -> bool:
        s = (user_input or "").strip()
        if not s:
            return False
        needles = (
            "检索记忆", "根据记忆", "查记忆", "你的记忆", "经验记忆", "不记得", "给你起过",
            "起的名字", "昵称", "称呼", "我是谁", "你是谁", "我叫什么", "之前给你", "约定过", "还记得吗",
        )
        return any(x in s for x in needles)

    def memory_dialogue_excerpt_for_expansion(self) -> str:
        per_msg = MEMORY_RETRIEVAL_MSG_MAX_CHARS
        rounds = MEMORY_RETRIEVAL_ROUNDS

        def _clip(text: str, n: int) -> str:
            t = (text or "").strip()
            if not t:
                return ""
            if len(t) <= n:
                return t
            return t[: max(1, n - 1)] + "…"

        hist = list(getattr(self.agent, "conversation_history", None) or [])
        want = rounds * 2
        tail = hist[-want:] if len(hist) >= want else hist[:]
        if tail and (tail[-1].get("role") or "") == "user":
            tail = tail[:-1]
        while tail and (tail[0].get("role") or "") != "user":
            tail = tail[1:]
        if len(tail) % 2 == 1:
            tail = tail[1:]
        lines: List[str] = []
        for msg in tail:
            role = (msg.get("role") or "").strip().lower()
            if role not in ("user", "assistant"):
                continue
            raw_content = str(msg.get("content") or "")
            if self._is_internal_slash_history_message(role, raw_content):
                continue
            content = _clip(raw_content, per_msg)
            if not content:
                continue
            tag = "用户" if role == "user" else "助手"
            lines.append(f"[{tag}] {content}")
        return "\n".join(lines).strip()

    def memory_expansion_reference_block(self) -> str:
        pref = self.session_summary_for_retrieval()
        dia = self.memory_dialogue_excerpt_for_expansion()
        parts: List[str] = []
        if pref:
            parts.append(pref)
        if dia:
            parts.append("【近期对话摘录】\n" + dia)
        return "\n\n".join(parts).strip()

    @staticmethod
    def parse_memory_expansion_json(text: str) -> Optional[Dict[str, Any]]:
        raw = (text or "").strip()
        if not raw or raw.startswith("❌") or raw.startswith("调用大模型"):
            return None
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
            raw = re.sub(r"\s*```\s*$", "", raw)
        data = None
        try:
            data = json.loads(raw)
        except Exception:
            start = raw.find("{")
            if start >= 0:
                depth = 0
                for i in range(start, len(raw)):
                    if raw[i] == "{":
                        depth += 1
                    elif raw[i] == "}":
                        depth -= 1
                        if depth == 0:
                            try:
                                data = json.loads(raw[start : i + 1])
                            except Exception:
                                data = None
                            break
        if not isinstance(data, dict):
            return None
        keys = ("keywords", "aliases", "entities", "topics", "preferences_hint")
        out: Dict[str, Any] = {}
        for k in keys:
            v = data.get(k)
            if isinstance(v, list):
                cleaned = [str(x).strip() for x in v if str(x).strip()][:12]
                out[k] = cleaned[:10]
            else:
                out[k] = []
        if not any(out.values()):
            return None
        return out

    def memory_expansion_keywords_query_string(self, expansion: Dict[str, Any]) -> str:
        chunks: List[str] = []
        for k in ("keywords", "aliases", "entities", "topics", "preferences_hint"):
            for s in expansion.get(k) or []:
                s = str(s).strip()[:80]
                if s:
                    chunks.append(s)
        joined = " ".join(chunks).strip()
        if len(joined) > MEMORY_EXPANSION_MAX_KEYWORD_CHARS:
            joined = joined[: MEMORY_EXPANSION_MAX_KEYWORD_CHARS]
        return joined

    def should_run_memory_query_expansion(
        self, rows_sem: List[Dict[str, Any]], rows_boost: List[Dict[str, Any]], identity_mode: bool
    ) -> bool:
        if not getattr(self.agent, "memory_fallback_expansion_enabled", True):
            return False
        if identity_mode and rows_boost:
            return False
        if not rows_sem:
            return True
        scores = [float(r.get("raw_score") or 0) for r in rows_sem]
        if not scores:
            return True
        return max(scores) < MEMORY_FALLBACK_MIN_RAW_SCORE

    def run_memory_expansion_llm(self, user_input: str) -> Optional[Dict[str, Any]]:
        ref = self.memory_expansion_reference_block()
        body = (user_input or "").strip()
        payload = ((ref + "\n\n---\n\n") if ref else "") + "【当前用户提问】（仅用于提取检索用词与同义实体，不要直接回答用户）\n" + body
        try:
            raw = self.agent.call_ai(payload, context="", stream=False, memory_query_expansion_mode=True)
        except Exception:
            get_logger().exception("经验记忆：查询扩展 LLM 调用异常")
            return None
        if not isinstance(raw, str):
            return None
        return self.parse_memory_expansion_json(raw)

    def memory_rows_for_prompt(self, user_input: str) -> List[Dict[str, Any]]:
        raw_ui = (user_input or "").strip()
        q = self.build_memory_retrieval_query(user_input)
        if not q.strip():
            return []
        sk = self.agent._memory_scope_key()

        rows_boost: List[Dict[str, Any]] = []
        identity_mode = self.user_input_emphasizes_memory_or_identity(raw_ui)
        if identity_mode:
            for bq in (
                "用户偏好 昵称 名字 称呼 助手身份 起名 约定",
                "preference nickname identity assistant name 称呼",
            ):
                rows_boost.extend(self.agent.memory_service.search_memories(bq, top_k=5, scope_key=sk))
        seen_b: Set[str] = set()
        boost_uniq: List[Dict[str, Any]] = []
        for r in rows_boost:
            rid = str(r.get("id") or "").strip()
            if rid and rid not in seen_b:
                seen_b.add(rid)
                boost_uniq.append(r)
        rows_boost = boost_uniq

        rows_sem = self.agent.memory_service.search_memories(q, top_k=6, scope_key=sk)

        rows_exp: List[Dict[str, Any]] = []
        _mem_log = get_logger()
        if self.should_run_memory_query_expansion(rows_sem, rows_boost, identity_mode):
            max_raw = max((float(r.get("raw_score") or 0) for r in rows_sem), default=0.0)
            if not rows_sem:
                _mem_log.info("经验记忆：触发查询扩展 fallback（主检索无命中）")
            else:
                _mem_log.info(
                    "经验记忆：触发查询扩展 fallback（主检索偏弱 max_raw=%.2f < %.2f）",
                    max_raw,
                    MEMORY_FALLBACK_MIN_RAW_SCORE,
                )
            exp = self.run_memory_expansion_llm(raw_ui)
            if exp:
                kw = self.memory_expansion_keywords_query_string(exp)
                if kw:
                    q2 = (q.strip() + "\n\n【扩展检索词】\n" + kw).strip()
                    rows_exp = self.agent.memory_service.search_memories(q2, top_k=8, scope_key=sk)
                    _mem_log.info(
                        "经验记忆：查询扩展已执行（扩展词约 %d 字，二次检索 %d 条）",
                        len(kw),
                        len(rows_exp),
                    )
                else:
                    _mem_log.info("经验记忆：查询扩展未产生可用关键词（各槽位为空）")
            else:
                _mem_log.info("经验记忆：查询扩展未生效（模型返回不可解析或调用失败）")

        seen: Set[str] = set()
        merged: List[Dict[str, Any]] = []

        def _add_rows(rs: List[Dict[str, Any]]) -> None:
            for r in rs:
                if len(merged) >= 12:
                    return
                rid = str(r.get("id") or "").strip()
                if not rid or rid in seen:
                    continue
                merged.append(r)
                seen.add(rid)

        _add_rows(rows_boost)
        _add_rows(rows_sem)
        _add_rows(rows_exp)

        def _from_recent_item(item: Dict[str, Any]) -> Dict[str, Any]:
            prev = item.get("preview") or ""
            ca = item.get("created_at")
            try:
                ca_f = float(ca) if ca is not None else 0.0
            except (TypeError, ValueError):
                ca_f = 0.0
            return {
                "id": item.get("id"),
                "title": item.get("title") or "",
                "content": (prev if isinstance(prev, str) else str(prev))[:600],
                "tier": item.get("tier") or "",
                "memory_type": item.get("memory_type") or "",
                "source": item.get("source") or "",
                "system_note": None,
                "created_at": ca_f,
            }

        recent = self.agent.memory_service.list_recent(limit=20, scope_key=sk)
        for item in recent:
            if len(merged) >= 12:
                break
            rid = str(item.get("id") or "").strip()
            if not rid or rid in seen:
                continue
            merged.append(_from_recent_item(item))
            seen.add(rid)

        merged.sort(key=self.memory_row_sort_key)
        return merged[:12]

    def memory_context_for_prompt(self, user_input: str, max_chars: int = 2400) -> str:
        if not self.agent._ensure_memory_service():
            return ""
        try:
            rq = self.build_memory_retrieval_query(user_input)
            if not rq.strip():
                return ""
            rows = self.memory_rows_for_prompt(user_input)
            if not rows:
                return ""
            lines = [
                "【经验记忆（内化教训与偏好；关键事实请仍核实）】",
                "若同主题（如称呼、显示名）出现多条：答复时的「当前口径」以记录时间最新者为准；较早条目为沿革/曾用信息，用户未问及不必展开，问起可如实说明。",
            ]
            total = len("\n".join(lines))
            for r in rows:
                block = f"- ({r.get('tier', '')}) {r.get('title', '')}: {r.get('content', '')[:500]}"
                if r.get("system_note"):
                    block += f" [内省备注: {r['system_note'][:200]}]"
                ca = r.get("created_at")
                if ca is not None:
                    try:
                        ts = float(ca)
                        if ts > 0:
                            block += f" [记录时间: {datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M')}]"
                    except (TypeError, ValueError, OSError, OverflowError):
                        pass
                if total + 1 + len(block) > max_chars:
                    break
                lines.append(block)
                total += 1 + len(block)
                mid = str(r.get("id") or "").strip()
                if mid:
                    try:
                        self.agent.memory_service.touch_memory(mid)
                    except Exception:
                        pass
            return "\n".join(lines) if len(lines) > 1 else ""
        except Exception:
            return ""

    def schedule_auto_memory_reflect(self) -> None:
        if not self.agent._ensure_memory_service():
            return
        now = time.monotonic()
        if now - getattr(self.agent, "_last_memory_reflect_at", 0.0) < 45.0:
            return
        self.agent._last_memory_reflect_at = now

        def _run() -> None:
            try:
                self.run_memory_reflection_body()
            except Exception:
                try:
                    get_logger().exception("自动记忆反思失败")
                except Exception:
                    pass

        threading.Thread(target=_run, daemon=True, name=f"{get_app_logger_root()}-memory-reflect").start()

    def run_memory_reflection_body(self) -> None:
        if not self.agent._ensure_memory_service():
            return
        hist_all = self.agent.conversation_history[-12:] if self.agent.conversation_history else []
        hist: List[Dict[str, Any]] = []
        for msg in hist_all:
            role = str(msg.get("role") or "").strip().lower()
            content = str(msg.get("content") or "")
            if self._is_internal_slash_history_message(role, content):
                continue
            hist.append(msg)
        if len(hist) > 6:
            hist = hist[-6:]
        op_tail = self.agent.operation_results[-4:] if self.agent.operation_results else []
        blob = {"recent_chat": hist, "recent_operations": op_tail}
        payload = json.dumps(blob, ensure_ascii=False)[:12000]
        raw = self.agent.call_ai(payload, context="", stream=False, reflection_mode=True, return_message=False)
        if not isinstance(raw, str) or not raw.strip():
            return
        text = raw.strip()
        data = None
        try:
            data = json.loads(text)
        except Exception:
            start = text.find("{")
            if start >= 0:
                depth = 0
                for i in range(start, len(text)):
                    if text[i] == "{":
                        depth += 1
                    elif text[i] == "}":
                        depth -= 1
                        if depth == 0:
                            try:
                                data = json.loads(text[start : i + 1])
                            except Exception:
                                data = None
                            break
        if not isinstance(data, dict):
            return
        mems = data.get("memories")
        if not isinstance(mems, list):
            return
        sk = self.agent._memory_scope_key()
        for m in mems[:8]:
            if not isinstance(m, dict) or not m.get("must_store"):
                continue
            title = str(m.get("title") or "经验").strip()[:500]
            content = str(m.get("content") or "").strip()
            if not content:
                continue
            tier = str(m.get("tier") or "episodic").strip().lower()
            if tier not in ("working", "episodic", "durable"):
                tier = "episodic"
            mtype = str(m.get("memory_type") or "lesson").strip()[:64]
            sys_note = str(m.get("system_note") or "").strip()[:2000] or None
            try:
                self.agent.memory_service.add_memory(
                    title=title,
                    content=content,
                    tier=tier,
                    memory_type=mtype,
                    scope_key=sk,
                    source="auto",
                    system_note=sys_note,
                )
            except Exception:
                continue

    def _resolve_token_counter(self) -> Optional[Callable[[str], int]]:
        custom = getattr(self.agent, "token_estimator", None)
        if callable(custom):
            return custom
        if self._builtin_token_counter_init_done:
            return self._builtin_token_counter
        # Non-blocking: warmup continues in background; foreground falls back
        # to heuristic estimation until tokenizer is ready.
        self._start_token_counter_warmup()
        return None

    def _estimate_text_tokens(self, text: str) -> int:
        s = str(text or "")
        if not s:
            return 0
        counter = self._resolve_token_counter()
        if callable(counter):
            try:
                n = int(counter(s))
                if n >= 0:
                    return n
            except Exception:
                pass
        cjk = len(re.findall(r"[\u3400-\u9fff]", s))
        other = max(0, len(s) - cjk)
        # CJK average token density is higher; ASCII often ~= 4 chars per token.
        return cjk + max(1, (other + 3) // 4)

    def _estimate_message_tokens(self, role: str, content: str) -> int:
        return 6 + self._estimate_text_tokens(role) + self._estimate_text_tokens(content)

    def _store_context_usage_snapshot(self, context_window: int, total_input_tokens: int) -> None:
        ctx_window = max(1, int(context_window or DEFAULT_CONTEXT_WINDOW))
        total = max(0, int(total_input_tokens or 0))
        usage_pct = max(0, min(999, int(round((total * 100.0) / ctx_window))))
        self.agent._last_context_window = ctx_window
        self.agent._last_context_input_tokens = total
        self.agent._last_context_usage_percent = usage_pct

    def _persist_context_usage_snapshot(self) -> None:
        persisted = False
        try:
            persist_fn = getattr(self.agent, "_persist_active_chat_usage_snapshot", None)
            if callable(persist_fn):
                persist_fn()
                persisted = True
        except Exception:
            persisted = False
        try:
            sync_fn = getattr(self.agent, "_sync_active_chat_messages", None)
            if callable(sync_fn) and not persisted:
                sync_fn()
        except Exception:
            pass

    def _clip_text_to_token_budget(self, text: str, max_tokens: int) -> str:
        s = str(text or "")
        if max_tokens <= 0 or not s:
            return ""
        if self._estimate_text_tokens(s) <= max_tokens:
            return s
        lo, hi = 0, len(s)
        best = ""
        suffix = "…"
        while lo <= hi:
            mid = (lo + hi) // 2
            candidate = s[:mid].rstrip()
            if mid < len(s):
                candidate = (candidate + suffix) if candidate else suffix
            if self._estimate_text_tokens(candidate) <= max_tokens:
                best = candidate
                lo = mid + 1
            else:
                hi = mid - 1
        return best

    def _first_user_requirement(self, fallback: str) -> str:
        try:
            chat = self.agent._find_chat_by_id(getattr(self.agent, "active_chat_id", ""))
        except Exception:
            chat = None
        active_task_id = str(getattr(self.agent, "_active_runtime_task_id", "") or "").strip()
        if isinstance(chat, dict) and active_task_id:
            tasks = chat.get("tasks")
            if isinstance(tasks, list):
                for t in tasks:
                    if not isinstance(t, dict):
                        continue
                    if str(t.get("id") or "").strip() != active_task_id:
                        continue
                    root = str(t.get("root_user_input") or "").strip()
                    if root:
                        return root
                    break
        hist = self.history_for_regular_context()
        for msg in hist:
            if str(msg.get("role") or "").strip().lower() != "user":
                continue
            if self._is_excluded_user_message_for_model_context(msg):
                continue
            c = str(msg.get("content") or "").strip()
            if self._is_builtin_slash_user_message("user", c):
                continue
            if c:
                return c
        return str(fallback or "").strip()

    def _context_token_budgets(self) -> Dict[str, int]:
        ctx_window = parse_context_window(
            ((getattr(self.agent, "params", None) or {}).get("context_window")),
            default_value=DEFAULT_CONTEXT_WINDOW,
        )
        if ctx_window <= SMALL_CTX_MAX:
            profile = "small"
            system_ratio, history_ratio, op_ratio, summary_ratio = 0.50, 0.26, 0.14, 0.10
            memory_share_ratio = 0.42
            assistant_clip_tokens = 180
        elif ctx_window <= MEDIUM_CTX_MAX:
            profile = "medium"
            system_ratio, history_ratio, op_ratio, summary_ratio = 0.45, 0.35, 0.12, 0.08
            memory_share_ratio = 0.45
            assistant_clip_tokens = 260
        else:
            profile = "large"
            system_ratio, history_ratio, op_ratio, summary_ratio = 0.38, 0.48, 0.10, 0.06
            memory_share_ratio = 0.55
            assistant_clip_tokens = 400

        output_reserve = int(ctx_window * CONTEXT_OUTPUT_RESERVE_RATIO)
        output_reserve = max(CONTEXT_OUTPUT_RESERVE_MIN, min(output_reserve, CONTEXT_OUTPUT_RESERVE_MAX))
        safety_margin = max(CONTEXT_SAFETY_MARGIN_MIN, int(ctx_window * CONTEXT_SAFETY_MARGIN_RATIO))
        input_budget = max(512, ctx_window - output_reserve - safety_margin)
        system_budget = max(200, int(input_budget * system_ratio))
        history_budget = max(120, int(input_budget * history_ratio))
        op_context_budget = max(80, int(input_budget * op_ratio))
        history_summary_budget = max(80, int(input_budget * summary_ratio))
        return {
            "profile": profile,
            "context_window": ctx_window,
            "input_budget": input_budget,
            "system_budget": system_budget,
            "history_budget": history_budget,
            "op_context_budget": op_context_budget,
            "history_summary_budget": history_summary_budget,
            "memory_share_ratio": int(memory_share_ratio * 100),
            "assistant_clip_tokens": assistant_clip_tokens,
        }

    def _should_use_simple_chat_context(self, budgets: Dict[str, Any]) -> bool:
        try:
            ctx_window = int(budgets.get("context_window") or DEFAULT_CONTEXT_WINDOW)
        except Exception:
            ctx_window = DEFAULT_CONTEXT_WINDOW
        return ctx_window < SIMPLE_CHAT_SYSTEM_PROMPT_MIN_CONTEXT_WINDOW

    def _build_simple_chat_messages(
        self,
        user_input: str,
        budgets: Dict[str, Any],
    ) -> Tuple[List[Dict[str, Any]], bool]:
        user_text = str(user_input or "")
        user_tokens = self._estimate_message_tokens("user", user_text)
        input_budget = int(budgets.get("input_budget") or 1024)
        history_budget = max(0, input_budget - user_tokens)
        history_messages, history_stats = self._build_history_messages_by_budget(
            history_budget,
            int(budgets.get("history_summary_budget") or 80),
            int(budgets.get("assistant_clip_tokens") or 180),
            source_history=self.history_for_regular_context(),
        )
        messages: List[Dict[str, Any]] = list(history_messages)
        messages.append({"role": "user", "content": user_text})

        try:
            history_tokens = sum(
                self._estimate_message_tokens(str(m.get("role") or ""), str(m.get("content") or ""))
                for m in history_messages
            )
            total_input_tokens = int(history_tokens + user_tokens)
            ctx_window = int(budgets.get("context_window") or DEFAULT_CONTEXT_WINDOW)
            usage_pct = max(0, min(999, int(round((total_input_tokens * 100.0) / max(1, ctx_window)))))
            self.agent._last_context_usage_percent_precompression = usage_pct
            self.agent._last_context_aggressive_compression_applied = False
            self._store_context_usage_snapshot(ctx_window, total_input_tokens)
            if bool(getattr(self.agent, "_force_current_input_as_requirement_once", False)):
                self.agent._force_current_input_as_requirement_once = False
            get_logger().info(
                "context-pack profile=simple-chat ctx_window=%s input_budget=%s system=0 history=%s user=%s "
                "history_trimmed_assistant=%s history_summary_messages=%s history_dropped=%s",
                budgets.get("context_window"),
                budgets.get("input_budget"),
                history_tokens,
                user_tokens,
                history_stats.get("assistant_trimmed", 0),
                history_stats.get("summary_messages", 0),
                history_stats.get("dropped_messages", 0),
            )
        except Exception:
            pass
        return messages, True

    def _software_development_prompt_append(self) -> str:
        cached = getattr(self, "_software_development_prompt_cache", None)
        if isinstance(cached, str):
            return cached
        prompt_path = Path(__file__).resolve().parents[1] / "prompts" / "domain_software_development.md"
        try:
            text = prompt_path.read_text(encoding="utf-8").strip()
        except Exception:
            text = ""
        if text:
            text = "\n\n" + text + "\n"
        self._software_development_prompt_cache = text
        return text

    def _summarize_history_excerpt(self, rows: List[Dict[str, Any]], summary_budget: int) -> str:
        if not rows or summary_budget <= 0:
            return ""
        lines: List[str] = []
        for item in rows:
            role = "U" if str(item.get("role") or "").strip().lower() == "user" else "A"
            c = str(item.get("content") or "").replace("\n", " ").strip()
            if not c:
                continue
            lines.append(f"{role}:{c[:180]}")
            if len(lines) >= 12:
                break
        if not lines:
            return ""
        summary = "[历史摘要]\n" + " | ".join(lines)
        return self._clip_text_to_token_budget(summary, summary_budget)

    def _build_history_messages_by_budget(
        self,
        history_budget: int,
        summary_budget: int,
        assistant_clip_tokens: int,
        source_history: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
        hist = list(source_history if source_history is not None else self._context_eligible_history())
        if not hist or history_budget <= 0:
            return [], {"assistant_trimmed": 0, "summary_messages": 0, "dropped_messages": 0}

        normalized: List[Dict[str, Any]] = []
        assistant_trimmed = 0
        parse_slash_result = getattr(self.agent, "_parse_internal_slash_result_history_content", None)
        parse_worked_summary = getattr(self.agent, "_parse_task_worked_summary_history_content", None)
        for msg in hist:
            role = str(msg.get("role") or "").strip().lower()
            if role not in ("user", "assistant"):
                continue
            raw_content = str(msg.get("content") or "")
            if role == "user" and self._is_excluded_user_message_for_model_context(msg):
                continue
            if role == "user" and self._is_builtin_slash_user_message(role, raw_content):
                continue
            if role == "assistant" and self.parse_context_compaction_notice_content(raw_content) is not None:
                continue
            if role == "assistant" and callable(parse_slash_result):
                try:
                    slash_payload = parse_slash_result(raw_content)
                except Exception:
                    slash_payload = None
                if isinstance(slash_payload, dict):
                    continue
            if role == "assistant" and callable(parse_worked_summary):
                try:
                    worked_payload = parse_worked_summary(raw_content)
                except Exception:
                    worked_payload = None
                if isinstance(worked_payload, dict):
                    continue
            content = self._normalize_history_content_for_model(role, raw_content)
            if role == "assistant":
                before = content
                content = self._clip_text_to_token_budget(content, assistant_clip_tokens)
                if content != before:
                    assistant_trimmed += 1
            normalized.append({"role": role, "content": content})

        if not normalized:
            return [], {"assistant_trimmed": assistant_trimmed, "summary_messages": 0, "dropped_messages": 0}

        def _total_cost(items: List[Dict[str, Any]]) -> int:
            return sum(self._estimate_message_tokens(str(i.get("role") or ""), str(i.get("content") or "")) for i in items)

        working = list(normalized)
        dropped_for_summary: List[Dict[str, Any]] = []
        summary_message: Optional[Dict[str, Any]] = None

        # Stage 2: compress older dialogue into one summary message before dropping whole messages.
        target_without_summary = max(24, history_budget - max(40, summary_budget))
        while len(working) > 2 and _total_cost(working) > target_without_summary:
            dropped_for_summary.append(working.pop(0))
        if dropped_for_summary:
            summary_text = self._summarize_history_excerpt(dropped_for_summary, summary_budget)
            if summary_text:
                summary_message = {"role": "assistant", "content": summary_text}
                working.insert(0, summary_message)

        # Stage 3: still too big -> drop whole oldest messages.
        dropped_messages = 0
        while len(working) > 1 and _total_cost(working) > history_budget:
            if summary_message is not None and len(working) > 2:
                working.pop(1)
            else:
                working.pop(0)
            dropped_messages += 1

        if summary_message is not None and working and working[0] is summary_message and _total_cost(working) > history_budget:
            other_cost = _total_cost(working[1:])
            allowed = max(16, history_budget - other_cost - 6)
            clipped_summary = self._clip_text_to_token_budget(str(summary_message.get("content") or ""), allowed)
            if clipped_summary:
                summary_message["content"] = clipped_summary
            else:
                working.pop(0)

        if not working and normalized:
            # keep one latest message as last resort
            last = normalized[-1]
            max_content_tokens = max(16, history_budget - 8)
            working = [
                {
                    "role": str(last.get("role") or "assistant"),
                    "content": self._clip_text_to_token_budget(str(last.get("content") or ""), max_content_tokens),
                }
            ]

        stats = {
            "assistant_trimmed": assistant_trimmed,
            "summary_messages": 1 if summary_message else 0,
            "dropped_messages": dropped_messages,
        }
        return working, stats

    def _message_cost_for_tail_budget(self, msg: Dict[str, Any]) -> int:
        role = str(msg.get("role") or "").strip().lower()
        content = self._normalize_history_content_for_model(role, str(msg.get("content") or ""))
        return self._estimate_message_tokens(role, content)

    def _auto_tail_count_within_budget(self, rows: List[Tuple[int, Dict[str, Any]]], max_tokens: int) -> int:
        if not rows or max_tokens <= 0:
            return 0
        total = 0
        tail_count = 0
        pos = len(rows) - 1
        while pos >= 0:
            if self.is_context_compaction_summary_message(rows[pos][1]):
                break
            group_start = pos
            role = str(rows[pos][1].get("role") or "").strip().lower()
            if role == "assistant" and pos - 1 >= 0:
                prev = rows[pos - 1][1]
                if (
                    str(prev.get("role") or "").strip().lower() == "user"
                    and not self.is_context_compaction_summary_message(prev)
                ):
                    group_start = pos - 1
            group = rows[group_start:pos + 1]
            if any(self.is_context_compaction_summary_message(m) for _idx, m in group):
                break
            cost = sum(self._message_cost_for_tail_budget(m) for _idx, m in group)
            if cost <= 0:
                break
            if total + cost > max_tokens:
                break
            total += cost
            tail_count += len(group)
            pos = group_start - 1
        return tail_count

    def _compaction_candidate_rows(self, mode: str) -> List[Tuple[int, Dict[str, Any]]]:
        rows = self._history_with_indices_for_regular_context()
        if not rows:
            return []
        normalized_mode = str(mode or "").strip().lower()
        compact_until_pos = len(rows) - 1
        if normalized_mode == "auto":
            budgets = self._context_token_budgets()
            ctx_window = int(budgets.get("context_window") or DEFAULT_CONTEXT_WINDOW)
            tail_budget = max(1, int(ctx_window * AUTO_COMPACT_TAIL_WINDOW_RATIO))
            tail_count = self._auto_tail_count_within_budget(rows, tail_budget)
            compact_until_pos = len(rows) - tail_count - 1
        if compact_until_pos < 0:
            return []
        candidates = rows[:compact_until_pos + 1]
        has_new_dialogue = any(
            not self.is_context_compaction_summary_message(m)
            and not self.is_context_compaction_notice_message(m)
            for _idx, m in candidates
        )
        if not has_new_dialogue:
            return []
        return candidates

    def build_compaction_messages(
        self,
        mode: str,
        source_history: List[Dict[str, Any]],
        compact_until_index: int,
    ) -> List[Dict[str, Any]]:
        import os

        _ = compact_until_index
        self.agent._reload_skills()
        try:
            system_prompt = self.agent._compose_system_prompt_snapshot(include_tools=True)
        except Exception:
            system_prompt = str(getattr(self.agent, "system_prompt", "") or "")
        budgets = self._context_token_budgets()
        history_budget = max(160, int(int(budgets.get("input_budget") or 1024) * 0.72))
        summary_budget = max(80, int(history_budget * 0.10))
        assistant_clip = max(120, int(budgets.get("assistant_clip_tokens") or 260))
        history_messages, _stats = self._build_history_messages_by_budget(
            history_budget,
            summary_budget,
            assistant_clip,
            source_history=source_history,
        )
        os_info = os.uname() if hasattr(os, "uname") else os.name
        workspace_root_text = self._model_visible_workspace_directory_text()
        workspace_data_dir_text = self._model_visible_path_text(
            getattr(self.agent, "workspace_config_dir", None)
        )
        workspace_skills_dir = (Path(self.agent.workspace_config_dir) / "skills").resolve()
        default_install_skills_dir = (Path.home() / get_app_config_dirname() / "skills").resolve()
        runtime_tail_raw = (
            f"当前操作系统信息：{os_info}\n"
            f"当前 workspace 名称：{self.agent.workspace_name}\n"
            f"当前 chat 名称（弱提示，仅会话标签，不代表本轮任务目标）：{self.agent.active_chat_name}\n"
            f"当前 workspace 根目录（绝对路径）：{workspace_root_text}\n"
            f"当前 workspace 数据目录（绝对路径）：{workspace_data_dir_text}\n"
            f"默认技能安装路径（绝对路径）：{default_install_skills_dir}\n"
            f"当前 workspace skills 目录（绝对路径）：{workspace_skills_dir}\n"
            "安装第三方 skill 时：若用户未指定安装位置，必须使用“默认技能安装路径（绝对路径）”；"
            "仅当用户明确要求安装到 workspace 时，才可使用“当前 workspace skills 目录（绝对路径）”。\n"
        )
        sys_content = (
            f"{str(getattr(self.agent, '_skills_routing_prefix', '') or '')}"
            f"{system_prompt}\n"
            f"{self._software_development_prompt_append()}"
            f"{runtime_tail_raw}"
            "\n【上下文 compact 摘要任务】\n"
            "你正在为后续同一 chat 生成持久上下文摘要。请只输出摘要正文，不要寒暄、不要工具调用、不要 Markdown 代码块。\n"
            "摘要必须保留：用户原始目标、已完成/未完成事项、关键约束、重要决策、文件/命令/工具结果、错误与中断状态、后续继续时必须知道的事实。\n"
            "如果已有上一条【上下文摘要】，请把它与后续消息合并为一份更新后的摘要，不要重复无关细节。\n"
        )
        user_content = (
            f"compact_mode={str(mode or '').strip().lower() or 'manual'}\n"
            "请基于以上历史消息生成一份可替代这些消息的上下文摘要。"
        )
        return [{"role": "system", "content": sys_content}] + history_messages + [{"role": "user", "content": user_content}]

    def _format_compaction_banner_line(self, text: str) -> str:
        label = f" {str(text or '').strip()} "
        width = self._terminal_columns_for_compaction_banner()
        if len(label) >= width:
            return label.strip()
        pad = width - len(label)
        left = pad // 2
        right = pad - left
        return ("─" * left) + label + ("─" * right)

    def _compaction_output_stream(self) -> Any:
        stream = sys.stdout
        seen: Set[int] = set()
        while stream is not None:
            sid = id(stream)
            if sid in seen:
                break
            seen.add(sid)
            nxt = getattr(stream, "_primary", None)
            if nxt is None:
                nxt = getattr(stream, "_base_stream", None)
            if nxt is None:
                break
            stream = nxt
        return stream or sys.stdout

    def _terminal_columns_for_compaction_banner(self) -> int:
        stream = self._compaction_output_stream()
        if stream is not sys.stdout:
            width_raw = self._terminal_columns_from_compaction_streams(stream)
            if width_raw > 0:
                return max(1, width_raw - 1)
        fn_prompt = getattr(self.agent, "_terminal_columns_for_prompt_separator", None)
        if callable(fn_prompt):
            try:
                width0 = int(fn_prompt(default=80) or 0)
                if width0 > 0:
                    return max(1, width0)
            except Exception:
                pass
        width_raw = self._terminal_columns_from_compaction_streams(stream)
        if width_raw > 0:
            return width_raw
        return 80

    def _terminal_columns_from_compaction_streams(self, stream: Any) -> int:
        candidates: List[int] = []
        terminal_columns_attr = get_app_runtime_attr_name("terminal_columns")
        for obj in (sys.stdout, stream, sys.__stdout__):
            try:
                fn = getattr(obj, terminal_columns_attr, None)
                if callable(fn):
                    candidates.append(int(fn() or 0))
            except Exception:
                pass
        for obj in (stream, sys.__stdout__, sys.stdout):
            try:
                if hasattr(obj, "fileno"):
                    candidates.append(int(__import__("os").get_terminal_size(obj.fileno()).columns or 0))
            except Exception:
                pass
        for name in ("_terminal_columns_for_line_estimate",):
            fn2 = getattr(self.agent, name, None)
            if callable(fn2):
                try:
                    candidates.append(int(fn2() or 0))
                except Exception:
                    pass
        width = max([c for c in candidates if c > 0], default=80)
        return max(1, int(width))

    def _write_compaction_raw(self, text: str) -> None:
        stream = self._compaction_output_stream()
        try:
            stream.write(str(text or ""))
            stream.flush()
        except Exception:
            try:
                sys.stdout.write(str(text or ""))
                sys.stdout.flush()
            except Exception:
                pass

    def _print_compaction_banner(self, text: str) -> int:
        line = self._format_compaction_banner_line(text)
        self._write_compaction_raw("\n" + _ansi_gray(line) + "\n\n")
        try:
            self.agent._terminal_cursor_at_line_start = True
        except Exception:
            pass
        return 3

    def _clear_compaction_banner(self, rendered_lines: int) -> None:
        rows = max(0, int(rendered_lines or 0))
        if rows <= 0:
            return
        stream = self._compaction_output_stream()
        try:
            if not (hasattr(stream, "isatty") and stream.isatty()):
                return
        except Exception:
            return
        try:
            for _ in range(min(rows, 20)):
                stream.write("\x1b[1A\r\x1b[2K")
            stream.flush()
        except Exception:
            pass

    def compact_context(self, mode: str = "manual") -> bool:
        normalized_mode = str(mode or "").strip().lower() or "manual"
        if normalized_mode not in {"auto", "manual"}:
            normalized_mode = "manual"
        if not self._context_compaction_lock.acquire(blocking=False):
            if normalized_mode == "manual":
                print("Context compaction is already running.")
            return False
        try:
            return self._compact_context_locked(normalized_mode)
        finally:
            self._context_compaction_lock.release()

    def _compact_context_locked(self, mode: str) -> bool:
        candidates_with_idx = self._compaction_candidate_rows(mode)
        if not candidates_with_idx:
            if mode == "manual":
                print("No context available to compact.")
            return False
        start_text = "Automatically compacting context" if mode == "auto" else "Compacting context"
        start_banner_lines = self._print_compaction_banner(start_text)
        source_history = [m for _idx, m in candidates_with_idx]
        insert_after_idx = int(candidates_with_idx[-1][0])
        messages = self.build_compaction_messages(mode, source_history, insert_after_idx)
        try:
            raw = self.agent.call_ai(
                "Generate context compaction summary.",
                context="",
                stream=False,
                return_message=False,
                messages_override=messages,
                record_history_override=False,
            )
        except Exception as e:
            get_logger().exception("context compact: model call failed")
            if mode == "manual":
                print(f"Context compaction failed: {e}")
            return False
        summary = str(raw or "").strip() if isinstance(raw, str) else ""
        if summary.startswith("❌") or summary.startswith("Error calling LLM API") or not summary:
            if mode == "manual":
                print(summary or "Context compaction failed: empty summary.")
            return False
        summary = summary.replace("```", "").strip()
        content = self.build_context_compaction_summary_content(
            summary=summary,
            mode=mode,
            covered_message_count=len(source_history),
        )
        created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        task_id = str(getattr(self.agent, "_active_runtime_task_id", "") or "").strip()
        msg = {
            "role": "assistant",
            "content": content,
            "task_id": task_id,
            "created_at": created_at,
        }
        notice_msg = {
            "role": "assistant",
            "content": self.build_context_compaction_notice_content("Context automatically compacted"),
            "task_id": task_id,
            "created_at": created_at,
        }
        try:
            self.agent.conversation_history.insert(insert_after_idx + 1, msg)
            self.agent.conversation_history.insert(insert_after_idx + 2, notice_msg)
            self.agent._sync_active_chat_messages()
            self.refresh_context_usage_snapshot(context_hint="context compacted")
        except Exception:
            get_logger().exception("context compact: failed to persist summary")
            if mode == "manual":
                print("Context compaction failed while saving summary.")
            return False
        self._clear_compaction_banner(start_banner_lines)
        self._print_compaction_banner("Context automatically compacted")
        return True

    def maybe_auto_compact_before_user_message(self, user_input: str) -> bool:
        if not self._context_compaction_lock.acquire(blocking=False):
            return False
        try:
            self.refresh_context_usage_snapshot(user_input_hint=str(user_input or ""))
            usage_pct = int(getattr(self.agent, "_last_context_usage_percent", 0) or 0)
            try:
                trigger_pct = int(getattr(self.agent, "auto_compact_trigger_percent", AUTO_COMPACT_TRIGGER_PCT) or AUTO_COMPACT_TRIGGER_PCT)
            except Exception:
                trigger_pct = AUTO_COMPACT_TRIGGER_PCT
            trigger_pct = max(1, min(100, trigger_pct))
            if usage_pct < trigger_pct:
                return False
            if not self._compaction_candidate_rows("auto"):
                return False
            return self._compact_context_locked("auto")
        finally:
            self._context_compaction_lock.release()

    def refresh_context_usage_snapshot(
        self,
        user_input_hint: str = "",
        context_hint: str = "",
        expected_chat_id: str = "",
        expected_state_key: str = "",
    ) -> None:
        """
        Refresh status-bar usage percentage from current chat/model state
        without invoking memory retrieval or LLM calls.
        """
        try:
            expected = str(expected_chat_id or "").strip()
            if expected:
                current = str(getattr(self.agent, "active_chat_id", "") or "").strip()
                if current != expected:
                    return
            expected_key = str(expected_state_key or "").strip()
            budgets = self._context_token_budgets()
            filtered_history = self.history_for_regular_context()
            history_messages, _stats = self._build_history_messages_by_budget(
                int(budgets["history_budget"]),
                int(budgets["history_summary_budget"]),
                int(budgets["assistant_clip_tokens"]),
                source_history=filtered_history,
            )
            history_tokens = sum(
                self._estimate_message_tokens(str(m.get("role") or ""), str(m.get("content") or ""))
                for m in history_messages
            )
            compose_prompt = getattr(self.agent, "_compose_system_prompt_snapshot", None)
            if callable(compose_prompt):
                try:
                    system_prompt_snapshot = str(compose_prompt(include_tools=True) or "")
                except Exception:
                    system_prompt_snapshot = str(getattr(self.agent, "system_prompt", "") or "")
            else:
                system_prompt_snapshot = str(getattr(self.agent, "system_prompt", "") or "")
            sys_text = (
                f"{str(getattr(self.agent, '_skills_routing_prefix', '') or '')}"
                f"{system_prompt_snapshot}\n"
                f"{self._software_development_prompt_append()}"
                f"当前 workspace 名称：{str(getattr(self.agent, 'workspace_name', '') or '')}\n"
                f"当前 chat 名称：{str(getattr(self.agent, 'active_chat_name', '') or '')}\n"
            )
            system_tokens = self._estimate_message_tokens("system", sys_text)
            force_new_requirement = bool(
                getattr(self.agent, "_force_current_input_as_requirement_once", False)
            )
            requirement = (
                str(user_input_hint or "").strip()
                if force_new_requirement
                else self._first_user_requirement(str(user_input_hint or "").strip())
            )
            user_anchor = (
                "【关键约束】\n"
                "1) 用户原始需求必须持续满足。\n"
                "2) 本轮用户输入优先级最高。\n\n"
                f"用户原始需求: {requirement}\n"
                f"用户输入: {str(user_input_hint or '').strip()}\n"
            )
            if context_hint:
                user_anchor += f"操作上下文: {str(context_hint)}\n"
            user_tokens = self._estimate_message_tokens("user", user_anchor)
            total_input_tokens = int(system_tokens + history_tokens + user_tokens)
            if expected:
                current = str(getattr(self.agent, "active_chat_id", "") or "").strip()
                if current != expected:
                    return
            if expected_key and self._context_usage_state_key() != expected_key:
                return
            self._store_context_usage_snapshot(
                int(budgets.get("context_window") or DEFAULT_CONTEXT_WINDOW),
                total_input_tokens,
            )
            self._persist_context_usage_snapshot()
        except Exception:
            # Keep previous snapshot on refresh failure.
            pass

    def schedule_context_usage_refresh_async(
        self,
        user_input_hint: str = "",
        context_hint: str = "",
        expected_chat_id: str = "",
    ) -> bool:
        """
        Recompute context usage in background to avoid blocking UI/input loop.
        """
        target_chat_id = str(expected_chat_id or "").strip()
        if not target_chat_id:
            target_chat_id = str(getattr(self.agent, "active_chat_id", "") or "").strip()
        request_payload = {
            "user_input_hint": str(user_input_hint or ""),
            "context_hint": str(context_hint or ""),
            "expected_chat_id": target_chat_id,
            "expected_state_key": self._context_usage_state_key(),
        }
        with self._context_usage_refresh_lock:
            if self._context_usage_refresh_inflight:
                # Coalesce requests while one refresh is in flight; keep only the latest snapshot intent.
                self._context_usage_refresh_pending = dict(request_payload)
                return False
            self._context_usage_refresh_inflight = True

        def _run(initial_payload: Dict[str, str]) -> None:
            payload = dict(initial_payload)
            try:
                while True:
                    self.refresh_context_usage_snapshot(
                        user_input_hint=str(payload.get("user_input_hint") or ""),
                        context_hint=str(payload.get("context_hint") or ""),
                        expected_chat_id=str(payload.get("expected_chat_id") or ""),
                        expected_state_key=str(payload.get("expected_state_key") or ""),
                    )
                    with self._context_usage_refresh_lock:
                        pending = self._context_usage_refresh_pending
                        self._context_usage_refresh_pending = None
                        if not pending:
                            self._context_usage_refresh_inflight = False
                            break
                        payload = dict(pending)
            finally:
                with self._context_usage_refresh_lock:
                    self._context_usage_refresh_inflight = False
                    self._context_usage_refresh_pending = None

        threading.Thread(
            target=_run,
            args=(request_payload,),
            daemon=True,
            name=f"{get_app_logger_root()}-context-usage-refresh",
        ).start()
        return True

    def build_regular_task_messages(self, user_input: str, context: str = "") -> Tuple[List[Dict[str, Any]], bool]:
        budgets = self._context_token_budgets()
        if self._should_use_simple_chat_context(budgets):
            return self._build_simple_chat_messages(user_input, budgets)

        import os

        os_info = os.uname() if hasattr(os, "uname") else os.name
        date_time = datetime.now().strftime("%Y-%m-%d %A %H:%M:%S")

        self.update_session_summary_rolling()
        self.maybe_refresh_session_summary_llm()
        self.agent._reload_skills()
        self.agent.system_prompt = self.agent._compose_system_prompt_snapshot(include_tools=True)
        op_context_budget = int(budgets["op_context_budget"])
        memory_share = float(int(budgets.get("memory_share_ratio", 45))) / 100.0
        mem_budget = max(80, int(int(budgets["system_budget"]) * memory_share))
        tail_budget = max(120, int(int(budgets["system_budget"]) - mem_budget))
        mem_block_raw = self.memory_context_for_prompt(user_input)
        mem_block = mem_block_raw
        if mem_block:
            mem_block = self._clip_text_to_token_budget(mem_block, mem_budget)
        active_skill_prompt = str(getattr(self.agent, "_active_skill_full_prompt", "") or "").strip()
        active_skill_id = str(getattr(self.agent, "_active_skill_id", "") or "").strip()
        active_skill_source = str(getattr(self.agent, "_active_skill_source", "") or "").strip()
        active_skill_chunked = bool(getattr(self.agent, "_active_skill_chunked", False))
        active_skill_section = int(getattr(self.agent, "_active_skill_section", 0) or 0)
        active_skill_total_sections = int(getattr(self.agent, "_active_skill_total_sections", 0) or 0)
        skill_front_system_content = ""
        skill_tail_system_content = ""
        if active_skill_prompt:
            summary_budget = max(120, min(420, int(tail_budget * 0.40)))
            skill_summary = self._clip_text_to_token_budget(active_skill_prompt, summary_budget).strip()
            skill_id_display = active_skill_id or "unknown"
            source_suffix = f", source={active_skill_source}" if active_skill_source else ""
            section_suffix = ""
            if active_skill_chunked and active_skill_total_sections > 0:
                section_suffix = f", section={max(1, active_skill_section)}/{active_skill_total_sections}"
            skill_front_system_content = (
                "【动态技能执行摘要（前置）】\n"
                f"active_skill_id={skill_id_display}{source_suffix}{section_suffix}\n"
                "执行优先级：若与普通历史叙述冲突，优先遵循本摘要（安全硬约束除外）。\n"
                "关键规则摘要：\n"
                f"{skill_summary}"
            )
            skill_tail_system_content = (
                "【技能锚点】"
                f"active_skill_id={skill_id_display}；本轮执行请优先遵循前置技能摘要。"
            )
        immutable_system_core = (
            f"{self.agent._skills_routing_prefix}{self.agent.system_prompt}\n"
            f"{self._software_development_prompt_append()}"
        )
        # Key runtime metadata is intentionally non-clippable.
        workspace_root_text = self._model_visible_workspace_directory_text()
        workspace_data_dir_text = self._model_visible_path_text(
            getattr(self.agent, "workspace_config_dir", None)
        )
        workspace_skills_dir = (Path(self.agent.workspace_config_dir) / "skills").resolve()
        default_install_skills_dir = (Path.home() / get_app_config_dirname() / "skills").resolve()
        runtime_tail_raw = (
            f"当前操作系统信息：{os_info}\n"
            f"当前 workspace 名称：{self.agent.workspace_name}\n"
            f"当前 chat 名称（弱提示，仅会话标签，不代表本轮任务目标）：{self.agent.active_chat_name}\n"
            f"当前 workspace 根目录（绝对路径）：{workspace_root_text}\n"
            f"当前 workspace 数据目录（绝对路径）：{workspace_data_dir_text}\n"
            f"默认技能安装路径（绝对路径）：{default_install_skills_dir}\n"
            f"当前 workspace skills 目录（绝对路径）：{workspace_skills_dir}\n"
            "安装第三方 skill 时：若用户未指定安装位置，必须使用“默认技能安装路径（绝对路径）”；"
            "仅当用户明确要求安装到 workspace 时，才可使用“当前 workspace skills 目录（绝对路径）”。\n"
        )
        tail_context = immutable_system_core + runtime_tail_raw
        if mem_block:
            sys_prefix = (
                "【经验记忆 — 须主动落实】\n"
                "以下为当前工作区已持久化条目。其后每一轮答复前都须先判断是否相关；"
                "相关则自然语言输出必须以本段为准，不得以未约定的通用云端/供应商默认人设替代。\n\n"
                + mem_block + "\n\n---\n\n" + tail_context
            )
        else:
            sys_prefix = tail_context
        messages: List[Dict[str, Any]] = [{"role": "system", "content": sys_prefix}]
        if skill_front_system_content:
            messages.append({"role": "system", "content": skill_front_system_content})
        filtered_history = self.history_for_regular_context()
        history_messages, history_stats = self._build_history_messages_by_budget(
            int(budgets["history_budget"]),
            int(budgets["history_summary_budget"]),
            int(budgets["assistant_clip_tokens"]),
            source_history=filtered_history,
        )
        interruption_line = self._latest_interruption_context_line(filtered_history)
        for msg in history_messages:
            messages.append(msg)

        force_new_requirement = bool(
            getattr(self.agent, "_force_current_input_as_requirement_once", False)
        )
        workspace_directory = self._model_visible_workspace_directory_text()
        original_requirement = (
            str(user_input or "").strip()
            if force_new_requirement
            else self._first_user_requirement(user_input)
        )
        current_input = ""
        current_input += (
            "【关键约束】\n"
            "1) 用户原始需求必须持续满足。\n"
            "2) 本轮用户输入优先级最高。\n"
            "3) 如有最近操作结果，结论需与其一致。\n\n"
        )
        if force_new_requirement:
            last_cancelled_task = str(getattr(self.agent, "_last_cancelled_task", "") or "").strip()
            current_input += (
                "4) 上一任务已由用户取消；本轮若是新任务，禁止主动恢复或重做被取消任务，"
                "除非用户明确要求继续。\n\n"
            )
            if last_cancelled_task:
                current_input += f"最近被取消的任务: {last_cancelled_task}\n"
        if mem_block:
            current_input += (
                "【硬性要求】作答前须核对首条 system 消息中的「经验记忆」："
                "与本轮用户问题相关的条目必须在答复中体现，不得用与这些记录无关的通用助手或供应商设定替代。\n\n"
            )
        if skill_front_system_content:
            current_input += (
                "【硬性要求】本轮存在已激活 skill（见前置技能摘要与后置锚点）；"
                "若与普通历史叙述冲突，执行时必须优先遵循该 skill（安全硬约束除外）。\n\n"
            )
        current_input += (
            f"当前 workspace: {self.agent.workspace_name}\n"
            f"当前目录（workspace）: {workspace_directory}\n"
        )
        if self.agent.operation_results:
            latest_op = self.agent.operation_results[-1]
            if isinstance(latest_op, dict) and ("timestamp" in latest_op):
                latest_op = dict(latest_op)
                latest_op.pop("timestamp", None)
            op_line = f"最近的操作结果: {latest_op}\n"
            current_input += self._clip_text_to_token_budget(op_line, op_context_budget)
        if context:
            ctx_line = f"操作上下文: {context}\n"
            current_input += self._clip_text_to_token_budget(ctx_line, op_context_budget)
        if interruption_line:
            current_input += f"最近的中断状态: {interruption_line}\n"
        # 用户原始需求必须进入上下文（即使历史被压缩）。
        current_input += f"用户原始需求: {original_requirement}\n"
        # Keep timestamp at the tail to preserve upstream cache prefix stability.
        current_input += f"用户输入: {user_input}\n"
        current_input += f"本地时间参考: {date_time}"
        if skill_tail_system_content:
            messages.append({"role": "system", "content": skill_tail_system_content})
        current_user_msg = {"role": "user", "content": current_input}
        messages.append(current_user_msg)

        system_tokens = 0
        history_tokens = 0
        user_tokens = 0
        try:
            system_tokens = self._estimate_message_tokens("system", sys_prefix)
            if skill_front_system_content:
                system_tokens += self._estimate_message_tokens("system", skill_front_system_content)
            if skill_tail_system_content:
                system_tokens += self._estimate_message_tokens("system", skill_tail_system_content)
            history_tokens = sum(
                self._estimate_message_tokens(str(m.get("role") or ""), str(m.get("content") or ""))
                for m in history_messages
            )
            user_tokens = self._estimate_message_tokens("user", current_input)
            total_input_tokens = int(system_tokens + history_tokens + user_tokens)
            ctx_window = int(budgets.get("context_window") or DEFAULT_CONTEXT_WINDOW)
            usage_pct = max(0, min(999, int(round((total_input_tokens * 100.0) / max(1, ctx_window)))))
            self.agent._last_context_usage_percent_precompression = usage_pct
            self.agent._last_context_aggressive_compression_applied = False
            if usage_pct > AGGRESSIVE_COMPRESS_TRIGGER_PCT:
                target_tokens = max(256, int((ctx_window * AGGRESSIVE_COMPRESS_TARGET_PCT) / 100))
                aggressive_user_budget = max(120, int(target_tokens * 0.45))
                aggressive_system_budget = max(80, int(target_tokens * 0.35))
                aggressive_history_budget = max(40, int(target_tokens * 0.20))
                aggressive_history_summary_budget = max(30, int(aggressive_history_budget * 0.55))
                aggressive_assistant_clip = max(48, int(int(budgets.get("assistant_clip_tokens") or 180) * 0.35))
                aggressive_op_context_budget = max(24, int(op_context_budget * 0.35))
                aggressive_mem_budget = max(24, int(aggressive_system_budget * 0.35))

                mem_block2 = ""
                if mem_block_raw:
                    mem_block2 = self._clip_text_to_token_budget(mem_block_raw, aggressive_mem_budget)
                tail_context2 = immutable_system_core + runtime_tail_raw
                if mem_block2:
                    sys_prefix2 = (
                        "【经验记忆 — 压缩模式】\n"
                        + mem_block2
                        + "\n\n---\n\n"
                        + tail_context2
                    )
                else:
                    sys_prefix2 = tail_context2

                history_messages2, history_stats2 = self._build_history_messages_by_budget(
                    aggressive_history_budget,
                    aggressive_history_summary_budget,
                    aggressive_assistant_clip,
                    source_history=filtered_history,
                )
                current_input2_head = ""
                current_input2_head += (
                    "【关键约束】\n"
                    "1) 用户原始需求必须持续满足。\n"
                    "2) 本轮用户输入优先级最高。\n"
                    "3) 如有最近操作结果，结论需与其一致。\n\n"
                )
                if skill_front_system_content:
                    current_input2_head += (
                        "【硬性要求】本轮存在已激活 skill（见前置技能摘要与后置锚点）；"
                        "若与普通历史叙述冲突，执行时必须优先遵循该 skill（安全硬约束除外）。\n\n"
                    )
                if force_new_requirement:
                    last_cancelled_task = str(getattr(self.agent, "_last_cancelled_task", "") or "").strip()
                    current_input2_head += (
                        "4) 上一任务已由用户取消；本轮若是新任务，禁止主动恢复或重做被取消任务，"
                        "除非用户明确要求继续。\n\n"
                    )
                    if last_cancelled_task:
                        current_input2_head += f"最近被取消的任务: {last_cancelled_task}\n"
                if interruption_line:
                    current_input2_head += f"最近的中断状态: {interruption_line}\n"
                if self.agent.operation_results:
                    latest_op2 = self.agent.operation_results[-1]
                    if isinstance(latest_op2, dict) and ("timestamp" in latest_op2):
                        latest_op2 = dict(latest_op2)
                        latest_op2.pop("timestamp", None)
                    op_line2 = f"最近的操作结果: {latest_op2}\n"
                    current_input2_head += self._clip_text_to_token_budget(
                        op_line2,
                        max(48, aggressive_op_context_budget),
                    )
                current_input2_optional = (
                    f"当前 workspace: {self.agent.workspace_name}\n"
                    f"当前目录（workspace）: {workspace_directory}\n"
                )
                if context:
                    ctx_line2 = f"操作上下文: {context}\n"
                    current_input2_optional += self._clip_text_to_token_budget(ctx_line2, aggressive_op_context_budget)
                # Hard anchors: never clip original requirement and current input.
                current_input2_tail = (
                    f"用户原始需求: {original_requirement}\n"
                    f"用户输入: {user_input}\n"
                    f"本地时间参考: {date_time}"
                )
                required_anchor = current_input2_head + current_input2_tail
                optional_budget = max(0, aggressive_user_budget - self._estimate_text_tokens(required_anchor))
                current_input2_optional = self._clip_text_to_token_budget(current_input2_optional, optional_budget)
                current_input2 = current_input2_head + current_input2_optional + current_input2_tail

                system_tokens2 = self._estimate_message_tokens("system", sys_prefix2)
                if skill_front_system_content:
                    system_tokens2 += self._estimate_message_tokens("system", skill_front_system_content)
                if skill_tail_system_content:
                    system_tokens2 += self._estimate_message_tokens("system", skill_tail_system_content)
                history_tokens2 = sum(
                    self._estimate_message_tokens(str(m.get("role") or ""), str(m.get("content") or ""))
                    for m in history_messages2
                )
                user_tokens2 = self._estimate_message_tokens("user", current_input2)
                total_input_tokens2 = int(system_tokens2 + history_tokens2 + user_tokens2)

                if total_input_tokens2 < total_input_tokens:
                    messages = [{"role": "system", "content": sys_prefix2}]
                    if skill_front_system_content:
                        messages.append({"role": "system", "content": skill_front_system_content})
                    messages += list(history_messages2)
                    if skill_tail_system_content:
                        messages.append({"role": "system", "content": skill_tail_system_content})
                    messages.append({"role": "user", "content": current_input2})
                    sys_prefix = sys_prefix2
                    history_messages = history_messages2
                    current_input = current_input2
                    history_stats = history_stats2
                    system_tokens = system_tokens2
                    history_tokens = history_tokens2
                    user_tokens = user_tokens2
                    total_input_tokens = total_input_tokens2
                    self.agent._last_context_aggressive_compression_applied = True
                    get_logger().info(
                        "context-pack aggressive-compress triggered pre_pct=%s target_pct=%s post_pct=%s",
                        usage_pct,
                        AGGRESSIVE_COMPRESS_TARGET_PCT,
                        int(round((total_input_tokens2 * 100.0) / max(1, ctx_window))),
                    )

            self._store_context_usage_snapshot(ctx_window, total_input_tokens)
            if force_new_requirement:
                self.agent._force_current_input_as_requirement_once = False
            get_logger().info(
                "context-pack profile=%s ctx_window=%s input_budget=%s system=%s history=%s user=%s "
                "history_trimmed_assistant=%s history_summary_messages=%s history_dropped=%s",
                budgets.get("profile"),
                budgets.get("context_window"),
                budgets.get("input_budget"),
                system_tokens,
                history_tokens,
                user_tokens,
                history_stats.get("assistant_trimmed", 0),
                history_stats.get("summary_messages", 0),
                history_stats.get("dropped_messages", 0),
            )
        except Exception:
            pass
        return messages, True
