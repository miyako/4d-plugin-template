"""Execute the rendered bump shell against isolated local Git repositories."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]


class BumpVersionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name).resolve()
        self.source = root / "source"
        self.remote = root / "remote.git"
        subprocess.run(["git", "init", "--bare", "--quiet", str(self.remote)], check=True)
        self.source.mkdir()
        self.git("init", "--quiet", "--initial-branch=main")
        self.git("config", "user.name", "Synthetic fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        (self.source / "VERSION").write_bytes(b"1.2.3\n")
        self.git("add", "VERSION")
        self.git("commit", "--quiet", "-m", "fixture")
        self.sha = self.git("rev-parse", "HEAD").strip()
        self.git("remote", "add", "origin", str(self.remote))
        self.git("push", "--quiet", "origin", "HEAD:refs/heads/main")
        template = (ROOT / "templates/plugin-pipeline/bump-version.yml.in").read_text()
        workflow = yaml.load(template.replace("@@RELEASE_BRANCH@@", "main"), Loader=yaml.BaseLoader)
        self.script = next(step["run"] for step in workflow["jobs"]["bump"]["steps"]
                           if step.get("name") == "Bump VERSION and commit without creating tags")
        self.env = {**os.environ, "GITHUB_SHA": self.sha, "MODE": "patch",
                    "GITHUB_OUTPUT": str(root / "output")}

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.source, stderr=subprocess.PIPE).decode()

    def run_bump(self):
        return subprocess.run(["bash", "-c", self.script], cwd=self.source, env=self.env,
                              capture_output=True, text=True)

    def remote_head(self):
        return self.git("ls-remote", "origin", "refs/heads/main").split()[0]

    def test_patch_minor_major_commit_only_version_without_tags(self):
        for mode, version in (("patch", "1.2.4"), ("minor", "1.3.0"), ("major", "2.0.0")):
            with self.subTest(mode=mode):
                self.git("checkout", "--quiet", "--detach", self.sha)
                self.env["MODE"] = mode
                # Each mode gets an independent remote branch at the same source.
                remote = self.remote.parent / (mode + ".git")
                subprocess.run(["git", "init", "--bare", "--quiet", str(remote)], check=True)
                self.git("remote", "set-url", "origin", str(remote))
                self.git("push", "--quiet", "origin", "HEAD:refs/heads/main")
                result = self.run_bump()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual((self.source / "VERSION").read_text(), version + "\n")
                self.assertEqual(self.git("diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"),
                                 "VERSION\n")
                self.assertEqual(self.remote_head(), self.git("rev-parse", "HEAD").strip())
                self.assertEqual(self.git("ls-remote", "--tags", "origin"), "")

    def test_dirty_wrong_sha_and_invalid_version_never_push(self):
        for failure in ("dirty", "wrong-sha", "whitespace", "no-newline"):
            with self.subTest(failure=failure):
                self.env["GITHUB_SHA"] = self.git("rev-parse", "HEAD").strip()
                if failure == "dirty":
                    extra = self.source / "untracked"
                    extra.write_text("synthetic")
                elif failure == "wrong-sha":
                    self.env["GITHUB_SHA"] = "a" * 40
                else:
                    value = b"1.2.3 \n" if failure == "whitespace" else b"1.2.3"
                    (self.source / "VERSION").write_bytes(value)
                    self.git("add", "VERSION")
                    self.git("commit", "--quiet", "-m", failure)
                    self.env["GITHUB_SHA"] = self.git("rev-parse", "HEAD").strip()
                result = self.run_bump()
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.remote_head(), self.sha)
                if failure == "dirty":
                    extra.unlink()

    def test_existing_tag_rejected_without_branch_push(self):
        self.git("tag", "v1.2.4")
        self.git("push", "--quiet", "origin", "refs/tags/v1.2.4")
        self.assertNotEqual(self.run_bump().returncode, 0)
        self.assertEqual(self.remote_head(), self.sha)

    def test_moved_branch_is_not_overwritten(self):
        self.git("commit", "--quiet", "--allow-empty", "-m", "concurrent change")
        advanced = self.git("rev-parse", "HEAD").strip()
        self.git("push", "--quiet", "origin", "HEAD:refs/heads/main")
        self.git("checkout", "--quiet", "--detach", self.sha)
        self.assertNotEqual(self.run_bump().returncode, 0)
        self.assertEqual(self.remote_head(), advanced)


if __name__ == "__main__":
    unittest.main()
