"""Startup Tips configuration loader."""

from __future__ import annotations

import json
import random
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


DEFAULT_STARTUP_TIP = "Use /workspace to manage workspaces."
DEFAULT_STARTUP_TIP_ENTRY: Dict[str, Any] = {
    "text": DEFAULT_STARTUP_TIP,
    "highlights": ["/workspace"],
    "id": "manage_workspaces",
}


def startup_tips_config_path() -> Path:
    return Path(__file__).resolve().parent / "startup_tips.json"


def _normalize_tip_entry(item: Any) -> Optional[Dict[str, Any]]:
    if isinstance(item, str):
        text = item.strip()
        if not text:
            return None
        return {"text": text, "highlights": []}
    if not isinstance(item, dict):
        return None

    text = str(item.get("text") or "").strip()
    if not text:
        return None
    tip_id = str(item.get("id") or "").strip() or None
    highlights_raw = item.get("highlights", [])
    highlights: List[str] = []
    if isinstance(highlights_raw, list):
        for h in highlights_raw:
            hs = str(h or "").strip()
            if hs:
                highlights.append(hs)

    entry: Dict[str, Any] = {"text": text, "highlights": highlights}
    if tip_id:
        entry["id"] = tip_id
    return entry


def _translate_tip_entry(entry: Dict[str, Any], language: Optional[str] = None) -> Dict[str, Any]:
    tip_text = str(entry.get("text") or "")
    tip_id = str(entry.get("id") or "").strip()
    if tip_id:
        try:
            from ..core.localization import translate

            tip_text = translate(f"startup.tip.{tip_id}", language, fallback=tip_text)
        except Exception:
            pass
    result = dict(entry)
    result["text"] = tip_text
    return result


def _default_startup_tip_entry(language: Optional[str] = None) -> Dict[str, Any]:
    try:
        from ..core.localization import translate

        tip_text = translate("startup.tip.manage_workspaces", language, fallback=DEFAULT_STARTUP_TIP)
    except Exception:
        tip_text = DEFAULT_STARTUP_TIP
    return {"text": tip_text, "highlights": ["/workspace"], "id": "manage_workspaces"}


def load_startup_tip_entries(path: Optional[Path] = None, language: Optional[str] = None) -> List[Dict[str, Any]]:
    cfg_path = Path(path) if path is not None else startup_tips_config_path()
    raw: object = {}
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return [_translate_tip_entry(_default_startup_tip_entry(language), language)]

    tips_raw: object
    if isinstance(raw, list):
        tips_raw = raw
    elif isinstance(raw, dict):
        tips_raw = raw.get("tips", [])
    else:
        tips_raw = []

    tips: List[Dict[str, Any]] = []
    for item in tips_raw if isinstance(tips_raw, list) else []:
        normalized = _normalize_tip_entry(item)
        if normalized:
            tips.append(_translate_tip_entry(normalized, language))
    return tips or [_translate_tip_entry(_default_startup_tip_entry(language), language)]


def load_startup_tips(path: Optional[Path] = None, language: Optional[str] = None) -> List[str]:
    return [str(item.get("text") or "") for item in load_startup_tip_entries(path=path, language=language)]


def get_random_startup_tip(path: Optional[Path] = None, language: Optional[str] = None) -> str:
    return str(get_random_startup_tip_entry(path=path, language=language).get("text") or DEFAULT_STARTUP_TIP)


def get_random_startup_tip_entry(path: Optional[Path] = None, language: Optional[str] = None) -> Dict[str, Any]:
    tips = load_startup_tip_entries(path=path, language=language)
    if not tips:
        return _default_startup_tip_entry(language)
    return dict(random.choice(tips))


def format_tip_with_highlights(
    text: str,
    highlights: List[str],
    highlight_formatter: Callable[[str], str],
) -> str:
    base = str(text or "")
    if not base:
        return base
    clean_highlights = [str(h or "").strip() for h in (highlights or [])]
    clean_highlights = [h for h in clean_highlights if h]
    if not clean_highlights:
        return base

    # Sort by length to prefer longer matches when terms overlap.
    unique = []
    seen = set()
    for term in sorted(clean_highlights, key=len, reverse=True):
        key = term.lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(term)
    pattern = re.compile("|".join(re.escape(t) for t in unique))
    return pattern.sub(lambda m: highlight_formatter(m.group(0)), base)
