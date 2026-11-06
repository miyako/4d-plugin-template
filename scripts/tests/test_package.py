"""Hermetic packaging tests; fixtures explicitly provide reference-like configuration."""

from contextlib import ExitStack
import hashlib
import json
from pathlib import Path, PureWindowsPath
import plistlib
import shutil
import struct
import sys
import unittest
from unittest.mock import patch
import uuid
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import package
import verify_distribution

CONFIG = {
    "schema": 1, "product": "get-icon", "source_dir": "get-icon", "build_dir": "get-icon/build",
    "test_project": "get-icon/get-icon-test/Project/get-icon.4DProject",
    "startup_method": "test_all", "sdk_dir": "4D-Plugin-SDK",
    "bundle_identifier": "com.example.get-icon", "macos_deployment_target": "11.0",
    "release_branch": "production", "signing_environment": "release-signing",
    "profile": "macos-universal-windows-x64", "signing_policy": "developer-id",
    "native_tests": True, "dependencies": [],
}
BINDING = dict(source_commit="a" * 40, sdk_commit="b" * 40, version="1.2.3")
DETAILS = (
    "CodeDirectory v=20500 flags=0x10000(runtime)\n"
    "Authority=Developer ID Application: Example (AB12345678)\n"
    "Authority=Developer ID Certification Authority\nAuthority=Apple Root CA\n"
    "Timestamp=Nov 3, 2026 at 8:00:00 PM\nTeamIdentifier=AB12345678\n"
)


def create_source(root, settings=CONFIG):
    (root / ".github").mkdir()
    package.write_json(root / ".github/plugin-pipeline.json", settings)
    source = root / settings["source_dir"]
    source.mkdir(parents=True, exist_ok=True)
    (source / "manifest.json").write_bytes(b'{"name":"fixture"}\n')
    (source / "constants.xlf").write_bytes(b'<xliff version="1.0"/>\n')
    project = root / settings["test_project"]
    project.parent.mkdir(parents=True)
    project.write_text('{"compatibilityVersion":2101}\n')
    (root / settings["build_dir"]).mkdir(parents=True, exist_ok=True)
    (root / settings["sdk_dir"]).mkdir()
    (root / "VERSION").write_bytes(b"1.2.3\n")
    plugins = project.parent.parent / "Plugins"
    product = settings["product"]
    mac = plugins / f"{product}.bundle/Contents"
    (mac / "MacOS").mkdir(parents=True)
    (mac / f"MacOS/{product}").write_bytes(b"fixture Mach-O")
    (mac / "Resources").mkdir()
    (mac / "Info.plist").write_bytes(plistlib.dumps({
        "CFBundlePackageType": "BNDL", "CFBundleExecutable": product,
        "CFBundleIdentifier": settings["bundle_identifier"],
        "CFBundleVersion": "1.2.3", "CFBundleShortVersionString": "1.2.3"}))
    win = plugins / f"{product}/Contents"
    (win / "Windows64").mkdir(parents=True)
    (win / "Resources").mkdir()
    pe = bytearray(90)
    pe[:2] = b"MZ"
    struct.pack_into("<I", pe, 60, 64)
    pe[64:68] = b"PE\0\0"
    struct.pack_into("<H", pe, 68, 0x8664)
    struct.pack_into("<H", pe, 88, 0x20B)
    (win / f"Windows64/{product}.4DX").write_bytes(pe)
    for contents in (mac, win):
        for name in ("manifest.json", "constants.xlf"):
            shutil.copyfile(source / name, contents / "Resources" / name)
    return plugins


