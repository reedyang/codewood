import tempfile
import unittest
from pathlib import Path

from cli.core.sandbox import (
    DEFAULT_SANDBOX_LEVEL,
    DEFAULT_SANDBOX_NETWORK,
    normalize_sandbox_level,
    normalize_sandbox_network,
)
from cli.core.sandbox.config import (
    CONFIG_KEY_LEVEL,
    CONFIG_KEY_NETWORK,
    persist_sandbox_settings,
    read_sandbox_settings,
)
from cli.core.config.config_jsonc import load_config_jsonc


class NormalizeSandboxLevelTests(unittest.TestCase):
    def test_valid_levels(self):
        self.assertEqual(normalize_sandbox_level("read_only"), "read_only")
        self.assertEqual(normalize_sandbox_level("workspace_write"), "workspace_write")
        self.assertEqual(normalize_sandbox_level("full_access"), "full_access")

    def test_aliases(self):
        self.assertEqual(normalize_sandbox_level("read-only"), "read_only")
        self.assertEqual(normalize_sandbox_level("workspace-write"), "workspace_write")
        self.assertEqual(normalize_sandbox_level("danger-full-access"), "full_access")
        self.assertEqual(normalize_sandbox_level("full"), "full_access")
        self.assertEqual(normalize_sandbox_level("unrestricted"), "full_access")

    def test_invalid_falls_back_to_default(self):
        self.assertEqual(normalize_sandbox_level("banana"), DEFAULT_SANDBOX_LEVEL)
        self.assertEqual(normalize_sandbox_level(""), DEFAULT_SANDBOX_LEVEL)
        self.assertEqual(normalize_sandbox_level(None), DEFAULT_SANDBOX_LEVEL)
        self.assertEqual(normalize_sandbox_level(123), DEFAULT_SANDBOX_LEVEL)


class NormalizeSandboxNetworkTests(unittest.TestCase):
    def test_values(self):
        self.assertTrue(normalize_sandbox_network(True))
        self.assertFalse(normalize_sandbox_network(False))
        self.assertTrue(normalize_sandbox_network("true"))
        self.assertTrue(normalize_sandbox_network(1))
        self.assertFalse(normalize_sandbox_network("false"))
        self.assertFalse(normalize_sandbox_network(0))
        self.assertFalse(normalize_sandbox_network(None))
        self.assertFalse(normalize_sandbox_network("banana"))


class PersistSandboxSettingsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.config_dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_read_defaults_when_missing(self):
        settings = read_sandbox_settings(self.config_dir)
        self.assertEqual(settings[CONFIG_KEY_LEVEL], DEFAULT_SANDBOX_LEVEL)
        self.assertEqual(settings[CONFIG_KEY_NETWORK], DEFAULT_SANDBOX_NETWORK)

    def test_persist_roundtrip(self):
        result = persist_sandbox_settings(
            self.config_dir, level="workspace_write", network=False
        )
        self.assertEqual(result[CONFIG_KEY_LEVEL], "workspace_write")
        self.assertFalse(result[CONFIG_KEY_NETWORK])
        settings = read_sandbox_settings(self.config_dir)
        self.assertEqual(settings[CONFIG_KEY_LEVEL], "workspace_write")
        self.assertFalse(settings[CONFIG_KEY_NETWORK])

    def test_preserves_other_keys(self):
        cfg_path = self.config_dir / "config.jsonc"
        cfg_path.write_text('{"execution_policy": "moderate"}\n', encoding="utf-8")
        persist_sandbox_settings(self.config_dir, level="read_only")
        cfg = load_config_jsonc(cfg_path)
        self.assertEqual(cfg.get("execution_policy"), "moderate")
        self.assertEqual(cfg.get("sandbox_level"), "read_only")

    def test_rejects_invalid_level(self):
        with self.assertRaises(ValueError):
            persist_sandbox_settings(self.config_dir, level="banana")

    def test_rejects_non_bool_network(self):
        with self.assertRaises(ValueError):
            persist_sandbox_settings(self.config_dir, network="yes")

    def test_partial_update(self):
        persist_sandbox_settings(self.config_dir, level="workspace_write")
        settings = read_sandbox_settings(self.config_dir)
        self.assertEqual(settings[CONFIG_KEY_NETWORK], DEFAULT_SANDBOX_NETWORK)


if __name__ == "__main__":
    unittest.main()
