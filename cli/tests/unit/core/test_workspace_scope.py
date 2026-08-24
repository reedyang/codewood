"""Unit tests for the shared workspace-context resolution helpers."""

from __future__ import annotations

import unittest
import os
from pathlib import Path

from cli.core.workspace_scope import (
    effective_workspace_config_dir,
    effective_workspace_id,
    effective_workspace_name,
    effective_workspace_root,
    effective_work_directory,
)


class _OverrideAgent:
    """Fake agent exposing the real ``_effective_*`` accessor surface."""

    def __init__(self, ctx: dict, globals_: dict) -> None:
        self._ctx = ctx
        self._globals = globals_

    def _effective_workspace_root(self) -> Path:
        raw = self._ctx.get("workspace_root")
        return Path(raw) if raw else Path(self._globals["workspace_root"])

    def _effective_workspace_config_dir(self) -> Path:
        raw = self._ctx.get("workspace_config_dir")
        return Path(raw) if raw else Path(self._globals["workspace_config_dir"])

    def _effective_workspace_id(self) -> str:
        return str(self._ctx.get("workspace_id") or self._globals["workspace_id"])

    def _effective_workspace_name(self) -> str:
        return str(self._ctx.get("workspace_name") or self._globals["workspace_name"])

    def _effective_work_directory(self) -> Path:
        raw = self._ctx.get("work_directory")
        return Path(raw) if raw else Path(self._globals["work_directory"])


class WorkspaceScopeHelperTests(unittest.TestCase):
    def test_prefers_override_accessor_over_global_attributes(self):
        agent = _OverrideAgent(
            ctx={
                "workspace_id": "ws_b",
                "workspace_name": "B",
                "workspace_root": r"D:\ws_b",
                "workspace_config_dir": r"D:\ws_b\.codewood",
                "work_directory": r"D:\ws_b",
            },
            globals_={
                "workspace_id": "ws_a",
                "workspace_name": "A",
                "workspace_root": r"D:\ws_a",
                "workspace_config_dir": r"D:\ws_a\.codewood",
                "work_directory": r"D:\ws_a",
            },
        )
        self.assertEqual(effective_workspace_root(agent), Path(r"D:\ws_b"))
        self.assertEqual(
            effective_workspace_config_dir(agent), Path(r"D:\ws_b\.codewood")
        )
        self.assertEqual(effective_workspace_id(agent), "ws_b")
        self.assertEqual(effective_workspace_name(agent), "B")
        self.assertEqual(effective_work_directory(agent), Path(r"D:\ws_b"))

    def test_falls_back_to_global_attributes_without_accessors(self):
        agent = type(
            "Bare",
            (),
            {
                "workspace_root": Path(r"D:\ws_a"),
                "workspace_config_dir": Path(r"D:\ws_a\.codewood"),
                "workspace_id": "ws_a",
                "workspace_name": "A",
                "work_directory": Path(r"D:\ws_a"),
            },
        )()
        self.assertEqual(effective_workspace_root(agent), Path(r"D:\ws_a").resolve())
        self.assertEqual(
            effective_workspace_config_dir(agent), Path(r"D:\ws_a\.codewood")
        )
        self.assertEqual(effective_workspace_id(agent), "ws_a")
        self.assertEqual(effective_workspace_name(agent), "A")
        self.assertEqual(effective_work_directory(agent), Path(r"D:\ws_a"))

    @unittest.skipUnless(os.name == "nt", "Windows drive-letter work directory")
    def test_falls_back_to_work_directory_when_root_missing(self):
        agent = type("Bare", (), {"work_directory": Path(r"D:\wd")})()
        self.assertEqual(effective_workspace_root(agent), Path(r"D:\wd").resolve())


if __name__ == "__main__":
    unittest.main()
