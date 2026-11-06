#!/usr/bin/env python3
"""Check tracked pipeline ownership fingerprints before any generated CI build."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ".github/plugin-pipeline-generated.json"
WORKFLOWS = {"bump-version.yml", "test.yml", "release.yml"}


def object_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def digest(content):
    return hashlib.sha256(content).hexdigest()


def managed_section(name, content):
    if name not in (".gitignore", ".gitattributes"):
        return content
    # Generator uses the same named section for both additive repository files.
    begin = b"# BEGIN 4D PLUGIN PIPELINE (managed)"
    end = b"# END 4D PLUGIN PIPELINE (managed)"
    if content.count(begin) != 1 or content.count(end) != 1:
        raise ValueError(f"Ambiguous or missing managed section: {name}")
    match = re.search(rb"(?m)^" + re.escape(begin) + rb"\n.*?^"
                      + re.escape(end) + rb"(?:\n|$)", content, re.S)
    if not match:
        raise ValueError(f"Malformed managed section: {name}")
    return match.group()


def check(root=ROOT):
    root = Path(root)
    manifest_path = root / MANIFEST
    if any(path.is_symlink() for path in (manifest_path, *manifest_path.parents)):
        raise ValueError("Symlink pipeline ownership manifest")
    manifest = json.loads(manifest_path.read_bytes(), object_pairs_hook=object_pairs)
    if (not isinstance(manifest, dict)
            or set(manifest) != {"schema", "files", "config_sha256", "contract_sha256"}
            or type(manifest["schema"]) is not int or manifest["schema"] != 1
            or not isinstance(manifest["files"], dict)):
        raise ValueError("Invalid generated pipeline ownership manifest")
    expected = manifest["files"]
    required = {".github/plugin-pipeline.json", ".github/plugin-pipeline-contract.json",
                ".gitignore", ".gitattributes", "scripts/check_pipeline.py"}
    required |= {f".github/workflows/{name}" for name in WORKFLOWS}
    if not required.issubset(expected):
        raise ValueError("Required managed pipeline files are missing")
    for name, fingerprint in expected.items():
        if (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*", name)
                or any(part in (".", "..") for part in name.split("/"))
                or not isinstance(fingerprint, str) or not re.fullmatch("[0-9a-f]{64}", fingerprint)):
            raise ValueError("Unsafe generated manifest entry")
        target = root / name
        if any(path.is_symlink() for path in (target, *target.parents)):
            raise ValueError(f"Symlink managed file: {name}")
        if not target.is_file() or digest(managed_section(name, target.read_bytes())) != fingerprint:
            raise ValueError(f"Generated pipeline drift: {name}")
    for field, name in (("config_sha256", ".github/plugin-pipeline.json"),
                        ("contract_sha256", ".github/plugin-pipeline-contract.json")):
        if manifest[field] != expected[name]:
            raise ValueError(f"Invalid {field}")
    actual = {path.name for path in (root / ".github/workflows").iterdir()
              if path.suffix in (".yaml", ".yml")}
    if actual != WORKFLOWS:
        raise ValueError("Generated plugins must have exactly the three canonical workflows")
    return len(expected)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    try:
        count = check(args.root)
    except (ValueError, OSError) as error:
        parser.exit(1, f"pipeline-check: {error}\n")
    print(f"Pipeline ownership verified ({count} files).")


if __name__ == "__main__":
    main()
