#!/usr/bin/env python3
"""Prepare the separately delivered example scaffold from an immutable local ref."""

import argparse
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys

import generate_plugin_pipeline


ROOT = Path(__file__).resolve().parents[1]
BASELINE = "f8e20f052407c3e74c33192884dc82b31a2bd27a"


def git(*args):
    return subprocess.check_output(["git", "-C", str(ROOT), *args])


def prepare(source_ref, output):
    if not re.fullmatch("[0-9a-f]{40}", source_ref):
        raise ValueError("Use a full immutable example commit SHA")
    if git("cat-file", "-t", source_ref).strip() != b"commit":
        raise ValueError("The source revision must be a commit")
    output = Path(output)
    if output.exists():
        raise ValueError("Use a new output directory; existing output is never overwritten")
    for parent in (output, *output.parents):
        if parent.is_symlink():
            raise ValueError("Refusing a symlink output path")
    files = {}
    for entry in git("ls-tree", "-r", "-z", source_ref).split(b"\0"):
        if not entry:
            continue
        metadata, name = entry.split(b"\t", 1)
        mode, kind, _ = metadata.split()
        name = name.decode("utf-8")
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("Unsafe path in source tree")
        if name.startswith("example/") or name in (
                ".gitignore", ".gitattributes", ".gitmodules", "LICENSE", "README.md"):
            if kind != b"blob" or mode not in (b"100644", b"100755"):
                raise ValueError(f"Non-regular example file: {name}")
            files[name] = git("show", f"{source_ref}:{name}")
    required = {
        "example/CMakeLists.txt", "example/manifest.json", "example/constants.xlf",
        "example/example-4dplugin.cpp", "example/example-4dplugin.h",
        "example/example-test/Project/example.4DProject",
        "example/example-test/Project/Sources/Methods/test_all.4dm",
        ".gitmodules", "LICENSE", "README.md",
    }
    if not required.issubset(files):
        raise ValueError("Source ref is not the complete example scaffold")
    overlay = ROOT / "templates/example-overlay"
    files["example/CMakeLists.txt"] = (overlay / "CMakeLists.txt").read_bytes()
    files["example/Info.plist.in"] = (overlay / "Info.plist.in").read_bytes()
    files["VERSION"] = (overlay / "VERSION").read_bytes()
    for name in (
            "AGENTS.md", "SKILL.md",
            ".github/instructions/plugin-dev.instructions.md", "docs/plugin-pipeline.md",
            "scripts/set-release-secrets.sh", "scripts/tests/test_release_secrets.py"):
        files[name] = (ROOT / name).read_bytes()
    files["README.md"] += (
        "\n\n## Canonical pipeline\n\n"
        "This runnable scaffold consumes the template repository's "
        "[canonical pipeline](docs/plugin-pipeline.md). Its three generated workflows "
        "provide two release actions, Bump version and Build, plus separate unsigned "
        "Build and Test checks. Bumps commit "
        "VERSION without tagging; release is explicitly dispatched from main and "
        "preflights immutable source/version/release/tag before native work and "
        "publishes vVERSION only after package acceptance. Regenerate through "
        "the canonical template generator; do not hand-edit the pipeline.\n"
    ).encode("utf-8")
    config = json.loads((ROOT / "templates/plugin-pipeline/example.json").read_text())
    # Render every canonical input before creating the output directory.
    generated = generate_plugin_pipeline.render(config)
    files.update({name: content for name, content in generated.items()
                  if name not in (".gitignore", ".gitattributes")})
    output.mkdir(parents=True)
    for name, content in files.items():
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        if name.endswith(".sh"):
            target.chmod(0o755)
    generate_plugin_pipeline.generate(config, output)
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-ref", default=BASELINE)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        prepare(args.source_ref, args.output)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"example: {error}\n")
    print(f"Prepared example update from {args.source_ref}; no branch was modified.")


if __name__ == "__main__":
    sys.exit(main())
