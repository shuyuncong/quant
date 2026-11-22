import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ledger_heartbeat as heartbeat


class FindStaleTests(unittest.TestCase):
    def test_only_ledgers_behind_the_expected_session_or_unreadable_are_reported(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "fresh.json").write_text(json.dumps({"as_of": "2026-11-20"}))
            (root / "ahead.json").write_text(json.dumps({"as_of": "2026-11-23T15:00:00"}))
            (root / "behind.json").write_text(json.dumps({"last_session": "2026-11-19"}))
            (root / "broken.json").write_text("{")
            problems = heartbeat.find_stale(
                [
                    ("fresh", root / "fresh.json", "as_of"),
                    ("ahead", root / "ahead.json", "as_of"),
                    ("behind", root / "behind.json", "last_session"),
                    ("broken", root / "broken.json", "as_of"),
                    ("missing", root / "missing.json", "as_of"),
                    ("wrong-field", root / "fresh.json", "last_session"),
                ],
                date(2026, 11, 20),
            )
        self.assertEqual([line.split("：")[0] for line in problems], ["behind", "broken", "missing", "wrong-field"])
        self.assertIn("停在 2026-11-19，应到 2026-11-20", problems[0])


if __name__ == "__main__":
    unittest.main()
