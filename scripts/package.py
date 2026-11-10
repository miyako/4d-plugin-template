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
    validate_dependencies(data)
    return data


# Static third-party dependency profile; mirrors generate_plugin_pipeline.validate_dependencies.
DEPENDENCY_FIELDS = {"profile", "windows", "notices_dir", "allowed_licenses",
                     "copyleft_acknowledged", "components"}
SPDX_ID = r"[A-Za-z0-9][A-Za-z0-9.+-]{0,63}"
COPYLEFT = re.compile(r"L?GPL-")
PORT_NAME = r"[a-z0-9][a-z0-9-]{0,63}"
NOTICE_FILE = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}"
NOTICES_DIR = r"[A-Za-z0-9][A-Za-z0-9_-]*(?:/[A-Za-z0-9][A-Za-z0-9_.-]*)*"
VCPKG_TRIPLET = "x64-windows-static-release"
VCPKG_HOST_TRIPLET = "x64-windows-release-host"
VCPKG_FIELDS = {"$schema", "name", "version", "version-semver", "version-date", "version-string",
                "port-version", "description", "homepage", "license", "dependencies",
                "overrides", "builtin-baseline", "supports"}
THIRD_PARTY_NOTICES = "THIRD-PARTY-NOTICES"
THIRD_PARTY_RECORD = "THIRD-PARTY.json"


def license_ids(expression):
    """SPDX ids in a license expression; operators and exceptions are not licenses."""
    require(isinstance(expression, str) and 0 < len(expression) <= 256,
            "Invalid SPDX license expression")
    invalid = f"Invalid SPDX license expression: {expression}"
    ids, depth, operand, exception, after_id = [], 0, True, False, False
    for token in re.findall(r"[()]|[^\s()]+", expression):
        licence = False
        if token == "(":
            require(operand and not exception, invalid)
            depth += 1
        elif token == ")":
            require(not operand and depth > 0, invalid)
            depth -= 1
        elif token in ("AND", "OR", "WITH"):
            require(not operand and (token != "WITH" or after_id), invalid)
            operand, exception = True, token == "WITH"
        else:
            require(operand and re.fullmatch(SPDX_ID, token) and token != "NOASSERTION", invalid)
            if not exception:
                ids.append(token)
                licence = True
            operand, exception = False, False
        after_id = licence
    require(not operand and not depth, invalid)
    return ids


def sorted_unique(value, field):
    require(isinstance(value, list) and all(isinstance(item, str) for item in value)
            and value == sorted(set(value)),
            f"dependencies.{field} must be a sorted list of unique strings")
    return value


def validate_dependencies(data):
    dependencies = data["dependencies"]
    if type(dependencies) is list and not dependencies:
        return None
    require(isinstance(dependencies, dict) and set(dependencies) == DEPENDENCY_FIELDS,
            "dependencies must be [] or a static dependency profile object")
    require(dependencies["profile"] == "static", "Unsupported dependency profile")
    require(dependencies["windows"] in ("vcpkg", "none"), "dependencies.windows must be vcpkg or none")
    notices = dependencies["notices_dir"]
    require(isinstance(notices, str) and re.fullmatch(NOTICES_DIR, notices)
            and not any(part.endswith(".") for part in notices.split("/")),
            "Unsafe dependencies.notices_dir")
    safe_path(notices)
    for field in ("build_dir", "sdk_dir"):
        other, path = PurePosixPath(data[field]), PurePosixPath(notices)
        require(path != other and other not in path.parents and path not in other.parents,
                f"dependencies.notices_dir must not overlap {field}")
    allowed = sorted_unique(dependencies["allowed_licenses"], "allowed_licenses")
    require(allowed, "dependencies.allowed_licenses must not be empty")
    for license in allowed:
        require(re.fullmatch(SPDX_ID, license) and license != "NOASSERTION",
                f"Invalid SPDX license id: {license}")
        require(not license.upper().startswith("AGPL"),
                f"AGPL is not distributable as a statically linked plugin: {license}")
    acknowledged = sorted_unique(dependencies["copyleft_acknowledged"], "copyleft_acknowledged")
    components = dependencies["components"]
    require(isinstance(components, dict) and components,
            "dependencies.components must be a non-empty object")
    used, windows = set(), False
    for name, component in components.items():
        require(re.fullmatch(PORT_NAME, name) and not name.startswith("vcpkg-"),
                f"Invalid dependency component name: {name!r}")
        require(isinstance(component, dict) and set(component) == {"license", "notice", "platforms"},
                f"Component {name} must define exactly license, notice and platforms")
        ids = license_ids(component["license"])
        for license in ids:
            require(license in allowed, f"Component {name} license is not allowed: {license}")
        used.update(ids)
        notice = component["notice"]
        require(isinstance(notice, str) and re.fullmatch(NOTICE_FILE, notice)
                and not notice.endswith("."), f"Unsafe notice file for component {name}")
        safe_path(notice)
        platforms = sorted_unique(component["platforms"], "platforms")
        require(platforms and set(platforms) <= {"macos", "windows"},
                f"Component {name} platforms must be macos and/or windows")
        windows = windows or "windows" in platforms
    copyleft = {license for license in used if COPYLEFT.match(license)}
    require(set(acknowledged) == copyleft,
            "dependencies.copyleft_acknowledged must list exactly the GPL/LGPL licenses "
            f"used by components: {sorted(copyleft)}")
    require(dependencies["windows"] != "vcpkg" or windows,
            "A vcpkg profile requires at least one Windows component")
    return dependencies


