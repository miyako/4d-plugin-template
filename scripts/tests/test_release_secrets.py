"""Hermetic helper tests: no real Keychain, GitHub, or OpenSSL operations."""

import base64
import errno
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import shutil
import subprocess
import sys
import tempfile
import termios
import time
import unittest


HELPER = Path(__file__).resolve().parents[1] / "set-release-secrets.sh"
NAMES = [
    "APPLE_DEVELOPER_ID_CERTIFICATE",
    "APPLE_DEVELOPER_ID_CERTIFICATE_PASSWORD",
    "KEYCHAIN_PASSWORD",
    "NOTARYTOOL_APPLE_ID",
    "NOTARYTOOL_PASSWORD",
    "NOTARYTOOL_TEAM_ID",
]
CERTIFICATE = b"synthetic-p12-not-a-real-certificate"
VALUES = {name: "synthetic-value-" + name for name in NAMES}
VALUES[NAMES[0]] = base64.b64encode(CERTIFICATE).decode()

MOCK = r'''
import fcntl
import json
import os
from pathlib import Path
import stat
import sys

path = Path(os.environ["MOCK_STATE"])
tool = Path(sys.argv[0]).name
args = sys.argv[1:]
input_value = (
    sys.stdin.read() if tool == "openssl" or (tool == "gh" and args[:2] == ["secret", "set"])
    else ""
)
lock = path.with_suffix(".lock").open("w")
fcntl.flock(lock, fcntl.LOCK_EX)
state = json.loads(path.read_text())
code = 0

def option(name):
    return args[args.index(name) + 1]

if tool == "security":
    assert option("-s") == "4d-plugin-release"
    name = option("-a")
    if args[0] == "find-generic-password":
        if state.get("lookup_failure") == name:
            code = 36
        elif name not in state["values"]:
            code = 44
        else:
            print(state["values"][name], end="")
    elif args[0] == "add-generic-password":
        if state.get("write_failure"):
            print("synthetic Keychain write failure", file=sys.stderr)
            code = 1
        else:
            index = args.index("-w")
            state["values"][name] = (
                args[index + 1] if index + 1 < len(args)
                else "synthetic-prompted-" + name
            )
            state["writes"].append(name)
    else:
        raise AssertionError(args)
elif tool == "gh":
    if args == ["auth", "status"]:
        code = 1 if state.get("auth_failure") else 0
    elif args[:2] == ["secret", "set"]:
        value = input_value
        assert value == state["values"][args[2]]
        assert "\n" not in value
        if state.get("upload_failure"):
            print("synthetic upload failure", file=sys.stderr)
            code = 1
        else:
            state["uploads"].append([option("--repo"), args[2]])
    elif args[:2] == ["secret", "list"]:
        state["lists"].append(option("--repo"))
        print("SECRET_NAME  synthetic-timestamp")
    else:
        raise AssertionError(args)
elif tool == "openssl":
    if args[0] == "pkcs12":
        temporary = Path(option("-in"))
        assert temporary.read_bytes() == b"synthetic-p12-not-a-real-certificate"
        state["temporary"].append(str(temporary))
        state["modes"].append(stat.S_IMODE(temporary.stat().st_mode))
        password = input_value
        assert password == state["values"]["APPLE_DEVELOPER_ID_CERTIFICATE_PASSWORD"]
        assert "-nokeys" in args and option("-passin") == "stdin"
        legacy = "-legacy" in args
        state["attempts"].append(legacy)
        mode = state.get("openssl_mode")
        if mode == "failure" or (mode == "legacy" and not legacy):
            code = 1
        else:
            print("synthetic-certificate-output")
    elif args[0] == "x509":
        data = input_value
        if not data:
            code = 1
        else:
            print("subject=CN=synthetic Developer ID Application")
            print("notAfter=synthetic expiry")
    else:
        raise AssertionError(args)
else:
    raise AssertionError(tool)

path.write_text(json.dumps(state))
sys.exit(code)
'''


class ReleaseSecretsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="release-helper-test-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.state_path = self.root / "state.json"
        self.state = {
            "values": VALUES.copy(),
            "writes": [],
            "uploads": [],
            "lists": [],
            "temporary": [],
            "modes": [],
            "attempts": [],
        }
        for tool in ("security", "gh", "openssl"):
            path = self.bin / tool
            path.write_text("#!" + sys.executable + "\n" + MOCK)
            path.chmod(0o700)
        for tool in ("base64", "tr", "mktemp", "rm"):
            target = shutil.which(tool)
            self.assertIsNotNone(target)
            (self.bin / tool).symlink_to(target)
        self.env = {
            "PATH": str(self.bin),
            "HOME": str(self.root),
            "TMPDIR": str(self.root),
            "MOCK_STATE": str(self.state_path),
            "LC_ALL": "C",
        }

    def run_helper(self, *repos, answer="y", **environment):
        self.state_path.write_text(json.dumps(self.state))
        master, slave = pty.openpty()

        def terminal():
            os.setsid()
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)

        process = subprocess.Popen(
            ["/bin/zsh", "-f", str(HELPER), *repos],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env={**self.env, **environment},
            preexec_fn=terminal,
        )
        os.close(slave)
        output = bytearray()
        answered = False
        deadline = time.monotonic() + 15
        try:
            while time.monotonic() < deadline:
                ready, _, _ = select.select([master], [], [], 0.1)
                if ready:
                    try:
                        chunk = os.read(master, 65536)
                    except OSError as error:
                        if error.errno == errno.EIO:
                            break
                        raise
                    if not chunk:
                        break
                    output.extend(chunk)
                    if b"[y/N]" in output and not answered:
                        os.write(master, answer.encode())
                        answered = True
                elif process.poll() is not None:
                    break
            else:
                self.fail("mock helper timed out")
            code = process.wait(timeout=5)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            os.close(master)
        self.state = json.loads(self.state_path.read_text())
        text = output.decode()
        for value in self.state["values"].values():
            if value:
                self.assertNotIn(value, text)
        for path in self.state["temporary"]:
            self.assertFalse(Path(path).exists(), "decoded certificate not cleaned up")
        self.assertTrue(all(mode == 0o600 for mode in self.state["modes"]))
        return code, text

    def test_zero_arguments(self):
        code, output = self.run_helper()
        self.assertNotEqual(code, 0)
        self.assertIn("usage:", output)
        self.assertEqual(self.state["uploads"], [])

    def test_missing_prerequisites(self):
        for tool in ("gh", "security", "openssl", "base64", "tr", "mktemp", "rm"):
            with self.subTest(tool=tool):
                path = self.bin / tool
                backup = self.bin / (tool + ".disabled")
                path.rename(backup)
                try:
                    code, output = self.run_helper("owner/repo")
                    self.assertNotEqual(code, 0)
                    self.assertIn(tool + " is not installed", output)
                    self.assertEqual(self.state["uploads"], [])
                finally:
                    backup.rename(path)

    def test_auth_failure(self):
        self.state["auth_failure"] = True
        code, output = self.run_helper("owner/repo")
        self.assertNotEqual(code, 0)
        self.assertIn("gh auth login", output)
        self.assertEqual(self.state["writes"], [])

    def test_missing_certificate(self):
        del self.state["values"][NAMES[0]]
        code, output = self.run_helper("owner/repo")
        self.assertNotEqual(code, 0)
        self.assertIn("CERT_P12=", output)
        self.assertEqual(self.state["uploads"], [])

    def test_missing_override_file(self):
        code, output = self.run_helper("owner/repo", CERT_P12=str(self.root / "absent"))
        self.assertNotEqual(code, 0)
        self.assertIn("not found:", output)
        self.assertEqual(self.state["writes"], [])

    def test_certificate_encoding_failure(self):
        certificate = self.root / "selected.p12"
        certificate.write_bytes(CERTIFICATE)
        encoder = self.bin / "base64"
        encoder.unlink()
        encoder.write_text("#!/bin/zsh\nprint -u2 'synthetic encoding failure'\nexit 1\n")
        encoder.chmod(0o700)
        code, output = self.run_helper("owner/repo", CERT_P12=str(certificate))
        self.assertNotEqual(code, 0)
        self.assertIn("encoding failure", output)
        self.assertEqual(self.state["writes"], [])
        self.assertEqual(self.state["uploads"], [])

    def test_certificate_password_failure(self):
        self.state["openssl_mode"] = "failure"
        code, output = self.run_helper("owner/repo")
        self.assertNotEqual(code, 0)
        self.assertIn("cannot be opened", output)
        self.assertEqual(self.state["attempts"], [False, True])
        self.assertEqual(self.state["uploads"], [])

    def test_legacy_retry(self):
        self.state["openssl_mode"] = "legacy"
        code, _ = self.run_helper("owner/repo")
        self.assertEqual(code, 0)
        self.assertEqual(self.state["attempts"], [False, True])
        self.assertEqual(len(self.state["uploads"]), 6)

    def test_rejected_confirmation(self):
        code, output = self.run_helper("owner/repo", answer="n")
        self.assertNotEqual(code, 0)
        self.assertIn("synthetic expiry", output)
        self.assertEqual(self.state["uploads"], [])

    def test_multiple_repositories_reuse(self):
        code, _ = self.run_helper("owner/first", "owner/second")
        self.assertEqual(code, 0)
        self.assertEqual(
            self.state["uploads"],
            [[repo, name] for repo in ("owner/first", "owner/second") for name in NAMES],
        )
        self.assertEqual(self.state["lists"], ["owner/first", "owner/second"])
        self.assertEqual(self.state["writes"], [])

    def test_first_time_and_replacement(self):
        certificate = self.root / "selected.p12"
        certificate.write_bytes(CERTIFICATE)
        self.state["values"] = {}
        code, _ = self.run_helper("owner/repo", CERT_P12=str(certificate))
        self.assertEqual(code, 0)
        self.assertEqual(self.state["writes"], NAMES)
        self.state["writes"] = []
        del self.state["values"][NAMES[1]]
        code, _ = self.run_helper("owner/repo", CERT_P12=str(certificate))
        self.assertEqual(code, 0)
        self.assertEqual(self.state["writes"], NAMES[:2])

    def test_keychain_write_failure(self):
        del self.state["values"][NAMES[1]]
        self.state["write_failure"] = True
        code, output = self.run_helper("owner/repo")
        self.assertNotEqual(code, 0)
        self.assertIn("Keychain write failure", output)
        self.assertEqual(self.state["uploads"], [])

    def test_keychain_lookup_failure(self):
        for name in (NAMES[0], NAMES[1]):
            with self.subTest(name=name):
                self.state["lookup_failure"] = name
                code, output = self.run_helper("owner/repo")
                self.assertNotEqual(code, 0)
                self.assertIn("Keychain lookup failed for " + name, output)
                self.assertEqual(self.state["writes"], [])
                self.assertEqual(self.state["uploads"], [])

    def test_upload_failure(self):
        self.state["upload_failure"] = True
        code, output = self.run_helper("owner/repo", "owner/second")
        self.assertNotEqual(code, 0)
        self.assertIn("upload failure", output)
        self.assertEqual(self.state["lists"], [])

    def test_empty_value(self):
        self.state["values"]["KEYCHAIN_PASSWORD"] = ""
        code, output = self.run_helper("owner/repo")
        self.assertNotEqual(code, 0)
        self.assertIn("empty value for KEYCHAIN_PASSWORD", output)
        self.assertEqual(len(self.state["uploads"]), 2)


if __name__ == "__main__":
    unittest.main()
