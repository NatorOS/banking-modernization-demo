import tempfile
import unittest
from pathlib import Path

from modern import server
from scripts import dashboard

ROOT = Path(__file__).resolve().parents[1]


class DashboardLauncherTests(unittest.TestCase):
    def test_serves_arc_build_when_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            build = Path(tmp) / "index.html"
            build.write_text("<html></html>")
            self.assertEqual(dashboard.select_dashboard(build), build)

    def test_falls_back_to_classic_dashboard(self):
        missing = Path(tempfile.gettempdir()) / "no-such-arc-build" / "index.html"
        self.assertEqual(dashboard.select_dashboard(missing), server.DASHBOARD)

    def test_arc_page_keeps_server_substitution_tokens(self):
        # modern.server injects the backend label and initial namespace by plain string replacement.
        page = (ROOT / "web" / "index.html").read_text()
        self.assertIn("__BACKEND_LABEL__", page)
        self.assertIn('value="demo-local"', page)

    def test_lambda_package_excludes_the_launcher_and_web_build(self):
        from scripts.package_lambda import INCLUDE
        self.assertNotIn("scripts", {directory for directory, _ in INCLUDE})
        self.assertNotIn("web", {directory for directory, _ in INCLUDE})


if __name__ == "__main__":
    unittest.main()
