"""Tool: webfetch.

Fetch content from an HTTP(S) URL and return it as text, markdown, or HTML.
Markdown is the default format.
"""
from __future__ import annotations

import html
import html.parser
import re
from typing import Any, Dict, List, Optional, Tuple

import requests

from .base import BaseTool

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/143.0.0.0 Safari/537.36"
)
_DEFAULT_TIMEOUT = 30
_MAX_TIMEOUT = 120
_MAX_RESPONSE_BYTES = 5 * 1024 * 1024


def _accept_header(fmt: str) -> str:
    if fmt == "markdown":
        return "text/markdown;q=1.0, text/x-markdown;q=0.9, text/plain;q=0.8, text/html;q=0.7, */*;q=0.1"
    if fmt == "text":
        return "text/plain;q=1.0, text/markdown;q=0.9, text/html;q=0.8, */*;q=0.1"
    return "text/html;q=1.0, application/xhtml+xml;q=0.9, text/plain;q=0.8, text/markdown;q=0.7, */*;q=0.1"


# --- HTML parsing (stdlib only) -------------------------------------------

class _MDConverter(html.parser.HTMLParser):
    """Convert HTML to Markdown using Python's built-in HTMLParser."""

    _SKIP_TAGS = {"script", "style", "noscript", "iframe", "object", "embed"}
    _VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input",
                  "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self, target_format: str = "markdown") -> None:
        super().__init__(convert_charrefs=True)
        self._fmt = target_format
        self._out: List[str] = []
        self._tag_stack: List[str] = []
        self._skip_depth = 0
        self._pre = False
        self._list_type: List[Optional[str]] = []
        self._table_cells: List[str] = []
        self._in_table = False

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()
        # Void elements (no closing tag) never nest content.
        if tag in self._VOID_TAGS:
            return
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        self._tag_stack.append(tag)
        if tag == "pre":
            self._pre = True
        elif tag in ("ul", "ol"):
            self._list_type.append("1." if tag == "ol" else "-")
            self._out.append("\n")
        elif tag == "li":
            self._out.append(self._list_type[-1] + " ")
        elif tag == "br":
            self._out.append("\n")
        elif tag == "hr":
            self._out.append("\n---\n")
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._out.append("\n" + "#" * int(tag[1]) + " ")
        elif tag == "p":
            self._out.append("\n\n")
        elif tag == "blockquote":
            self._out.append("\n")
        elif tag in ("th", "td"):
            self._table_cells.append("")
        elif tag == "tr":
            self._table_cells = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self._SKIP_TAGS:
            self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if self._tag_stack and self._tag_stack[-1] == tag:
            self._tag_stack.pop()
        if tag == "pre":
            self._pre = False
            self._out.append("\n```\n")
        elif tag == "li":
            self._out.append("\n")
        elif tag in ("ul", "ol"):
            self._list_type.pop() if self._list_type else None
        elif tag == "p":
            self._out.append("\n\n")
        elif tag == "blockquote":
            self._out.append("\n")
        elif tag == "a":
            pass
        elif tag in ("th", "td"):
            pass
        elif tag == "tr":
            self._out.append("| " + " | ".join(self._table_cells) + " |\n")
        elif tag == "table":
            self._out.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._pre:
            self._out.append(data)
            return
        # Determine active inline formatting
        em = "strong" in self._tag_stack and "em" in self._tag_stack
        strong = "strong" in self._tag_stack or "b" in self._tag_stack
        italic = "em" in self._tag_stack or "i" in self._tag_stack
        code = "code" in self._tag_stack
        text = data.strip() if not self._table_cells else data.strip()
        if not text:
            return
        if code:
            wrapped = f"`{text}`"
        elif strong and italic:
            wrapped = f"***{text}***"
        elif strong:
            wrapped = f"**{text}**"
        elif italic:
            wrapped = f"*{text}*"
        else:
            wrapped = text
        if self._table_cells:
            self._table_cells[-1] += wrapped
        else:
            self._out.append(wrapped)

    def handle_startendtag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if tag == "br":
            self._out.append("\n")
        elif tag == "hr":
            self._out.append("\n---\n")

    def result(self) -> str:
        raw = "".join(self._out).strip()
        # Collapse 3+ newlines to 2
        return re.sub(r"\n{3,}", "\n\n", raw)


def _extract_body(html: str) -> str:
    """Return the <body> or <main> content of an HTML page, or the full string."""
    for tag in ("main", "body", "article"):
        m = re.search(f"<{tag}[^>]*>([\\s\\S]*)</{tag}>", html, re.IGNORECASE)
        if m:
            return m.group(1)
    return html