class Fixture(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).parent / (".package-fixture-" + uuid.uuid4().hex)
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(package, "ROOT", self.root))
        self.real_snapshot = package.snapshot
        self.snapshot = self.stack.enter_context(patch.object(package, "snapshot", return_value=BINDING))
        self.real_native = package.validate_macos
        self.native = self.stack.enter_context(patch.object(package, "validate_macos"))
        self.runner = self.stack.enter_context(patch.object(package, "run", side_effect=self.export))
        self.stack.enter_context(patch.object(package, "toolchain", return_value={
            "compiler_id": "Clang", "compiler_version": "17.0", "cmake": "cmake version 3.30.0"}))
        self.plugins = create_source(self.root)
        self.inputs, self.output = self.root / "inputs", self.root / "assembled"
        package.stage("macos", self.inputs)
        package.stage("windows", self.inputs)

    def export(self, args, cwd=None):
        return b"SDK license\n" if "-C" in args else b"plugin license\n"

    def assemble(self):
        package.assemble(self.inputs, self.output)
        return self.output / "payload"

    def signed_payload(self):
        payload = self.assemble()
        paths = package.layout()
        binary = payload / paths["mac"]
        binary.write_bytes(binary.read_bytes() + b"signed")
        signature = payload / paths["signature"]
        signature.parent.mkdir()
        signature.write_bytes(b"signature")
        package.inventory(payload, payload / "PROVENANCE.json", "AB12345678")
        return payload


