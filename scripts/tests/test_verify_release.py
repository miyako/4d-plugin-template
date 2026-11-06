"""Offline GH metadata stubs for the strictly read-only handoff CLI."""

import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import package
import verify_release
from test_package import CONFIG, BINDING


class VerifyReleaseTests(unittest.TestCase):
    def setUp(self):
        self.settings = patch.object(package, "config", return_value=CONFIG)
        self.settings.start()
        self.addCleanup(self.settings.stop)
        self.binding = patch.object(package, "snapshot", return_value=BINDING)
        self.binding.start()
        self.addCleanup(self.binding.stop)
        self.run = {"id": 12, "event": "workflow_dispatch", "head_branch": "production",
                    "head_sha": BINDING["source_commit"], "status": "completed", "conclusion": "success",
                    "html_url": "https://github.com/example/repo/actions/runs/12",
                    "path": ".github/workflows/release.yml"}
        names = {"get-icon-1.2.3.zip", "get-icon-1.2.3.dmg", "PROVENANCE.json",
                 "SHA256SUMS", "zip-notarization.json", "dmg-notarization.json"}
        self.release = {"id": 1, "tag_name": "v1.2.3", "draft": False, "published_at": "2026-11-06",
                        "assets": [{"name": name, "size": 10, "state": "uploaded"} for name in sorted(names)]}
        self.jobs = {"total_count": 1, "jobs": [{"name": "publish", "status": "completed", "conclusion": "success"}]}
        self.artifacts = {"total_count": 1, "artifacts": [{"name": "get-icon-signed-packages-" + BINDING["source_commit"], "id": 3,
                                                        "size_in_bytes": 123, "expired": False}]}
        self.calls = []
        self.api_patch = patch.object(verify_release, "api", side_effect=self.api)
        self.api_patch.start()
        self.addCleanup(self.api_patch.stop)
        self.watch_patch = patch.object(verify_release.subprocess, "run")
        self.watch = self.watch_patch.start()
        self.addCleanup(self.watch_patch.stop)

    def api(self, endpoint, timeout):
        self.calls.append(endpoint)
        if "/jobs?" in endpoint:
            return self.jobs
        if "/artifacts?" in endpoint:
            return self.artifacts
        if "/releases/" in endpoint:
            return self.release
        if "/commits/" in endpoint:
            return {"sha": BINDING["source_commit"]}
        return self.run

    def test_success_consolidates_metadata(self):
        result = verify_release.verify_release("example/repo", 12)
        self.assertTrue(result["metadata_verified"])
        self.assertFalse(result["distribution_verified"])
        self.assertNotIn("verified", result)
        self.assertEqual(len(result["assets"]), 6)
        self.assertEqual(result["artifacts"][0]["id"], 3)
        self.assertIn("metadata alone", result["handoff"])
        self.assertEqual(len(self.calls), 6)
        self.watch.assert_not_called()

    def test_wrong_run_gates(self):
        for field, value in (("id", 13), ("event", "push"), ("head_branch", "main"),
                             ("head_sha", "b" * 40), ("path", ".github/workflows/test.yml")):
            original = self.run[field]
            self.run[field] = value
            with self.subTest(field=field), self.assertRaisesRegex(verify_release.VerificationError, "Failed run gates"):
                verify_release.verify_release("example/repo", 12)
            self.run[field] = original

    def test_failed_run(self):
        self.run["conclusion"] = "failure"
        with self.assertRaisesRegex(verify_release.VerificationError, "Failed Actions"):
            verify_release.verify_release("example/repo", 12)

    def test_immediate_timeout(self):
        self.run["status"] = "in_progress"
        with self.assertRaisesRegex(verify_release.VerificationError, "Timeout"):
            verify_release.verify_release("example/repo", 12, timeout=0)
        self.assertEqual(len(self.calls), 1)

    def test_wait_has_deadline(self):
        self.run["status"] = "queued"
        with patch.object(verify_release.time, "monotonic", side_effect=[0, 0, 10]):
            with self.assertRaisesRegex(verify_release.VerificationError, "Timeout"):
                verify_release.verify_release("example/repo", 12, timeout=5)
        self.watch.assert_not_called()

    def test_one_watch_then_fresh_metadata(self):
        self.run["status"] = "queued"
        def finished(*args, **kwargs):
            self.run["status"] = "completed"
        self.watch.side_effect = finished
        result = verify_release.verify_release("example/repo", 12, timeout=5)
        self.assertTrue(result["metadata_verified"])
        self.watch.assert_called_once()
        self.assertEqual(self.watch.call_args.args[0],
                         ["gh", "run", "watch", "12", "--repo", "example/repo", "--exit-status"])
        self.assertLessEqual(self.watch.call_args.kwargs["timeout"], 5)
        self.assertTrue(self.watch.call_args.kwargs["check"])
        self.assertEqual(self.calls.count("repos/example/repo/actions/runs/12"), 2)

    def test_watch_timeout_is_clear_without_api_polling(self):
        self.run["status"] = "in_progress"
        self.watch.side_effect = subprocess.TimeoutExpired("gh", 5)
        with self.assertRaisesRegex(verify_release.VerificationError, "Timeout waiting"):
            verify_release.verify_release("example/repo", 12, timeout=5)
        self.watch.assert_called_once()
        self.assertEqual(len(self.calls), 1)

    def test_failed_watch_refreshes_failed_run_metadata(self):
        self.run["status"] = "in_progress"
        def failed(*args, **kwargs):
            self.run.update(status="completed", conclusion="failure")
            raise subprocess.CalledProcessError(1, "gh")
        self.watch.side_effect = failed
        with self.assertRaisesRegex(verify_release.VerificationError, "Failed Actions watch") as caught:
            verify_release.verify_release("example/repo", 12, timeout=5)
        self.assertEqual(caught.exception.report["run"]["conclusion"], "failure")
        self.assertEqual(len(self.calls), 2)

    def test_failed_job(self):
        self.jobs["jobs"][0]["conclusion"] = "skipped"
        with self.assertRaisesRegex(verify_release.VerificationError, "Failed job"):
            verify_release.verify_release("example/repo", 12)

    def test_bounded_pages(self):
        self.jobs["total_count"] = 101
        with self.assertRaisesRegex(ValueError, "bounded page"):
            verify_release.verify_release("example/repo", 12)

    def test_draft_or_missing_assets(self):
        self.release["draft"] = True
        with self.assertRaisesRegex(verify_release.VerificationError, "release/assets"):
            verify_release.verify_release("example/repo", 12)
        self.release["draft"] = False
        self.release["assets"].pop()
        with self.assertRaisesRegex(verify_release.VerificationError, "release/assets"):
            verify_release.verify_release("example/repo", 12)

    def test_tag_binding(self):
        with patch.object(verify_release, "api", side_effect=lambda e, t:
                          {"sha": "bad"} if "/commits/" in e else self.api(e, t)):
            with self.assertRaisesRegex(verify_release.VerificationError, "immutable run source"):
                verify_release.verify_release("example/repo", 12)

    def test_expired_signed_artifact(self):
        self.artifacts["artifacts"][0]["expired"] = True
        with self.assertRaisesRegex(verify_release.VerificationError, "signed artifact gate"):
            verify_release.verify_release("example/repo", 12)

    def test_signed_artifact_id_and_size_required(self):
        artifact = self.artifacts["artifacts"][0]
        for key, value in (("id", None), ("id", 0), ("id", True),
                           ("size_in_bytes", 0), ("size_in_bytes", True)):
            original = artifact.get(key)
            artifact[key] = value
            with self.subTest(key=key, value=value), self.assertRaisesRegex(verify_release.VerificationError, "signed artifact gate"):
                verify_release.verify_release("example/repo", 12)
            artifact[key] = original

    def test_malformed_jobs(self):
        self.jobs["jobs"] = "not a list"
        with self.assertRaisesRegex(ValueError, "Invalid jobs"):
            verify_release.verify_release("example/repo", 12)

    def test_invalid_inputs_no_api_calls(self):
        for repo, run_id, timeout in (("bad repo", 12, 1), ("a/b", -1, 1),
                                      ("a/b", 12, -1), ("a/b", 12, float("nan"))):
            with self.subTest(repo=repo), self.assertRaises(ValueError):
                verify_release.verify_release(repo, run_id, timeout)
        self.assertEqual(self.calls, [])

    def test_api_get_only_and_timeout(self):
        self.api_patch.stop()
        with patch.object(verify_release.subprocess, "check_output", return_value=b'{"id":12}') as call:
            self.assertEqual(verify_release.api("repos/a/b/actions/runs/12", 3), {"id": 12})
            self.assertEqual(call.call_args.args[0], ["gh", "api", "--method", "GET", "repos/a/b/actions/runs/12"])
            self.assertEqual(call.call_args.kwargs["timeout"], 3)
        with patch.object(verify_release.subprocess, "check_output", side_effect=subprocess.TimeoutExpired("gh", 3)):
            with self.assertRaisesRegex(verify_release.VerificationError, "Timeout"):
                verify_release.api("repos/a/b/actions/runs/12", 3)

    def test_cli_failure_report(self):
        self.run["conclusion"] = "failure"
        with patch("builtins.print") as printed:
            status = verify_release.main(["--repository", "example/repo", "--run-id", "12"])
        self.assertEqual(status, 1)
        report = json.loads(printed.call_args.args[0])
        self.assertFalse(report["metadata_verified"])
        self.assertFalse(report["distribution_verified"])
        self.assertIn("run", report)
        self.assertIn("Failed Actions", report["error"])


if __name__ == "__main__":
    unittest.main()
