#!/usr/bin/env python3
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as module


class BackupAgeTest(unittest.TestCase):
    def test_manifest_iso_timestamp_with_compact_offset_is_recognized(self):
        timestamp = datetime.now(timezone(timedelta(hours=2))).strftime(
            "%Y-%m-%dT%H:%M:%S%z"
        )

        result = module.backup_age(timestamp)

        self.assertEqual(result["days"], 0)
        self.assertEqual(result["label"], "Today")
        self.assertFalse(result["stale"])

    def test_manifest_iso_timestamp_with_colon_offset_is_recognized(self):
        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")

        result = module.backup_age(timestamp)

        self.assertEqual(result["days"], 0)
        self.assertEqual(result["label"], "Today")

    def test_utc_z_timestamp_is_recognized(self):
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        self.assertEqual(module.backup_age(timestamp)["label"], "Today")

    def test_legacy_timestamp_remains_supported(self):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        self.assertEqual(module.backup_age(timestamp)["label"], "Today")

    def test_invalid_timestamp_remains_unknown(self):
        self.assertEqual(
            module.backup_age("not-a-date"),
            {"days": None, "label": "Unknown age", "stale": True},
        )


if __name__ == "__main__":
    unittest.main()
