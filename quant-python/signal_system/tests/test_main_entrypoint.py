from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import unittest


SIGNAL_SYSTEM_ROOT = Path(__file__).resolve().parents[1]


class MainEntrypointTests(unittest.TestCase):
    def test_help_works_from_signal_system_directory_without_pythonpath(self):
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        result = subprocess.run(
            [sys.executable, "main.py", "--help"],
            cwd=SIGNAL_SYSTEM_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("A股缠论多周期信号监控", result.stdout)


if __name__ == "__main__":
    unittest.main()
