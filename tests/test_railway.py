from pathlib import Path
import unittest
from unittest.mock import patch
from paperbot.config import resolve_data_dir


class RailwayStorageTests(unittest.TestCase):
    def test_missing_volume_prevents_ephemeral_trading_journal(self):
        with patch.dict("os.environ", {"RAILWAY_PROJECT_ID": "test-project", "DATA_DIR": "/data"}, clear=True):
            with self.assertRaisesRegex(ValueError, "Volume fehlt"):
                resolve_data_dir()

    def test_paths_outside_volume_are_rejected(self):
        for path in ("/tmp/trades", "/data/../other", "/database"):
            with self.subTest(path=path), patch.dict("os.environ", {
                "RAILWAY_SERVICE_ID": "test-service", "RAILWAY_VOLUME_MOUNT_PATH": "/data", "DATA_DIR": path
            }, clear=True):
                with self.assertRaisesRegex(ValueError, "innerhalb"):
                    resolve_data_dir()

    def test_volume_default_and_subdirectory_are_supported(self):
        with patch.dict("os.environ", {"RAILWAY_PROJECT_ID": "test-project", "RAILWAY_VOLUME_MOUNT_PATH": "/data"}, clear=True):
            self.assertEqual(resolve_data_dir(), Path("/data"))
        with patch.dict("os.environ", {
            "RAILWAY_PROJECT_ID": "test-project", "RAILWAY_VOLUME_MOUNT_PATH": "/data", "DATA_DIR": "/data/trades"
        }, clear=True):
            self.assertEqual(resolve_data_dir(), Path("/data/trades"))

    def test_local_and_sparked_start_keep_original_default(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(resolve_data_dir(), Path("data"))
