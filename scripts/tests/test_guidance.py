"""Offline checks for the canonical guide and source-level example migration."""

from pathlib import Path
import plistlib
import unittest


ROOT = Path(__file__).resolve().parents[2]


class GuidanceTests(unittest.TestCase):
    def test_current_contract_has_no_competing_inline_workflows(self):
        guide = (ROOT / ".github/instructions/plugin-dev.instructions.md").read_text()
        self.assertLess(guide.index("Canonical generated pipeline"), guide.index("## Table of Contents"))
        for name in ("test.yml", "bump-version.yml", "release.yml"):
            self.assertIn(f"../../templates/plugin-pipeline/{name}.in", guide)
            self.assertNotIn(f"Historical `{name}` example", guide)
        self.assertIn("explicit release dispatch", guide)
        self.assertNotIn("release.yml will pick up the pushed tag", guide)
        self.assertNotIn("used by `bump-version.yml` to tag releases", guide)
        self.assertNotIn("softprops/action-gh-release", guide)
        self.assertNotIn("```yaml\nname: Build", guide)
        self.assertIn('MACOSX_BUNDLE_BUNDLE_VERSION "${PLUGIN_VERSION}"', guide)
        self.assertNotIn('MACOSX_BUNDLE_BUNDLE_VERSION "1.0"', guide)

    def test_vendored_windows_libraries_are_checked_not_assumed_safe(self):
        guide = (ROOT / ".github/instructions/plugin-dev.instructions.md").read_text()
        self.assertNotIn("just symbol stubs, not runtime code", guide)
        self.assertIn("../../scripts/check_windows_libs.py", guide)
        self.assertTrue((ROOT / "scripts/check_windows_libs.py").is_file())
        self.assertIn("#37-vendored-windows-libraries-must-match-headers-crt-and-toolset", guide)
        self.assertIn("### 37. Vendored Windows libraries must match headers, CRT and toolset", guide)
        self.assertIn("is not Windows host qualification", guide)
        self.assertIn("not host-qualified", guide)

    def test_pipeline_docs_distinguish_permissions_without_local_install_history(self):
        docs = (ROOT / "docs/plugin-pipeline.md").read_text()
        self.assertIn("within the release workflow", docs)
        self.assertIn("separate bump workflow needs", docs)
        self.assertNotIn("Its local\nhelper predates", docs)
        self.assertNotIn("At the reviewed baseline, the installed", docs)

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
