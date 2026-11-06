#!/usr/bin/env python3
"""Verify final distribution bytes and extract a fresh source-bound smoke-test payload."""

import argparse
import hashlib
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import zipfile

sys.dont_write_bytecode = True
import package
import release_checks


def native(args):
    return subprocess.check_output(args, stderr=subprocess.STDOUT, cwd=package.ROOT).decode()


def verify_checksums(directory, expected):
    files = package.files_in(directory)
    package.exact_files(files, expected | {"SHA256SUMS"}, "Distribution")
    package.exact_directories(directory, files)
    raw = (directory / "SHA256SUMS").read_bytes()
    package.require(raw.endswith(b"\n") and b"\r" not in raw, "Invalid checksum line endings")
    entries = {}
    for line in raw.decode().splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  (.+)", line)
        package.require(match is not None, "Invalid SHA256SUMS entry")
        name = package.safe_path(match[2])
        package.require("/" not in name and name not in entries, "Duplicate or nested checksum entry")
        entries[name] = match[1]
    package.exact_files(entries, expected, "Checksums")
    for name, digest in entries.items():
        package.require(hashlib.sha256(files[name].read_bytes()).hexdigest() == digest,
                        f"SHA-256 mismatch: {name}")


def extract_zip(archive, output, inventory):
    package.validate_hashes(inventory)
    allowed = set(inventory) | {"PROVENANCE.json"}
    allowed_dirs = {p.as_posix() for name in allowed for p in PurePosixPath(name).parents
                    if p != PurePosixPath(".")}
    with zipfile.ZipFile(archive) as zipped:
        files, names = {}, set()
        for entry in zipped.infolist():
            name = entry.filename[:-1] if entry.is_dir() else entry.filename
            package.safe_path(name)
            package.require(name.casefold() not in names, f"Duplicate ZIP entry: {name}")
            names.add(name.casefold())
            mode = entry.external_attr >> 16
            package.require(not stat.S_ISLNK(mode)
                            and (stat.S_IFMT(mode) in (0, stat.S_IFREG, stat.S_IFDIR)),
                            f"Non-regular ZIP entry: {name}")
            package.require(not entry.flag_bits & 1, "Encrypted ZIP entries are forbidden")
            if entry.is_dir():
                package.require(name in allowed_dirs, f"Unexpected ZIP directory: {name}")
            else:
                package.require(not stat.S_ISDIR(mode), f"Invalid ZIP entry type: {name}")
                files[name] = entry
        package.exact_files(files, allowed, "ZIP")
        package.fresh_directory(output)
        for name, entry in files.items():
            destination = output / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            with zipped.open(entry) as source, destination.open("xb") as target:
                # Stream large binaries rather than retaining a second archive in memory.
                import shutil
                shutil.copyfileobj(source, target)
            destination.chmod(0o755 if name == package.layout()["mac"] else 0o644)


def verify_distribution(directory, output):
    directory, output = Path(directory).absolute(), Path(output).absolute()
    package.no_symlink_path(directory)
    package.no_symlink_path(output)
    package.require(not output.exists(), f"Output already exists: {output}")
    package.require(directory not in output.parents, "Extraction output must be outside distribution")
    settings, binding = package.config(), package.snapshot()
    version, product = binding["version"], settings["product"]
    zip_name, dmg_name = f"{product}-{version}.zip", f"{product}-{version}.dmg"
    expected = {zip_name, dmg_name, "PROVENANCE.json", "zip-notarization.json", "dmg-notarization.json"}
    verify_checksums(directory, expected)
    final = package.read_json(directory / "PROVENANCE.json")
    package.validate_metadata(final, binding)
    team = final.get("expected_team")
    package.require(isinstance(team, str) and re.fullmatch("[A-Z0-9]{10}", team),
                    "Final distribution requires an expected Developer ID team")
    for format in ("zip", "dmg"):
        release_checks.notarization(package.read_json(directory / f"{format}-notarization.json"))
    package.require(sys.platform == "darwin", "Final distribution verification requires macOS")
    extract_zip(directory / zip_name, output, final.get("files"))
    package.require((output / "PROVENANCE.json").read_bytes()
                    == (directory / "PROVENANCE.json").read_bytes(), "Archived inventory differs")
    package.verify(output, output / "PROVENANCE.json")
    bundle = output / package.layout()["bundle"]
    native(["codesign", "--verify", "--strict", str(bundle)])
    native(["codesign", "--verify", "--strict", str(output / package.layout()["mac"])])
    release_checks.signature(native(["codesign", "--display", "--verbose=4", str(bundle)]), team, True)
    dmg = directory / dmg_name
    native(["codesign", "--verify", "--strict", str(dmg)])
    release_checks.signature(native(["codesign", "--display", "--verbose=4", str(dmg)]), team)
    native(["xcrun", "stapler", "validate", str(dmg)])
    native(["spctl", "--assess", "--type", "open", "--context", "context:primary-signature", str(dmg)])
    return {"distribution_verified": True, "source_commit": binding["source_commit"], "version": version,
            "product": product, "payload": str(output), "bundle": str(bundle)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        import json
        print(json.dumps(verify_distribution(args.directory, args.output), sort_keys=True))
    except (ValueError, OSError, subprocess.CalledProcessError, zipfile.BadZipFile) as error:
        parser.exit(1, f"verify-distribution: {error}\n")


if __name__ == "__main__":
    main()
