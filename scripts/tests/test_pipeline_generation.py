"""Offline generator and parsed-workflow contract tests; helper files are fixtures."""

import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch
import uuid

import yaml


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "pipeline_generator", ROOT / "scripts" / "generate_plugin_pipeline.py"
)
generator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(generator)


def parsed(files):
    return {Path(name).name: yaml.load(content, Loader=yaml.BaseLoader)
            for name, content in files.items()
            if name.startswith(".github/workflows/")}


def steps(workflow, job):
    return workflow["jobs"][job].get("steps", [])


def named(workflow, job, name):
    matches = [step for step in steps(workflow, job) if step.get("name") == name]
    assert len(matches) == 1, name
    return matches[0]


def assert_contract(workflows, action, contract, config):
    """Fail on structural drift, including security-critical artifact ordering."""
    assert set(workflows) == set(contract["workflows"])
    branch = config["release_branch"]
    guard = (
        "[ \"$GITHUB_EVENT_NAME\" = workflow_dispatch ]",
        f"[ \"$DEFAULT_BRANCH\" = '{branch}' ]",
        f"[ \"$GITHUB_REF\" = 'refs/heads/{branch}' ]",
    )
    for filename, expected in contract["workflows"].items():
        workflow = workflows[filename]
        assert workflow["name"] == expected["name"]
        assert set(workflow["on"]) == set(expected["events"])
        assert workflow["permissions"] == expected["permissions"]
        assert set(workflow["jobs"]) == set(expected["jobs"])
        for job, dependencies in expected["jobs"].items():
            actual = workflow["jobs"][job]
            needs = actual.get("needs", [])
            if isinstance(needs, str):
                needs = [needs]
            assert needs == dependencies
            assert actual.get("permissions", workflow["permissions"]) == (
                {"contents": "write"} if filename == "bump-version.yml"
                or filename == "release.yml" and job == "publish"
                else {"contents": "read"}
            )
            if "uses" not in actual:
                checkouts = [step for step in steps(workflow, job)
                             if step.get("uses", "").startswith("actions/checkout@")]
                assert len(checkouts) == 1
                for checkout in checkouts:
                    assert checkout["with"]["ref"] == contract["checkout_ref"]
                    assert checkout["with"]["fetch-depth"] == "0"
                    if filename != "bump-version.yml":
                        assert checkout["with"]["submodules"] == "recursive"
                if filename != "bump-version.yml":
                    assert steps(workflow, job)[1] == {
                        "name": "Verify pipeline ownership",
                        "run": "python3 scripts/check_pipeline.py",
                    }
            for step in steps(workflow, job):
                if step.get("uses", "").startswith("actions/download-artifact@"):
                    assert set(step["with"]) == {"name", "path"}
                    assert "${{ github.sha }}" in step["with"]["name"]
                if step.get("uses", "").startswith("actions/upload-artifact@"):
                    assert step["with"]["if-no-files-found"] in ("error", "ignore")
                text = json.dumps(step)
                if "secrets." in text:
                    assert filename == "release.yml" and job == "sign-package"
                    assert actual["environment"] == config["signing_environment"]
                assert "secrets.PAT" not in text
                assert "workflow_dispatch" not in step.get("uses", "")
                if step.get("uses") == "./.github/actions/tool4d-test":
                    assert step["with"] == {
                        "project": config["test_project"],
                        "startup_method": config["startup_method"],
                    }
    bump = workflows["bump-version.yml"]
    mode = bump["on"]["workflow_dispatch"]["inputs"]
    assert set(mode) == {"mode"}
    assert mode["mode"] == {
        "type": "choice", "description": "Semantic version bump",
        "options": ["patch", "minor", "major"], "default": "patch", "required": "true",
    }
    for line in guard:
        assert line in steps(bump, "bump")[0]["run"]
    bump_run = named(bump, "bump", "Bump VERSION and commit without creating tags")["run"]
    assert "git add VERSION" in bump_run and "git commit" in bump_run
    assert f"git push origin HEAD:refs/heads/{branch}" in bump_run
    assert "git tag " not in bump_run and "git push origin \"v" not in bump_run
    test = workflows["test.yml"]
    assert all(value in ("", None) for value in test["on"].values())
    matrix = test["jobs"]["test"]["strategy"]
    assert matrix["fail-fast"] == "false"
    assert matrix["matrix"]["include"] == [
        {"os": "macos-latest"}, {"os": "windows-latest"},
    ]
    assert '-DCMAKE_OSX_ARCHITECTURES="arm64;x86_64"' in named(
        test, "test", "Configure (macOS)")["run"]
    assert "-A x64" in named(test, "test", "Configure (Windows)")["run"]
    assert "-G " not in named(test, "test", "Configure (Windows)")["run"]
    assert named(test, "test", "Native tests")["if"] == "env.NATIVE_TESTS == 'true'"
    assert test["jobs"]["test"]["env"] == {
        "NATIVE_TESTS": "true" if config["native_tests"] else "false",
    }
    helper_tests = named(test, "test", "Package provenance and release checks tests")
    assert helper_tests["shell"] == "bash"
    assert "set -euo pipefail" in helper_tests["run"]
    assert "for suite in test_package.py test_release_checks.py test_sign_package.py test_publish_release.py test_verify_release.py test_pipeline_contract.py;" in helper_tests["run"]
    assert 'python3 -m unittest discover -s scripts/tests -p "$suite" -v' in helper_tests["run"]
    native_steps = steps(test, "test")
    integration = named(test, "test", "4D integration")
    assert "if" not in integration and "continue-on-error" not in integration
    assert native_steps.index(integration) > native_steps.index(named(test, "test", "Build"))
    assert native_steps.index(integration) < native_steps.index(
        named(test, "test", "Stage tested native input (macOS)"))
    native_upload = native_steps[-1]
    assert native_upload["uses"] == "actions/upload-artifact@v4"
    assert native_upload["with"]["name"] == (
        "native-${{ runner.os == 'macOS' && 'macos' || 'windows' }}-${{ github.sha }}"
    )
    assert native_upload["with"]["path"] == (
        ".package-inputs/${{ runner.os == 'macOS' && 'macos' || 'windows' }}/"
    )
    assert native_upload["with"]["include-hidden-files"] == "true"
    assembly = named(test, "package-layout", "Validate same-run cross-platform assembly")["run"]
    assert "package.py assemble --inputs .package-inputs" in assembly
    assert "package.py verify --payload .package-validation/payload" in assembly
    layout = named(test, "package-layout", "Exercise ZIP and DMG layout")["run"]
    for token in ("ditto -x -k", "package.py verify --payload .package-validation/extracted",
                  "hdiutil create", "hdiutil attach",
                  "package.py verify --payload .package-validation/mounted", "trap "):
        assert token in layout
    installed_bundle = str(Path(config["test_project"]).parent.parent / "Plugins"
                           / (config["product"] + ".bundle"))
    assert f'[ ! -e "{installed_bundle}" ]' in layout
    assert f'[ ! -L "{installed_bundle}" ]' in layout
    assert "rm -rf" not in layout
    assert steps(test, "package-layout")[-1]["uses"] == "./.github/actions/tool4d-test"
    release = workflows["release.yml"]
    assert release["on"]["workflow_dispatch"] in ("", None)
    for line in guard:
        assert line in named(release, "authorize", "Require an explicit default-branch dispatch")["run"]
    expected_if = (
        f"github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/{branch}'"
    )
    assert release["jobs"]["build"]["uses"] == "./.github/workflows/test.yml"
    assert set(release["jobs"]["build"]) == {"needs", "if", "uses"}
    for job in ("build", "sign-package"):
        assert release["jobs"][job]["if"] == expected_if
    assert release["jobs"]["sign-package"]["environment"] == config["signing_environment"]
    sign = named(release, "sign-package", "Developer ID sign, notarize, and package")
    secrets = {
        "APPLE_DEVELOPER_ID_CERTIFICATE", "APPLE_DEVELOPER_ID_CERTIFICATE_PASSWORD",
        "KEYCHAIN_PASSWORD", "NOTARYTOOL_APPLE_ID", "NOTARYTOOL_TEAM_ID",
        "NOTARYTOOL_PASSWORD",
    }
    assert sign["env"] == {name: "${{ secrets." + name + " }}" for name in secrets}
    assert sign["run"] == "bash scripts/sign-package.sh .release-package/payload .signed-distribution"
    signed_steps = steps(release, "sign-package")
    verify = named(release, "sign-package",
                   "Reverify distribution and install verified ZIP for smoke testing")
    assert "verify_distribution.py --directory .signed-distribution --output .signed-extracted" in verify["run"]
    assert f'[ ! -e "{installed_bundle}" ]' in verify["run"]
    assert f'[ ! -L "{installed_bundle}" ]' in verify["run"]
    assert "rm -rf" not in verify["run"]
    smoke = named(release, "sign-package", "Smoke test the signed packaged plugin")
    assert signed_steps.index(sign) < signed_steps.index(verify) < signed_steps.index(smoke)
    assert signed_steps.index(smoke) < len(signed_steps) - 1
    upload = signed_steps[-1]
    assert upload["uses"] == "actions/upload-artifact@v4"
    signed_name = config["product"] + "-signed-packages-${{ github.sha }}"
    assert upload["with"]["name"] == signed_name
    assert upload["with"]["path"] == ".signed-distribution/"
    assert upload["with"]["if-no-files-found"] == "error"
    for job in ("package-layout", "sign-package"):
        workflow = test if job == "package-layout" else release
        downloads = [step["with"] for step in steps(workflow, job)
                     if step.get("uses") == "actions/download-artifact@v4"]
        assert downloads == [
            {"name": "native-macos-${{ github.sha }}", "path": ".package-inputs/macos"},
            {"name": "native-windows-${{ github.sha }}", "path": ".package-inputs/windows"},
        ]
    publish_steps = steps(release, "publish")
    assert publish_steps[2]["with"] == {"name": signed_name, "path": ".signed-distribution"}
    assert len(publish_steps) == 4
    publication = publish_steps[3]
    assert publication["env"] == {"GH_TOKEN": "${{ github.token }}"}
    assert publication["run"] == contract["publication_helper"]
    assert set(action["inputs"]) == set(contract["tool4d_inputs"])
    assert all(value["required"] == "true" and "default" not in value
               for value in action["inputs"].values())
    assert action["runs"]["using"] == "composite"
    action_steps = action["runs"]["steps"]
    assert "grep -qx PASS" in action_steps[1]["run"]
    assert "set -euo pipefail" in action_steps[1]["run"]
    assert "$exitCode -ne 0" in action_steps[2]["run"]
    assert "-cmatch '^PASS$'" in action_steps[2]["run"]
    for step in action_steps:
        assert step["env"] == {
            "TEST_PROJECT": "${{ inputs.project }}",
            "STARTUP_METHOD": "${{ inputs.startup_method }}",
        }
        assert "${{ inputs." not in step["run"]


