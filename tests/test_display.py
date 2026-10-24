"""Tests of the text shown in the plugin's widgets.

They need PyQGIS (the module imports ``qgis.core``) and are skipped without it.
"""

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

HAVE_QGIS = True
try:
    for candidate in ("/usr/share/qgis/python/plugins",):
        if os.path.isdir(candidate) and candidate not in sys.path:
            sys.path.append(candidate)
    from fricmaps_plugin.core import utils
except Exception:  # pragma: no cover - depends on the interpreter
    HAVE_QGIS = False


@unittest.skipUnless(HAVE_QGIS, "PyQGIS not available")
class DisplayTextTest(unittest.TestCase):
    MESSAGE = "ℹ️ ⚠️ Hydrography processing failed"  # both symbols carry U+FE0F

    def test_variation_selector_is_removed_on_macos(self):
        # Qt 5 on macOS draws an empty box instead of the whole line otherwise.
        with mock.patch.object(utils.sys, "platform", "darwin"):
            shown = utils.display_text(self.MESSAGE)
        self.assertNotIn("️", shown)
        self.assertEqual(shown, "ℹ ⚠ Hydrography processing failed")

    def test_text_is_unchanged_elsewhere(self):
        for platform in ("linux", "win32"):
            with mock.patch.object(utils.sys, "platform", platform):
                self.assertEqual(utils.display_text(self.MESSAGE), self.MESSAGE)


if __name__ == "__main__":
    unittest.main()