def dependency_profile():
    dependencies = config()["dependencies"]
    return dependencies if isinstance(dependencies, dict) else None


def uses_vcpkg():
    profile = dependency_profile()
    return profile is not None and profile["windows"] == "vcpkg"


def validate_vcpkg_manifest(content):
    """Pinned, registry-free vcpkg manifest: builtin-baseline plus exact-version overrides."""
    require(isinstance(content, bytes) and b"\r" not in content, "vcpkg.json must use LF line endings")
    manifest = json.loads(content, object_pairs_hook=unique_object)
    require(isinstance(manifest, dict) and set(manifest) <= VCPKG_FIELDS,
            "vcpkg.json has unsupported fields (registries, overlays and configuration are forbidden)")
    baseline = manifest.get("builtin-baseline")
    require(isinstance(baseline, str) and re.fullmatch("[0-9a-f]{40}", baseline),
            "vcpkg.json must pin builtin-baseline to a full vcpkg commit")
    overrides = manifest.get("overrides", [])
    require(isinstance(overrides, list), "vcpkg.json overrides must be a list")
    names = []
    for override in overrides:
        require(isinstance(override, dict) and {"name", "version"} <= set(override)
                <= {"name", "version", "port-version"}, "vcpkg.json overrides must pin exact versions")
        require(isinstance(override["name"], str) and re.fullmatch(PORT_NAME, override["name"]),
                "Invalid vcpkg override name")
        require(isinstance(override["version"], str) and override["version"]
                and not any(c in override["version"] for c in "\r\n\0"), "Invalid vcpkg override version")
        require(type(override.get("port-version", 0)) is int and override.get("port-version", 0) >= 0,
                "Invalid vcpkg override port-version")
        names.append(override["name"])
    require(len(names) == len(set(names)), "Duplicate vcpkg override")
    return {"baseline": baseline,
            "overrides": sorted(overrides, key=lambda override: override["name"])}


def vcpkg_baseline():
    require(uses_vcpkg(), "The pipeline config does not enable the vcpkg dependency profile")
    no_symlink_path(ROOT / "vcpkg.json")
    require(not (ROOT / "vcpkg-configuration.json").exists(),
            "vcpkg-configuration.json is forbidden by the static dependency profile")
    print(validate_vcpkg_manifest((ROOT / "vcpkg.json").read_bytes())["baseline"])


def committed_vcpkg(commit):
    require(not run(["git", "ls-tree", "--name-only", commit, "--", "vcpkg-configuration.json"]).strip(),
            "vcpkg-configuration.json is forbidden by the static dependency profile")
    return validate_vcpkg_manifest(run(["git", "show", f"{commit}:vcpkg.json"]))


