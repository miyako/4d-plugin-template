"""Synthetic signature, keychain and authorization checks; no real credentials."""

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import package
import release_checks
from test_package import CONFIG, BINDING, DETAILS, create_source


class ReleaseChecksTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).parent / (".release-fixture-" + uuid.uuid4().hex)
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)

    def test_signature_valid(self):
        release_checks.signature(DETAILS, "AB12345678", True)
        release_checks.signature(DETAILS.replace("flags=0x10000(runtime)", "flags=0x0(none)"), "AB12345678")

    def test_invalid_signatures(self):
        for invalid in (DETAILS.replace("AB12345678", "CD12345678"),
                        DETAILS.replace("Timestamp=", "Signed Time="),
                        DETAILS.replace("Developer ID Application: ", "Apple Development: "),
                        DETAILS.replace("flags=0x10000(runtime)", "flags=0x0(runtime)"),
                        DETAILS.replace("flags=0x10000(runtime)", "flags=0x10000(none)"),
                        DETAILS.replace("Authority=Apple Root CA\n", ""),
                        DETAILS + "Signature=adhoc\n",
                        DETAILS + "TeamIdentifier=AB12345678\n"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                release_checks.signature(invalid, "AB12345678", True)

    def test_team_validation(self):
        for team in ("", "AB123", None, "ab12345678"):
            with self.subTest(team=team), self.assertRaises(ValueError):
                release_checks.signature(DETAILS, team)

    def test_notarization(self):
        release_checks.notarization({"status": "Accepted", "id": "request"})
        for report in ({}, [], {"status": "Accepted"}, {"status": "Accepted", "id": ""},
                       {"status": "Invalid", "id": "id"}, {"status": "In Progress", "id": "id"}):
            with self.subTest(report=report), self.assertRaises(ValueError):
                release_checks.notarization(report)

    def test_keychain_roundtrip_capture_before_registration(self):
        original = '    "/Users/example/login.keychain-db"\n    "/Users/example/with spaces/shared"\n'
        snapshot = self.root / "search-list"
        keychain = self.root / "signing.keychain-db"
        with patch.object(release_checks.subprocess, "check_output", return_value=original), \
                patch.object(release_checks.subprocess, "run") as run:
            release_checks.keychain_search_list(snapshot, capture=True)
            run.assert_not_called()
            release_checks.keychain_search_list(snapshot, keychain)
            release_checks.keychain_search_list(snapshot)
            self.assertEqual(snapshot.read_text(), original)
            prefix = ["security", "list-keychains", "-d", "user", "-s"]
            paths = ["/Users/example/login.keychain-db", "/Users/example/with spaces/shared"]
            self.assertEqual(run.call_args_list[0].args[0], [*prefix, str(keychain), *paths])
            self.assertEqual(run.call_args_list[1].args[0], [*prefix, *paths])

    def test_failed_registration_keeps_snapshot(self):
        path = self.root / "search-list"
        with patch.object(release_checks.subprocess, "check_output", return_value='"/Users/example/login"\n'), \
                patch.object(release_checks.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "security")):
            release_checks.keychain_search_list(path, capture=True)
            with self.assertRaises(subprocess.CalledProcessError):
                release_checks.keychain_search_list(path, self.root / "keychain")
        self.assertEqual(path.read_text(), '"/Users/example/login"\n')

    def test_snapshot_cannot_be_overwritten(self):
        snapshot = self.root / "search-list"
        snapshot.write_text("existing")
        with self.assertRaisesRegex(ValueError, "already exists"):
            release_checks.keychain_search_list(snapshot, capture=True)

    def test_final_checksums(self):
        path = self.root / "product.dmg"
        path.write_bytes(b"before")
        release_checks.checksums(self.root)
        before = (self.root / "SHA256SUMS").read_bytes()
        path.write_bytes(b"after signing and stapling")
        release_checks.checksums(self.root)
        self.assertNotEqual(before, (self.root / "SHA256SUMS").read_bytes())
        self.assertEqual((self.root / "SHA256SUMS").read_text(),
                         hashlib.sha256(path.read_bytes()).hexdigest() + "  product.dmg\n")

    def test_checksums_reject_directories(self):
        (self.root / "empty").mkdir()
        with self.assertRaises(ValueError):
            release_checks.checksums(self.root)

    def test_authorization_immutable_manual_branch_and_secrets(self):
        create_source(self.root)
        env = {"GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF": "refs/heads/production",
               "GITHUB_SHA": BINDING["source_commit"]}
        secret_names = ("APPLE_DEVELOPER_ID_CERTIFICATE", "APPLE_DEVELOPER_ID_CERTIFICATE_PASSWORD",
                        "KEYCHAIN_PASSWORD", "NOTARYTOOL_APPLE_ID", "NOTARYTOOL_TEAM_ID", "NOTARYTOOL_PASSWORD")
        env.update({name: "synthetic" for name in secret_names})
        env["NOTARYTOOL_TEAM_ID"] = "AB12345678"
        with patch.object(package, "ROOT", self.root), patch.object(package, "snapshot", return_value=BINDING), \
                patch.object(sys, "platform", "darwin"), patch.dict(os.environ, env, clear=True):
            self.assertEqual(release_checks.authorize().split("\t"), ["get-icon", "production", "1.2.3", "get-icon/build"])
            for key, value, message in (("GITHUB_EVENT_NAME", "push", "explicit dispatch"),
                                        ("GITHUB_REF", "refs/heads/main", "explicit dispatch"),
                                        ("GITHUB_SHA", "c" * 40, "immutable"),
                                        ("NOTARYTOOL_PASSWORD", "", "missing"),
                                        ("NOTARYTOOL_TEAM_ID", "bad-team", "10-character")):
                with self.subTest(key=key), patch.dict(os.environ, {key: value}):
                    with self.assertRaisesRegex(ValueError, message):
                        release_checks.authorize()


if __name__ == "__main__":
    unittest.main()