class PackageTests(Fixture):
    def test_signed_roundtrip(self):
        payload = self.signed_payload()
        package.verify(payload, payload / "PROVENANCE.json")
        data = package.read_json(payload / "PROVENANCE.json")
        before = package.read_json(payload / "BUILD-PROVENANCE.json")
        self.assertNotEqual(data["files"][package.layout()["mac"]],
                            before["inputs"]["macos"]["files"][package.layout()["mac"]])
        self.assertEqual(data["expected_team"], "AB12345678")

    def test_unsigned_roundtrip(self):
        payload = self.assemble()
        package.inventory(payload, payload / "PROVENANCE.json")
        package.verify(payload, payload / "PROVENANCE.json")

    def test_config_no_default(self):
        (self.root / ".github/plugin-pipeline.json").unlink()
        with self.assertRaises(FileNotFoundError):
            package.config()

    def test_reject_unsupported_config(self):
        for key, value in (("schema", True), ("profile", "linux"), ("signing_policy", "adhoc"),
                           ("dependencies", ["openssl"]), ("native_tests", "true"),
                           ("source_dir", "../escape"), ("release_branch", "a..b"),
                           ("product", "a/b"), ("bundle_identifier", "invalid")):
            with self.subTest(key=key):
                package.write_json(self.root / ".github/plugin-pipeline.json", {**CONFIG, key: value})
                with self.assertRaises(package.PackageError):
                    package.config()

    def test_duplicate_config_keys(self):
        (self.root / ".github/plugin-pipeline.json").write_text('{"schema":1,"schema":1}')
        with self.assertRaisesRegex(package.PackageError, "Duplicate"):
            package.config()

    def test_config_generator_value_contract(self):
        path = self.root / ".github/plugin-pipeline.json"
        for key, value in (("product", "P" * 64), ("startup_method", "s" * 31),
                           ("bundle_identifier", "org.example--.plugin-"),
                           ("macos_deployment_target", "10.13"),
                           ("macos_deployment_target", "26.0"),
                           ("release_branch", "release/2_0"),
                           ("signing_environment", "S" * 64)):
            with self.subTest(key=key, value=value):
                package.write_json(path, {**CONFIG, key: value})
                self.assertEqual(package.config()[key], value)
        for key, value in (("product", "P" * 65), ("startup_method", "s" * 32),
                           ("signing_environment", "S" * 65), ("signing_environment", "sign;echo"),
                           ("macos_deployment_target", "10.12"),
                           ("macos_deployment_target", "11.0.1"),
                           ("release_branch", "release.name"), ("source_dir", "source dir"),
                           ("build_dir", "outside/build"), ("test_project", "outside/Project/test.4DProject"),
                           ("sdk_dir", "get-icon/sdk"), ("sdk_dir", "get-icon"),
                           ("build_dir", "get-icon/get-icon-test"),
                           ("source_dir", "NUL"), ("sdk_dir", "vendor/trailing."),
                           ("sdk_dir", "vendor/.hidden"), ("sdk_dir", "vendor/-option")):
            with self.subTest(key=key, value=value):
                package.write_json(path, {**CONFIG, key: value})
                with self.assertRaises(package.PackageError):
                    package.config()

    def test_config_drives_product_and_paths(self):
        settings = {**CONFIG, "product": "Sample", "source_dir": "native",
                    "build_dir": "native/build", "test_project": "native/tests/Project/Sample.4DProject",
                    "sdk_dir": "sdk", "bundle_identifier": "org.example.Sample"}
        other = self.root / "custom"
        other.mkdir()
        create_source(other, settings)
        with patch.object(package, "ROOT", other):
            package.stage("macos", other / "inputs")
            package.stage("windows", other / "inputs")
            package.assemble(other / "inputs", other / "output")
            self.assertTrue((other / "output/payload/Sample.bundle/Contents/MacOS/Sample").exists())

    def test_binding_rejected(self):
        path = self.inputs / "windows/manifest.json"
        original = package.read_json(path)
        for key in package.BINDING:
            with self.subTest(key=key):
                package.write_json(path, {**original, key: "different"})
                with self.assertRaisesRegex(package.PackageError, key):
                    self.assemble()

    def test_input_hash_mutation(self):
        path = self.inputs / "windows/payload" / package.layout()["win"]
        path.write_bytes(path.read_bytes() + b"tampered")
        with self.assertRaisesRegex(package.PackageError, "SHA-256"):
            self.assemble()

    def test_rehashed_resource_must_match_source(self):
        payload = self.inputs / "windows/payload"
        (payload / package.layout()["resource"]).write_bytes(b"different resource")
        path = self.inputs / "windows/manifest.json"
        data = package.read_json(path)
        data["files"] = package.hashes(payload)
        package.write_json(path, data)
        with self.assertRaisesRegex(package.PackageError, "differs from clean source"):
            self.assemble()

    def test_extra_file_and_empty_directory(self):
        for path in (self.inputs / "extra", self.inputs / "macos/extra-dir"):
            with self.subTest(path=path):
                if path.suffix or path.name == "extra":
                    path.write_bytes(b"extra")
                else:
                    path.mkdir()
                with self.assertRaises(package.PackageError):
                    self.assemble()
                if path.is_dir():
                    path.rmdir()
                else:
                    path.unlink()

    def test_symlink_and_case_collision(self):
        (self.inputs / "link").symlink_to(self.inputs / "windows")
        with self.assertRaisesRegex(package.PackageError, "Symlink"):
            package.files_in(self.inputs)
        (self.inputs / "link").unlink()
        # Filesystem-independent portable path check covers Windows serialization.
        root = PureWindowsPath(r"C:\payload")
        self.assertEqual(package.portable_relative(root / r"get-icon.bundle\Contents\Info.plist", root),
                         package.layout()["plist"])

    def test_unsafe_paths(self):
        for value in ("../escape", "/absolute", r"C:\native\file", "a\\b", "a//b",
                      "a/./b", "a/../b", "NUL.txt", "a/b.", "a/b ", "a/\n"):
            with self.subTest(value=value), self.assertRaises(package.PackageError):
                package.safe_path(value)

    def test_pe_architecture(self):
        path = self.inputs / "windows/payload" / package.layout()["win"]
        for machine, magic in ((0x14C, 0x20B), (0x8664, 0x10B)):
            data = bytearray(path.read_bytes())
            struct.pack_into("<H", data, 68, machine)
            struct.pack_into("<H", data, 88, magic)
            path.write_bytes(data)
            with self.assertRaisesRegex(package.PackageError, "AMD64"):
                package.validate_pe(path)

    def test_plist_identifier(self):
        path = self.inputs / "macos/payload" / package.layout()["plist"]
        data = plistlib.loads(path.read_bytes())
        data["CFBundleIdentifier"] = "wrong.identifier"
        path.write_bytes(plistlib.dumps(data))
        manifest = self.inputs / "macos/manifest.json"
        metadata = package.read_json(manifest)
        metadata["files"] = package.hashes(self.inputs / "macos/payload")
        package.write_json(manifest, metadata)
        with self.assertRaisesRegex(package.PackageError, "CFBundleIdentifier"):
            self.assemble()

    def test_final_mutation_and_extra(self):
        payload = self.signed_payload()
        (payload / "LICENSE").write_bytes(b"changed")
        with self.assertRaisesRegex(package.PackageError, "SHA-256"):
            package.verify(payload, payload / "PROVENANCE.json")

    def test_rehashed_license_still_source_bound(self):
        payload = self.signed_payload()
        (payload / "LICENSE").write_bytes(b"changed")
        path = payload / "PROVENANCE.json"
        data = package.read_json(path)
        data["files"] = package.hashes(payload, ("PROVENANCE.json",))
        package.write_json(path, data)
        with self.assertRaisesRegex(package.PackageError, "license mismatch"):
            package.verify(payload, path)

    def test_recomputed_final_inventory_cannot_accept_unchanged_input_drift(self):
        payload = self.signed_payload()
        final = payload / "PROVENANCE.json"
        original_inventory = final.read_bytes()
        paths = package.layout()
        for key, message in (("win", "Pre-sign input differs"), ("resource", "differs from clean source"),
                             ("constants", "differs from clean source"), ("plist", "Pre-sign input differs"),
                             ("LICENSE", "Source license mismatch"), ("SDK-LICENSE.md", "Source license mismatch")):
            name = paths.get(key, key)
            target = payload / name
            original = target.read_bytes()
            if key == "plist":
                changed = plistlib.loads(original)
                changed["UnrelatedRepackagingField"] = "changed"
                target.write_bytes(plistlib.dumps(changed))
            else:
                target.write_bytes(original + b"repackaged")
            data = package.read_json(final)
            data["files"] = package.hashes(payload, ("PROVENANCE.json",))
            package.write_json(final, data)
            with self.subTest(file=name), self.assertRaisesRegex(package.PackageError, message):
                package.verify(payload, final)
            target.write_bytes(original)
            final.write_bytes(original_inventory)

    def test_inventory_refuses_drift_before_writing_recomputed_inventory(self):
        payload = self.signed_payload()
        final = payload / "PROVENANCE.json"
        original_inventory = final.read_bytes()
        paths = package.layout()
        for key in ("win", "resource", "constants", "plist", "LICENSE", "SDK-LICENSE.md"):
            name = paths.get(key, key)
            target = payload / name
            original = target.read_bytes()
            if key == "plist":
                changed = plistlib.loads(original)
                changed["RepackagingField"] = True
                target.write_bytes(plistlib.dumps(changed))
            else:
                target.write_bytes(original + b"drift")
            final.unlink()
            with self.subTest(file=name), self.assertRaises(package.PackageError):
                package.inventory(payload, final, "AB12345678")
            self.assertFalse(final.exists())
            target.write_bytes(original)
            final.write_bytes(original_inventory)

    def test_recomputed_final_inventory_rechecks_native_and_plist_files(self):
        payload = self.signed_payload()
        final = payload / "PROVENANCE.json"
        paths = package.layout()
        self.native.reset_mock()
        package.verify(payload, final)
        if sys.platform == "darwin":
            self.native.assert_called_once_with(payload / paths["mac"])
        original_inventory = final.read_bytes()
        for key, message in (("win", "AMD64"), ("plist", "CFBundleIdentifier")):
            target = payload / paths[key]
            original = target.read_bytes()
            if key == "win":
                changed = bytearray(original)
                struct.pack_into("<H", changed, 68, 0x14C)
                target.write_bytes(changed)
            else:
                changed = plistlib.loads(original)
                changed["CFBundleIdentifier"] = "wrong.identifier"
                target.write_bytes(plistlib.dumps(changed))
            data = package.read_json(final)
            data["files"] = package.hashes(payload, ("PROVENANCE.json",))
            package.write_json(final, data)
            with self.subTest(file=key), self.assertRaisesRegex(package.PackageError, message):
                package.verify(payload, final)
            target.write_bytes(original)
            final.write_bytes(original_inventory)
        with patch.object(sys, "platform", "darwin"):
            self.native.side_effect = package.PackageError("Final Mach-O architecture/dependency check failed")
            with self.assertRaisesRegex(package.PackageError, "Final Mach-O"):
                package.verify(payload, final)
            final.unlink()
            with self.assertRaisesRegex(package.PackageError, "Final Mach-O"):
                package.inventory(payload, final, "AB12345678")

    def test_inventory_requires_signature_and_correct_output(self):
        payload = self.assemble()
        with self.assertRaisesRegex(package.PackageError, "Missing signed"):
            package.inventory(payload, payload / "PROVENANCE.json", "AB12345678")
        with self.assertRaisesRegex(package.PackageError, "must be"):
            package.inventory(payload, payload / "other.json")

    def test_fresh_output(self):
        self.output.mkdir()
        with self.assertRaisesRegex(package.PackageError, "already exists"):
            self.assemble()

    def test_snapshot_gitlink_dirty_and_version(self):
        def git(args, cwd=None):
            if args[1:3] == ["rev-parse", "HEAD"]:
                return (BINDING["source_commit"] + "\n").encode()
            if args[1] == "ls-tree":
                return f'160000 commit {BINDING["sdk_commit"]}\t4D-Plugin-SDK\0'.encode()
            if "-C" in args:
                return (BINDING["sdk_commit"] + "\n").encode()
            if args[1] == "status":
                return b""
            if args[-1].endswith(":.github/plugin-pipeline.json"):
                return (self.root / ".github/plugin-pipeline.json").read_bytes()
            return b"1.2.3\n"
        with patch.object(package, "run", side_effect=git):
            self.assertEqual(self.real_snapshot(), BINDING)
            (self.root / "VERSION").write_bytes(b"1.2.4\n")
            with self.assertRaisesRegex(package.PackageError, "immutable"):
                self.real_snapshot()
        (self.root / "VERSION").write_bytes(b"1.2.3\n")
        for which, replacement, message in (("ls-tree", b"100644 blob a\t4D-Plugin-SDK\0", "gitlink"),
                                             ("status", b" M file\n", "dirty")):
            with patch.object(package, "run", side_effect=lambda a: replacement if a[1] == which else git(a)):
                with self.assertRaisesRegex(package.PackageError, message):
                    self.real_snapshot()

    def test_configured_sdk_binding_and_license_reads(self):
        sdk_dir = "vendor/custom-sdk"
        package.write_json(self.root / ".github/plugin-pipeline.json", {**CONFIG, "sdk_dir": sdk_dir})
        commands = []

        def git(args, cwd=None):
            commands.append(args)
            if args[1] == "ls-tree":
                return f'160000 commit {BINDING["sdk_commit"]}\t{sdk_dir}\0'.encode()
            if args[:3] == ["git", "-C", sdk_dir]:
                return (BINDING["sdk_commit"] + "\n").encode()
            if args[1] == "status":
                return b""
            if args[1] == "show":
                if args[-1].endswith(":.github/plugin-pipeline.json"):
                    return (self.root / ".github/plugin-pipeline.json").read_bytes()
                return b"1.2.3\n"
            return (BINDING["source_commit"] + "\n").encode()

        with patch.object(package, "run", side_effect=git):
            self.assertEqual(self.real_snapshot(), BINDING)
        self.assertIn(["git", "ls-tree", "-z", BINDING["source_commit"], "--", sdk_dir], commands)
        self.assertIn(["git", "-C", sdk_dir, "rev-parse", "HEAD"], commands)
        self.runner.reset_mock()
        payload = self.assemble()
        package.inventory(payload, payload / "PROVENANCE.json")
        package.verify(payload, payload / "PROVENANCE.json")
        sdk_reads = [call.args[0] for call in self.runner.call_args_list if "-C" in call.args[0]]
        self.assertEqual(len(sdk_reads), 3)
        self.assertTrue(all(args == ["git", "-C", sdk_dir, "show",
                                     f'{BINDING["sdk_commit"]}:LICENSE.md'] for args in sdk_reads))

    def test_snapshot_config_and_numeric_version_are_immutable(self):
        path = self.root / ".github/plugin-pipeline.json"
        committed_config = path.read_bytes()

        def git(args, cwd=None):
            if args[1] == "rev-parse":
                return (BINDING["source_commit"] + "\n").encode()
            if args[-1].endswith(":.github/plugin-pipeline.json"):
                return committed_config
            if args[1] == "ls-tree":
                return f'160000 commit {BINDING["sdk_commit"]}\t4D-Plugin-SDK\0'.encode()
            if "-C" in args:
                return (BINDING["sdk_commit"] + "\n").encode()
            if args[1] == "status":
                return b""
            return (self.root / "VERSION").read_bytes()

        with patch.object(package, "run", side_effect=git):
            for version in (b"1.2.3-rc.1\n", b"1.2.3+build\n", b"01.2.3\n", b" 1.2.3\n"):
                (self.root / "VERSION").write_bytes(version)
                with self.subTest(version=version), self.assertRaisesRegex(package.PackageError, "semantic version"):
                    self.real_snapshot()
            package.write_json(path, {**CONFIG, "release_branch": "other"})
            with self.assertRaisesRegex(package.PackageError, "config bytes differ"):
                self.real_snapshot()

    def test_native_system_dependency_rules(self):
        for imports, commands, architectures, valid in (
            ("binary:\n\t/usr/lib/libSystem.B.dylib (compatibility version 1)\n", "", "arm64 x86_64", True),
            ("binary:\n\t@rpath/libfoo.dylib (compatibility version 1)\n", "", "arm64 x86_64", False),
            ("binary:\n\t/usr/lib/libSystem.B.dylib (compatibility version 1)\n", "cmd LC_RPATH", "arm64 x86_64", False),
            ("binary:\n\t/usr/lib/libSystem.B.dylib (compatibility version 1)\n", "", "arm64", False)):
            with patch.object(sys, "platform", "darwin"), patch.object(package, "run", side_effect=lambda args:
                    (architectures if args[0] == "lipo" else imports if args[1] == "-L" else commands).encode()):
                if valid:
                    self.real_native(Path("fixture"))
                else:
                    with self.assertRaises(package.PackageError):
                        self.real_native(Path("fixture"))


