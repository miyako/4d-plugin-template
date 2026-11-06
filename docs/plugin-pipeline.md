# Native plugin pipeline

This repository owns native-plugin generation instructions and pipeline source.
Installed skills may consume these files, but must not maintain forked workflow
architectures. Component CI/Publish and no-prefix tags are separate conventions.

The reference is [get-icon at `67f6ccb`](https://github.com/miyako/4d-plugin-get-icon/tree/67f6ccbfe0fe8f61480e9a8397b076d19f66e033).
Its workflow files, packaging helpers and acceptance checks were inspected
read-only. The credential setup source is tutorial PR #17, commit `b648aed`.
Reference updates require an explicit contract review, not a floating download
of another repository's workflows.

## Three workflows

| Display name | File | Events and behavior |
|---|---|---|
| Bump version | `bump-version.yml` | Manual default-branch dispatch; `mode` is patch/minor/major, default patch. Validate and commit `VERSION`; no tag or release dispatch. |
| Build and Test | `test.yml` | Push, pull request, manual dispatch, reusable workflow. Build/test both platforms and validate same-run combined-package inputs. |
| Build and Release | `release.yml` | Manual default-branch dispatch, no inputs. Invoke `test.yml`, assemble, sign/notarize, verify, smoke-test, then publish verified draft bytes as `vVERSION`. |

`VERSION` is the only version source. A bump does not start a release. Dispatch
release on the new revision; rerunning an old run still uses its original SHA.
The automatic GitHub token is sufficient: no PAT or tag-event chaining is
required. Repository rules must permit the bump to push to the default branch;
otherwise update `VERSION` through a reviewed PR.

Normal builds have read-only contents permission and no signing secrets.
Signing runs in the configured release environment; within the release workflow,
only publication receives write permission. The separate bump workflow needs
write permission to commit `VERSION`. Configure default-branch deployment
restrictions and reviewers as appropriate. Credentials are not authorization
to publish.

## Generate, do not reinvent

The inert templates are in `templates/plugin-pipeline`; `main` does not run
these as its own workflows. Start with `example.json`, edit only supported
configuration fields, and generate into the target plugin repository:

```sh
python3 scripts/generate_plugin_pipeline.py \
  --config templates/plugin-pipeline/example.json \
  --output /absolute/path/to/plugin-repository
python3 scripts/generate_plugin_pipeline.py \
  --config templates/plugin-pipeline/example.json \
  --output /absolute/path/to/plugin-repository --check
```

The generated repository contains `.github/plugin-pipeline.json`, exactly the
three workflow files, the parameterized tool4d action, and required tracked
helpers/tests. Generation is deterministic and refuses conflicting outputs;
`--check` detects drift without writing. Do not patch generated workflow logic
per project; change canonical source/configuration and validate it first.

For an owned pipeline, `--update-managed` replaces only files whose current
bytes still match `.github/plugin-pipeline-generated.json`; hand-edited or
missing managed files fail instead of being overwritten. The separately
authorized `--replace-legacy-workflows` migration replaces only the three
canonical workflow filenames, not unrelated workflows or helper files. Do not
use migration mode to bypass ownership checks. Managed ignore and LF attributes
preserve unrelated repository rules.

The existing example branch is the executable scaffold. Updating canonical
`main` alone does not update that scaffold. Its subsequent branch-targeted
change must generate these files, add `VERSION` and a `BNDL` Info.plist template,
wire CMake bundle versions to `VERSION`, and preserve greeting test behavior.
Do not claim example synchronization until its actual source and generated
files pass the same checks.

The source-level migration overlay is tracked in `templates/example-overlay`,
not merely described in this guide. Prepare the separate example delivery
offline from its reviewed local commit:

```sh
python3 scripts/prepare_example_update.py \
  --source-ref f8e20f052407c3e74c33192884dc82b31a2bd27a \
  --output /absolute/path/to/new-example-update
```

This preserves the real greeting implementation, manifest/constants and 4D
test methods, replaces CMake with VERSION/BNDL wiring, and adds the canonical
pipeline. It never changes a branch or an existing directory. Refresh the
remote example revision before accepting this separate delivery; preparing an
overlay is not proof that the remote example branch has been updated.

## Configuration and acceptance

Configuration controls product/identifier, source/build/test paths, SDK path,
startup method, optional real native tests, deployment target, release branch
and signing environment. Values are validated before rendering; arbitrary shell
commands and GitHub expressions are not configuration.

The supported baseline is universal macOS plus Windows x64, Developer ID signed
and notarized macOS, and explicitly unsigned Windows. Unsupported platforms,
signing policies or third-party dependency profiles fail closed. This is not an
unsigned fallback. Adding another profile requires architecture, dependency
closure, license/provenance, packaging and signing acceptance tests; do not
weaken the baseline to accommodate it.

Invariants include immutable dispatch SHA and SDK gitlink, clean checkout,
immutable LF `VERSION`, same-run tested artifacts, portable exact layouts,
matching resource bytes, architecture/dependency checks and immutable licenses.
Reject symlinks, unsafe paths, case collisions, missing/additional/tampered
files and conflicting version tags.

Production signing registers the temporary keychain in the user search list
before use, restores that list and deletes temporary certificate/keychain files
on exit/signals. Cleanup failures are errors. It requires a unique Developer ID
Application identity for the configured team, secure timestamp and hardened
runtime; there is no ad-hoc production fallback.

Preserve strict bundle/Mach-O/DMG verification and signature metadata checks.
Require explicit Accepted notarization for ZIP and separately signed DMG;
staple and validate the DMG and assess it with Gatekeeper. Inventory the final
signed payload and checksum final archives after stapling. Verify extracted ZIP
bytes and run the actual packaged plugin through tool4d. Verify again after
artifact download, then compare downloaded draft upload bytes and recheck the
tag's SHA before publishing. Separate trust-boundary checks are not redundant
polling and must not be removed.

The ZIP cannot carry a stapled ticket; the DMG is the stapled download. Normal
test-only ad-hoc seals validate layout but are never uploaded as distributions.
Reference package tooling uses Python 3.11+ and standard-library APIs.

The portable ZIP deliberately excludes resource-fork/extended-attribute
sidecars; required signed regular files remain in the exact inventory. This
allows fail-closed extraction without treating arbitrary `__MACOSX` files as
accepted payload. The DMG retains the normal macOS container acceptance path.

## Credentials

Use the [canonical user-run credential instructions](../.github/instructions/plugin-dev.instructions.md#required-github-secrets-for-release).
They define the same six secrets used by the reference signing job and preserve
the `4d-plugin-release` local service. Never execute the credential helper as an
agent, read values, or modify installed skills as part of project generation.

## Offline checks and release handoff

```sh
python3 -m unittest discover -s scripts/tests -p 'test_*.py'
zsh -n scripts/set-release-secrets.sh
bash -n scripts/sign-package.sh
```

Validate generated fixtures with `--check` and actionlint on all three generated
workflow files. Offline tests must cover structural events/inputs/job edges,
permissions, revision/artifact wiring, deterministic rendering, unsafe config,
unsupported profiles and workflow drift. Package/signing/publication tests use
isolated synthetic fixtures and mocked external commands, never real credentials
or Actions. Static checks are not proof of live Apple or GitHub acceptance.

An approved release handoff records repository, source SHA, VERSION, run ID/URL,
artifact names/checksums, acceptance and the failing gate when incomplete. Reuse
that run ID, wait with a deadline, then retrieve consolidated results; do not
poll repeatedly, invent status commands or automatically merge/retry/release.
The read-only release verification helper never dispatches or publishes.

From the clean generated plugin checkout at the release's source revision:

```sh
python3 scripts/verify_release.py --repository OWNER/REPO --run-id RUN_ID \
  --timeout 600 --output /absolute/path/to/new-handoff.json
```

This checks run/release metadata, not downloaded archive acceptance; that
distinction is explicit in its report. Production `publish_release.py` verifies
the final distribution, refuses an existing release or conflicting tag, creates
a draft, downloads and checks its bytes, rechecks the tag SHA and only then
publishes. Do not run that publication helper without separate authorization.
