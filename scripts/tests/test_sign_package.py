"""Execute the signing shell with isolated synthetic tools and no live credentials."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import package
from test_package import Fixture, BINDING, DETAILS


FAKE_TOOL = r'''
import json
import os
from pathlib import Path
import sys
import zipfile
root = Path(os.environ["FIXTURE_ROOT"])
name = Path(sys.argv[0]).name
args = sys.argv[1:]
action = args[0] if args else ""
with (root / "tool-log").open("a") as log:
    log.write(name + " " + action + "\n")
failure = os.environ.get("FAIL_AT")
if failure == name + ":" + action:
    raise SystemExit(9)
if name == "git":
    if args[:2] == ["rev-parse", "HEAD"]:
        print("a" * 40)
    elif action == "ls-tree":
        sys.stdout.write("160000 commit " + "b" * 40 + "\t4D-Plugin-SDK\0")
    elif action == "-C" and "rev-parse" in args:
        print("b" * 40)
    elif action == "status":
        pass
    elif action == "show":
        if args[-1].endswith(":.github/plugin-pipeline.json"):
            sys.stdout.buffer.write((root / ".github/plugin-pipeline.json").read_bytes())
        else:
            sys.stdout.buffer.write(b"1.2.3\n" if args[-1].endswith(":VERSION") else b"plugin license\n")
    elif action == "-C" and "show" in args:
        sys.stdout.buffer.write(b"SDK license\n")
    else:
        raise SystemExit("Unexpected git command")
elif name == "security":
    state = root / "keychain-state"
    if action == "list-keychains":
        if "-s" in args:
            paths = args[args.index("-s") + 1:]
            if failure == "restore" and len(paths) == 2:
                raise SystemExit(9)
            state.write_text(json.dumps(paths))
        else:
            for item in json.loads(state.read_text()):
                print(json.dumps(item))
    elif action == "create-keychain":
        path = Path(args[-1])
        path.touch()
        state.write_text(json.dumps([str(path), *json.loads(state.read_text())]))
    elif action == "delete-keychain":
        Path(args[-1]).unlink()
    elif action == "find-identity":
        print('1) ' + 'C' * 40 + ' "Developer ID Application: Example (AB12345678)"')
elif name == "base64":
    import base64
    Path(args[args.index("-o") + 1]).write_bytes(base64.b64decode(sys.stdin.buffer.read()))
elif name == "lipo":
    print("arm64 x86_64")
elif name == "otool":
    if args[0] == "-L":
        print("fixture:\n\t/usr/lib/libSystem.B.dylib (compatibility version 1.0)")
elif name == "codesign":
    path = Path(args[-1])
    if "--display" in args:
        sys.stderr.write(os.environ["FAKE_DETAILS"])
    elif "--sign" in args:
        if path.is_dir():
            binary = path / "Contents/MacOS/get-icon"
            binary.write_bytes(binary.read_bytes() + b"signed")
            signature = path / "Contents/_CodeSignature/CodeResources"
            signature.parent.mkdir()
            signature.write_bytes(b"signature")
        else:
            path.write_bytes(path.read_bytes() + b"signed DMG")
elif name == "xcrun":
    if args[:2] == ["notarytool", "submit"]:
        archive = args[2]
        status = "Invalid" if os.environ.get("REJECT_FORMAT", "") and archive.endswith(os.environ["REJECT_FORMAT"]) else "Accepted"
        print(json.dumps({"status": status, "id": "synthetic-request"}))
    elif args[:2] == ["stapler", "staple"]:
        path = Path(args[-1])
        path.write_bytes(path.read_bytes() + b"stapled")
elif name == "hdiutil":
    Path(args[-1]).write_bytes(b"synthetic DMG")
elif name == "ditto":
    (root / "ditto-args").write_text(json.dumps(args))
    source, destination = map(Path, args[-2:])
    with zipfile.ZipFile(destination, "w") as zipped:
        for path in sorted(source.rglob("*")):
            if path.is_file():
                zipped.write(path, path.relative_to(source).as_posix())
elif name == "spctl":
    pass
else:
    raise SystemExit("Unexpected fake command")
'''

PYTHON_SHIM = r'''
import runpy
import subprocess
import sys
from pathlib import Path
sys.platform = "darwin"
sys.argv = sys.argv[1:]
if sys.argv[0] == "-":
    exec(compile(sys.stdin.read(), "<fixture stdin>", "exec"), {"__name__": "__main__"})
else:
    sys.path.insert(0, str(Path(sys.argv[0]).resolve().parent))
    runpy.run_path(sys.argv[0], run_name="__main__")
'''


@unittest.skipIf(os.name == "nt", "macOS signing shell orchestration is exercised by the macOS job")
class SignPackageTests(Fixture):
    def setUp(self):
        super().setUp()
        self.payload = self.assemble()
        self.scripts = self.root / "scripts"
        self.scripts.mkdir()
        original = Path(package.__file__).parent
        for name in ("package.py", "release_checks.py", "sign-package.sh"):
            shutil.copyfile(original / name, self.scripts / name)
        self.bin = self.root / "fake-bin"
        self.bin.mkdir()
        for name in ("git", "security", "base64", "lipo", "otool", "codesign", "xcrun",
                     "hdiutil", "ditto", "spctl", "python3"):
            path = self.bin / name
            path.write_text("#!" + sys.executable + "\n" + (PYTHON_SHIM if name == "python3" else FAKE_TOOL))
            path.chmod(0o755)
        self.original_keychains = [str(self.root / "login.keychain-db"), str(self.root / "with spaces/shared")]
        (self.root / "keychain-state").write_text(json.dumps(self.original_keychains))
        self.distribution = self.root / "distribution"
        # Avoid inheriting the user's real credential variables.
        self.env = {"PATH": str(self.bin) + os.pathsep + os.defpath, "HOME": str(self.root),
                    "FIXTURE_ROOT": str(self.root), "FAKE_DETAILS": DETAILS, "PYTHONDONTWRITEBYTECODE": "1",
                    "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF": "refs/heads/production",
                    "GITHUB_SHA": BINDING["source_commit"],
                    "APPLE_DEVELOPER_ID_CERTIFICATE": "ZHVtbXk=",
                    "APPLE_DEVELOPER_ID_CERTIFICATE_PASSWORD": "synthetic",
                    "KEYCHAIN_PASSWORD": "synthetic", "NOTARYTOOL_APPLE_ID": "example.invalid",
                    "NOTARYTOOL_TEAM_ID": "AB12345678", "NOTARYTOOL_PASSWORD": "synthetic"}

    def execute(self, **env):
        return subprocess.run(["/bin/bash", "scripts/sign-package.sh", str(self.payload), str(self.distribution)],
                              cwd=self.root, env={**self.env, **env}, capture_output=True, text=True, timeout=45)

    def assert_cleaned(self):
        self.assertEqual(json.loads((self.root / "keychain-state").read_text()), self.original_keychains)
        self.assertEqual(list((self.root / "get-icon/build").glob(".signing-*")), [])

    def test_signs_from_a_checkout_without_the_build_directory(self):
        shutil.rmtree(self.root / "get-icon/build", ignore_errors=True)
        result = self.execute()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_cleaned()

    def test_success_restores_registration_and_final_checksums(self):
        result = self.execute()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_cleaned()
        log = (self.root / "tool-log").read_text().splitlines()
        capture = log.index("security list-keychains")
        self.assertLess(capture, log.index("security create-keychain"))
        self.assertEqual(log.count("xcrun notarytool"), 3)
        self.assertTrue((self.distribution / "get-icon-1.2.3.zip").exists())
        self.assertTrue((self.distribution / "get-icon-1.2.3.dmg").read_bytes().endswith(b"stapled"))
        flags = json.loads((self.root / "ditto-args").read_text())
        self.assertIn("--norsrc", flags)
        self.assertIn("--noextattr", flags)
        self.assertNotIn("--sequesterRsrc", flags)
        import zipfile
        final = package.read_json(self.payload / "PROVENANCE.json")
        with zipfile.ZipFile(self.distribution / "get-icon-1.2.3.zip") as zipped:
            self.assertEqual(set(zipped.namelist()), set(final["files"]) | {"PROVENANCE.json"})
            import hashlib
            for name, digest in final["files"].items():
                self.assertEqual(hashlib.sha256(zipped.read(name)).hexdigest(), digest)
        from verify_distribution import verify_checksums
        verify_checksums(self.distribution, {p.name for p in self.distribution.iterdir() if p.name != "SHA256SUMS"})

    def test_wrong_branch_rejected_before_tools(self):
        result = self.execute(GITHUB_REF="refs/heads/main")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("explicit dispatch from production", result.stderr)
        self.assertFalse(self.distribution.exists())
        self.assertFalse((self.root / "tool-log").exists())

    def test_wrong_dispatch_sha_rejected(self):
        result = self.execute(GITHUB_SHA="b" * 40)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("immutable dispatch", result.stderr)
        self.assertFalse(self.distribution.exists())

    def test_missing_secrets_rejected(self):
        result = self.execute(NOTARYTOOL_PASSWORD="")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Required signing secrets are missing", result.stderr)
        self.assertFalse(self.distribution.exists())

    def test_import_failure_restores_cleanup(self):
        result = self.execute(FAIL_AT="security:import")
        self.assertNotEqual(result.returncode, 0)
        self.assert_cleaned()
        self.assertFalse((self.distribution / "SHA256SUMS").exists())

    def test_keychain_registration_failure_restores_cleanup(self):
        result = self.execute(FAIL_AT="security:set-keychain-settings")
        self.assertNotEqual(result.returncode, 0)
        self.assert_cleaned()

    def test_notary_zip_rejection_no_final_checksums(self):
        result = self.execute(REJECT_FORMAT=".zip")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not Accepted", result.stderr)
        self.assert_cleaned()
        self.assertFalse((self.distribution / "SHA256SUMS").exists())
        self.assertFalse((self.distribution / "get-icon-1.2.3.dmg").exists())

    def test_notary_dmg_rejection_no_final_checksums(self):
        result = self.execute(REJECT_FORMAT=".dmg")
        self.assertNotEqual(result.returncode, 0)
        self.assert_cleaned()
        self.assertFalse((self.distribution / "SHA256SUMS").exists())

    def test_wrong_signature_team_fails(self):
        result = self.execute(FAKE_DETAILS=DETAILS.replace("AB12345678", "CD12345678"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Signing team", result.stderr)
        self.assert_cleaned()

    def test_restore_failure_is_failure(self):
        result = self.execute(FAIL_AT="restore")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Could not restore", result.stderr)
        self.assertEqual(list((self.root / "get-icon/build").glob(".signing-*")), [])
        self.assertFalse((self.distribution / "SHA256SUMS").exists())

    def test_gatekeeper_failure_is_fatal(self):
        result = self.execute(FAIL_AT="spctl:--assess")
        self.assertNotEqual(result.returncode, 0)
        self.assert_cleaned()
        self.assertFalse((self.distribution / "SHA256SUMS").exists())

    def test_stapler_failure_is_fatal(self):
        result = self.execute(FAIL_AT="xcrun:stapler")
        self.assertNotEqual(result.returncode, 0)
        self.assert_cleaned()
        self.assertFalse((self.distribution / "SHA256SUMS").exists())


if __name__ == "__main__":
    unittest.main()