class DistributionTests(Fixture):
    def setUp(self):
        super().setUp()
        import release_checks
        self.payload = self.signed_payload()
        self.distribution = self.root / "distribution"
        self.distribution.mkdir()
        self.zip = self.distribution / "get-icon-1.2.3.zip"
        with zipfile.ZipFile(self.zip, "w") as zipped:
            for name, path in package.files_in(self.payload).items():
                zipped.write(path, name)
        (self.distribution / "get-icon-1.2.3.dmg").write_bytes(b"fake DMG")
        shutil.copyfile(self.payload / "PROVENANCE.json", self.distribution / "PROVENANCE.json")
        for format in ("zip", "dmg"):
            package.write_json(self.distribution / f"{format}-notarization.json", {"id": "request", "status": "Accepted"})
        release_checks.checksums(self.distribution)
        self.extract = self.root / "extracted"
        self.stack.enter_context(patch.object(sys, "platform", "darwin"))
        self.tools = self.stack.enter_context(patch.object(verify_distribution, "native", return_value=DETAILS))

    def test_roundtrip_all_native_gates(self):
        result = verify_distribution.verify_distribution(self.distribution, self.extract)
        self.assertTrue(result["distribution_verified"])
        self.assertEqual(result["bundle"], str(self.extract.absolute() / "get-icon.bundle"))
        commands = [call.args[0] for call in self.tools.call_args_list]
        self.assertTrue(any(args[:3] == ["xcrun", "stapler", "validate"] for args in commands))
        self.assertTrue(any(args[0] == "spctl" for args in commands))
        self.assertEqual(sum(args[:3] == ["codesign", "--verify", "--strict"] for args in commands), 3)

    def test_checksum_mutation_prevents_extraction(self):
        self.zip.write_bytes(self.zip.read_bytes() + b"changed")
        with self.assertRaisesRegex(package.PackageError, "SHA-256"):
            verify_distribution.verify_distribution(self.distribution, self.extract)
        self.assertFalse(self.extract.exists())

    def test_checksum_duplicate_or_missing(self):
        sums = self.distribution / "SHA256SUMS"
        sums.write_bytes(sums.read_bytes() + sums.read_bytes().splitlines(keepends=True)[0])
        with self.assertRaisesRegex(package.PackageError, "Duplicate"):
            verify_distribution.verify_distribution(self.distribution, self.extract)

    def test_not_accepted(self):
        import release_checks
        package.write_json(self.distribution / "dmg-notarization.json", {"id": "request", "status": "Invalid"})
        release_checks.checksums(self.distribution)
        with self.assertRaisesRegex(package.PackageError, "Accepted"):
            verify_distribution.verify_distribution(self.distribution, self.extract)

    def test_native_failure_is_fatal(self):
        import subprocess
        self.tools.side_effect = subprocess.CalledProcessError(1, "codesign")
        with self.assertRaises(subprocess.CalledProcessError):
            verify_distribution.verify_distribution(self.distribution, self.extract)

    def test_zip_traversal_rejected_before_extraction(self):
        with zipfile.ZipFile(self.zip, "a") as zipped:
            zipped.writestr("../escape", b"no")
        with self.assertRaises(package.PackageError):
            verify_distribution.extract_zip(self.zip, self.extract, package.hashes(self.payload, ("PROVENANCE.json",)))
        self.assertFalse(self.extract.exists())

    def test_zip_symlink_rejected(self):
        with zipfile.ZipFile(self.zip, "a") as zipped:
            entry = zipfile.ZipInfo("link")
            entry.external_attr = (0o120777 << 16)
            zipped.writestr(entry, b"../escape")
        with self.assertRaisesRegex(package.PackageError, "Non-regular"):
            verify_distribution.extract_zip(self.zip, self.extract, package.hashes(self.payload, ("PROVENANCE.json",)))

    def test_existing_output_rejected(self):
        self.extract.mkdir()
        with self.assertRaisesRegex(package.PackageError, "already exists"):
            verify_distribution.verify_distribution(self.distribution, self.extract)

    def rewrite_archive(self, omit=None, tamper=None):
        with zipfile.ZipFile(self.zip, "w") as zipped:
            for name, path in package.files_in(self.payload).items():
                if name != omit:
                    zipped.writestr(name, b"tampered" if name == tamper else path.read_bytes())
        import release_checks
        release_checks.checksums(self.distribution)

    def test_missing_signed_file_rejected(self):
        self.rewrite_archive(omit=package.layout()["signature"])
        with self.assertRaisesRegex(package.PackageError, "ZIP layout mismatch"):
            verify_distribution.verify_distribution(self.distribution, self.extract)
        self.assertFalse(self.extract.exists())
        self.tools.assert_not_called()

    def test_rechecksummed_archive_tampering_rejected(self):
        self.rewrite_archive(tamper=package.layout()["mac"])
        with self.assertRaisesRegex(package.PackageError, "SHA-256 mismatch"):
            verify_distribution.verify_distribution(self.distribution, self.extract)
        self.tools.assert_not_called()

    def test_archived_inventory_must_match_external_inventory(self):
        import release_checks
        path = self.distribution / "PROVENANCE.json"
        data = package.read_json(path)
        data["expected_team"] = "CD12345678"
        package.write_json(path, data)
        release_checks.checksums(self.distribution)
        with self.assertRaisesRegex(package.PackageError, "Archived inventory differs"):
            verify_distribution.verify_distribution(self.distribution, self.extract)
        self.tools.assert_not_called()

    def test_zip_appledouble_metadata_is_not_silently_extracted(self):
        with zipfile.ZipFile(self.zip, "a") as zipped:
            zipped.writestr("__MACOSX/._get-icon.bundle", b"resource fork")
        with self.assertRaisesRegex(package.PackageError, "ZIP layout mismatch"):
            verify_distribution.extract_zip(self.zip, self.extract, package.hashes(self.payload, ("PROVENANCE.json",)))
        self.assertFalse(self.extract.exists())

    def test_distribution_native_signature_notary_and_gatekeeper_failures(self):
        import subprocess
        for failure in ("team", "runtime", "stapler", "spctl"):
            def native(args):
                if failure == "stapler" and args[0] == "xcrun":
                    raise subprocess.CalledProcessError(1, args)
                if failure == "spctl" and args[0] == "spctl":
                    raise subprocess.CalledProcessError(1, args)
                if failure == "team":
                    return DETAILS.replace("AB12345678", "CD12345678")
                if failure == "runtime":
                    return DETAILS.replace("flags=0x10000(runtime)", "flags=0x0(none)")
                return DETAILS
            self.tools.side_effect = native
            with self.subTest(failure=failure), self.assertRaises((package.PackageError, subprocess.CalledProcessError)):
                verify_distribution.verify_distribution(self.distribution, self.extract)
            if self.extract.exists():
                shutil.rmtree(self.extract)


if __name__ == "__main__":
    unittest.main()
