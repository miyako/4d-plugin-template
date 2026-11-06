#!/usr/bin/env python3
"""Fail-closed Developer ID checks (MIT reference attribution in REFERENCE.txt)."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

sys.dont_write_bytecode = True
import package


def keychain_search_list(snapshot, keychain=None, capture=False):
    package.no_symlink_path(snapshot)
    if capture:
        package.require(not snapshot.exists(), "Keychain snapshot already exists")
        original = subprocess.check_output(["security", "list-keychains", "-d", "user"], text=True)
        snapshot.write_text(original, encoding="utf-8")
        return
    paths = shlex.split(snapshot.read_text(encoding="utf-8"))
    if keychain is not None:
        paths = [str(keychain), *[p for p in paths if p != str(keychain)]]
    subprocess.run(["security", "list-keychains", "-d", "user", "-s", *paths], check=True)


def signature(details, team, runtime=False):
    package.require(isinstance(team, str) and re.fullmatch(r"[A-Z0-9]{10}", team),
                    "Expected a 10-character Apple team ID")
    lines = details.splitlines()
    package.require([line for line in lines if line.startswith("TeamIdentifier=")]
                    == [f"TeamIdentifier={team}"], "Signing team does not match expected team")
    authorities = [line for line in lines if line.startswith("Authority=")]
    package.require(len(authorities) >= 3
                    and authorities[0].startswith("Authority=Developer ID Application: ")
                    and authorities[0].endswith(f"({team})")
                    and authorities[1:] == ["Authority=Developer ID Certification Authority",
                                            "Authority=Apple Root CA"],
                    "A Developer ID Application Apple certificate chain is required")
    timestamps = [line[10:].strip() for line in lines if line.startswith("Timestamp=")]
    package.require(len(timestamps) == 1 and timestamps[0].lower() not in ("", "none"),
                    "A secure signing timestamp is required")
    package.require(not any("adhoc" in line.lower() or line.startswith("Signed Time=") for line in lines),
                    "Ad-hoc signatures and local signing times are not distribution signatures")
    if runtime:
        flags = [re.search(r"\bflags=0x([0-9a-fA-F]+)\(([^)]*)\)", line) for line in lines]
        package.require(any(match and int(match[1], 16) & 0x10000
                            and "runtime" in match[2].split(",") for match in flags),
                        "The plugin must use the hardened runtime")


def notarization(report):
    package.require(isinstance(report, dict) and report.get("status") == "Accepted"
                    and isinstance(report.get("id"), str) and bool(report["id"].strip()),
                    f"Notarization was not Accepted: {report.get('status') if isinstance(report, dict) else 'invalid report'}")


def checksums(directory):
    files = package.files_in(directory)
    package.require(files and all("/" not in name for name in files),
                    "Distribution must contain regular artifact files only")
    package.exact_directories(directory, files)
    lines = [f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {name}\n"
             for name, path in files.items() if name != "SHA256SUMS"]
    package.require(lines, "Empty distribution")
    (directory / "SHA256SUMS").write_text("".join(lines), encoding="utf-8", newline="\n")


def authorize():
    settings = package.config()
    package.require(os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
                    and os.environ.get("GITHUB_REF") == f'refs/heads/{settings["release_branch"]}',
                    f'Production signing requires an explicit dispatch from {settings["release_branch"]}.')
    binding = package.snapshot()
    package.require(os.environ.get("GITHUB_SHA") == binding["source_commit"],
                    "Checkout must equal immutable dispatch GITHUB_SHA")
    package.require(sys.platform == "darwin", "Developer ID signing requires macOS")
    names = ("APPLE_DEVELOPER_ID_CERTIFICATE", "APPLE_DEVELOPER_ID_CERTIFICATE_PASSWORD",
             "KEYCHAIN_PASSWORD", "NOTARYTOOL_APPLE_ID", "NOTARYTOOL_TEAM_ID", "NOTARYTOOL_PASSWORD")
    missing = [name for name in names if not os.environ.get(name, "").strip()]
    package.require(not missing, "Required signing secrets are missing: " + ", ".join(missing))
    package.require(re.fullmatch(r"[A-Z0-9]{10}", os.environ["NOTARYTOOL_TEAM_ID"]),
                    "NOTARYTOOL_TEAM_ID must be a 10-character Apple team ID")
    return (f'{settings["product"]}\t{settings["release_branch"]}\t{binding["version"]}'
            f'\t{settings["build_dir"]}')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("signature")
    command.add_argument("--details", type=Path, required=True)
    command.add_argument("--team", required=True)
    command.add_argument("--runtime", action="store_true")
    command = commands.add_parser("notarization")
    command.add_argument("--report", type=Path, required=True)
    command = commands.add_parser("checksums")
    command.add_argument("--directory", type=Path, required=True)
    command = commands.add_parser("keychain-search-list")
    command.add_argument("--snapshot", type=Path, required=True)
    group = command.add_mutually_exclusive_group()
    group.add_argument("--add", type=Path)
    group.add_argument("--capture", action="store_true")
    commands.add_parser("authorize")
    args = parser.parse_args(argv)
    try:
        if args.command == "signature":
            signature(args.details.read_text(encoding="utf-8"), args.team, args.runtime)
        elif args.command == "notarization":
            notarization(package.read_json(args.report))
        elif args.command == "checksums":
            checksums(args.directory)
        elif args.command == "authorize":
            print(authorize())
        else:
            keychain_search_list(args.snapshot, args.add, args.capture)
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"release-checks: {error}\n")


if __name__ == "__main__":
    main()
