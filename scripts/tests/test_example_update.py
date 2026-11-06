"""The example update preserves executable source instead of replacing it with docs."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import prepare_example_update as update


class ExampleUpdateTests(unittest.TestCase):
    def test_real_example_preserves_methods_manifest_and_header(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory).resolve() / "example-update"
            update.prepare(update.BASELINE, output)
            for name in (
                "example/example-4dplugin.cpp", "example/example-4dplugin.h",
                "example/manifest.json", "example/constants.xlf",
                "example/example-test/Project/Sources/Methods/test_all.4dm",
                "example/example-test/Project/Sources/Methods/test_example_greeting.4dm",
            ):
                self.assertEqual((output / name).read_bytes(),
                                 update.git("show", f"{update.BASELINE}:{name}"))
            config = json.loads((output / ".github/plugin-pipeline.json").read_text())
            update.generate_plugin_pipeline.generate(config, output, check=True)
            self.assertEqual({p.name for p in (output / ".github/workflows").iterdir()},
                             {"bump-version.yml", "test.yml", "release.yml"})
            self.assertIn("BNDL", (output / "example/Info.plist.in").read_text())
            for name in ("scripts/set-release-secrets.sh", "scripts/tests/test_release_secrets.py"):
                self.assertEqual((output / name).read_bytes(), (ROOT / name).read_bytes())

    def test_existing_output_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "existing output"):
                update.prepare(update.BASELINE, Path(directory).resolve())

    def test_nonimmutable_ref_is_rejected_before_git(self):
        with patch.object(update, "git") as git:
            with self.assertRaisesRegex(ValueError, "immutable"):
                update.prepare("example", Path("unused"))
            git.assert_not_called()


if __name__ == "__main__":
    unittest.main()
