from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parent.parent / "agent_broker_v1"
CONFIG_LOCAL_PATH = PACKAGE_ROOT / "config_local.py"


class ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        if CONFIG_LOCAL_PATH.exists():
            CONFIG_LOCAL_PATH.unlink()
        importlib.invalidate_caches()
        sys.modules.pop("agent_broker_v1.config_local", None)
        sys.modules.pop("agent_broker_v1.config", None)

    def tearDown(self) -> None:
        if CONFIG_LOCAL_PATH.exists():
            CONFIG_LOCAL_PATH.unlink()
        importlib.invalidate_caches()
        sys.modules.pop("agent_broker_v1.config_local", None)
        sys.modules.pop("agent_broker_v1.config", None)

    def test_config_uses_defaults_when_no_local_override_exists(self) -> None:
        importlib.invalidate_caches()
        config = importlib.import_module("agent_broker_v1.config")

        self.assertEqual(
            config.BROKER_CFG_V1,
            {
                "allowlist": ["echo", "pwd", "sleep"],
            },
        )

    def test_config_local_overrides_base_config(self) -> None:
        CONFIG_LOCAL_PATH.write_text(
            'BROKER_CFG_V1 = {"allowlist": ["python3"], "extra": {"enabled": True}}\n',
            encoding="utf-8",
        )

        importlib.invalidate_caches()
        config = importlib.import_module("agent_broker_v1.config")

        self.assertEqual(
            config.BROKER_CFG_V1,
            {
                "allowlist": ["python3"],
                "extra": {"enabled": True},
            },
        )


if __name__ == "__main__":
    unittest.main()
