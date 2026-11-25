"""Tests for the shared settings loader."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from shared.config import ConfigError, load_config  # noqa: E402


class LoadConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="immich-utils-config-"))
        self.root = self.tmp / "root.json"
        self.script_dir = self.tmp / "script"
        self.script_dir.mkdir()
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        for k in ("IMMICH_URL", "IMMICH_API_KEY", "IMMICH_BACKUP_ROOT"):
            os.environ.pop(k, None)

    def write(self, path, data):
        path.write_text(json.dumps(data))

    def test_script_config_overrides_shared(self):
        self.write(self.root, {"server_url": "https://a.example/api/", "api_key": "shared"})
        self.write(self.script_dir / "config.json", {"api_key": "script", "extra": 1})
        cfg = load_config(self.script_dir, root_config=self.root)
        self.assertEqual(cfg, {"server_url": "https://a.example", "api_key": "script", "extra": 1})

    def test_env_and_cli_win(self):
        self.write(self.root, {"server_url": "https://a.example", "api_key": "shared", "backup_root": "x"})
        os.environ["IMMICH_API_KEY"] = "env"
        os.environ["IMMICH_BACKUP_ROOT"] = "env-root"
        cfg = load_config(self.script_dir, cli_values={"server_url": "http://b.example", "api_key": None},
                          extra_env={"backup_root": "IMMICH_BACKUP_ROOT"}, root_config=self.root)
        self.assertEqual(cfg["server_url"], "http://b.example")
        self.assertEqual(cfg["api_key"], "env")
        self.assertEqual(cfg["backup_root"], "env-root")

    def test_missing_and_invalid(self):
        with self.assertRaises(ConfigError):
            load_config(self.script_dir, root_config=self.root)
        self.write(self.root, {"server_url": "immich.local", "api_key": "k"})
        with self.assertRaises(ConfigError):
            load_config(self.script_dir, root_config=self.root)
        with self.assertRaises(ConfigError):
            load_config(self.script_dir, config_arg=str(self.tmp / "nope.json"), root_config=self.root)


if __name__ == "__main__":
    unittest.main()
