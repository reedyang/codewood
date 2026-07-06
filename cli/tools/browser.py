"""GUI-only embedded-browser tools.

These tools let the model drive the desktop GUI's embedded browser. They are
only exposed when running under the GUI (``agent._browser_dispatch`` is set by
the serve app). Each tool forwards a command to the frontend BrowserPanel and
returns its result.

Capability note: the embedded browser is an iframe. Navigation control is fully
supported, but reading DOM / console / evaluating script only works on pages
WE generate (the HTML-preview pages, which carry a same-origin bridge). For
arbitrary external (cross-origin) sites these reads return an explicit error,
because the browser security model forbids a parent frame from inspecting a
cross-origin child frame.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from .base import BaseTool


def _dispatch(agent: Any, action: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    fn = getattr(agent, "_browser_dispatch", None)
    if not callable(fn):
        return {"success": False, "error": "browser is not available (GUI only)"}
    try:
        return fn(action, payload or {})
    except Exception as exc:  # never let a transport error crash the loop
        return {"success": False, "error": str(exc)}


class BrowserOpenTool(BaseTool):
    name = "browser_open"
    description = "Open a URL in the embedded browser tab to show a web page."
    requires_gui = True
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The URL to open (http/https; a bare host is upgraded to https).",
            },
        },
        "required": ["url"],
    }

    def execute(self, agent: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        url = str((params or {}).get("url") or "").strip()
        if not url:
            return {"success": False, "error": "missing url"}
        return _dispatch(agent, "open", {"url": url})


class BrowserPreviewFileTool(BaseTool):
    name = "browser_preview_file"
    description = "Open a local .html/.htm file in the embedded browser tab."
    requires_gui = True
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path to a local .html/.htm file (absolute or workspace-relative).",
            },
        },
        "required": ["path"],
    }

    def execute(self, agent: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        path = str((params or {}).get("path") or "").strip()
        if not path:
            return {"success": False, "error": "missing path"}
        fn = getattr(agent, "_browser_preview_file", None)
        if not callable(fn):
            return {"success": False, "error": "browser is not available (GUI only)"}
        try:
            return fn(path)
        except Exception as exc:
            return {"success": False, "error": str(exc)}


class BrowserCloseTool(BaseTool):
    name = "browser_close"
    description = "Close the page in the embedded browser tab."
    requires_gui = True
    parameters: Dict[str, Any] = {"type": "object", "properties": {}}

    def execute(self, agent: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        return _dispatch(agent, "close")


class BrowserRefreshTool(BaseTool):
    name = "browser_refresh"
    description = "Reload the page in the embedded browser tab."
    requires_gui = True
    parameters: Dict[str, Any] = {"type": "object", "properties": {}}

    def execute(self, agent: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        return _dispatch(agent, "refresh")


class BrowserGetUrlTool(BaseTool):
    name = "browser_get_url"
    description = "Return the current URL in the embedded browser tab."
    requires_gui = True
    parameters: Dict[str, Any] = {"type": "object", "properties": {}}

    def execute(self, agent: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        return _dispatch(agent, "get_url")


class BrowserReadDomTool(BaseTool):
    name = "browser_read_dom"
    description = "Read the current page's DOM (outer HTML). Only works for same-origin pages."
    requires_gui = True
    parameters: Dict[str, Any] = {"type": "object", "properties": {}}

    def execute(self, agent: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        return _dispatch(agent, "read_dom")


class BrowserReadConsoleTool(BaseTool):
    name = "browser_read_console"
    description = "Read captured console output (log/info/warn/error). Only works for same-origin pages."
    requires_gui = True
    parameters: Dict[str, Any] = {"type": "object", "properties": {}}

    def execute(self, agent: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        return _dispatch(agent, "read_console")


class BrowserEvalTool(BaseTool):
    name = "browser_eval"
    description = "Evaluate a JavaScript expression in the current page. Only works for same-origin pages."
    requires_gui = True
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "script": {
                "type": "string",
                "description": "A JavaScript expression to evaluate in the page.",
            },
        },
        "required": ["script"],
    }

    def execute(self, agent: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        script = str((params or {}).get("script") or "")
        if not script.strip():
            return {"success": False, "error": "missing script"}
        return _dispatch(agent, "eval", {"script": script})
