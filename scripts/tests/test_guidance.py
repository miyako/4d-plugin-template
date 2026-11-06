"""Offline checks for the canonical guide and source-level example migration."""

from pathlib import Path
import plistlib
import unittest


ROOT = Path(__file__).resolve().parents[2]


class GuidanceTests(unittest.TestCase):
    def test_current_contract_is_linked_before_historical_examples(self):
        guide = (ROOT / ".github/instructions/plugin-dev.instructions.md").read_text()
        self.assertLess(guide.index("Canonical generated pipeline"), guide.index("## Table of Contents"))
        for name in ("test.yml", "bump-version.yml", "release.yml"):
            self.assertIn(f"Historical `{name}` example", guide)
        self.assertIn("explicit release dispatch", guide)
        self.assertIn("not a generation template", guide)
        self.assertIn('MACOSX_BUNDLE_BUNDLE_VERSION "${PLUGIN_VERSION}"', guide)
        self.assertNotIn('MACOSX_BUNDLE_BUNDLE_VERSION "1.0"', guide)

    def test_readme_identifies_real_branch_and_generation_owner(self):
        readme = (ROOT / "README.md").read_text()
        self.assertIn("tree/example", readme)
        self.assertIn("docs/plugin-pipeline.md", readme)
        self.assertIn("updated separately", readme)

    def test_example_overlay_has_versioned_bundle_contract(self):
        cmake = (ROOT / "templates/example-overlay/CMakeLists.txt").read_text()
        self.assertIn('/../VERSION"', cmake)
        self.assertIn('MACOSX_BUNDLE_INFO_PLIST', cmake)
        self.assertIn('MACOSX_BUNDLE_BUNDLE_VERSION "${PLUGIN_VERSION}"', cmake)
        self.assertIn('MACOSX_BUNDLE_SHORT_VERSION_STRING "${PLUGIN_VERSION}"', cmake)
        info = plistlib.loads((ROOT / "templates/example-overlay/Info.plist.in").read_bytes())
        self.assertEqual(info["CFBundlePackageType"], "BNDL")
        self.assertEqual((ROOT / "templates/example-overlay/VERSION").read_bytes(), b"1.0.0\n")


if __name__ == "__main__":
    unittest.main()
