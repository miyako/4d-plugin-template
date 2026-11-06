#!/usr/bin/env python3
"""Source-bound, config-driven native staging and signed payload inventories.

Adapted from miyako/4d-plugin-get-icon at 67f6ccbfe0fe8f61480e9a8397b076d19f66e033.
MIT Copyright (c) 2026 Keisuke Miyako; see templates/plugin-pipeline/REFERENCE.txt.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import plistlib
import re
import shutil
import struct
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
BINDING = ("source_commit", "sdk_commit", "version")
SEMVER = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")


class PackageError(ValueError):
    """A failed package contract."""


def require(condition, message):
    if not condition:
        raise PackageError(message)


def safe_path(value):
    require(isinstance(value, str) and bool(value), "Empty or non-string path")
    require("\\" not in value and ":" not in value, f"Non-portable path: {value!r}")
    require(not PurePosixPath(value).is_absolute()
            and not PureWindowsPath(value).is_absolute(), f"Absolute path: {value!r}")
    for part in value.split("/"):
        require(part not in ("", ".", "..") and not part.endswith((".", " "))
                and not any(ord(c) < 32 or c in '<>"|?*' for c in part),
                f"Unsafe path: {value!r}")
        require(not re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])",
                                 part.split(".")[0]), f"Reserved path: {value!r}")
    return value


def portable_relative(path, root):
    return safe_path(path.relative_to(root).as_posix())


def no_symlink_path(path):
    path = Path(os.path.abspath(path))
    for component in (path, *path.parents):
        require(not component.is_symlink(), f"Symlink forbidden: {component}")


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def read_json(path):
    no_symlink_path(path)
    return json.loads(Path(path).read_bytes(), object_pairs_hook=unique_object)


def write_json(path, value):
    path.write_bytes((json.dumps(value, indent=2, sort_keys=True) + "\n").encode())


def config():
    data = read_json(ROOT / ".github/plugin-pipeline.json")
    fields = {"schema", "product", "source_dir", "build_dir", "test_project",
              "startup_method", "sdk_dir", "bundle_identifier", "macos_deployment_target",
              "release_branch", "signing_environment", "profile", "signing_policy",
              "native_tests", "dependencies"}
    require(isinstance(data, dict) and set(data) == fields, "Invalid pipeline config fields")
    require(type(data["schema"]) is int and data["schema"] == 1, "Unsupported config schema")
    require(data["profile"] == "macos-universal-windows-x64", "Unsupported profile")
    require(data["signing_policy"] == "developer-id", "Unsupported signing policy")
    require(type(data["dependencies"]) is list and not data["dependencies"],
            "Additional dependencies are not supported by the baseline pipeline")
    require(type(data["native_tests"]) is bool, "native_tests must be boolean")
    patterns = {
        "product": r"[A-Za-z][A-Za-z0-9_-]{0,63}",
        "startup_method": r"[A-Za-z][A-Za-z0-9_]{0,30}",
        "bundle_identifier": r"[A-Za-z][A-Za-z0-9-]*(?:\.[A-Za-z0-9][A-Za-z0-9-]*)+",
        "macos_deployment_target": r"(?:10\.(?:1[3-9])|(?:1[1-9]|[2-9][0-9])\.[0-9]+)",
        "release_branch": r"[A-Za-z][A-Za-z0-9_-]*(?:/[A-Za-z0-9][A-Za-z0-9_-]*)*",
        "signing_environment": r"[A-Za-z][A-Za-z0-9_-]{0,63}",
    }
    for key, pattern in patterns.items():
        require(isinstance(data[key], str) and re.fullmatch(pattern, data[key]),
                f"Unsafe or unsupported {key}")
    for key in ("product", "source_dir", "build_dir", "test_project", "sdk_dir"):
        value = data[key]
        require(isinstance(value, str)
                and re.fullmatch(r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_.-]+)*", value)
                and not any(part.startswith((".", "-")) for part in value.split("/")),
                f"Unsafe {key}")
        safe_path(data[key])
        no_symlink_path(ROOT / data[key])
    source = PurePosixPath(data["source_dir"])
    for key in ("build_dir", "test_project"):
        require(source in PurePosixPath(data[key]).parents, f"{key} must be below source_dir")
    require(data["test_project"].endswith(".4DProject"), "test_project must be a .4DProject")
    sdk = PurePosixPath(data["sdk_dir"])
    build = PurePosixPath(data["build_dir"])
    project = PurePosixPath(data["test_project"])
    require(sdk != source and source not in sdk.parents and sdk not in source.parents,
            "sdk_dir must not overlap source_dir")
    require(build != project and build not in project.parents and project not in build.parents,
            "build_dir must not overlap test_project")
    return data


def layout():
    product = config()["product"]
    bundle = f"{product}.bundle"
    prefix = f"{bundle}/Contents"
    return dict(bundle=bundle, mac=f"{prefix}/MacOS/{product}",
                win=f"{prefix}/Windows64/{product}.4DX",
                resource=f"{prefix}/Resources/manifest.json",
                constants=f"{prefix}/Resources/constants.xlf",
                plist=f"{prefix}/Info.plist", signature=f"{prefix}/_CodeSignature/CodeResources")


def layouts():
    p = layout()
    return {"macos-universal": {p[k] for k in ("mac", "resource", "constants", "plist")},
            "windows-x64": {p[k] for k in ("win", "resource", "constants")}}


def walk_error(error):
    raise error


def files_in(root):
    root = Path(root)
    no_symlink_path(root)
    require(root.is_dir(), f"Missing payload directory: {root}")
    result, folded = {}, set()
    for directory, dirs, files in os.walk(root, followlinks=False, onerror=walk_error):
        for name in sorted(dirs + files):
            path = Path(directory) / name
            no_symlink_path(path)
            key = portable_relative(path, root)
            require(key.casefold() not in folded, f"Case-colliding path: {key}")
            folded.add(key.casefold())
            require(path.is_dir() or path.is_file(), f"Non-regular file: {key}")
            if path.is_file():
                result[key] = path
    return dict(sorted(result.items()))


def exact_directories(root, paths):
    allowed = {p.as_posix() for key in paths for p in PurePosixPath(key).parents
               if p != PurePosixPath(".")}
    for directory, dirs, _ in os.walk(root, followlinks=False, onerror=walk_error):
        for name in dirs:
            key = portable_relative(Path(directory) / name, Path(root))
            require(key in allowed, f"Unexpected directory: {key}")


def hashes(root, exclude=()):
    return {key: hashlib.sha256(path.read_bytes()).hexdigest()
            for key, path in files_in(root).items() if key not in exclude}


def run(args, cwd=None):
    return subprocess.check_output(args, cwd=ROOT if cwd is None else cwd)


def snapshot():
    settings = config()
    source = run(["git", "rev-parse", "HEAD"]).decode().strip()
    require((ROOT / ".github/plugin-pipeline.json").read_bytes()
            == run(["git", "show", f"{source}:.github/plugin-pipeline.json"]),
            "Pipeline config bytes differ from immutable HEAD:.github/plugin-pipeline.json")
    sdk_dir = settings["sdk_dir"]
    entry = run(["git", "ls-tree", "-z", source, "--", sdk_dir]).decode()
    metadata, separator, path = entry.rstrip("\0").partition("\t")
    link = metadata.split()
    require(separator and len(link) == 3 and link[:2] == ["160000", "commit"]
            and path == sdk_dir,
            "sdk_dir must be an immutable source gitlink")
    sdk = link[2]
    actual = run(["git", "-C", sdk_dir, "rev-parse", "HEAD"]).decode().strip()
    require(actual == sdk, "SDK checkout differs from source gitlink")
    status = run(["git", "status", "--porcelain", "--untracked-files=normal",
                  "--ignore-submodules=none"])
    require(not status, "Source checkout is dirty (tracked, untracked, or submodule changes)")
    version_bytes = (ROOT / "VERSION").read_bytes()
    require(version_bytes == run(["git", "show", f"{source}:VERSION"]),
            "VERSION bytes differ from immutable HEAD:VERSION")
    require(b"\r" not in version_bytes and version_bytes.endswith(b"\n")
            and version_bytes.count(b"\n") == 1, "VERSION must be one LF-terminated line")
    version = version_bytes[:-1].decode()
    require(SEMVER.fullmatch(version), f"Invalid semantic version: {version!r}")
    return dict(source_commit=source, sdk_commit=sdk, version=version)


def toolchain():
    candidates = sorted((ROOT / config()["build_dir"] / "CMakeFiles").glob("*/CMakeCXXCompiler.cmake"))
    require(len(candidates) == 1, "Expected exactly one CMake C++ compiler metadata file")
    text = candidates[0].read_text(encoding="utf-8")
    values = {}
    for field in ("ID", "VERSION"):
        match = re.search(rf'set\(CMAKE_CXX_COMPILER_{field}\s+"([^"\r\n]+)"\)', text)
        require(match, f"Missing CMAKE_CXX_COMPILER_{field}")
        values[f"compiler_{field.lower()}"] = match[1]
    lines = run(["cmake", "--version"]).decode().splitlines()
    require(lines and re.fullmatch(r"cmake version \S+", lines[0]), "Invalid CMake version")
    return {**values, "cmake": lines[0]}


def validate_metadata(data, expected):
    require(isinstance(data, dict) and type(data.get("schema")) is int
            and data["schema"] == 1, "Unsupported provenance schema")
    for key in BINDING:
        require(data.get(key) == expected[key], f"{key} differs from current source snapshot")


def validate_hashes(mapping):
    require(isinstance(mapping, dict) and mapping, "Missing file inventory")
    for key, value in mapping.items():
        safe_path(key)
        require(isinstance(value, str) and re.fullmatch("[0-9a-f]{64}", value),
                f"Invalid SHA-256 for {key}")


def exact_files(actual, expected, label):
    require(set(actual) == set(expected),
            f"{label} layout mismatch; missing={sorted(set(expected) - set(actual))}, "
            f"additional={sorted(set(actual) - set(expected))}")


def verify_hashes(payload, mapping, exclude=()):
    validate_hashes(mapping)
    actual = hashes(payload, exclude)
    exact_files(actual, mapping, "Inventory")
    for key in mapping:
        require(actual[key] == mapping[key], f"SHA-256 mismatch: {key}")


def validate_pe(path):
    with path.open("rb") as binary:
        header = binary.read(64)
        require(len(header) == 64 and header[:2] == b"MZ", "Invalid Windows DOS header")
        offset = struct.unpack_from("<I", header, 60)[0]
        require(offset >= 64, "Invalid PE header offset")
        binary.seek(offset)
        pe = binary.read(26)
        require(len(pe) == 26 and pe[:4] == b"PE\0\0"
                and struct.unpack_from("<H", pe, 4)[0] == 0x8664
                and struct.unpack_from("<H", pe, 24)[0] == 0x20B,
                "Windows binary must be AMD64 PE32+")


def validate_macos(path):
    require(sys.platform == "darwin", "Mach-O validation requires native Apple tooling")
    architectures = run(["lipo", "-archs", str(path)]).decode().split()
    require(len(architectures) == 2 and set(architectures) == {"arm64", "x86_64"},
            "Mach-O must be arm64 + x86_64")
    imports = run(["otool", "-L", str(path)]).decode().splitlines()
    dependencies = []
    for line in imports:
        if not line[:1].isspace():
            continue
        dependency = line.strip().split(" (", 1)[0]
        dependencies.append(dependency)
        require(dependency.startswith(("/System/Library/", "/usr/lib/"))
                and ".." not in dependency.split("/"), f"Non-system Mach-O dependency: {dependency}")
    require(dependencies, "No Mach-O runtime dependencies found")
    commands = run(["otool", "-l", str(path)]).decode()
    require(not re.search(r"\bcmd LC_RPATH\b", commands), "Mach-O rpaths are forbidden")


def validate_plist(payload, version):
    with (payload / layout()["plist"]).open("rb") as stream:
        info = plistlib.load(stream)
    require(isinstance(info, dict), "Info.plist must be a dictionary")
    settings = config()
    for key, value in {"CFBundlePackageType": "BNDL", "CFBundleExecutable": settings["product"],
                       "CFBundleIdentifier": settings["bundle_identifier"],
                       "CFBundleVersion": version, "CFBundleShortVersionString": version}.items():
        require(info.get(key) == value, f"Invalid Info.plist {key}; expected {value}")


def validate_input(data, expected, platform):
    validate_metadata(data, expected)
    require(set(data) == {"schema", *BINDING, "platform", "toolchain", "files"},
            "Unexpected input manifest fields")
    require(data.get("platform") == platform, f"Incorrect input platform: {platform}")
    compiler = data.get("toolchain")
    require(isinstance(compiler, dict) and set(compiler) == {"compiler_id", "compiler_version", "cmake"}
            and all(isinstance(v, str) and v and not any(c in v for c in "/\\\r\n")
                    for v in compiler.values()), "Invalid portable toolchain metadata")
    validate_hashes(data.get("files"))
    exact_files(data["files"], layouts()[platform], platform)


def fresh_directory(path):
    no_symlink_path(path)
    require(not path.exists(), f"Output already exists: {path}")
    path.mkdir(parents=True)


def stage(platform, output):
    expected, settings, p = snapshot(), config(), layout()
    target = {"macos": "macos-universal", "windows": "windows-x64"}[platform]
    source = ROOT / Path(settings["test_project"]).parent.parent / "Plugins"
    source /= p["bundle"] if platform == "macos" else settings["product"]
    source_files = files_in(source)
    canonical = {f'{p["bundle"]}/{key}': path for key, path in source_files.items()}
    exact_files(canonical, layouts()[target], target)
    exact_directories(source, source_files)
    source_resources(canonical)
    if platform == "macos":
        validate_macos(canonical[p["mac"]])
        validate_plist(source.parent, expected["version"])
    else:
        validate_pe(canonical[p["win"]])
    compiler = toolchain()
    destination = output / platform
    fresh_directory(destination)
    payload = destination / "payload"
    for key, path in canonical.items():
        dest = payload / key
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)
    data = {"schema": 1, **expected, "platform": target, "toolchain": compiler, "files": hashes(payload)}
    validate_input(data, expected, target)
    write_json(destination / "manifest.json", data)


def source_resources(paths):
    p = layout()
    for key, name in (("resource", "manifest.json"), ("constants", "constants.xlf")):
        require(paths[p[key]].read_bytes() == (ROOT / config()["source_dir"] / name).read_bytes(),
                f"Resource {name} differs from clean source")


def assemble(inputs, output):
    expected, p, all_layouts = snapshot(), layout(), layouts()
    input_files = files_in(inputs)
    exact_files(input_files, {f"{p}/manifest.json" for p in ("macos", "windows")}
                | {f"macos/payload/{p}" for p in all_layouts["macos-universal"]}
                | {f"windows/payload/{p}" for p in all_layouts["windows-x64"]}, "Inputs")
    exact_directories(inputs, input_files)
    manifests = {}
    for platform, target in (("macos", "macos-universal"), ("windows", "windows-x64")):
        manifests[platform] = read_json(inputs / platform / "manifest.json")
        validate_input(manifests[platform], expected, target)
        verify_hashes(inputs / platform / "payload", manifests[platform]["files"])
        source_resources(files_in(inputs / platform / "payload"))
    mac_payload, win_payload = inputs / "macos/payload", inputs / "windows/payload"
    validate_plist(mac_payload, expected["version"])
    validate_pe(win_payload / p["win"])
    if sys.platform == "darwin":
        validate_macos(mac_payload / p["mac"])
    license_bytes = run(["git", "show", f'{expected["source_commit"]}:LICENSE'])
    sdk_license = run(["git", "-C", config()["sdk_dir"], "show", f'{expected["sdk_commit"]}:LICENSE.md'])
    fresh_directory(output)
    payload = output / "payload"
    shutil.copytree(mac_payload, payload)
    (payload / p["mac"]).chmod(0o755)
    (payload / p["win"]).parent.mkdir(parents=True)
    shutil.copy2(win_payload / p["win"], payload / p["win"])
    (payload / "LICENSE").write_bytes(license_bytes)
    (payload / "SDK-LICENSE.md").write_bytes(sdk_license)
    write_json(payload / "BUILD-PROVENANCE.json",
               {"schema": 1, **expected, "hashes": "pre-sign", "inputs": manifests})


def build_provenance(payload):
    expected = snapshot()
    build = read_json(payload / "BUILD-PROVENANCE.json")
    validate_metadata(build, expected)
    require(set(build) == {"schema", *BINDING, "hashes", "inputs"} and build["hashes"] == "pre-sign",
            "Invalid build provenance")
    require(isinstance(build["inputs"], dict) and set(build["inputs"]) == {"macos", "windows"},
            "Missing build inputs")
    for platform, target in (("macos", "macos-universal"), ("windows", "windows-x64")):
        validate_input(build["inputs"][platform], expected, target)
    return build


def validate_payload_layout(payload, actual):
    p = layout()
    required = layouts()["macos-universal"] | layouts()["windows-x64"] | {
        "LICENSE", "SDK-LICENSE.md", "BUILD-PROVENANCE.json"}
    allowed = required | ({p["signature"]} if p["signature"] in actual else set())
    exact_files(actual, allowed, "Payload")
    exact_directories(payload, allowed)


def validate_payload_content(payload, build):
    p = layout()
    source_resources(files_in(payload))
    validate_plist(payload, build["version"])
    validate_pe(payload / p["win"])
    if sys.platform == "darwin":
        validate_macos(payload / p["mac"])
    require((payload / p["mac"]).stat().st_mode & 0o111,
            "macOS plugin executable permission is missing")
    for name, command in (
            ("LICENSE", ["git", "show", f'{build["source_commit"]}:LICENSE']),
            ("SDK-LICENSE.md", ["git", "-C", config()["sdk_dir"], "show", f'{build["sdk_commit"]}:LICENSE.md'])):
        require((payload / name).read_bytes() == run(command), f"Source license mismatch: {name}")
    current = hashes(payload)
    for platform in ("macos", "windows"):
        for key, digest in build["inputs"][platform]["files"].items():
            if key != p["mac"]:
                require(current[key] == digest, f"Pre-sign input differs: {key}")


def inventory(payload, output, team=None):
    no_symlink_path(output)
    require(output.absolute() == (payload / "PROVENANCE.json").absolute(),
            "Final inventory must be payload/PROVENANCE.json")
    require(not output.exists(), f"Output already exists: {output}")
    require(team is None or (isinstance(team, str) and re.fullmatch("[A-Z0-9]{10}", team)),
            "Invalid Apple team ID")
    build = build_provenance(payload)
    actual = hashes(payload)
    validate_payload_layout(payload, actual)
    validate_payload_content(payload, build)
    require(team is None or layout()["signature"] in actual, "Missing signed CodeResources")
    write_json(output, {"schema": 1, **{k: build[k] for k in BINDING},
                        "expected_team": team, "files": actual})


def verify(payload, manifest):
    data = read_json(manifest)
    validate_metadata(data, snapshot())
    require(set(data) == {"schema", *BINDING, "expected_team", "files"}, "Invalid final inventory fields")
    team = data["expected_team"]
    require(team is None or (isinstance(team, str) and re.fullmatch("[A-Z0-9]{10}", team)),
            "Invalid expected Apple team")
    build = build_provenance(payload)
    verify_hashes(payload, data.get("files"), exclude=("PROVENANCE.json",))
    validate_payload_layout(payload, data["files"])
    validate_payload_content(payload, build)
    require(team is None or layout()["signature"] in data["files"], "Missing signed CodeResources")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("stage")
    command.add_argument("--platform", required=True, choices=("macos", "windows"))
    command.add_argument("--output", type=Path, required=True)
    command = commands.add_parser("assemble")
    command.add_argument("--inputs", type=Path, required=True)
    command.add_argument("--output", type=Path, required=True)
    command = commands.add_parser("inventory")
    command.add_argument("--payload", type=Path, required=True)
    command.add_argument("--output", type=Path, required=True)
    command.add_argument("--team")
    command = commands.add_parser("verify")
    command.add_argument("--payload", type=Path, required=True)
    command.add_argument("--manifest", type=Path, required=True)
    commands.add_parser("config")
    args = vars(parser.parse_args(argv))
    action = args.pop("command")
    try:
        if action == "config":
            print(json.dumps(config(), sort_keys=True))
        else:
            globals()[action](**args)
    except (ValueError, OSError, subprocess.CalledProcessError, UnicodeError) as error:
        parser.exit(1, f"package: {error}\n")


if __name__ == "__main__":
    main()
