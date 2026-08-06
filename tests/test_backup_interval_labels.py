import unittest

import app as module


class BackupIntervalLabelTests(unittest.TestCase):
    def test_human_readable_day_week_and_month_labels(self):
        self.assertEqual(module.format_backup_interval(24), "Every 1 day")
        self.assertEqual(module.format_backup_interval(72), "Every 3 days")
        self.assertEqual(module.format_backup_interval(168), "Every 1 week")
        self.assertEqual(module.format_backup_interval(336), "Every 2 weeks")
        self.assertEqual(module.format_backup_interval(720), "Every 1 month")

    def test_schedule_accepts_new_week_and_month_presets(self):
        self.assertEqual(module.validate_backup_schedule(interval_hours="336")["interval_hours"], "336")
        self.assertEqual(module.validate_backup_schedule(interval_hours="720")["interval_hours"], "720")


if __name__ == "__main__":
    unittest.main()
