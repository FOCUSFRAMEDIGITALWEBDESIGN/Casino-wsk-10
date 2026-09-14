from pathlib import Path
import sqlite3
import tempfile
import unittest
from paperbot.store import Store


class UpgradeTests(unittest.TestCase):
    def test_snapshot_preserves_journal_before_pause_and_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "paperbot.sqlite3")
            try:
                store.set("enabled", True)
                store.session("2026-09-14", "25000")
                store.halt("2026-09-14")
                store.reserve("test-cid", "AAA", "entry", "2026-09-14", {"qty": "10"})
                path = store.backup_before_v2()
                store.set("enabled", False)
                self.assertEqual(store.backup_before_v2(), path)
                backup = sqlite3.connect(path)
                try:
                    self.assertEqual(backup.execute("SELECT value FROM settings WHERE key='enabled'").fetchone()[0], "true")
                    self.assertEqual(backup.execute("SELECT halted FROM days").fetchone()[0], 1)
                    self.assertEqual(backup.execute("SELECT cid FROM intents").fetchone()[0], "test-cid")
                    self.assertEqual(backup.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                finally:
                    backup.close()
            finally:
                store.close()

    def test_existing_v2_skips_migration_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "paperbot.sqlite3")
            try:
                store.set("strategy_version", "orb-v2")
                self.assertIsNone(store.backup_before_v2())
                self.assertFalse(store.path.with_name("paperbot-before-v2.sqlite3").exists())
            finally:
                store.close()