def _html_to_text(html: str) -> str:
    html = _extract_body(html)
    text = re.sub(r"<script[^>]*>[\s\S]*?</script>", "", html, flags=re.IGNORECASE)
    text = re.sub(r"<style[^>]*>[\s\S]*?</style>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text) if hasattr(html, "unescape") else text
    return re.sub(r"\s+", " ", text).strip()


def _html_to_markdown(html: str) -> str:
    html = _extract_body(html)
    # Strip script/style before feeding to the converter
    html = re.sub(r"<script[^>]*>[\s\S]*?</script>", "", html, flags=re.IGNORECASE)
    html = re.sub(r"<style[^>]*>[\s\S]*?</style>", "", html, flags=re.IGNORECASE)
    parser = _MDConverter(target_format="markdown")
    parser.feed(html)
    parser.close()
    return parser.result()


class WebFetchTool(BaseTool):
    name = "webfetch"
    description = (
        "Fetch content from an HTTP or HTTPS URL and return it as text, "
        "markdown, or HTML. Markdown is the default. "
        "Use a more targeted tool when one is available. This tool is read-only."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The HTTP or HTTPS URL to fetch content from",
            },
            "format": {
                "type": "string",
                "enum": ["text", "markdown", "html"],
                "description": "The format to return the content in (default: markdown)",
            },
            "timeout": {
                "type": "integer",
                "description": f"Optional timeout in seconds (max: {_MAX_TIMEOUT})",
            },
        },
        "required": ["url"],
    }

    def execute(self, agent: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        params = params if isinstance(params, dict) else {}
        url = str(params.get("url") or "").strip()
        if not url:
            return {"success": False, "error": "Missing required parameter: url"}
        if not url.startswith("http://") and not url.startswith("https://"):
            return {"success": False, "error": "URL must use http:// or https://"}

        fmt = str(params.get("format") or "markdown").strip().lower()
        if fmt not in ("text", "markdown", "html"):
            fmt = "markdown"

        timeout = _DEFAULT_TIMEOUT
        try:
            raw = int(params.get("timeout", 0) or 0)
            if 0 < raw <= _MAX_TIMEOUT:
                timeout = raw
        except (ValueError, TypeError):
            pass

        def _do_fetch(verify_ssl: bool = True) -> requests.Response:
            r = requests.get(
                url,
                headers={
                    "User-Agent": _BROWSER_UA,
                    "Accept": _accept_header(fmt),
                    "Accept-Language": "en-US,en;q=0.9",
                },
                timeout=timeout,
                stream=True,
                verify=verify_ssl,
            )
            r.raise_for_status()
            return r

        try:
            resp = _do_fetch(verify_ssl=True)
        except requests.exceptions.SSLError:
            try:
                import urllib3
                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
                resp = _do_fetch(verify_ssl=False)
            except requests.exceptions.RequestException as e2:
                return {"success": False, "error": f"SSL error (and retry failed): {e2}"}
        except requests.exceptions.Timeout:
            return {"success": False, "error": f"Request timed out after {timeout}s"}
        except requests.exceptions.ConnectionError as e:
            return {"success": False, "error": f"Connection error: {e}"}
        except requests.exceptions.HTTPError as e:
            return {"success": False, "error": f"HTTP error: {e}"}
        except requests.exceptions.RequestException as e:
            return {"success": False, "error": f"Request failed: {e}"}

        content_type = resp.headers.get("content-type", "") or ""
        raw_body = b""
        for chunk in resp.iter_content(chunk_size=65536, decode_unicode=False):
            raw_body += chunk
            if len(raw_body) > _MAX_RESPONSE_BYTES:
                return {"success": False, "error": f"Response too large (exceeds {_MAX_RESPONSE_BYTES} byte limit)"}

        body = raw_body.decode("utf-8", errors="replace")

        mime = content_type.split(";", 1)[0].strip().lower() if content_type else ""

        if mime.startswith("image/"):
            return {"success": False, "error": f"Unsupported fetched image content type: {mime}"}

        is_html = "html" in mime or (not mime and ("<html" in body[:500] or "<!DOCTYPE" in body[:500].upper()))
        if is_html:
            if fmt == "markdown":
                output = _html_to_markdown(body)
            elif fmt == "text":
                output = _html_to_text(body)
            else:
                output = body
        else:
            output = body

        return {
            "success": True,
            "url": url,
            "contentType": content_type,
            "format": fmt,
            "output": output,
        }