def committed_notices(commit):
    """Exact committed notices: one non-empty file per component, nothing else."""
    profile = dependency_profile()
    directory = profile["notices_dir"]
    expected = {component["notice"] for component in profile["components"].values()}
    listing = run(["git", "ls-tree", "-r", "-z", "--name-only", commit, "--", directory]).decode()
    actual = {name[len(directory) + 1:] for name in listing.split("\0") if name}
    require(actual == expected,
            f"{directory} must contain exactly the declared notices; "
            f"missing={sorted(expected - actual)}, additional={sorted(actual - expected)}")
    notices = {}
    for name in sorted(expected):
        content = run(["git", "show", f"{commit}:{directory}/{name}"])
        require(content.strip(), f"Empty third-party notice: {name}")
        notices[name] = content
    return notices


def vcpkg_closure():
    """Installed target-triplet ports must be exactly the declared Windows components."""
    profile = dependency_profile()
    installed = ROOT / config()["build_dir"] / "vcpkg_installed"
    info = installed / "vcpkg" / "info"
    no_symlink_path(info)
    require(info.is_dir(), f"Missing vcpkg install metadata: {info}")
    suffix = f"_{VCPKG_TRIPLET}.list"
    ports = {}
    for path in sorted(info.iterdir()):
        if not path.name.endswith(suffix):
            continue
        port = path.name.split("_", 1)[0]
        require(re.fullmatch(PORT_NAME, port), f"Invalid vcpkg port: {port!r}")
        if port.startswith("vcpkg-"):
            continue
        component = profile["components"].get(port)
        require(component is not None and "windows" in component["platforms"],
                f"Undeclared vcpkg port in Windows closure: {port}")
        spdx = read_json(installed / VCPKG_TRIPLET / "share" / port / "vcpkg.spdx.json")
        packages = [item for item in spdx.get("packages", []) if isinstance(item, dict)
                    and item.get("SPDXID") == "SPDXRef-port"] if isinstance(spdx, dict) else []
        require(len(packages) == 1, f"Missing vcpkg SPDX port record: {port}")
        version, concluded = packages[0].get("versionInfo"), packages[0].get("licenseConcluded")
        require(isinstance(version, str) and version and not any(c in version for c in "\r\n\0"),
                f"Invalid vcpkg port version: {port}")
        require(isinstance(concluded, str) and concluded, f"Missing vcpkg license: {port}")
        if concluded != "NOASSERTION":
            require(concluded == component["license"],
                    f"vcpkg license for {port} is {concluded}; declared {component['license']}")
        ports[port] = {"license": component["license"], "version": version}
    declared = {name for name, component in profile["components"].items()
                if "windows" in component["platforms"]}
    require(set(ports) == declared,
            f"Declared Windows components missing from vcpkg closure: {sorted(declared - set(ports))}")
    return ports


def validate_windows_dependencies(value):
    require(isinstance(value, dict) and set(value) == {"triplet", "ports"}
            and value["triplet"] == VCPKG_TRIPLET and isinstance(value["ports"], dict),
            "Invalid Windows dependency closure")
    profile = dependency_profile()
    declared = {name for name, component in profile["components"].items()
                if "windows" in component["platforms"]}
    require(set(value["ports"]) == declared, "Windows dependency closure differs from declared components")
    for port, record in value["ports"].items():
        require(isinstance(record, dict) and set(record) == {"license", "version"}
                and record["license"] == profile["components"][port]["license"]
                and isinstance(record["version"], str) and record["version"],
                f"Invalid Windows dependency record: {port}")


def third_party_record(build):
    """Deterministic THIRD-PARTY.json content, recomputed during verification."""
    profile = dependency_profile()
    commit = build["source_commit"]
    record = {
        "schema": 1,
        "source_commit": commit,
        "components": {name: {"license": component["license"],
                              "notice": f"{THIRD_PARTY_NOTICES}/{component['notice']}",
                              "platforms": component["platforms"]}
                       for name, component in sorted(profile["components"].items())},
        "copyleft_acknowledged": profile["copyleft_acknowledged"],
        "linkage": "static",
        "vcpkg": None,
        "corresponding_source": (
            f"Rebuild from plugin source commit {commit} with the third-party sources listed here"
            + (", resolved by the pinned vcpkg baseline and overrides" if profile["windows"] == "vcpkg"
               else "") + "; the same build relinks modified versions of statically linked components."),
    }
    if profile["windows"] == "vcpkg":
        record["vcpkg"] = {**committed_vcpkg(commit), "triplet": VCPKG_TRIPLET,
                           "host_triplet": VCPKG_HOST_TRIPLET,
                           "ports": build["inputs"]["windows"]["dependencies"]["ports"]}
    return (json.dumps(record, indent=2, sort_keys=True) + "\n").encode()


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