class PipelineGenerationTests(unittest.TestCase):
    def setUp(self):
        self.scratch = ROOT / (".pipeline-test-" + uuid.uuid4().hex)
        self.fixture = self.scratch / "canonical"
        self.fixture.mkdir(parents=True)
        shutil.copytree(ROOT / "templates", self.fixture / "templates")
        self.config = json.loads((ROOT / "templates/plugin-pipeline/example.json").read_text())
        for source in generator.HELPER_FILES:
            target = self.fixture / source
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("# synthetic offline helper fixture\n")
        self.output = self.scratch / "output"

    def tearDown(self):
        shutil.rmtree(self.scratch)

    def rendered(self):
        return generator.render(self.config, root=self.fixture)

    def assert_rendered_contract(self, files=None):
        files = files or self.rendered()
        assert_contract(
            parsed(files),
            yaml.load(files[".github/actions/tool4d-test/action.yml"], Loader=yaml.BaseLoader),
            json.loads(files[".github/plugin-pipeline-contract.json"]), self.config,
        )

    def test_yaml_contract(self):
        self.assert_rendered_contract()

    def test_exactly_three_inert_canonical_workflows(self):
        paths = set((ROOT / "templates/plugin-pipeline").glob("*.yml.in"))
        self.assertEqual({path.name for path in paths}, {
            "bump-version.yml.in", "test.yml.in", "release.yml.in", "tool4d-test.action.yml.in",
        })
        self.assertEqual(len(parsed(self.rendered())), 3)
        self.assertFalse(list((ROOT / ".github/workflows").glob("*.yml")))
        self.assertFalse(list((ROOT / ".github/workflows").glob("*.yaml")))

    def test_deterministic_idempotent_and_check(self):
        first = generator.generate(self.config, self.output, root=self.fixture)
        second = generator.generate(self.config, self.output, root=self.fixture)
        self.assertEqual(first, second)
        before = {name: (self.output / name).stat().st_mtime_ns for name in first}
        generator.generate(self.config, self.output, check=True, root=self.fixture)
        self.assertEqual(before, {name: (self.output / name).stat().st_mtime_ns for name in first})
        self.assertEqual(json.loads(first[".github/plugin-pipeline.json"]), self.config)
        self.assertEqual(first["scripts/package.py"], b"# synthetic offline helper fixture\n")
        self.assertNotIn("scripts/tests/test_release_secrets.py", first)
        self.assertEqual(first["templates/plugin-pipeline/REFERENCE.txt"],
                         b"# synthetic offline helper fixture\n")
        self.assertEqual(first["scripts/PIPELINE-LICENSE.txt"],
                         first["templates/plugin-pipeline/REFERENCE.txt"])

    def test_missing_check_is_read_only(self):
        with self.assertRaisesRegex(ValueError, "missing"):
            generator.generate(self.config, self.output, check=True, root=self.fixture)
        self.assertFalse(self.output.exists())

    def test_drift_refuses_entire_write_set(self):
        self.output.mkdir()
        conflict = self.output / ".github/workflows/release.yml"
        conflict.parent.mkdir(parents=True)
        conflict.write_text("existing release\n")
        for check in (False, True):
            with self.subTest(check=check), self.assertRaises(ValueError):
                generator.generate(self.config, self.output, check=check, root=self.fixture)
            self.assertEqual(conflict.read_text(), "existing release\n")
            self.assertFalse((self.output / ".github/workflows/bump-version.yml").exists())

    def test_check_detects_deleted_and_changed_files(self):
        generator.generate(self.config, self.output, root=self.fixture)
        helper = self.output / "scripts/package.py"
        helper.unlink()
        with self.assertRaisesRegex(ValueError, "missing"):
            generator.generate(self.config, self.output, check=True, root=self.fixture)
        helper.write_text("drift")
        with self.assertRaisesRegex(ValueError, "differs"):
            generator.generate(self.config, self.output, check=True, root=self.fixture)

    def test_extra_workflow_refuses_generation(self):
        path = self.output / ".github/workflows/fourth.yaml"
        path.parent.mkdir(parents=True)
        path.write_text("name: unexpected")
        with self.assertRaisesRegex(ValueError, "Unexpected workflows"):
            generator.generate(self.config, self.output, root=self.fixture)
        self.assertFalse((self.output / ".github/workflows/test.yml").exists())

    def test_symlinks_refused(self):
        self.output.mkdir()
        (self.output / "scripts").symlink_to(self.fixture / "scripts", target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            generator.generate(self.config, self.output, root=self.fixture)

    def test_missing_helper_fail_closed(self):
        (self.fixture / "scripts/verify_distribution.py").unlink()
        with self.assertRaisesRegex(ValueError, "Canonical helper not available"):
            generator.generate(self.config, self.output, root=self.fixture)
        self.assertFalse(self.output.exists())

    def test_owned_update_and_new_fingerprints(self):
        original = generator.generate(self.config, self.output, root=self.fixture)
        changed = dict(self.config, native_tests=True)
        with self.assertRaisesRegex(ValueError, "differs"):
            generator.generate(changed, self.output, root=self.fixture)
        updated = generator.generate(changed, self.output, root=self.fixture, update_managed=True)
        self.assertNotEqual(original[generator.MANIFEST], updated[generator.MANIFEST])
        self.assertIn(b"NATIVE_TESTS: 'true'", updated[".github/workflows/test.yml"])
        generator.generate(changed, self.output, check=True, root=self.fixture)
        manifest = json.loads(updated[generator.MANIFEST])
        self.assertEqual(manifest["config_sha256"],
                         generator.fingerprint(updated[".github/plugin-pipeline.json"]))
        self.assertEqual(manifest["contract_sha256"],
                         generator.fingerprint(updated[".github/plugin-pipeline-contract.json"]))

    def test_additive_gitignore_and_owned_section_update(self):
        self.output.mkdir()
        ignore = self.output / ".gitignore"
        user_patterns = b"# user rules\n/custom-cache/\n"
        ignore.write_bytes(user_patterns)
        generator.generate(self.config, self.output, root=self.fixture)
        self.assertTrue(ignore.read_bytes().startswith(user_patterns))
        self.assertIn(b"/example/build/\n", ignore.read_bytes())
        self.assertIn(b"/example/example-test/Plugins/\n", ignore.read_bytes())
        ignore.write_bytes(ignore.read_bytes() + b"\n/another-user-pattern/\n")
        generator.generate(self.config, self.output, root=self.fixture, check=True)
        updated = dict(self.config, build_dir="example/new-build")
        generator.generate(updated, self.output, root=self.fixture, update_managed=True)
        self.assertTrue(ignore.read_bytes().startswith(user_patterns))
        self.assertTrue(ignore.read_bytes().endswith(b"\n/another-user-pattern/\n"))
        self.assertIn(b"/example/new-build/\n", ignore.read_bytes())
        self.assertNotIn(b"/example/build/\n", ignore.read_bytes())
        generator.generate(updated, self.output, root=self.fixture, check=True)

    def test_gitignore_managed_drift_and_ambiguous_markers_fail_closed(self):
        original = generator.generate(self.config, self.output, root=self.fixture)
        ignore = self.output / ".gitignore"
        ignore.write_bytes(original[".gitignore"].replace(b"/.local-tools/\n", b""))
        for options in ({"check": True}, {"update_managed": True}):
            with self.assertRaisesRegex(ValueError, "gitignore|edited or missing"):
                generator.generate(self.config, self.output, root=self.fixture, **options)
        ignore.write_bytes(original[".gitignore"] + generator.IGNORE_BEGIN + b"\n")
        with self.assertRaisesRegex(ValueError, "Ambiguous"):
            generator.generate(self.config, self.output, root=self.fixture)

    def test_additive_attributes_preserve_custom_rules_and_keep_lf_effective(self):
        self.output.mkdir()
        attributes = self.output / ".gitattributes"
        custom = b"*.custom binary\nVERSION text eol=crlf\n"
        attributes.write_bytes(custom)
        generator.generate(self.config, self.output, root=self.fixture)
        self.assertTrue(attributes.read_bytes().startswith(custom))
        self.assertIn(b"example/manifest.json text eol=lf\n", attributes.read_bytes())
        self.assertIn(b"example/constants.xlf text eol=lf\n", attributes.read_bytes())
        attributes.write_bytes(attributes.read_bytes() + b"LICENSE text eol=crlf\n")
        with self.assertRaises(ValueError):
            generator.generate(self.config, self.output, root=self.fixture, check=True)
        generator.generate(self.config, self.output, root=self.fixture, update_managed=True)
        self.assertIn(b"LICENSE text eol=crlf\n", attributes.read_bytes())
        self.assertTrue(attributes.read_bytes().endswith(generator.ATTR_END + b"\n"))
        generator.generate(self.config, self.output, root=self.fixture, check=True)
        def git(*args):
            return subprocess.check_output(["git", *args], cwd=self.output, stderr=subprocess.DEVNULL)
        git("init", "-q")
        paths = ["example/manifest.json", "example/constants.xlf", "VERSION", "LICENSE"]
        for name in paths:
            path = self.output / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"first\r\nsecond\r\n")
        report = git("check-attr", "text", "eol", "--", *paths).decode()
        for name in paths:
            self.assertIn(f"{name}: text: set\n", report)
            self.assertIn(f"{name}: eol: lf\n", report)
        git("-c", "core.autocrlf=true", "add", ".gitattributes", *paths)
        for name in paths:
            self.assertEqual(git("show", ":" + name), b"first\nsecond\n")

    def test_generated_files_stay_lf_in_an_autocrlf_checkout(self):
        files = generator.generate(self.config, self.output, root=self.fixture)
        attributes = files[".gitattributes"].decode()
        for name in (*files, ".gitattributes", generator.MANIFEST):
            self.assertIn(f"{name} text eol=lf\n", attributes)

        def git(cwd, *args):
            return subprocess.check_output(["git", *args], cwd=cwd, stderr=subprocess.DEVNULL)
        git(self.output, "init", "-q")
        git(self.output, "add", ".")
        git(self.output, "-c", "user.name=test", "-c", "user.email=test@example.invalid",
            "commit", "-q", "-m", "generated")
        clone = self.output.parent / "autocrlf-clone"
        subprocess.check_call(["git", "-c", "core.autocrlf=true", "clone", "-q",
                               str(self.output), str(clone)], stderr=subprocess.DEVNULL)
        for name in files:
            self.assertNotIn(b"\r\n", (clone / name).read_bytes(), name)
        # The fixture's helpers are stand-ins; check the clone with the real checker.
        spec = importlib.util.spec_from_file_location(
            "real_check_pipeline", ROOT / "scripts" / "check_pipeline.py")
        checker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(checker)
        checker.check(clone)

    def test_attribute_drift_refused_and_source_substitutions(self):
        original = generator.generate(self.config, self.output, root=self.fixture)
        attributes = self.output / ".gitattributes"
        attributes.write_bytes(original[".gitattributes"].replace(b"eol=lf", b"eol=crlf"))
        for options in ({"check": True}, {"update_managed": True}):
            with self.assertRaisesRegex(ValueError, "gitattributes|edited or missing"):
                generator.generate(self.config, self.output, root=self.fixture, **options)
        self.config.update(source_dir="plugins/custom", build_dir="plugins/custom/build",
                           test_project="plugins/custom/test/Project/custom.4DProject")
        data = self.rendered()[".gitattributes"]
        self.assertIn(b"plugins/custom/manifest.json text eol=lf\n", data)
        self.assertIn(b"plugins/custom/constants.xlf text eol=lf\n", data)
        self.assertNotIn(b"4D-Plugin-SDK", data)

    def test_pipeline_outputs_do_not_dirty_snapshot_but_tracked_edits_do(self):
        helper_path = ROOT / "scripts/package.py"
        if not helper_path.is_file():
            self.skipTest("Canonical package helper not available")
        generator.generate(self.config, self.output, root=ROOT)
        helper_spec = importlib.util.spec_from_file_location("pipeline_gitignore_package", helper_path)
        helper = importlib.util.module_from_spec(helper_spec)
        helper_spec.loader.exec_module(helper)
        helper.ROOT = self.output
        version = self.output / "VERSION"
        version.write_bytes(b"1.2.3\n")
        tracked = self.output / "example/build/tracked.txt"
        tracked.parent.mkdir(parents=True)
        tracked.write_text("baseline\n")
        def git(*args):
            return subprocess.check_output(["git", *args], cwd=self.output, stderr=subprocess.DEVNULL)
        git("init", "-q")
        git("add", ".")
        git("add", "-f", "example/build/tracked.txt")
        ignored = [
            "example/build/output", "example/example-test/Plugins/output",
            ".local-tools/output", ".package-inputs/output", ".package-validation/output",
            ".release-package/output", ".signed-distribution/output", ".signed-extracted/output",
            ".publish-extracted/output", ".draft-upload-check/output", ".draft-extracted/output",
            ".release-notes", "scripts/__pycache__/module.pyc",
        ]
        for name in ignored:
            path = self.output / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"pipeline output")
        self.assertEqual(git("ls-files", "--others", "--exclude-standard"), b"")
        def source_commands(args, cwd=None):
            if args == ["git", "rev-parse", "HEAD"]:
                return b"a" * 40 + b"\n"
            if args[:2] == ["git", "ls-tree"]:
                return b"160000 commit " + b"b" * 40 + b"\t4D-Plugin-SDK\0"
            if args[:2] == ["git", "-C"]:
                return b"b" * 40 + b"\n"
            if args[:2] == ["git", "status"]:
                # Immutable source/SDK refs are synthetic; dirty-path detection
                # uses the real index and ignores, without creating any commits.
                return git("ls-files", "--others", "--exclude-standard") + git("diff", "--name-only")
            if args[:2] == ["git", "show"]:
                return version.read_bytes() if args[2].endswith(":VERSION") else (
                    self.output / ".github/plugin-pipeline.json").read_bytes()
            raise AssertionError(args)
        with patch.object(helper, "run", side_effect=source_commands):
            self.assertEqual(helper.snapshot()["version"], "1.2.3")
            unexpected = self.output / "unexpected.txt"
            unexpected.write_text("unexpected\n")
            with self.assertRaisesRegex(ValueError, "dirty"):
                helper.snapshot()
            unexpected.unlink()
            tracked.write_text("tracked ignored modification\n")
            self.assertEqual(git("diff", "--name-only").strip(), b"example/build/tracked.txt")
            with self.assertRaisesRegex(ValueError, "dirty"):
                helper.snapshot()

    def test_owned_update_refuses_modified_files_and_bad_manifest(self):
        original = generator.generate(self.config, self.output, root=self.fixture)
        target = self.output / ".github/workflows/test.yml"
        target.write_bytes(original[".github/workflows/test.yml"] + b"\n# user edit\n")
        before = (self.output / generator.MANIFEST).read_bytes()
        with self.assertRaisesRegex(ValueError, "edited or missing"):
            generator.generate(dict(self.config, native_tests=True), self.output,
                               root=self.fixture, update_managed=True)
        self.assertEqual((self.output / generator.MANIFEST).read_bytes(), before)
        target.write_bytes(original[".github/workflows/test.yml"])
        manifest = json.loads(before)
        manifest["files"]["../unrelated"] = "0" * 64
        (self.output / generator.MANIFEST).write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "Invalid ownership manifest"):
            generator.generate(self.config, self.output, root=self.fixture, update_managed=True)
        self.assertEqual(target.read_bytes(), original[".github/workflows/test.yml"])

    def test_update_requires_owned_pipeline(self):
        with self.assertRaisesRegex(ValueError, "requires an existing ownership manifest"):
            generator.generate(self.config, self.output, root=self.fixture, update_managed=True)
        self.assertFalse(self.output.exists())
        for options in ({"check": True, "update_managed": True},
                        {"update_managed": True, "replace_legacy_workflows": True}):
            with self.assertRaises(ValueError):
                generator.generate(self.config, self.output, root=self.fixture, **options)

    def test_owned_update_adds_new_managed_helpers_without_overwriting(self):
        original = generator.generate(self.config, self.output, root=self.fixture)
        manifest = json.loads(original[generator.MANIFEST])
        added = "scripts/publish_release.py"
        (self.output / added).unlink()
        del manifest["files"][added]
        (self.output / generator.MANIFEST).write_text(json.dumps(manifest))
        generator.generate(self.config, self.output, root=self.fixture, update_managed=True)
        self.assertEqual((self.output / added).read_bytes(), original[added])
        (self.output / added).write_bytes(b"user-owned helper")
        (self.output / generator.MANIFEST).write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "refusing overwrite"):
            generator.generate(self.config, self.output, root=self.fixture, update_managed=True)
        self.assertEqual((self.output / added).read_bytes(), b"user-owned helper")

    def test_explicit_legacy_replacement_preserves_unrelated_files(self):
        legacy = self.output / ".github/workflows/test.yml"
        legacy.parent.mkdir(parents=True)
        legacy.write_text("name: legacy\n")
        unrelated = self.output / "scripts/keep.py"
        unrelated.parent.mkdir()
        unrelated.write_text("untouched\n")
        generator.generate(self.config, self.output, root=self.fixture,
                           replace_legacy_workflows=True)
        self.assertEqual(unrelated.read_text(), "untouched\n")
        self.assertIn("name: Build and Test", legacy.read_text())
        generator.generate(self.config, self.output, root=self.fixture, check=True)
        with self.assertRaisesRegex(ValueError, "Owned pipelines"):
            generator.generate(self.config, self.output, root=self.fixture,
                               replace_legacy_workflows=True)

    def test_legacy_replacement_cannot_overwrite_helpers(self):
        helper = self.output / "scripts/package.py"
        helper.parent.mkdir(parents=True)
        helper.write_text("user helper\n")
        legacy = self.output / ".github/workflows/test.yml"
        legacy.parent.mkdir(parents=True)
        legacy.write_text("legacy\n")
        with self.assertRaisesRegex(ValueError, "refusing overwrite"):
            generator.generate(self.config, self.output, root=self.fixture,
                               replace_legacy_workflows=True)
        self.assertEqual(legacy.read_text(), "legacy\n")
        self.assertEqual(helper.read_text(), "user helper\n")
        self.assertFalse((self.output / generator.MANIFEST).exists())

    def test_actual_helpers_render_and_run_offline(self):
        missing = [source for source in generator.HELPER_FILES if not (ROOT / source).is_file()]
        if missing:
            self.skipTest("Canonical helpers not available: " + ", ".join(missing))
        generator.generate(self.config, self.output, root=ROOT)
        generator.generate(self.config, self.output, root=ROOT, check=True)
        subprocess.run([sys.executable, "scripts/check_pipeline.py"], cwd=self.output, check=True,
                       capture_output=True)
        self.assertEqual(
            (self.output / "templates/plugin-pipeline/REFERENCE.txt").read_bytes(),
            (ROOT / "templates/plugin-pipeline/REFERENCE.txt").read_bytes(),
        )
        self.assertEqual(
            (self.output / "scripts/PIPELINE-LICENSE.txt").read_bytes(),
            (ROOT / "templates/plugin-pipeline/REFERENCE.txt").read_bytes(),
        )
        scratch = self.output / self.config["build_dir"]
        scratch.mkdir(parents=True, exist_ok=True)
        environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "TMPDIR": str(scratch)}
        test_workflow = parsed(generator.render(self.config, root=ROOT))["test.yml"]
        snippet = named(test_workflow, "test", "Package provenance and release checks tests")["run"]
        result = subprocess.run(
            ["bash"], input=snippet, cwd=self.output, env=environment, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_real_helper_config_accepts_supported_knobs(self):
        path = ROOT / "scripts/package.py"
        if not path.is_file():
            self.skipTest("Canonical package helper not available")
        helper_spec = importlib.util.spec_from_file_location("pipeline_real_package", path)
        helper = importlib.util.module_from_spec(helper_spec)
        helper_spec.loader.exec_module(helper)
        variants = [
            self.config,
            dict(self.config, sdk_dir="vendor/4D-Plugin-SDK",
                 source_dir="plugins/sample", build_dir="plugins/sample/build",
                 test_project="plugins/sample/sample-test/Project/sample.4DProject",
                 bundle_identifier="org.example.sample-plugin"),
        ]
        helper.ROOT = self.output
        target = self.output / ".github/plugin-pipeline.json"
        target.parent.mkdir(parents=True)
        for config in variants:
            generator.validate_config(config)
            target.write_text(json.dumps(config))
            self.assertEqual(helper.config(), config)

    def test_actionlint_actual_generated_workflows(self):
        actionlint = shutil.which("actionlint")
        if not actionlint:
            self.skipTest("actionlint is not installed")
        missing = [source for source in generator.HELPER_FILES if not (ROOT / source).is_file()]
        if missing:
            self.skipTest("Canonical helpers not available: " + ", ".join(missing))
        generator.generate(self.config, self.output, root=ROOT)
        result = subprocess.run(
            [actionlint, *[str(self.output / name) for name in sorted(generator.LEGACY_WORKFLOWS)]],
            cwd=self.output, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_actual_generated_ci_checker_rejects_workflow_drift(self):
        generator.generate(self.config, self.output, root=ROOT)
        command = [sys.executable, "scripts/check_pipeline.py"]
        accepted = subprocess.run(command, cwd=self.output, capture_output=True, text=True)
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        workflow = self.output / ".github/workflows/test.yml"
        workflow.write_bytes(workflow.read_bytes() + b"\n# unauthorized drift\n")
        rejected = subprocess.run(command, cwd=self.output, capture_output=True, text=True)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("Generated pipeline drift", rejected.stderr)

    def test_managed_tests_are_helper_native_only(self):
        managed_tests = {destination for destination in generator.HELPER_FILES.values()
                         if destination.startswith("scripts/tests/")}
        self.assertEqual(managed_tests, {
            "scripts/tests/test_package.py",
            "scripts/tests/test_release_checks.py",
            "scripts/tests/test_sign_package.py",
            "scripts/tests/test_publish_release.py",
            "scripts/tests/test_verify_release.py",
            "scripts/tests/test_pipeline_contract.py",
        })
        files = self.rendered()
        self.assertEqual({name for name in files if name.startswith("scripts/tests/")},
                         managed_tests)

    def test_pipeline_license_is_independent_of_target_license(self):
        self.output.mkdir()
        license_path = self.output / "LICENSE"
        target_license = b"A different target repository license.\n"
        license_path.write_bytes(target_license)
        expected = (ROOT / "templates/plugin-pipeline/REFERENCE.txt").read_bytes()
        generator.generate(self.config, self.output, root=ROOT)
        generator.generate(self.config, self.output, root=ROOT, check=True)
        self.assertEqual(license_path.read_bytes(), target_license)
        self.assertEqual((self.output / "scripts/PIPELINE-LICENSE.txt").read_bytes(), expected)

    def test_explicit_placeholders_preserve_github_expressions(self):
        files = self.rendered()
        for name, content in files.items():
            if name.endswith(".yml"):
                self.assertNotIn(b"@@", content)
        self.assertIn(b"${{ github.sha }}", files[".github/workflows/test.yml"])
        template = self.fixture / "templates/plugin-pipeline/test.yml.in"
        template.write_text(template.read_text() + "\n# @@UNRECOGNIZED@@\n")
        with self.assertRaisesRegex(ValueError, "Unknown template placeholder"):
            self.rendered()

    def test_native_tests_are_opt_in(self):
        self.config["native_tests"] = True
        self.assert_rendered_contract()
        native = named(parsed(self.rendered())["test.yml"], "test", "Native tests")
        self.assertEqual(native["if"], "env.NATIVE_TESTS == 'true'")

    def test_rendered_shell_syntax_without_execution(self):
        files = self.rendered()
        workflows = parsed(files)
        snippets = []
        for workflow in workflows.values():
            for job in workflow["jobs"]:
                for step in steps(workflow, job):
                    if "run" in step and step.get("shell") != "pwsh":
                        snippets.append(step["run"])
        action = yaml.load(files[".github/actions/tool4d-test/action.yml"], Loader=yaml.BaseLoader)
        snippets += [step["run"] for step in action["runs"]["steps"] if step["shell"] == "bash"]
        for index, snippet in enumerate(snippets):
            result = subprocess.run(["bash", "-n"], input=snippet, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, f"snippet {index}: {result.stderr}")

    def test_archive_install_guards_preserve_existing_outputs(self):
        workflow = parsed(self.rendered())["test.yml"]
        snippet = named(workflow, "package-layout", "Exercise ZIP and DMG layout")["run"]
        guards = "\n".join(line for line in snippet.splitlines() if line.startswith("[ ! -"))
        target = (self.output / Path(self.config["test_project"]).parent.parent / "Plugins"
                  / (self.config["product"] + ".bundle"))
        target.mkdir(parents=True)
        sentinel = target / "user-output"
        sentinel.write_text("preserve\n")
        result = subprocess.run(["bash"], input="set -e\n" + guards,
                                cwd=self.output, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(sentinel.read_text(), "preserve\n")
        shutil.rmtree(target)
        target.symlink_to(target.parent / "missing-bundle")
        result = subprocess.run(["bash"], input="set -e\n" + guards,
                                cwd=self.output, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(target.is_symlink())

    def test_action_version_resolution_offline(self):
        action = yaml.load(self.rendered()[".github/actions/tool4d-test/action.yml"],
                           Loader=yaml.BaseLoader)
        script = action["runs"]["steps"][0]["run"].split("<<'PY'\n", 1)[1].split("\nPY", 1)[0]
        action_root = ROOT / ("pipeline-action-test-" + uuid.uuid4().hex)
        action_root.mkdir()
        self.addCleanup(shutil.rmtree, action_root)
        project = action_root / "project.4DProject"
        relative = project.relative_to(ROOT).as_posix()
        environment = {**os.environ, "TEST_PROJECT": relative, "STARTUP_METHOD": "test_all"}
        for compat, expected in ((2101, "21.x/21.1"), (2120, "21.x/21%20R2"),
                                 ("21A0", "21.x/21%20R10")):
            project.write_text(json.dumps({"compatibilityVersion": compat}))
            result = subprocess.run([sys.executable, "-c", script], cwd=ROOT,
                                    env=environment, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), expected)
        for value in ("../project.4DProject", "$(echo).4DProject", "/absolute.4DProject"):
            result = subprocess.run([sys.executable, "-c", script], cwd=ROOT,
                                    env={**environment, "TEST_PROJECT": value},
                                    capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)

    def test_valid_alternate_config(self):
        self.config.update(
            product="sample-plugin", source_dir="plugins/sample", build_dir="plugins/sample/build",
            test_project="plugins/sample/sample-test/Project/sample.4DProject",
            sdk_dir="vendor/4D-Plugin-SDK", bundle_identifier="org.example.sample-plugin",
            release_branch="stable", signing_environment="approved-release", startup_method="run_tests",
        )
        self.assert_rendered_contract()

    def static_dependencies(self, windows="vcpkg"):
        return {
            "profile": "static", "windows": windows, "notices_dir": "THIRD-PARTY-NOTICES",
            "allowed_licenses": ["LGPL-2.1-or-later", "MIT"],
            "copyleft_acknowledged": ["LGPL-2.1-or-later"],
            "components": {
                "glib": {"license": "LGPL-2.1-or-later", "notice": "glib.txt", "platforms": ["windows"]},
                "brotli": {"license": "MIT", "notice": "brotli.txt", "platforms": ["macos", "windows"]},
            },
        }

    def test_baseline_render_has_no_dependency_content(self):
        files = self.rendered()
        test = files[".github/workflows/test.yml"].decode()
        for marker in ("vcpkg", "timeout-minutes", "@@IF", "@@ELSE", "@@ENDIF"):
            self.assertNotIn(marker, test)
        self.assertFalse([name for name in files if name.startswith("vcpkg-triplets/")])
        self.assertNotIn(b"THIRD-PARTY-NOTICES", files[".gitattributes"])

    def test_static_vcpkg_profile_renders_cached_pinned_install(self):
        self.config["dependencies"] = self.static_dependencies()
        files = self.rendered()
        self.assert_rendered_contract(files)
        triplet = files["vcpkg-triplets/x64-windows-static-release.cmake"].decode()
        for line in ("set(VCPKG_TARGET_ARCHITECTURE x64)", "set(VCPKG_CRT_LINKAGE static)",
                     "set(VCPKG_LIBRARY_LINKAGE static)", "set(VCPKG_BUILD_TYPE release)"):
            self.assertIn(line, triplet)
        self.assertIn("set(VCPKG_BUILD_TYPE release)",
                      files["vcpkg-triplets/x64-windows-release-host.cmake"].decode())
        attributes = files[".gitattributes"].decode()
        for rule in ("THIRD-PARTY-NOTICES/** text eol=lf", "vcpkg.json text eol=lf",
                     "vcpkg-triplets/*.cmake text eol=lf"):
            self.assertIn(rule, attributes)
        workflow = parsed(files)["test.yml"]
        self.assertEqual(workflow["jobs"]["test"]["timeout-minutes"], "360")
        baseline = named(workflow, "test", "Resolve pinned vcpkg baseline (Windows)")
        self.assertIn("scripts/package.py vcpkg-baseline", baseline["run"])
        restore = named(workflow, "test", "Restore vcpkg binary cache (Windows)")
        self.assertTrue(restore["uses"].startswith("actions/cache/restore@"))
        self.assertIn("steps.vcpkg.outputs.baseline", restore["with"]["key"])
        self.assertIn("hashFiles('vcpkg.json', 'vcpkg-triplets/**')", restore["with"]["key"])
        install = named(workflow, "test", "Install static dependencies (Windows)")
        for fragment in ("checkout --quiet --detach $env:VCPKG_BASELINE", "--triplet x64-windows-static-release",
                         "--host-triplet x64-windows-release-host", "--overlay-triplets=vcpkg-triplets",
                         "--x-install-root=example/build/vcpkg_installed"):
            self.assertIn(fragment, install["run"])
        save = named(workflow, "test", "Save vcpkg binary cache (Windows)")
        self.assertTrue(save["uses"].startswith("actions/cache/save@"))
        self.assertIn("cache-hit != 'true'", save["if"])
        names = [step.get("name") for step in steps(workflow, "test")]
        self.assertLess(names.index("Save vcpkg binary cache (Windows)"), names.index("Configure (Windows)"))
        configure = named(workflow, "test", "Configure (Windows)")["run"]
        for fragment in ("-A x64", "scripts/buildsystems/vcpkg.cmake", "-DVCPKG_TARGET_TRIPLET=x64-windows-static-release",
                         "-DVCPKG_MANIFEST_INSTALL=OFF"):
            self.assertIn(fragment, configure)
        self.assertNotIn("-G", configure.split())
        generator.generate(self.config, self.output, root=self.fixture)
        generator.generate(self.config, self.output, check=True, root=self.fixture)
        (self.output / "vcpkg-triplets/x64-windows-static-release.cmake").write_text("set(VCPKG_CRT_LINKAGE dynamic)\n")
        with self.assertRaises(ValueError):
            generator.generate(self.config, self.output, check=True, root=self.fixture)

    def test_static_profile_without_vcpkg_keeps_baseline_workflows(self):
        baseline = self.rendered()
        self.config["dependencies"] = self.static_dependencies(windows="none")
        files = self.rendered()
        self.assert_rendered_contract(files)
        for name in (".github/workflows/test.yml", ".github/workflows/release.yml",
                     ".github/workflows/bump-version.yml"):
            self.assertEqual(files[name], baseline[name])
        self.assertFalse([name for name in files if name.startswith("vcpkg-triplets/")])
        self.assertIn(b"THIRD-PARTY-NOTICES/** text eol=lf", files[".gitattributes"])

    def test_condition_markers_fail_closed(self):
        enabled = {"VCPKG": True}
        self.assertEqual(generator.apply_conditions(
            "a\n@@IF:VCPKG@@\nb\n@@ELSE:VCPKG@@\nc\n@@ENDIF:VCPKG@@\nd\n", enabled, "t"), "a\nb\nd\n")
        self.assertEqual(generator.apply_conditions(
            "a\n@@IF:VCPKG@@\nb\n@@ELSE:VCPKG@@\nc\n@@ENDIF:VCPKG@@\nd\n", {"VCPKG": False}, "t"), "a\nc\nd\n")
        for text in ("@@IF:OTHER@@\n@@ENDIF:OTHER@@\n", "@@IF:VCPKG@@\nb\n",
                     "@@ENDIF:VCPKG@@\n", "@@IF:VCPKG@@\n@@IF:VCPKG@@\n@@ENDIF:VCPKG@@\n@@ENDIF:VCPKG@@\n",
                     "@@IF:VCPKG@@\n@@ENDIF:OTHER@@\n", "  @@IF:VCPKG@@ trailing\n@@ENDIF:VCPKG@@\n",
                     "@@ELSE:VCPKG@@\n"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                generator.apply_conditions(text, enabled, "t")

    def test_unsafe_dependency_profiles(self):
        def mutate(change):
            dependencies = self.static_dependencies()
            change(dependencies)
            return dependencies
        cases = {
            "unknown field": mutate(lambda d: d.update(overlay_ports=["ports"])),
            "profile": mutate(lambda d: d.update(profile="dynamic")),
            "windows": mutate(lambda d: d.update(windows="conan")),
            "agpl": mutate(lambda d: d.update(allowed_licenses=["AGPL-3.0-or-later", "LGPL-2.1-or-later", "MIT"])),
            "unacknowledged": mutate(lambda d: d.update(copyleft_acknowledged=[])),
            "stale acknowledgement": mutate(lambda d: d.update(
                copyleft_acknowledged=["GPL-2.0-only", "LGPL-2.1-or-later"])),
            "license not allowed": mutate(lambda d: d["components"]["brotli"].update(license="BSD-3-Clause")),
            "dangling operator": mutate(lambda d: d["components"]["brotli"].update(license="MIT AND")),
            "bad with": mutate(lambda d: d["components"]["brotli"].update(license="(MIT) WITH x")),
            "notice traversal": mutate(lambda d: d["components"]["brotli"].update(notice="../x.txt")),
            "port name": mutate(lambda d: d["components"].update({"Bad_Name": d["components"].pop("glib")})),
            "platforms": mutate(lambda d: d["components"]["glib"].update(platforms=["windows", "macos"])),
            "notices in build": mutate(lambda d: d.update(notices_dir="example/build/notices")),
            "notices absolute": mutate(lambda d: d.update(notices_dir="/notices")),
            "no components": mutate(lambda d: d.update(components={})),
            "vcpkg without windows": mutate(lambda d: d["components"]["glib"].update(platforms=["macos"])
                                            or d["components"]["brotli"].update(platforms=["macos"])),
        }
        for label, dependencies in cases.items():
            config = copy.deepcopy(self.config)
            config["dependencies"] = dependencies
            with self.subTest(label=label), self.assertRaises(ValueError):
                generator.validate_config(config)
        config = copy.deepcopy(self.config)
        config["dependencies"] = self.static_dependencies()
        generator.validate_config(config)

    def test_unsafe_config(self):
        cases = {
            "schema": [True, 2, "1"],
            "product": ["bad name", "bad;echo", "$(whoami)", "${{ github.token }}", "../bad", "-option", "CON", "lpt1"],
            "source_dir": ["/absolute", "../escape", "a/../b", "a//b", "a\\b", ".github", "a/.hidden", "a\nb", "a: b", "-option", "a/-option", "a/AUX", "a/name."],
            "build_dir": ["example", "elsewhere/build", "example/$HOME"],
            "test_project": ["example/example-test/Project/test.json", "example/build/test.4DProject"],
            "sdk_dir": ["example/sdk", "example", "a;rm"],
            "startup_method": ["test all", "-x", "x`id`", "test_all\nrun"],
            "bundle_identifier": ["com.$(id)", "invalid", "com.example';exit"],
            "macos_deployment_target": ["10.9", "11.0;echo", "latest"],
            "release_branch": ["main;echo", "main..x", "refs/heads/main\n", "-main"],
            "signing_environment": ["release: signing", "${{ secrets.PAT }}", ""],
            "profile": ["windows-only", None],
            "signing_policy": ["ad-hoc", "unsigned", None],
            "native_tests": ["true", 1, None],
            "dependencies": [[{"command": "echo"}], ["curl"], {}, None],
        }
        for field, values in cases.items():
            for value in values:
                config = copy.deepcopy(self.config)
                config[field] = value
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    generator.validate_config(config)
        for extra in ({**self.config, "command": "echo"}, {}):
            with self.assertRaises(ValueError):
                generator.validate_config(extra)

    def test_cli_and_duplicate_config(self):
        script = self.fixture / "scripts/generate_plugin_pipeline.py"
        shutil.copyfile(ROOT / "scripts/generate_plugin_pipeline.py", script)
        config = self.scratch / "config.json"
        config.write_text(json.dumps(self.config))
        command = [sys.executable, str(script), "--config", str(config), "--output", str(self.output)]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        result = subprocess.run(command + ["--check"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        result = subprocess.run(command + ["--replace-managed"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        config.write_text('{"schema":1,' + json.dumps(self.config)[1:])
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Duplicate configuration key", result.stderr)

    def test_negative_workflow_mutations(self):
        files = self.rendered()
        original = parsed(files)
        action = yaml.load(files[".github/actions/tool4d-test/action.yml"], Loader=yaml.BaseLoader)
        contract = json.loads(files[".github/plugin-pipeline-contract.json"])
        mutations = [
            lambda w: w["release.yml"]["on"].update(push={}),
            lambda w: w["release.yml"]["on"].update(workflow_dispatch={"inputs": {"tag": {}}}),
            lambda w: w["test.yml"]["on"].pop("pull_request"),
            lambda w: w["test.yml"]["on"].update(push={"tags": ["v*"]}),
            lambda w: w["test.yml"].update(name="Build"),
            lambda w: w["test.yml"].update(permissions={"contents": "write"}),
            lambda w: w["release.yml"]["jobs"]["build"].update(needs=[]),
            lambda w: w["release.yml"]["jobs"]["build"].update(uses="owner/repo/.github/workflows/test.yml@main"),
            lambda w: w["release.yml"]["jobs"]["sign-package"].pop("environment"),
            lambda w: w["release.yml"]["jobs"]["sign-package"].update(permissions={"contents": "write"}),
            lambda w: named(w["release.yml"], "authorize", "Require an explicit default-branch dispatch").update(run="echo approved"),
            lambda w: steps(w["test.yml"], "test")[0]["with"].update(ref="main"),
            lambda w: steps(w["test.yml"], "package-layout")[2]["with"].update(**{"run-id": "123"}),
            lambda w: steps(w["test.yml"], "package-layout")[2]["with"].update(name="native-macos"),
            lambda w: named(w["test.yml"], "test", "4D integration").update(**{"continue-on-error": "true"}),
            lambda w: named(w["test.yml"], "test", "Native tests").update(**{"if": "${{ true }}"}),
            lambda w: named(w["test.yml"], "package-layout", "Exercise ZIP and DMG layout").update(run="ditto -x -k"),
            lambda w: steps(w["release.yml"], "publish")[1].update(run="echo verified"),
            lambda w: steps(w["release.yml"], "sign-package")[-1]["with"].update(name="other-${{ github.sha }}"),
            lambda w: steps(w["release.yml"], "publish")[2]["with"].update(path="unverified"),
            lambda w: steps(w["release.yml"], "publish")[3].update(run="gh release create v1 --latest"),
            lambda w: w.update({"fourth.yml": copy.deepcopy(w["test.yml"])}),
        ]
        for index, mutate in enumerate(mutations):
            changed = copy.deepcopy(original)
            mutate(changed)
            with self.subTest(mutation=index), self.assertRaises((AssertionError, KeyError)):
                assert_contract(changed, action, contract, self.config)
        for platform in (1, 2):
            changed = copy.deepcopy(action)
            changed["runs"]["steps"][platform]["run"] = "echo PASS"
            with self.subTest(action_platform=platform), self.assertRaises(AssertionError):
                assert_contract(original, changed, contract, self.config)


if __name__ == "__main__":
    unittest.main()
