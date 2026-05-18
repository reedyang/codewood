import json
import re
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

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


class SessionMemoryService:
    def __init__(self, agent: Any) -> None:
        self.agent = agent

    def append_chat_message(self, role: str, content: str) -> None:
        r = str(role or "").strip().lower()
        if r not in ("user", "assistant"):
            return
        self.agent.conversation_history.append({"role": r, "content": str(content or "")})
        self.agent._sync_active_chat_messages()
        if r == "user":
            self.agent._maybe_schedule_auto_chat_name()

    def update_session_summary_rolling(self) -> None:
        hist = list(getattr(self.agent, "conversation_history", None) or [])
        chunks: List[str] = []
        snip = SESSION_SUMMARY_MSG_SNIPPET
        for msg in hist[-8:]:
            role = (msg.get("role") or "").strip().lower()
            if role not in ("user", "assistant"):
                continue
            c = str(msg.get("content") or "").replace("\n", " ").strip()
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
        if not getattr(self.agent, "session_summary_llm_enabled", True):
            return
        pairs = len(self.agent.conversation_history) // 2
        if pairs < SESSION_SUMMARY_LLM_INTERVAL_PAIRS:
            return
        if self.agent._last_llm_summary_pair_count > 0:
            if pairs - self.agent._last_llm_summary_pair_count < SESSION_SUMMARY_LLM_INTERVAL_PAIRS:
                return
        hist = self.agent.conversation_history[-SESSION_SUMMARY_LLM_HISTORY_MSGS:]
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
            content = _clip(str(msg.get("content") or ""), per_msg)
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
                "【经验记忆（内化教训与偏好，不是知识库文献；可与知识库并存；关键事实请仍核实）】",
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

        threading.Thread(target=_run, daemon=True, name="smartshell-memory-reflect").start()

    def run_memory_reflection_body(self) -> None:
        if not self.agent._ensure_memory_service():
            return
        hist = self.agent.conversation_history[-6:] if self.agent.conversation_history else []
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

    def build_regular_task_messages(self, user_input: str, context: str = "") -> Tuple[List[Dict[str, Any]], bool]:
        import os

        os_info = os.uname() if hasattr(os, "uname") else os.name
        date_time = datetime.now().strftime("%Y-%m-%d %A %H:%M:%S")

        self.update_session_summary_rolling()
        self.maybe_refresh_session_summary_llm()
        self.agent._reload_skills()
        self.agent.system_prompt = self.agent._compose_system_prompt_snapshot(include_tools=True)
        mem_block = self.memory_context_for_prompt(user_input)
        tail_context = (
            f"{self.agent._skills_routing_prefix}{self.agent.system_prompt}\n{self.agent._active_skill_full_prompt}"
            f"当前操作系统信息：{os_info}\n当前日期时间：{date_time}\n"
            f"当前 smart-shell 根目录（绝对路径）：{self.agent._self_repo_root}\n"
            f"当前 config 目录（绝对路径）：{self.agent.config_dir}\n"
            f"当前 workspace 名称：{self.agent.workspace_name}\n"
            f"当前 chat 名称（弱提示，仅会话标签，不代表本轮任务目标）：{self.agent.active_chat_name}\n"
            f"当前 workspace 目录（绝对路径）：{self.agent.ai_workspace_dir}\n"
        )
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
        for msg in self.agent.conversation_history[-CHAT_RECENT_MESSAGES:]:
            messages.append(msg)

        current_input = ""
        if mem_block:
            current_input += (
                "【硬性要求】作答前须核对上一条 system 开头的「经验记忆」："
                "与本轮用户问题相关的条目必须在答复中体现，不得用与这些记录无关的通用助手或供应商设定替代。\n\n"
            )
        current_input += (
            f"当前 workspace: {self.agent.workspace_name}\n"
            f"当前工作目录: {self.agent.work_directory}\n"
        )
        if self.agent.operation_results:
            current_input += f"最近的操作结果: {self.agent.operation_results[-1]}\n"
        if context:
            current_input += f"操作上下文: {context}\n"
        current_input += f"用户输入: {user_input}"
        messages.append({"role": "user", "content": current_input})
        return messages, True