WINDOWS_SYSTEM_DLLS = frozenset(name + ".dll" for name in (
    "advapi32", "bcrypt", "cfgmgr32", "comctl32", "comdlg32", "crypt32", "d2d1", "d3d11",
    "dbghelp", "dnsapi", "dwmapi", "dwrite", "dxgi", "gdi32", "gdiplus", "imm32", "iphlpapi",
    "kernel32", "mpr", "msimg32", "mswsock", "ncrypt", "netapi32", "normaliz", "ntdll",
    "ole32", "oleacc", "oleaut32", "opengl32", "psapi", "rpcrt4", "secur32", "setupapi",
    "shcore", "shell32", "shlwapi", "ucrtbase", "urlmon", "user32", "userenv", "uxtheme", "version",
    "windowscodecs", "winhttp", "wininet", "winmm", "wldap32", "ws2_32", "wtsapi32"))
WINDOWS_API_SET = re.compile(r"(?:api|ext)-ms-[a-z0-9-]+-l[0-9]+-[0-9]+-[0-9]+\.dll")
# The exact layout ships one self-contained 4DX; Visual C++ runtimes must be linked statically.
VCRUNTIME_DLL = re.compile(r"(?:vcruntime|msvcp|msvcr|concrt|vcomp|vccorlib|vcamp|mfc|ucrtbased)"
                           r"[a-z0-9_]*\.dll")
PE_MAX_IMPORTS = 4096


def pe_imports(data):
    require(len(data) >= 64 and data[:2] == b"MZ", "Invalid Windows DOS header")
    pe = struct.unpack_from("<I", data, 60)[0]
    require(pe >= 64 and pe + 24 <= len(data) and data[pe:pe + 4] == b"PE\0\0",
            "Invalid PE header offset")
    machine, sections, optional_size = (struct.unpack_from("<H", data, pe + o)[0] for o in (4, 6, 20))
    optional = pe + 24
    require(machine == 0x8664 and optional + 2 <= len(data)
            and struct.unpack_from("<H", data, optional)[0] == 0x20B,
            "Windows binary must be AMD64 PE32+")
    require(112 <= optional_size and optional + optional_size <= len(data) and 0 < sections <= 96,
            "Invalid PE optional header")
    directories = struct.unpack_from("<I", data, optional + 108)[0]
    require(directories <= 16 and 112 + 8 * directories <= optional_size,
            "Invalid PE data directories")
    table = optional + optional_size
    require(table + 40 * sections <= len(data), "Truncated PE section table")
    mapped = []
    for index in range(sections):
        virtual_size, virtual_address, raw_size, raw_offset = struct.unpack_from(
            "<IIII", data, table + 40 * index + 8)
        require(raw_offset + raw_size <= len(data), "PE section exceeds file")
        mapped.append((virtual_address, max(virtual_size, raw_size), raw_offset, raw_size))

    def offset(rva, size):
        for address, extent, raw, raw_size in mapped:
            if address <= rva and rva + size <= address + min(extent, raw_size):
                return raw + rva - address
        raise PackageError(f"Unmapped PE RVA: {rva:#x}")

    def name_at(rva):
        start = offset(rva, 1)
        end = data.find(b"\0", start, start + 256)
        require(end > start, "Invalid PE import name")
        raw = data[start:end]
        require(all(0x21 <= c < 0x7F and c not in b'\\/:<>"|?*' for c in raw),
                "Invalid PE import name")
        return raw.decode("ascii")

    names = []
    for directory, size, field in ((1, 20, 12), (13, 32, 4)):
        if directory >= directories:
            continue
        rva, length = struct.unpack_from("<II", data, optional + 112 + 8 * directory)
        if not rva and not length:
            continue
        require(rva, "Invalid PE import directory")
        for index in range(PE_MAX_IMPORTS + 1):
            require(index < PE_MAX_IMPORTS, "Too many PE import descriptors")
            descriptor = data[offset(rva + size * index, size):][:size]
            if not any(descriptor):
                break
            names.append(name_at(struct.unpack_from("<I", descriptor, field)[0]))
    return names


