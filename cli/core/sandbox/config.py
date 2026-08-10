"""Persist and apply sandbox settings (``config.jsonc`` keys)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from . import (
    DEFAULT_SANDBOX_LEVEL,
    DEFAULT_SANDBOX_NETWORK,
    normalize_sandbox_level,
    normalize_sandbox_network,
)

CONFIG_KEY_LEVEL = "sandbox_level"
CONFIG_KEY_NETWORK = "sandbox_network"


def read_sandbox_settings(config_dir: Any) -> Dict[str, Any]:
    """Read sandbox settings from ``config.jsonc`` (no agent required)."""
    level = DEFAULT_SANDBOX_LEVEL
    network = DEFAULT_SANDBOX_NETWORK
    try:
        from ..config.config_jsonc import CONFIG_JSONC_FILENAME, load_config_jsonc

        cfg_path = Path(config_dir) / CONFIG_JSONC_FILENAME
        if cfg_path.exists():
            cfg = load_config_jsonc(cfg_path) or {}
            if isinstance(cfg, dict):
                level = normalize_sandbox_level(cfg.get(CONFIG_KEY_LEVEL))
                if CONFIG_KEY_NETWORK in cfg:
                    network = normalize_sandbox_network(cfg.get(CONFIG_KEY_NETWORK))
    except Exception:
        pass
    return {CONFIG_KEY_LEVEL: level, CONFIG_KEY_NETWORK: network}


def apply_to_agent(agent: Any) -> None:
    """Set ``agent.sandbox_level`` / ``agent.sandbox_network`` from config."""
    settings = read_sandbox_settings(getattr(agent, "config_dir", None))
    try:
        agent.sandbox_level = settings[CONFIG_KEY_LEVEL]
        agent.sandbox_network = settings[CONFIG_KEY_NETWORK]
    except Exception:
        pass


def persist_sandbox_settings(
    config_dir: Any, level: Optional[str] = None, network: Optional[bool] = None
) -> Dict[str, Any]:
    """Persist sandbox settings to ``config.jsonc``; return the normalized pair.

    Only keys explicitly passed are touched; the rest of the config file is
    preserved. Raises on invalid input so callers can surface a 400.
    """
    from ..config.config_jsonc import (
        CONFIG_JSONC_FILENAME,
        load_config_jsonc,
        save_config_jsonc,
    )

    normalized: Dict[str, Any] = {}
    if level is not None:
        raw = str(level).strip().lower()
        if raw not in (
            "read_only",
            "read-only",
            "workspace_write",
            "workspace-write",
            "full_access",
            "danger-full-access",
            "full",
            "unrestricted",
        ):
            raise ValueError(f"invalid sandbox level: {level!r}")
        value = normalize_sandbox_level(level)
        normalized[CONFIG_KEY_LEVEL] = value
    if network is not None:
        if not isinstance(network, bool):
            raise ValueError("sandbox_network must be a boolean")
        normalized[CONFIG_KEY_NETWORK] = network

    cfg_path = Path(config_dir) / CONFIG_JSONC_FILENAME
    cfg_data: Dict[str, Any] = {}
    if cfg_path.exists():
        try:
            loaded = load_config_jsonc(cfg_path) or {}
            if isinstance(loaded, dict):
                cfg_data = dict(loaded)
        except Exception:
            cfg_data = {}
    cfg_data.update(normalized)
    save_config_jsonc(cfg_path, cfg_data)
    return {
        CONFIG_KEY_LEVEL: normalize_sandbox_level(cfg_data.get(CONFIG_KEY_LEVEL)),
        CONFIG_KEY_NETWORK: (
            normalize_sandbox_network(cfg_data[CONFIG_KEY_NETWORK])
            if CONFIG_KEY_NETWORK in cfg_data
            else DEFAULT_SANDBOX_NETWORK
        ),
    }
