from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Optional

from ..core.config.config_jsonc import CONFIG_JSONC_FILENAME, load_config_jsonc, save_config_jsonc

DEFAULT_DISPLAY_LANGUAGE = "en"
SUPPORTED_DISPLAY_LANGUAGES = ("en", "zh-CN")

_LANGUAGE_ALIASES = {
    "en": "en",
    "english": "en",
    "zh": "zh-CN",
    "zh-cn": "zh-CN",
    "zh_cn": "zh-CN",
    "simplified chinese": "zh-CN",
    "简体中文": "zh-CN",
}

_RESOURCE_DIR = Path(__file__).resolve().parent / "locales"


def normalize_display_language(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    if not text:
        return None
    mapped = _LANGUAGE_ALIASES.get(text.casefold())
    if mapped:
        return mapped
    if text in SUPPORTED_DISPLAY_LANGUAGES:
        return text
    return None


@lru_cache(maxsize=8)
def _load_locale_map(language: str) -> Dict[str, str]:
    lang = normalize_display_language(language) or DEFAULT_DISPLAY_LANGUAGE
    path = _RESOURCE_DIR / f"{lang}.json"
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
    except Exception:
        pass
    return {}


def reload_locales() -> None:
    _load_locale_map.cache_clear()


def _lookup(language: Any, key: str, fallback: str) -> str:
    lang = normalize_display_language(language) or DEFAULT_DISPLAY_LANGUAGE
    locale_map = _load_locale_map(lang)
    value = locale_map.get(str(key))
    if value is None or value == "":
        return str(fallback)
    return str(value)


def translate(key: str, locale: Any, fallback: Optional[str] = None, **kwargs: Any) -> str:
    raw = _lookup(locale, str(key or ""), str(fallback or key or ""))
    if kwargs:
        try:
            return raw.format(**kwargs)
        except Exception:
            return raw
    return raw


def text(key: str, locale: Any, fallback: Optional[str] = None, **kwargs: Any) -> str:
    normalized = normalize_display_language(locale)
    if normalized is None and fallback is not None and normalize_display_language(fallback) is not None:
        raise TypeError("text() now expects (key, language, fallback=None, **kwargs)")
    return translate(str(key or ""), locale, fallback=fallback, **kwargs)


def language_display_name(language: Any) -> str:
    normalized = normalize_display_language(language) or DEFAULT_DISPLAY_LANGUAGE
    if normalized == "zh-CN":
        return "简体中文"
    return "English"


def read_display_language(config_dir: Path) -> str:
    cfg_path = Path(config_dir) / CONFIG_JSONC_FILENAME
    if not cfg_path.exists():
        return DEFAULT_DISPLAY_LANGUAGE
    try:
        data = load_config_jsonc(cfg_path)
    except Exception:
        return DEFAULT_DISPLAY_LANGUAGE
    if not isinstance(data, dict):
        return DEFAULT_DISPLAY_LANGUAGE
    return normalize_display_language(data.get("language")) or DEFAULT_DISPLAY_LANGUAGE


def get_display_language(agent: Any) -> str:
    raw = normalize_display_language(getattr(agent, "display_language", None))
    if raw:
        return raw
    cfg_data = getattr(agent, "_resolved_config_data", None)
    if isinstance(cfg_data, dict):
        raw = normalize_display_language(cfg_data.get("language"))
        if raw:
            return raw
    cfg_dir = getattr(agent, "config_dir", None)
    if cfg_dir is not None:
        return read_display_language(Path(cfg_dir))
    return DEFAULT_DISPLAY_LANGUAGE


def apply_display_language(agent: Any, language: Any) -> Dict[str, Any]:
    normalized = normalize_display_language(language)
    if normalized is None:
        return {"success": False, "error": f"Unsupported display language: {language!r}"}

    cfg_dir = getattr(agent, "config_dir", None)
    if cfg_dir is None:
        return {"success": False, "error": "Agent config_dir is not available"}

    cfg_path = Path(cfg_dir) / CONFIG_JSONC_FILENAME
    cfg_data: Dict[str, Any] = {}
    if cfg_path.exists():
        try:
            loaded = load_config_jsonc(cfg_path)
            if isinstance(loaded, dict):
                cfg_data = dict(loaded)
        except Exception:
            cfg_data = {}

    cfg_data["language"] = normalized
    cfg_data.pop("display_language", None)
    try:
        save_config_jsonc(cfg_path, cfg_data)
    except Exception as e:
        return {"success": False, "error": str(e)}

    try:
        setattr(agent, "display_language", normalized)
    except Exception:
        pass
    try:
        resolved = getattr(agent, "_resolved_config_data", None)
        if isinstance(resolved, dict):
            resolved["language"] = normalized
            resolved.pop("display_language", None)
    except Exception:
        pass

    reload_locales()
    return {"success": True, "path": str(cfg_path), "language": normalized}
