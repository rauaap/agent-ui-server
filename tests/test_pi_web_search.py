"""Execute the vendored TypeScript search extension without provider calls."""

import shutil
import subprocess
import unittest
from pathlib import Path


class PiWebSearchTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("pi") and shutil.which("node"), "Pi/Node not accessible")
    def test_cleanup_preserves_provider_behavior_and_surfaces_errors(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [
                shutil.which("node"),
                str(root / "tests/fixtures/pi_web_search_test.mjs"),
                str(Path(shutil.which("pi")).resolve()),
                str(root / "src/agent_ui_server/pi_web_search"),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("pi-web-search cleanup checks passed", result.stdout)
