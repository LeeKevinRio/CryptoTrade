"""家用真金：ENV_FILE 讀獨立設定檔、keep_awake 非 Windows 不動作"""

import os
import tempfile
import unittest
from unittest.mock import patch

from src.utils.keep_awake import keep_awake


class TestEnvFile(unittest.TestCase):
    def test_env_file_is_used(self):
        from src.utils.config import load_config
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as f:
            f.write("BINANCE_API_KEY=live-key-from-file\nBINANCE_API_SECRET=live-secret\n")
            path = f.name
        try:
            env = {k: v for k, v in os.environ.items() if k not in ("BINANCE_API_KEY", "BINANCE_API_SECRET")}
            env.update({"ENV_FILE": path, "BINANCE_TESTNET": "true"})
            with patch.dict(os.environ, env, clear=True):
                c = load_config()
            self.assertEqual(c["binance"]["api_key"], "live-key-from-file")
        finally:
            os.unlink(path)

    def test_existing_env_wins_over_file(self):
        from src.utils.config import load_config
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as f:
            f.write("BINANCE_API_KEY=from-file\nBINANCE_API_SECRET=s\n")
            path = f.name
        try:
            with patch.dict(os.environ, {"ENV_FILE": path, "BINANCE_API_KEY": "from-env",
                                         "BINANCE_API_SECRET": "s", "BINANCE_TESTNET": "true"}):
                c = load_config()
            self.assertEqual(c["binance"]["api_key"], "from-env")
        finally:
            os.unlink(path)


class TestKeepAwake(unittest.TestCase):
    def test_noop_off_windows(self):
        self.assertFalse(keep_awake())


if __name__ == "__main__":
    unittest.main()
