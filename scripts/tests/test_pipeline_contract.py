"""Offline generated-CI drift checks, without an installed YAML parser."""

import json
from pathlib import Path
import sys
import tempfile
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import check_pipeline as checks


class PipelineContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.files = {
            ".github/plugin-pipeline.json": b'{"schema": 1}\n',
            ".github/plugin-pipeline-contract.json": b'{"schema": 1}\n',
            ".gitignore": b"# BEGIN 4D PLUGIN PIPELINE (managed)\n/build/\n# END 4D PLUGIN PIPELINE (managed)\n",
            ".gitattributes": b"# BEGIN 4D PLUGIN PIPELINE (managed)\nVERSION text eol=lf\n# END 4D PLUGIN PIPELINE (managed)\n",
            "scripts/check_pipeline.py": b"synthetic checker\n",
        }
        for name in checks.WORKFLOWS:
            self.files[f".github/workflows/{name}"] = f"name: {name}\n".encode()
        for name, content in self.files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        hashes = {name: checks.digest(checks.managed_section(name, content))
                  for name, content in self.files.items()}
        manifest = {"schema": 1, "files": hashes,
                    "config_sha256": hashes[".github/plugin-pipeline.json"],
                    "contract_sha256": hashes[".github/plugin-pipeline-contract.json"]}
        (self.root / checks.MANIFEST).write_text(json.dumps(manifest))

    def test_intact_fingerprints_pass(self):
        self.assertEqual(checks.check(self.root), len(self.files))

    def test_workflow_edit_missing_file_and_fourth_workflow_fail(self):
        path = self.root / ".github/workflows/test.yml"
        path.write_bytes(b"name: drift\n")
        with self.assertRaisesRegex(ValueError, "drift"):
            checks.check(self.root)
        path.write_bytes(self.files[".github/workflows/test.yml"])
        path.unlink()
        with self.assertRaisesRegex(ValueError, "drift"):
            checks.check(self.root)
        path.write_bytes(self.files[".github/workflows/test.yml"])
        (path.parent / "fourth.yaml").write_bytes(b"name: extra\n")
        with self.assertRaisesRegex(ValueError, "exactly"):
            checks.check(self.root)

    def test_unmanaged_rules_are_preserved_but_managed_drift_fails(self):
        path = self.root / ".gitignore"
        path.write_bytes(b"custom/\n" + self.files[".gitignore"] + b"other/\n")
        checks.check(self.root)
        path.write_bytes(path.read_bytes().replace(b"/build/", b"/wrong/"))
        with self.assertRaisesRegex(ValueError, "drift"):
            checks.check(self.root)

    def test_unsafe_manifest_paths_and_duplicate_json_keys_fail(self):
        path = self.root / checks.MANIFEST
        original = path.read_bytes()
        manifest = json.loads(original)
        manifest["files"]["../escape"] = "a" * 64
        path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "Unsafe"):
            checks.check(self.root)
        path.write_bytes(b'{"schema":1,"schema":1}')
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            checks.check(self.root)

    def test_symlink_outputs_fail(self):
        path = self.root / ".github/workflows/test.yml"
        path.unlink()
        path.symlink_to(self.root / ".github/workflows/release.yml")
        with self.assertRaisesRegex(ValueError, "Symlink"):
            checks.check(self.root)


if __name__ == "__main__":
    unittest.main()
