"""Hermetic publication orchestration; no GitHub writes or signing tools are run."""

from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import publish_release as publish


REAL_API = publish.api
SOURCE = "a" * 40
BINDING = {"source_commit": SOURCE, "sdk_commit": "b" * 40, "version": "1.2.3"}
ENV = {"GITHUB_REPOSITORY": "owner/plugin", "GITHUB_EVENT_NAME": "workflow_dispatch",
       "GITHUB_REF": "refs/heads/main", "GITHUB_SHA": SOURCE}


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.directory = self.root / ".signed-distribution"
        self.directory.mkdir()
        (self.directory / "SHA256SUMS").write_bytes(b"accepted checksums\n")
        (self.directory / "plugin-1.2.3.zip").write_bytes(b"synthetic archive")
        self.mutations = []
        self.tag_exists = False
        self.release_exists = False
        self.tag_sha = SOURCE
        self.draft_tamper = False
        self.tag_changes = False
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, ENV, clear=True).start()
        patch.object(publish.package, "config", return_value={"release_branch": "main"}).start()
        patch.object(publish.package, "snapshot", return_value=BINDING).start()
        patch.object(publish, "api", side_effect=self.api).start()
        patch.object(publish, "command", side_effect=self.command).start()
        self.verify = patch.object(publish.verify_distribution, "verify_distribution").start()

    def api(self, endpoint, optional=False, **fields):
        if endpoint == "repos/owner/plugin":
            return {"default_branch": "main"}
        if "/releases/tags/" in endpoint:
            return {"draft": False} if self.release_exists else None
        if "/git/ref/tags/" in endpoint:
            return {} if self.tag_exists else None
        if endpoint.endswith("/git/refs"):
            self.mutations.append(("tag", fields))
            return {}
        if "/commits/" in endpoint:
            sha = "c" * 40 if self.tag_changes and self.mutations else self.tag_sha
            return {"sha": sha}
        raise AssertionError(endpoint)

    def command(self, args):
        self.assertEqual(args[:2], ["gh", "release"])
        self.mutations.append(tuple(args[2:4]))
        if args[2] == "download":
            draft = Path(args[args.index("--dir") + 1])
            draft.mkdir()
            content = b"tampered\n" if self.draft_tamper else b"accepted checksums\n"
            (draft / "SHA256SUMS").write_bytes(content)
        return b""

    def run_publish(self):
        return publish.publish(self.directory, self.root / ".publish-extracted")

    def test_success_verifies_both_trust_boundaries_before_publishing(self):
        self.assertTrue(self.run_publish()["published"])
        self.assertEqual(self.verify.call_count, 2)
        self.assertEqual([item[0] for item in self.mutations], ["tag", "create", "download", "edit"])

    def test_same_source_existing_tag_is_allowed_without_moving_it(self):
        self.tag_exists = True
        self.run_publish()
        self.assertNotIn("tag", [item[0] for item in self.mutations])

    def test_conflicting_tag_stops_before_create(self):
        self.tag_exists = True
        self.tag_sha = "c" * 40
        with self.assertRaisesRegex(ValueError, "another source"):
            self.run_publish()
        self.assertEqual(self.mutations, [])

    def test_existing_release_is_never_overwritten(self):
        self.release_exists = True
        with self.assertRaisesRegex(ValueError, "already has a release"):
            self.run_publish()
        self.assertEqual(self.mutations, [])

    def test_draft_checksum_tamper_never_publishes(self):
        self.draft_tamper = True
        with self.assertRaisesRegex(ValueError, "checksum file differs"):
            self.run_publish()
        self.assertNotIn("edit", [item[0] for item in self.mutations])

    def test_draft_inventory_or_signature_failure_never_publishes(self):
        self.verify.side_effect = [None, ValueError("synthetic draft signature failure")]
        with self.assertRaisesRegex(ValueError, "draft signature"):
            self.run_publish()
        self.assertNotIn("edit", [item[0] for item in self.mutations])

    def test_tag_change_before_publication_stops_at_draft(self):
        self.tag_changes = True
        with self.assertRaisesRegex(ValueError, "tag changed"):
            self.run_publish()
        self.assertNotIn("edit", [item[0] for item in self.mutations])

    def test_dispatch_source_and_branch_guards(self):
        for key, value in (("GITHUB_EVENT_NAME", "push"), ("GITHUB_REF", "refs/heads/feature"),
                           ("GITHUB_SHA", "c" * 40)):
            with self.subTest(key=key), patch.dict(os.environ, {key: value}):
                with self.assertRaisesRegex(ValueError, "explicit"):
                    self.run_publish()
        self.assertEqual(self.mutations, [])

    def test_default_branch_guard_is_not_a_caller_override(self):
        original = self.api

        def changed_default(endpoint, **kwargs):
            if endpoint == "repos/owner/plugin":
                return {"default_branch": "develop"}
            return original(endpoint, **kwargs)

        with patch.object(publish, "api", side_effect=changed_default):
            with self.assertRaisesRegex(ValueError, "repository default"):
                self.run_publish()
        self.assertEqual(self.mutations, [])

    def test_stale_draft_output_stops_before_tag_creation(self):
        (self.root / ".draft-upload-check").mkdir()
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.run_publish()
        self.assertEqual(self.mutations, [])

    def test_final_acceptance_failure_prevents_all_mutations(self):
        self.verify.side_effect = ValueError("synthetic final package failure")
        with self.assertRaisesRegex(ValueError, "final package failure"):
            self.run_publish()
        self.assertEqual(self.mutations, [])

    def test_draft_create_or_download_error_never_publishes(self):
        for failing_operation in ("create", "download"):
            with self.subTest(operation=failing_operation):
                self.mutations.clear()

                def fail(args):
                    if args[2] == failing_operation:
                        raise subprocess.CalledProcessError(1, args, stderr=b"synthetic failure")
                    return self.command(args)

                with patch.object(publish, "command", side_effect=fail):
                    with self.assertRaises(subprocess.CalledProcessError):
                        self.run_publish()
                self.assertNotIn("edit", [item[0] for item in self.mutations])

    def test_remote_error_has_no_publication_fallback(self):
        with patch.object(publish, "api", side_effect=ValueError("remote unavailable")):
            with self.assertRaisesRegex(ValueError, "remote unavailable"):
                self.run_publish()
        self.assertEqual(self.mutations, [])

    def test_optional_lookup_only_accepts_http_404(self):
        for status in (404, 403, 500):
            error = subprocess.CalledProcessError(1, ["gh"], output=f"HTTP/2 {status}\n\n{{}}\n".encode())
            with self.subTest(status=status), patch.object(publish, "command", side_effect=error):
                if status == 404:
                    self.assertIsNone(REAL_API("repos/owner/plugin/releases/tags/v1", optional=True))
                else:
                    with self.assertRaisesRegex(ValueError, "GitHub request failed"):
                        REAL_API("repos/owner/plugin/releases/tags/v1", optional=True)

    def test_api_rejects_malformed_or_unexpected_success_responses(self):
        for response in (b"{}", b"HTTP/2 200\n\n[]", b"HTTP/2 302\n\n{}"):
            with self.subTest(response=response), patch.object(publish, "command", return_value=response):
                with self.assertRaises(ValueError):
                    REAL_API("repos/owner/plugin")

    def test_api_accepts_object_with_realistic_http_headers(self):
        response = b'HTTP/2.0 200 OK\r\nContent-Type: application/json\r\n\r\n{"default_branch":"main"}'
        with patch.object(publish, "command", return_value=response):
            self.assertEqual(REAL_API("repos/owner/plugin"), {"default_branch": "main"})


if __name__ == "__main__":
    unittest.main()
