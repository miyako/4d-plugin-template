#!/usr/bin/env python3
"""Render an opt-in pipeline; replacements require explicit, scoped authorization."""

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sys


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "templates" / "plugin-pipeline"
TEMPLATE_FILES = {
    "bump-version.yml.in": ".github/workflows/bump-version.yml",
    "test.yml.in": ".github/workflows/test.yml",
    "release.yml.in": ".github/workflows/release.yml",
    "tool4d-test.action.yml.in": ".github/actions/tool4d-test/action.yml",
    "contract.json": ".github/plugin-pipeline-contract.json",
    "REFERENCE.txt": "scripts/PIPELINE-LICENSE.txt",
}
# These shared implementations are maintained separately from the renderer.
HELPER_FILES = {
    "templates/plugin-pipeline/REFERENCE.txt": "templates/plugin-pipeline/REFERENCE.txt",
    "scripts/package.py": "scripts/package.py",
    "scripts/release_checks.py": "scripts/release_checks.py",
    "scripts/sign-package.sh": "scripts/sign-package.sh",
    "scripts/verify_distribution.py": "scripts/verify_distribution.py",
    "scripts/publish_release.py": "scripts/publish_release.py",
    "scripts/verify_release.py": "scripts/verify_release.py",
    "scripts/check_pipeline.py": "scripts/check_pipeline.py",
    "scripts/tests/test_package.py": "scripts/tests/test_package.py",
    "scripts/tests/test_release_checks.py": "scripts/tests/test_release_checks.py",
    "scripts/tests/test_sign_package.py": "scripts/tests/test_sign_package.py",
    "scripts/tests/test_publish_release.py": "scripts/tests/test_publish_release.py",
    "scripts/tests/test_verify_release.py": "scripts/tests/test_verify_release.py",
    "scripts/tests/test_pipeline_contract.py": "scripts/tests/test_pipeline_contract.py",
}
FIELDS = {
    "schema", "product", "source_dir", "build_dir", "test_project",
    "startup_method", "sdk_dir", "bundle_identifier", "macos_deployment_target",
    "release_branch", "signing_environment", "profile", "signing_policy",
    "native_tests", "dependencies",
}
PLACEHOLDER = re.compile(r"@@([A-Z_]+)@@")
CONDITION = re.compile(r"[ \t]*@@(IF|ELSE|ENDIF):([A-Z_]+)@@[ \t]*")
DEPENDENCY_FIELDS = {
    "profile", "windows", "notices_dir", "allowed_licenses", "copyleft_acknowledged", "components",
}
SPDX_ID = r"[A-Za-z0-9][A-Za-z0-9.+-]{0,63}"
COPYLEFT = r"L?GPL-"
PORT_NAME = r"[a-z0-9][a-z0-9-]{0,63}"
NOTICE_FILE = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}"
NOTICES_DIR = r"[A-Za-z0-9][A-Za-z0-9_-]*(?:/[A-Za-z0-9][A-Za-z0-9_.-]*)*"
# Conditional outputs are rendered only when their condition is enabled.
CONDITIONAL_FILES = {
    "vcpkg-x64-windows-static-release.cmake": ("VCPKG", "vcpkg-triplets/x64-windows-static-release.cmake"),
    "vcpkg-x64-windows-release-host.cmake": ("VCPKG", "vcpkg-triplets/x64-windows-release-host.cmake"),
}
MANIFEST = ".github/plugin-pipeline-generated.json"
IGNORE_BEGIN = b"# BEGIN 4D PLUGIN PIPELINE (managed)"
IGNORE_END = b"# END 4D PLUGIN PIPELINE (managed)"
ATTR_BEGIN = IGNORE_BEGIN
ATTR_END = IGNORE_END
MANAGED_SECTIONS = {
    ".gitignore": (IGNORE_BEGIN, IGNORE_END),
    ".gitattributes": (ATTR_BEGIN, ATTR_END),
}
LEGACY_WORKFLOWS = {
    ".github/workflows/bump-version.yml",
    ".github/workflows/test.yml",
    ".github/workflows/release.yml",
}
REQUIRED_OWNERSHIP = LEGACY_WORKFLOWS | {
    ".github/actions/tool4d-test/action.yml",
    ".github/plugin-pipeline-contract.json",
    ".github/plugin-pipeline.json",
}


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate configuration key: {key}")
        result[key] = value
    return result


