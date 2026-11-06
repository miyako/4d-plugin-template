#!/usr/bin/env python3
"""Bounded read-only Actions/release metadata verification; never changes GitHub state.

This is a handoff check, not a replacement for verify_distribution.py's byte and
native signature verification. Downloads and installed smoke tests stay in CI.
"""

import argparse
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import time

sys.dont_write_bytecode = True
import package


class VerificationError(ValueError):
    def __init__(self, message, report=None):
        super().__init__(message)
        self.report = report or {}


def api(endpoint, timeout):
    try:
        response = subprocess.check_output(["gh", "api", "--method", "GET", endpoint],
                                           timeout=max(0.1, timeout), stderr=subprocess.PIPE)
        return json.loads(response, object_pairs_hook=package.unique_object)
    except subprocess.TimeoutExpired as error:
        raise VerificationError("Timeout while reading GitHub metadata") from error
    except subprocess.CalledProcessError as error:
        raise VerificationError(f"Could not read GitHub metadata: {endpoint} (exit {error.returncode})") from error
    except (ValueError, UnicodeError) as error:
        raise VerificationError(f"Invalid GitHub metadata: {endpoint}") from error


def verify_release(repository, run_id, timeout=600):
    package.require(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository),
                    "repository must be OWNER/REPO")
    package.require(type(run_id) is int and run_id > 0, "run-id must be a positive integer")
    package.require(math.isfinite(timeout) and timeout >= 0, "Invalid wait bounds")
    settings, binding = package.config(), package.snapshot()
    start = time.monotonic()
    deadline = start + timeout

    def get(endpoint):
        remaining = deadline - time.monotonic()
        if remaining <= 0 and timeout > 0:
            raise VerificationError("Timeout while verifying release metadata", report)
        # timeout=0 means one immediate inspection, with a bounded per-request timeout.
        try:
            return api(f"repos/{repository}/{endpoint}", min(30, remaining) if timeout else 30)
        except VerificationError as error:
            error.report = report
            raise

    report = {"repository": repository, "run_id": run_id, **binding,
              "metadata_verified": False, "distribution_verified": False}

    def read_run():
        run = get(f"actions/runs/{run_id}")
        package.require(isinstance(run, dict), "Invalid run metadata")
        report["run"] = {key: run.get(key) for key in
                         ("id", "event", "head_branch", "head_sha", "status", "conclusion", "html_url", "path")}
        gates = {
            "run_id": run.get("id") == run_id,
            "manual_dispatch": run.get("event") == "workflow_dispatch",
            "release_branch": run.get("head_branch") == settings["release_branch"],
            "source_commit": run.get("head_sha") == binding["source_commit"],
            "release_workflow": run.get("path") == ".github/workflows/release.yml",
        }
        report["gates"] = gates
        if not all(gates.values()):
            raise VerificationError("Failed run gates: " + ", ".join(k for k, v in gates.items() if not v), report)
        return run

    initial = read_run()
    watch_failure = None
    if initial.get("status") != "completed":
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise VerificationError(f"Timeout waiting for Actions run {run_id}: {initial.get('status')}", report)
        try:
            subprocess.run(["gh", "run", "watch", str(run_id), "--repo", repository, "--exit-status"],
                           timeout=remaining, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except subprocess.TimeoutExpired as error:
            raise VerificationError(f"Timeout waiting for Actions run {run_id}", report) from error
        except subprocess.CalledProcessError as error:
            watch_failure = error.returncode
    run = read_run()
    if watch_failure is not None:
        raise VerificationError(f"Failed Actions watch (exit {watch_failure}): {run.get('conclusion')}", report)
    if run.get("status") != "completed" or run.get("conclusion") != "success":
        raise VerificationError(f"Failed Actions run: {run.get('status')}/{run.get('conclusion')}", report)
    jobs = get(f"actions/runs/{run_id}/jobs?per_page=100")
    package.require(isinstance(jobs, dict) and isinstance(jobs.get("jobs"), list)
                    and all(isinstance(job, dict) for job in jobs["jobs"]), "Invalid jobs metadata")
    job_list = jobs.get("jobs", [])
    package.require(type(jobs.get("total_count")) is int and jobs["total_count"] == len(job_list)
                    and 0 < len(job_list) < 100, "Jobs metadata is missing or exceeds bounded page")
    report["jobs"] = [{"name": job.get("name"), "conclusion": job.get("conclusion")}
                      for job in job_list]
    failed = [job.get("name", "unnamed") for job in job_list
              if job.get("status") != "completed" or job.get("conclusion") != "success"]
    if failed:
        raise VerificationError("Failed job gates: " + ", ".join(failed), report)
    artifacts = get(f"actions/runs/{run_id}/artifacts?per_page=100")
    package.require(isinstance(artifacts, dict) and isinstance(artifacts.get("artifacts"), list)
                    and all(isinstance(item, dict) for item in artifacts["artifacts"]),
                    "Invalid artifacts metadata")
    artifact_list = artifacts.get("artifacts", [])
    package.require(type(artifacts.get("total_count")) is int
                    and artifacts["total_count"] == len(artifact_list) < 100,
                    "Artifacts metadata exceeds bounded page")
    report["artifacts"] = [{"name": item.get("name"), "id": item.get("id"),
                            "expired": item.get("expired"), "size_in_bytes": item.get("size_in_bytes")}
                           for item in artifact_list]
    expected_artifact = f'{settings["product"]}-signed-packages-{binding["source_commit"]}'
    signed = [item for item in artifact_list if item.get("name") == expected_artifact]
    if (len(signed) != 1 or signed[0].get("expired") is not False
            or type(signed[0].get("id")) is not int or signed[0]["id"] <= 0
            or type(signed[0].get("size_in_bytes")) is not int or signed[0]["size_in_bytes"] <= 0):
        raise VerificationError("Failed signed artifact gate: missing, invalid, expired, or empty " + expected_artifact, report)
    tag = "v" + binding["version"]
    release = get(f"releases/tags/{tag}")
    package.require(isinstance(release, dict) and isinstance(release.get("assets"), list)
                    and all(isinstance(asset, dict) for asset in release["assets"]),
                    "Invalid release/assets metadata")
    report["release"] = {key: release.get(key) for key in ("id", "tag_name", "draft", "html_url", "published_at")}
    assets = release.get("assets", [])
    report["assets"] = [{"name": asset.get("name"), "size": asset.get("size"),
                         "digest": asset.get("digest"), "browser_download_url": asset.get("browser_download_url")}
                        for asset in assets]
    expected = {f'{settings["product"]}-{binding["version"]}.{ext}' for ext in ("zip", "dmg")} | {
        "PROVENANCE.json", "zip-notarization.json", "dmg-notarization.json", "SHA256SUMS"}
    names = [asset.get("name") for asset in assets]
    if (release.get("tag_name") != tag or release.get("draft") is not False
            or not release.get("published_at") or len(names) != len(expected) or set(names) != expected
            or any(asset.get("state") != "uploaded" or type(asset.get("size")) is not int
                   or asset["size"] <= 0 for asset in assets)):
        raise VerificationError("Failed published release/assets gates", report)
    commit = get(f"commits/{tag}")
    if not isinstance(commit, dict) or commit.get("sha") != binding["source_commit"]:
        raise VerificationError("Release tag differs from immutable run source", report)
    report.update(metadata_verified=True, handoff="Verify SHA256SUMS and native signatures with verify_distribution.py; metadata alone does not verify downloaded bytes.")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    status = 0
    try:
        report = verify_release(args.repository, args.run_id, args.timeout)
    except (ValueError, OSError) as error:
        status = 1
        report = {**getattr(error, "report", {}), "metadata_verified": False,
                  "distribution_verified": False, "error": str(error)}
    if args.output:
        try:
            package.no_symlink_path(args.output)
            package.require(not args.output.exists(), f"Output already exists: {args.output}")
            package.write_json(args.output, report)
        except (ValueError, OSError) as error:
            status = 1
            report.update(metadata_verified=False, error=str(error))
    print(json.dumps(report, indent=2, sort_keys=True))
    return status


if __name__ == "__main__":
    sys.exit(main())
