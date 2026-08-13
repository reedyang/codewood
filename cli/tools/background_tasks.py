"""Background task registry and completion notifications.

A background task is a long-running unit started with ``background=True``:
either a shell command spawned by the ``shell`` tool, or a sub-agent started
by the ``run_subagent`` tool (``kind="subagent"``, driven by a worker thread
with a ``cancel`` callback instead of an OS process).  The tool returns
immediately with a ``background_task_id`` (equal to the issuing tool call id);
the work keeps running in the background.  When it finishes (normally, with a
failure code, or after being killed via the user interrupt path or
``background_task_kill``), the manager finalizes the task: it collapses the
captured output, writes a full-output sidecar file, updates the persisted tool
round (so the GUI block shows the result after reload), emits a final
``background_task_output`` SSE event (end=true) and queues a completion
notification that the main loop drains before the next model call (delivered
as a hidden internal user message).
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

BG_TASK_STATUS_RUNNING = "running"
BG_TASK_STATUS_COMPLETED = "completed"
BG_TASK_STATUS_FAILED = "failed"
BG_TASK_STATUS_KILLED = "killed"
BG_TASK_STATUS_NOT_FOUND = "not_found"

#: Bounded tail kept in the model-visible payload / persisted tool round.
#: The full output is always written to a sidecar file referenced by
#: ``full_output_path``.
BG_TASK_OUTPUT_MAX_CHARS = 60000

_BG_SINK_EMIT_INTERVAL = 0.5


def _bounded_output(text: str, max_chars: int = BG_TASK_OUTPUT_MAX_CHARS) -> str:
    """Return ``text`` clipped to ``max_chars`` with a truncation notice."""
    text = str(text or "")
    if len(text) <= max_chars:
        return text
    head = text[: max_chars - 200]
    return (
        f"{head}\n... (background task output truncated at {max_chars} chars; "
        "use `background_task_status` for the full_output_path) ...\n"
    )


class BackgroundOutputSink:
    """Display sink for a background task's streaming output.

    Replaces the terminal/GUI stdout target of the shell stream thread so a
    background command's live output never leaks into the current round.  In
    GUI mode, chunks are batched (~0.5s) and forwarded to the
    ``background_task_output`` SSE event so the mapped tool-call block updates
    live; the authoritative final text is sent by the finalizer with
    ``end=True``.
    """

    def __init__(self, agent: Any, task_id: str) -> None:
        self._agent = agent
        self._task_id = str(task_id or "")
        self._buf = ""
        self._last_emit = 0.0
        self._lock = threading.Lock()

    def write(self, text: Any) -> None:
        if not text:
            return
        with self._lock:
            self._buf += str(text)
            now = time.time()
            if self._buf and (now - self._last_emit) >= _BG_SINK_EMIT_INTERVAL:
                chunk, self._buf = self._buf, ""
                self._last_emit = now
                self._emit(chunk)

    def flush(self) -> None:
        with self._lock:
            chunk, self._buf = self._buf, ""
        if chunk:
            self._emit(chunk)

    def close(self) -> None:
        self.flush()

    def _emit(self, chunk: str) -> None:
        hook = getattr(self._agent, "_gui_bg_task_output_emit", None)
        if callable(hook):
            try:
                hook(self._task_id, chunk, end=False, status="")
            except Exception:
                pass


class BackgroundTaskRecord:
    """Mutable per-task state shared by the shell worker and the manager."""

    __slots__ = (
        "task_id",
        "agent",
        "chat_key",
        "command",
        "cwd",
        "process_ref",
        "worker_state",
        "stdout_chunks",
        "stream_chunks_lock",
        "merge_path",
        "sink",
        "status",
        "done_event",
        "start_time",
        "end_time",
        "final_out",
        "return_code",
        "timed_out",
        "aborted_by_user",
        "pause_interrupt",
        "output_path",
        "notification_pending",
        "notification_injected",
        "finalized",
        "_finalize_lock",
        "kind",
        "cancel",
        "session_marker",
    )

    def __init__(
        self,
        *,
        task_id: str,
        agent: Any,
        chat_key: str,
        command: str,
        cwd: str,
        process_ref: Dict[str, Any],
        worker_state: Dict[str, Any],
        stdout_chunks: List[str],
        stream_chunks_lock: threading.Lock,
        merge_path: Optional[str],
        sink: Optional[BackgroundOutputSink],
        kind: str = "shell",
        cancel: Optional[Any] = None,
    ) -> None:
        self.task_id = str(task_id or "")
        self.agent = agent
        self.chat_key = str(chat_key or "")
        self.command = str(command or "")
        self.cwd = str(cwd or "")
        self.process_ref = process_ref
        self.worker_state = worker_state
        self.stdout_chunks = stdout_chunks
        self.stream_chunks_lock = stream_chunks_lock
        self.merge_path = merge_path
        self.sink = sink
        self.status = BG_TASK_STATUS_RUNNING
        self.done_event = (
            worker_state.get("done") if isinstance(worker_state, dict) else None
        )
        self.start_time = time.time()
        self.end_time: Optional[float] = None
        self.final_out = ""
        self.return_code: Optional[int] = None
        self.timed_out = False
        self.aborted_by_user = False
        self.pause_interrupt = False
        self.output_path = ""
        self.notification_pending = False
        self.notification_injected = False
        self.finalized = False
        self._finalize_lock = threading.Lock()
        self.kind = str(kind or "shell")
        self.cancel = cancel
        self.session_marker = ""

    def process(self) -> Any:
        if isinstance(self.process_ref, dict):
            return self.process_ref.get("process")
        return None


class BackgroundTaskManager:
    """Session-scoped registry of background shell tasks."""

    def __init__(self, agent: Any) -> None:
        self._agent = agent
        self._tasks: Dict[str, BackgroundTaskRecord] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Registration / lifecycle
    # ------------------------------------------------------------------
    def register_task(
        self,
        *,
        task_id: str,
        agent: Any,
        chat_key: str,
        command: str,
        cwd: str,
        process_ref: Dict[str, Any],
        worker_state: Dict[str, Any],
        stdout_chunks: List[str],
        stream_chunks_lock: threading.Lock,
        merge_path: Optional[str],
        sink: Optional[BackgroundOutputSink],
        kind: str = "shell",
        cancel: Optional[Any] = None,
    ) -> BackgroundTaskRecord:
        record = BackgroundTaskRecord(
            task_id=task_id,
            agent=agent,
            chat_key=chat_key,
            command=command,
            cwd=cwd,
            process_ref=process_ref,
            worker_state=worker_state,
            stdout_chunks=stdout_chunks,
            stream_chunks_lock=stream_chunks_lock,
            merge_path=merge_path,
            sink=sink,
            kind=kind,
            cancel=cancel,
        )
        with self._lock:
            self._tasks[record.task_id] = record
        return record

    def watch(self, record: BackgroundTaskRecord) -> None:
        threading.Thread(
            target=self._watch_loop,
            args=(record,),
            name="codewood-bg-watch",
            daemon=True,
        ).start()

    def _watch_loop(self, record: BackgroundTaskRecord) -> None:
        try:
            if record.done_event is not None:
                record.done_event.wait()
        finally:
            try:
                self._finalize(record)
            except Exception:
                pass

    def get(self, task_id: str) -> Optional[BackgroundTaskRecord]:
        if not task_id:
            return None
        with self._lock:
            return self._tasks.get(str(task_id))

    def all(self) -> List[BackgroundTaskRecord]:
        with self._lock:
            return list(self._tasks.values())

    def shutdown(self) -> None:
        """Terminate any still-running background tasks (app/chat teardown)."""
        for record in self.all():
            if record.status == BG_TASK_STATUS_RUNNING:
                self.kill(record.task_id)

    # ------------------------------------------------------------------
    # Public tool-facing API
    # ------------------------------------------------------------------
    def kill(self, task_id: str) -> Dict[str, Any]:
        record = self.get(task_id)
        if record is None:
            return {
                "success": False,
                "background_task_id": str(task_id or ""),
                "status": BG_TASK_STATUS_NOT_FOUND,
                "error": f"unknown background task id: {task_id}",
            }
        if record.status != BG_TASK_STATUS_RUNNING:
            return {
                "success": True,
                "background_task_id": record.task_id,
                "status": record.status,
                "return_code": record.return_code,
                "output": _bounded_output(record.final_out),
                "full_output_path": record.output_path,
            }
        agent = record.agent
        # Non-process tasks (e.g. background sub-agents) expose a ``cancel``
        # callback instead of a process tree: ask the worker to stop at its
        # next checkpoint. The watcher finalizes the record as usual.
        cancel = getattr(record, "cancel", None)
        if callable(cancel):
            try:
                cancel()
            except Exception:
                pass
        process = record.process()
        if process is not None:
            mark = getattr(agent, "_mark_process_aborted", None)
            if callable(mark):
                try:
                    mark(process)
                except Exception:
                    pass
            terminator = getattr(agent, "_terminate_single_process_tree", None)
            if callable(terminator):
                try:
                    terminator(process)
                except Exception:
                    pass
        return {
            "success": True,
            "background_task_id": record.task_id,
            "status": BG_TASK_STATUS_KILLED,
            "message": f"background task {record.task_id} kill requested",
        }

    def status(self, task_id: str) -> Dict[str, Any]:
        record = self.get(task_id)
        if record is None:
            return {
                "success": False,
                "background_task_id": str(task_id or ""),
                "status": BG_TASK_STATUS_NOT_FOUND,
                "error": f"unknown background task id: {task_id}",
            }
        payload: Dict[str, Any] = {
            "success": True,
            "background_task_id": record.task_id,
            "status": record.status,
            "command": record.command,
            "return_code": record.return_code,
            "full_output_path": record.output_path,
        }
        if record.status == BG_TASK_STATUS_RUNNING:
            with record.stream_chunks_lock:
                partial = "".join(record.stdout_chunks)
            payload["output"] = _bounded_output(
                f"(background task still running; partial output)\n{partial}"
            )
        else:
            payload["output"] = _bounded_output(record.final_out)
        if record.end_time is not None and record.start_time:
            payload["elapsed_seconds"] = round(record.end_time - record.start_time, 2)
        return payload

    def wait(self, task_id: Optional[str], timeout: float) -> Dict[str, Any]:
        """Block up to ``timeout`` seconds; returns when the task ends or time runs out."""
        timeout = max(0.0, float(timeout))
        started = time.monotonic()
        record = self.get(task_id) if task_id else None
        if record is not None and record.status != BG_TASK_STATUS_RUNNING:
            waited = 0.0
        elif record is not None and record.done_event is not None:
            remaining = max(0.0, timeout - (time.monotonic() - started))
            finished = record.done_event.wait(remaining) if remaining > 0 else record.done_event.is_set()
            waited = time.monotonic() - started
            if finished:
                # The worker finished; give the finalizer a moment to settle the
                # terminal status so the returned payload is authoritative.
                settle_deadline = time.monotonic() + 2.0
                while record.status == BG_TASK_STATUS_RUNNING and time.monotonic() < settle_deadline:
                    time.sleep(0.01)
        else:
            # Plain sleep (no task id) in small slices so interrupts stay snappy.
            while True:
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    break
                time.sleep(min(0.2, remaining))
            waited = time.monotonic() - started
        result: Dict[str, Any] = {
            "success": True,
            "waited_seconds": round(waited, 2),
        }
        if record is not None:
            result["background_task_id"] = record.task_id
            current = self.status(record.task_id)
            result["task_status"] = current.get("status")
            if current.get("status") != BG_TASK_STATUS_RUNNING:
                result["task_result"] = current
        return result

    # ------------------------------------------------------------------
    # Notifications
    # ------------------------------------------------------------------
    def drain_notifications(self) -> List[Dict[str, Any]]:
        """Return completion payloads for tasks that finished and were not yet
        injected into the model context.  Marks them as injected."""
        out: List[Dict[str, Any]] = []
        for record in self.all():
            if not record.notification_pending or record.notification_injected:
                continue
            record.notification_injected = True
            payload: Dict[str, Any] = {
                "background_task_id": record.task_id,
                "status": record.status,
                "return_code": record.return_code,
                "output": _bounded_output(record.final_out),
                "full_output_path": record.output_path,
            }
            if record.status in (BG_TASK_STATUS_FAILED, BG_TASK_STATUS_KILLED):
                payload["error"] = _failure_detail(record)
            out.append(payload)
        return out

    # ------------------------------------------------------------------
    # Finalize
    # ------------------------------------------------------------------
    def _finalize(self, record: BackgroundTaskRecord) -> None:
        with record._finalize_lock:
            if record.finalized:
                return
            record.finalized = True
        agent = record.agent
        worker_state = record.worker_state if isinstance(record.worker_state, dict) else {}
        is_subagent = str(getattr(record, "kind", "") or "") == "subagent"
        process = record.process()

        aborted_by_user = False
        pause_interrupt = False
        if is_subagent:
            # Background sub-agents have no OS process: the worker reports its
            # own abort (a cancel was requested via ``background_task_kill``).
            aborted_by_user = bool(worker_state.get("aborted", False))
            pause_interrupt = False
        else:
            consume_abort = getattr(agent, "_consume_process_aborted", None)
            if callable(consume_abort):
                try:
                    aborted_by_user = bool(consume_abort(process))
                except Exception:
                    pass
            if aborted_by_user:
                consume_pause = getattr(agent, "_consume_process_pause", None)
                if callable(consume_pause):
                    try:
                        pause_interrupt = bool(consume_pause(process))
                    except Exception:
                        pass

        from .shell import _collapse_cr_output  # local import avoids cycles

        with record.stream_chunks_lock:
            out = "".join(record.stdout_chunks)
        out = _collapse_cr_output(out)

        _rc_raw = worker_state.get("return_code")
        rc = int(_rc_raw if _rc_raw is not None else -1)
        timed_out = bool(worker_state.get("timed_out", False))

        if aborted_by_user:
            if not is_subagent:
                from .shell import _shell_abort_notice  # local import
                from .shell import SHELL_CANCEL_ABORT_NOTICE

                notice = ""
                try:
                    notice = _shell_abort_notice(agent, process)
                except Exception:
                    notice = ""
                if not notice:
                    notice = SHELL_CANCEL_ABORT_NOTICE
                out = notice
            status = BG_TASK_STATUS_KILLED
        else:
            status = BG_TASK_STATUS_COMPLETED if rc == 0 else BG_TASK_STATUS_FAILED

        if record.merge_path:
            from .shell import append_shell_merge_output_path  # local import

            try:
                out = append_shell_merge_output_path(out, rc, record.merge_path)
            except Exception:
                pass

        record.final_out = out
        record.return_code = rc
        record.timed_out = timed_out
        record.aborted_by_user = aborted_by_user
        record.pause_interrupt = pause_interrupt
        record.status = status
        record.end_time = time.time()

        record.output_path = _write_bg_output_file(agent, record.task_id, out)

        if not is_subagent:
            unreg = getattr(agent, "_unregister_interruptible_process", None)
            if callable(unreg):
                try:
                    unreg(process)
                except Exception:
                    pass

        try:
            self._update_round_output(record)
        except Exception:
            pass

        try:
            if record.sink is not None:
                record.sink.close()
        except Exception:
            pass

        gui_hook = getattr(agent, "_gui_bg_task_output_emit", None)
        if callable(gui_hook):
            try:
                gui_hook(
                    record.task_id,
                    _bounded_output(record.final_out),
                    end=True,
                    status=status,
                    return_code=rc,
                )
            except Exception:
                pass

        if not bool(getattr(agent, "_gui_plain_stream", False)):
            try:
                print(format_task_summary(record.task_id, status, rc))
            except Exception:
                pass

        record.notification_pending = True

    def _update_round_output(self, record: BackgroundTaskRecord, _attempt: int = 0) -> bool:
        """Rewrite the persisted tool round (raw entry) of the issuing tool
        call (shell or run_subagent) with the final bounded output so a
        reload shows it expanded."""
        agent = record.agent
        final = _bounded_output(record.final_out)
        tid = record.task_id

        def _match_entry(entry: Any) -> bool:
            if not isinstance(entry, dict):
                return False
            tool = str(entry.get("tool") or "").strip().lower()
            return (
                tool in ("shell", "run_subagent")
                and str(entry.get("bgTaskId") or "") == tid
            )

        updated = False
        marker = str(getattr(record, "session_marker", "") or "")
        pending_raw = getattr(agent, "_accumulated_tool_rounds_raw", None)
        if isinstance(pending_raw, list):
            for entry in pending_raw:
                if _match_entry(entry):
                    entry["output"] = final
                    if marker:
                        entry["marker"] = marker
                    updated = True

        hist = getattr(agent, "conversation_history", None)
        if isinstance(hist, list):
            for msg in reversed(hist):
                if not isinstance(msg, dict):
                    continue
                if str(msg.get("role") or "").strip().lower() != "assistant":
                    continue
                raw_list = msg.get("_tool_rounds_raw")
                if not isinstance(raw_list, list):
                    continue
                hit = False
                for entry in raw_list:
                    if _match_entry(entry):
                        entry["output"] = final
                        if marker:
                            entry["marker"] = marker
                        hit = True
                if hit:
                    updated = True
                    break

        if updated:
            sync = getattr(agent, "_sync_active_chat_messages", None)
            if callable(sync):
                try:
                    sync()
                except Exception:
                    pass
        if not updated and _attempt < 4:
            # The runtime loop records the tool round (raw entry) right after
            # the shell tool returns.  For very fast tasks the finalizer can
            # run before that record lands, so retry briefly instead of losing
            # the write-back entirely.
            threading.Timer(
                0.5,
                lambda: self._update_round_output(record, _attempt + 1),
            ).start()
        return updated


def _failure_detail(record: BackgroundTaskRecord) -> str:
    if record.status == BG_TASK_STATUS_KILLED:
        if record.pause_interrupt:
            return "background task interrupted by user (supplement pending)"
        return "background task killed (by user or background_task_kill)"
    if record.status == BG_TASK_STATUS_FAILED:
        tail = str(record.final_out or "").strip().splitlines()
        detail = tail[-1][:400] if tail else ""
        return f"command exited with code {record.return_code}" + (f": {detail}" if detail else "")
    return ""


def _write_bg_output_file(agent: Any, task_id: str, out: str) -> str:
    """Write the full output to a sidecar file; return its path or ""."""
    # Only write when the agent has a chat data directory (the normal GUI/CLI
    # path).  Without a chat manager (unit tests / minimal harness) skip the
    # write entirely — never fall back to the process temp dir, which can be
    # remapped by sandboxing and hang file operations.
    try:
        chat_mgr = getattr(agent, "_chat_state_manager", None)
        chat_id = str(getattr(agent, "active_chat_id", "") or "")
        if chat_mgr is not None and chat_id:
            data_dir = chat_mgr.chat_data_dir_for_chat(chat_id)
            if data_dir is not None:
                path = Path(data_dir) / f"bg_task_{task_id}.txt"
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with open(path, "w", encoding="utf-8", errors="replace") as fh:
                        fh.write(str(out or ""))
                    return str(path)
                except Exception:
                    return ""
    except Exception:
        return ""
    return ""


def format_task_summary(task_id: str, status: str, return_code: Optional[int]) -> str:
    """One-line human summary used by the TUI completion notice."""
    rc = "" if return_code is None else f" rc={return_code}"
    return f"[bg task {task_id} {status}{rc}]"