def fingerprint(content):
    return hashlib.sha256(content).hexdigest()


def section_parts(content, name):
    begin, end = MANAGED_SECTIONS[name]
    if begin not in content and end not in content:
        return content, None, b""
    if content.count(begin) != 1 or content.count(end) != 1:
        raise ValueError(f"Ambiguous managed {name} section")
    match = re.search(
        rb"(?m)^" + re.escape(begin) + rb"\n.*?^"
        + re.escape(end) + rb"(?:\n|$)", content, re.S,
    )
    if not match:
        raise ValueError(f"Malformed managed {name} section")
    return content[:match.start()], match.group(), content[match.end():]


def ignore_parts(content):
    return section_parts(content, ".gitignore")


def managed_fingerprint(name, content):
    if name in MANAGED_SECTIONS:
        _, section, _ = section_parts(content, name)
        if section is None:
            raise ValueError(f"Missing managed {name} section")
        content = section
    return fingerprint(content)


def ownership(files):
    return {
        "schema": 1,
        "files": {name: managed_fingerprint(name, content) for name, content in sorted(files.items())},
        "config_sha256": fingerprint(files[".github/plugin-pipeline.json"]),
        "contract_sha256": fingerprint(files[".github/plugin-pipeline-contract.json"]),
    }


def validate_config(config):
    if not isinstance(config, dict) or set(config) != FIELDS:
        raise ValueError("Configuration must contain exactly the schema-1 fields")
    if type(config["schema"]) is not int or config["schema"] != 1:
        raise ValueError("Unsupported schema")
    if type(config["native_tests"]) is not bool:
        raise ValueError("native_tests must be boolean")
    if config["profile"] != "macos-universal-windows-x64":
        raise ValueError("Unsupported profile")
    if config["signing_policy"] != "developer-id":
        raise ValueError("Unsupported signing policy")
    patterns = {
        "product": r"[A-Za-z][A-Za-z0-9_-]{0,63}",
        "startup_method": r"[A-Za-z][A-Za-z0-9_]{0,30}",
        "bundle_identifier": r"[A-Za-z][A-Za-z0-9-]*(?:\.[A-Za-z0-9][A-Za-z0-9-]*)+",
        "macos_deployment_target": r"(?:10\.(?:1[3-9])|(?:1[1-9]|[2-9][0-9])\.[0-9]+)",
        "release_branch": r"[A-Za-z][A-Za-z0-9_-]*(?:/[A-Za-z0-9][A-Za-z0-9_-]*)*",
        "signing_environment": r"[A-Za-z][A-Za-z0-9_-]{0,63}",
    }
    for field, pattern in patterns.items():
        value = config[field]
        if not isinstance(value, str) or not re.fullmatch(pattern, value):
            raise ValueError(f"Unsafe or unsupported {field}")
    for field in ("product", "source_dir", "build_dir", "test_project", "sdk_dir"):
        value = config[field]
        if not isinstance(value, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_.-]+)*", value
        ):
            raise ValueError(f"Unsafe {field}")
        if any(part in (".", "..") or part.startswith((".", "-"))
               for part in value.split("/")):
            raise ValueError(f"Unsafe {field}")
        for part in value.split("/"):
            if part.endswith(".") or re.fullmatch(
                r"(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])", part.split(".")[0], re.I
            ):
                raise ValueError(f"Non-portable {field}")
    source = PurePosixPath(config["source_dir"])
    for field in ("build_dir", "test_project"):
        path = PurePosixPath(config[field])
        if source not in path.parents:
            raise ValueError(f"{field} must be below source_dir")
    if not config["test_project"].endswith(".4DProject"):
        raise ValueError("test_project must identify a .4DProject")
    sdk = PurePosixPath(config["sdk_dir"])
    build = PurePosixPath(config["build_dir"])
    project = PurePosixPath(config["test_project"])
    if sdk == source or source in sdk.parents or sdk in source.parents:
        raise ValueError("sdk_dir must not overlap source_dir")
    if build == project or build in project.parents or project in build.parents:
        raise ValueError("build_dir must not overlap test_project")
    validate_dependencies(config)
    return config


