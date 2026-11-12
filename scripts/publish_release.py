#!/usr/bin/env python3
"""Publish a new source-bound release only after final and draft byte acceptance."""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

sys.dont_write_bytecode = True
import package
import verify_distribution


class PublicationError(ValueError):
    pass


def command(args):
    return subprocess.check_output(args, cwd=package.ROOT, stderr=subprocess.PIPE)


def api(endpoint, optional=False, **fields):
    args = ["gh", "api", "--include", endpoint]
    if not fields:
        args += ["--method", "GET"]
    for key, value in fields.items():
        args += ["-f", f"{key}={value}"]
    try:
        raw = command(args).decode()
    except subprocess.CalledProcessError as error:
        # gh --include writes the HTTP status to stdout even for error responses.
        status = (error.output or b"").decode(errors="replace")
        if optional and re.match(r"HTTP/\S+\s+404\b", status):
            return None
        raise PublicationError(f"GitHub request failed: {endpoint} (exit {error.returncode})") from error
    _, separator, body = raw.replace("\r\n", "\n").partition("\n\n")
    if not separator:
        raise PublicationError("GitHub response is missing HTTP headers")
    if not re.match(r"HTTP/\S+\s+2[0-9][0-9]\b", raw):
        raise PublicationError("GitHub response has an unexpected HTTP status")
    data = json.loads(body, object_pairs_hook=package.unique_object)
    if not isinstance(data, dict):
        raise PublicationError("GitHub response must be an object")
    return data


def preflight():
    """Read-only authorization and version availability, repeated at publication."""
    settings, binding = package.config(), package.snapshot()
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise PublicationError("GITHUB_REPOSITORY must identify OWNER/REPO")
    if (os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
            or os.environ.get("GITHUB_REF") != "refs/heads/" + settings["release_branch"]
            or os.environ.get("GITHUB_SHA") != binding["source_commit"]):
        raise PublicationError("Publication requires the explicit configured-branch dispatch SHA")
    if not re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", binding["version"]):
        raise PublicationError("Publication requires a numeric semantic VERSION")
    # Confirm default-branch authorization against repository metadata, not a caller's input.
    repository_data = api(f"repos/{repository}")
    if repository_data.get("default_branch") != settings["release_branch"]:
        raise PublicationError("Configured release branch is not the repository default branch")
    tag = "v" + binding["version"]
    if api(f"repos/{repository}/releases/tags/{tag}", optional=True) is not None:
        raise PublicationError("Version already has a release; refusing to replace or reuse it")
    remote_ref = api(f"repos/{repository}/git/ref/tags/{tag}", optional=True)
    if remote_ref is not None:
        if api(f"repos/{repository}/commits/{tag}").get("sha") != binding["source_commit"]:
            raise PublicationError("Existing version tag points to another source commit")
    return repository, binding, tag, remote_ref


def publish(directory, output):
    directory, output = Path(directory).absolute(), Path(output).absolute()
    repository, binding, tag, remote_ref = preflight()
    draft = directory.parent / ".draft-upload-check"
    extracted = directory.parent / ".draft-extracted"
    for path in (draft, extracted):
        package.no_symlink_path(path)
        if path.exists():
            raise PublicationError(f"Draft verification output already exists: {path}")
    verify_distribution.verify_distribution(directory, output)
    if remote_ref is None:
        api(f"repos/{repository}/git/refs", ref=f"refs/tags/{tag}", sha=binding["source_commit"])
    notes = (
        f"Source commit: `{binding['source_commit']}`\n\n"
        "macOS universal: Developer ID signed and notarized. Windows x64: included, "
        "not Authenticode signed. Verify SHA256SUMS before installation."
    )
    files = package.files_in(directory)
    command(["gh", "release", "create", tag, *[str(files[name]) for name in sorted(files)],
             "--repo", repository, "--verify-tag", "--target", binding["source_commit"],
             "--title", tag, "--notes", notes, "--draft"])
    command(["gh", "release", "download", tag, "--repo", repository, "--dir", str(draft)])
    if (directory / "SHA256SUMS").read_bytes() != (draft / "SHA256SUMS").read_bytes():
        raise PublicationError("Draft checksum file differs from the accepted distribution")
    verify_distribution.verify_distribution(draft, extracted)
    if api(f"repos/{repository}/commits/{tag}").get("sha") != binding["source_commit"]:
        raise PublicationError("Version tag changed before publication")
    command(["gh", "release", "edit", tag, "--repo", repository, "--draft=false", "--latest"])
    return {"repository": repository, "tag": tag, **binding, "published": True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preflight", action="store_true", help="Read-only checks before native work")
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.preflight and (args.directory or args.output):
        parser.error("--preflight cannot publish a distribution")
    if not args.preflight and (args.directory is None or args.output is None):
        parser.error("publication requires --directory and --output")
    try:
        if args.preflight:
            repository, binding, tag, _ = preflight()
            result = {"repository": repository, "tag": tag, **binding, "preflight_verified": True}
        else:
            result = publish(args.directory, args.output)
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"publish: {error}\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
