"""Offline checks for the canonical guide and source-level example migration."""

import json
from pathlib import Path
import plistlib
import unittest


ROOT = Path(__file__).resolve().parents[2]


class GuidanceTests(unittest.TestCase):
    def test_release_names_and_skill_entrypoint_are_consistent(self):
        skill = (ROOT / "SKILL.md").read_text()
        self.assertTrue(skill.startswith("---\nname: native-plugin-pipeline\n"))
        for name in ("AGENTS.md", "README.md", ".github/instructions/plugin-dev.instructions.md"):
            self.assertIn("SKILL.md", (ROOT / name).read_text())
        for name in ("SKILL.md", "README.md", "docs/plugin-pipeline.md",
                     ".github/instructions/plugin-dev.instructions.md"):
            text = (ROOT / name).read_text()
            self.assertIn("**Bump version**", text)
            self.assertIn("**Build**", text)
            self.assertNotIn("Build and Release", text)
        contract = json.loads((ROOT / "templates/plugin-pipeline/contract.json").read_text())
        self.assertEqual(contract["workflows"]["release.yml"]["name"], "Build")

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

    def test_static_link_universal_arm64_and_git_bash_guidance(self):
        guide = (ROOT / ".github/instructions/plugin-dev.instructions.md").read_text()
        docs = (ROOT / "docs/plugin-pipeline.md").read_text()
        for heading in ("### 39. Statically linked libraries are exported from the plugin (macOS)",
                        "### 40. Universal static builds of autotools dependencies (macOS)",
                        "### 41. Old C libraries on arm64: endianness and integer widths",
                        "### 42. Bash scripts on Windows runners (Git Bash)"):
            self.assertIn(heading, guide)
        self.assertIn('"LINKER:-exported_symbol,_FourDPackex"', guide)
        self.assertIn("#18-windows-cross-platform-build-issues", guide)
        self.assertIn("### macOS and `\"windows\": \"none\"`", docs)
        self.assertIn("plugin-pipeline.md#macos-and-windows-none", guide)
        self.assertIn('( cd "$dest" && tar -xzf - ) < "$archive"', guide)
        self.assertIn("Rosetta runs are native evidence, not x86_64 4D acceptance.", guide)

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