def license_ids(expression):
    """SPDX ids in a license expression; operators and exceptions are not licenses."""
    if not isinstance(expression, str) or not 0 < len(expression) <= 256:
        raise ValueError("Invalid SPDX license expression")
    invalid = ValueError(f"Invalid SPDX license expression: {expression}")
    ids, depth, operand, exception, after_id = [], 0, True, False, False
    for token in re.findall(r"[()]|[^\s()]+", expression):
        licence = False
        if token == "(":
            if not operand or exception:
                raise invalid
            depth += 1
        elif token == ")":
            if operand or not depth:
                raise invalid
            depth -= 1
        elif token in ("AND", "OR", "WITH"):
            if operand or (token == "WITH" and not after_id):
                raise invalid
            operand, exception = True, token == "WITH"
        else:
            if not operand or not re.fullmatch(SPDX_ID, token) or token == "NOASSERTION":
                raise invalid
            if not exception:
                ids.append(token)
                licence = True
            operand, exception = False, False
        after_id = licence
    if operand or depth:
        raise invalid
    return ids


def sorted_unique(value, field):
    if (not isinstance(value, list) or not all(isinstance(item, str) for item in value)
            or value != sorted(set(value))):
        raise ValueError(f"dependencies.{field} must be a sorted list of unique strings")
    return value


def validate_dependencies(config):
    dependencies = config["dependencies"]
    if type(dependencies) is list and not dependencies:
        return None
    if not isinstance(dependencies, dict) or set(dependencies) != DEPENDENCY_FIELDS:
        raise ValueError("dependencies must be [] or a static dependency profile object")
    if dependencies["profile"] != "static":
        raise ValueError("Unsupported dependency profile")
    if dependencies["windows"] not in ("vcpkg", "none"):
        raise ValueError("dependencies.windows must be vcpkg or none")
    notices = dependencies["notices_dir"]
    if not isinstance(notices, str) or not re.fullmatch(NOTICES_DIR, notices) or any(
            part.endswith(".") for part in notices.split("/")):
        raise ValueError("Unsafe dependencies.notices_dir")
    for field in ("build_dir", "sdk_dir"):
        other = PurePosixPath(config[field])
        path = PurePosixPath(notices)
        if path == other or other in path.parents or path in other.parents:
            raise ValueError(f"dependencies.notices_dir must not overlap {field}")
    allowed = sorted_unique(dependencies["allowed_licenses"], "allowed_licenses")
    if not allowed:
        raise ValueError("dependencies.allowed_licenses must not be empty")
    for license in allowed:
        if not re.fullmatch(SPDX_ID, license) or license == "NOASSERTION":
            raise ValueError(f"Invalid SPDX license id: {license}")
        if license.upper().startswith("AGPL"):
            raise ValueError(f"AGPL is not distributable as a statically linked plugin: {license}")
    acknowledged = sorted_unique(dependencies["copyleft_acknowledged"], "copyleft_acknowledged")
    components = dependencies["components"]
    if not isinstance(components, dict) or not components:
        raise ValueError("dependencies.components must be a non-empty object")
    used, windows = set(), False
    for name, component in components.items():
        if not re.fullmatch(PORT_NAME, name) or name.startswith("vcpkg-"):
            raise ValueError(f"Invalid dependency component name: {name!r}")
        if not isinstance(component, dict) or set(component) != {"license", "notice", "platforms"}:
            raise ValueError(f"Component {name} must define exactly license, notice and platforms")
        ids = license_ids(component["license"])
        for license in ids:
            if license not in allowed:
                raise ValueError(f"Component {name} license is not allowed: {license}")
        used.update(ids)
        notice = component["notice"]
        if (not isinstance(notice, str) or not re.fullmatch(NOTICE_FILE, notice)
                or notice.endswith(".")):
            raise ValueError(f"Unsafe notice file for component {name}")
        platforms = sorted_unique(component["platforms"], "platforms")
        if not platforms or not set(platforms) <= {"macos", "windows"}:
            raise ValueError(f"Component {name} platforms must be macos and/or windows")
        windows = windows or "windows" in platforms
    copyleft = {license for license in used if re.match(COPYLEFT, license)}
    if set(acknowledged) != copyleft:
        raise ValueError("dependencies.copyleft_acknowledged must list exactly the GPL/LGPL "
                         f"licenses used by components: {sorted(copyleft)}")
    if dependencies["windows"] == "vcpkg" and not windows:
        raise ValueError("A vcpkg profile requires at least one Windows component")
    return dependencies


def conditions(config):
    dependencies = config["dependencies"]
    return {"VCPKG": isinstance(dependencies, dict) and dependencies["windows"] == "vcpkg"}