def validate_pe(path):
    imports = pe_imports(Path(path).read_bytes())
    require(imports, "Windows binary has no imports")
    for name in imports:
        folded = name.lower()
        require(not VCRUNTIME_DLL.fullmatch(folded),
                f"VC++ runtime import forbidden (link the static CRT): {name}")
        require(folded in WINDOWS_SYSTEM_DLLS or WINDOWS_API_SET.fullmatch(folded),
                f"Non-system Windows import: {name}")
    return imports


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
    closure = platform == "windows-x64" and uses_vcpkg()
    require(set(data) == {"schema", *BINDING, "platform", "toolchain", "files"}
            | ({"dependencies"} if closure else set()),
            "Unexpected input manifest fields")
    if closure:
        validate_windows_dependencies(data["dependencies"])
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
    closure = None
    if dependency_profile():
        committed_notices(expected["source_commit"])
        if platform == "windows" and uses_vcpkg():
            committed_vcpkg(expected["source_commit"])
            closure = {"triplet": VCPKG_TRIPLET, "ports": vcpkg_closure()}
    compiler = toolchain()
    destination = output / platform
    fresh_directory(destination)
    payload = destination / "payload"
    for key, path in canonical.items():
        dest = payload / key
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)
    data = {"schema": 1, **expected, "platform": target, "toolchain": compiler, "files": hashes(payload)}
    if closure is not None:
        data["dependencies"] = closure
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
    build = {"schema": 1, **expected, "hashes": "pre-sign", "inputs": manifests}
    notices = committed_notices(expected["source_commit"]) if dependency_profile() else {}
    record = third_party_record(build) if dependency_profile() else None
    fresh_directory(output)
    payload = output / "payload"
    shutil.copytree(mac_payload, payload)
    (payload / p["mac"]).chmod(0o755)
    (payload / p["win"]).parent.mkdir(parents=True)
    shutil.copy2(win_payload / p["win"], payload / p["win"])
    (payload / "LICENSE").write_bytes(license_bytes)
    (payload / "SDK-LICENSE.md").write_bytes(sdk_license)
    if record is not None:
        (payload / THIRD_PARTY_NOTICES).mkdir()
        for name, content in notices.items():
            (payload / THIRD_PARTY_NOTICES / name).write_bytes(content)
        (payload / THIRD_PARTY_RECORD).write_bytes(record)
    write_json(payload / "BUILD-PROVENANCE.json", build)


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


def third_party_files():
    profile = dependency_profile()
    if profile is None:
        return set()
    return {THIRD_PARTY_RECORD} | {f"{THIRD_PARTY_NOTICES}/{component['notice']}"
                                   for component in profile["components"].values()}


def validate_payload_layout(payload, actual):
    p = layout()
    required = layouts()["macos-universal"] | layouts()["windows-x64"] | {
        "LICENSE", "SDK-LICENSE.md", "BUILD-PROVENANCE.json"} | third_party_files()
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
    # Windows file systems cannot store the POSIX executable bit; the macOS jobs that
    # assemble, sign and verify the distribution still require it.
    if os.name != "nt":
        require((payload / p["mac"]).stat().st_mode & 0o111,
                "macOS plugin executable permission is missing")
    for name, command in (
            ("LICENSE", ["git", "show", f'{build["source_commit"]}:LICENSE']),
            ("SDK-LICENSE.md", ["git", "-C", config()["sdk_dir"], "show", f'{build["sdk_commit"]}:LICENSE.md'])):
        require((payload / name).read_bytes() == run(command), f"Source license mismatch: {name}")
    if dependency_profile():
        for name, content in committed_notices(build["source_commit"]).items():
            require((payload / THIRD_PARTY_NOTICES / name).read_bytes() == content,
                    f"Third-party notice mismatch: {name}")
        require((payload / THIRD_PARTY_RECORD).read_bytes() == third_party_record(build),
                f"{THIRD_PARTY_RECORD} differs from source and build provenance")
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
    commands.add_parser("vcpkg-baseline")
    args = vars(parser.parse_args(argv))
    action = args.pop("command")
    try:
        if action == "config":
            print(json.dumps(config(), sort_keys=True))
        elif action == "vcpkg-baseline":
            vcpkg_baseline()
        else:
            globals()[action](**args)
    except (ValueError, OSError, subprocess.CalledProcessError, UnicodeError) as error:
        parser.exit(1, f"package: {error}\n")


if __name__ == "__main__":
    main()