def apply_conditions(text, enabled, template):
    """Keep or drop whole marker-delimited line blocks; markers never nest."""
    output, active, state = [], None, None
    for line in text.splitlines(keepends=True):
        match = CONDITION.fullmatch(line.rstrip("\r\n"))
        if match:
            kind, name = match.groups()
            if name not in enabled:
                raise ValueError(f"Unknown template condition in {template}: {name}")
            if kind == "IF" and active is None:
                active, state = name, "if"
            elif kind == "ELSE" and active == name and state == "if":
                state = "else"
            elif kind == "ENDIF" and active == name:
                active, state = None, None
            else:
                raise ValueError(f"Malformed template condition in {template}: {line.strip()}")
            continue
        if active is None or (state == "if") == enabled[active]:
            output.append(line)
    if active is not None:
        raise ValueError(f"Unterminated template condition in {template}: {active}")
    return "".join(output)


def render(config, root=ROOT):
    validate_config(config)
    templates = root / "templates" / "plugin-pipeline"
    values = {key.upper(): str(value) for key, value in config.items()
              if isinstance(value, str)}
    values["NATIVE_TESTS"] = "true" if config["native_tests"] else "false"
    project = PurePosixPath(config["test_project"])
    values["TEST_ROOT"] = str(project.parent.parent)
    enabled = conditions(config)
    selected = dict(TEMPLATE_FILES)
    selected.update({template: destination for template, (condition, destination)
                     in CONDITIONAL_FILES.items() if enabled[condition]})
    result = {}
    for template, destination in selected.items():
        if template == "REFERENCE.txt":
            result[destination] = (templates / template).read_bytes()
            continue
        text = (templates / template).read_text(encoding="utf-8")
        if template.endswith(".in"):
            text = apply_conditions(text, enabled, template)
            def replace(match):
                key = match.group(1)
                if key not in values:
                    raise ValueError(f"Unknown template placeholder: {key}")
                return values[key]
            text = PLACEHOLDER.sub(replace, text)
            if "@@" in text:
                raise ValueError(f"Unresolved template placeholder in {template}")
        result[destination] = text.encode("utf-8")
    result[".github/plugin-pipeline.json"] = (
        json.dumps(config, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    ignored = [
        f"/{config['build_dir']}/", f"/{values['TEST_ROOT']}/Plugins/",
        "/.local-tools/", "/.package-inputs/", "/.package-validation/",
        "/.release-package/", "/.signed-distribution/", "/.signed-extracted/",
        "/.publish-extracted/", "/.draft-upload-check/", "/.draft-extracted/",
        "/.release-notes", "__pycache__/",
    ]
    result[".gitignore"] = IGNORE_BEGIN + b"\n" + "\n".join(ignored).encode() + b"\n" + IGNORE_END + b"\n"
    attributes = [
        f"{config['source_dir']}/manifest.json text eol=lf",
        f"{config['source_dir']}/constants.xlf text eol=lf",
        "VERSION text eol=lf", "LICENSE text eol=lf",
    ]
    dependencies = config["dependencies"]
    if isinstance(dependencies, dict):
        attributes.append(f"{dependencies['notices_dir']}/** text eol=lf")
        if enabled["VCPKG"]:
            attributes += ["vcpkg.json text eol=lf", "vcpkg-triplets/*.cmake text eol=lf"]
    result[".gitattributes"] = (
        ATTR_BEGIN + b"\n" + "\n".join(attributes).encode() + b"\n" + ATTR_END + b"\n"
    )
    for source, destination in HELPER_FILES.items():
        path = root / source
        if not path.is_file():
            raise ValueError(f"Canonical helper not available: {source}")
        result[destination] = path.read_bytes()
    return result


def generate(config, output, check=False, root=ROOT, update_managed=False,
             replace_legacy_workflows=False):
    if check and (update_managed or replace_legacy_workflows):
        raise ValueError("--check cannot authorize replacement")
    if update_managed and replace_legacy_workflows:
        raise ValueError("Choose one replacement mode")
    files = render(config, root)
    output = Path(output)
    desired_manifest = ownership(files)
    authorized = set()
    previous_path = output / MANIFEST
    if update_managed:
        if any(path.is_symlink() for path in (previous_path, *previous_path.parents)):
            raise ValueError("Refusing symlink ownership manifest")
        if not previous_path.is_file():
            raise ValueError("--update-managed requires an existing ownership manifest")
        previous = json.loads(previous_path.read_bytes(), object_pairs_hook=unique_object)
        if (not isinstance(previous, dict) or set(previous) != set(desired_manifest)
                or type(previous["schema"]) is not int or previous["schema"] != 1
                or not isinstance(previous["files"], dict)
                or not set(previous["files"]).issubset(files)
                or not REQUIRED_OWNERSHIP.issubset(previous["files"])):
            raise ValueError("Invalid ownership manifest or changed managed file set")
        for field, name in (("config_sha256", ".github/plugin-pipeline.json"),
                            ("contract_sha256", ".github/plugin-pipeline-contract.json")):
            if previous[field] != previous["files"][name]:
                raise ValueError("Invalid config/contract ownership fingerprint")
        for name, digest in previous["files"].items():
            target = output / name
            if (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
                    or any(path.is_symlink() for path in (target, *target.parents))
                    or not target.is_file() or managed_fingerprint(name, target.read_bytes()) != digest):
                raise ValueError(f"Managed file edited or missing; refusing update: {name}")
        authorized = set(previous["files"]) | {MANIFEST}
    elif replace_legacy_workflows:
        if previous_path.exists() or previous_path.is_symlink():
            raise ValueError("Owned pipelines must use --update-managed, not legacy replacement")
        authorized = LEGACY_WORKFLOWS
    for name in MANAGED_SECTIONS:
        path = output / name
        if any(component.is_symlink() for component in (path, *path.parents)):
            raise ValueError(f"Refusing symlink destination: {name}")
        if path.exists():
            if not path.is_file():
                raise ValueError(f"{name} must be a regular file")
            prefix, section, suffix = section_parts(path.read_bytes(), name)
            desired_section = files[name]
            if section is None:
                if check:
                    raise ValueError(f"Managed {name} section missing")
                authorized.add(name)
                if prefix and not prefix.endswith(b"\n"):
                    prefix += b"\n"
            elif section != desired_section and name not in authorized:
                raise ValueError(f"Managed {name} section differs; use an owned update")
            if name == ".gitattributes":
                # Put mandatory LF rules last so preserved custom rules cannot
                # silently override source-byte normalization on Windows.
                custom = prefix + suffix
                if custom and not custom.endswith(b"\n"):
                    custom += b"\n"
                files[name] = custom + desired_section
                if not check:
                    authorized.add(name)
            else:
                files[name] = prefix + desired_section + suffix
    files[MANIFEST] = (json.dumps(desired_manifest, indent=2, sort_keys=True) + "\n").encode()
    # Preflight the whole set before writing anything; never follow output symlinks.
    for name, content in files.items():
        target = output / name
        if any(path.is_symlink() for path in (target, *target.parents)):
            raise ValueError(f"Refusing symlink destination: {name}")
        if target.exists() and (not target.is_file()
                                or target.read_bytes() != content and name not in authorized):
            raise ValueError(f"Generated file differs; refusing overwrite: {name}")
        if check and not target.is_file():
            raise ValueError(f"Generated file missing: {name}")
    workflows = output / ".github" / "workflows"
    if workflows.exists():
        extras = [path.name for path in workflows.iterdir()
                  if path.suffix in (".yml", ".yaml")
                  and f".github/workflows/{path.name}" not in files]
        if extras:
            raise ValueError("Unexpected workflows: " + ", ".join(sorted(extras)))
    if not check:
        for name, content in files.items():
            target = output / name
            if not target.exists() or target.read_bytes() != content:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
                if name.endswith(".sh"):
                    target.chmod(0o755)
    return files


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--check", action="store_true")
    replacement = parser.add_mutually_exclusive_group()
    replacement.add_argument(
        "--update-managed", "--replace-managed", action="store_true",
        help="Update only files whose current bytes match the prior ownership manifest",
    )
    replacement.add_argument(
        "--replace-legacy-workflows", action="store_true",
        help="Explicitly replace only bump-version.yml, test.yml, and release.yml; preserve other files",
    )
    args = parser.parse_args(argv)
    try:
        config = json.loads(args.config.read_text(encoding="utf-8"),
                            object_pairs_hook=unique_object)
        generate(config, args.output, args.check, update_managed=args.update_managed,
                 replace_legacy_workflows=args.replace_legacy_workflows)
    except (OSError, ValueError) as error:
        parser.exit(1, f"pipeline: {error}\n")
    print("Pipeline verified." if args.check else "Pipeline generated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
