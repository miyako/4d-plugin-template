# Native plugin pipeline

This repository owns native-plugin generation instructions and pipeline source.
Installed skills may consume these files, but must not maintain forked workflow
architectures. Component CI/Publish and no-prefix tags are separate conventions.

The reference is [get-icon at `67f6ccb`](https://github.com/miyako/4d-plugin-get-icon/tree/67f6ccbfe0fe8f61480e9a8397b076d19f66e033).
Its workflow files, packaging helpers and acceptance checks were inspected
read-only. The credential setup source is tutorial PR #17, commit `b648aed`.
Reference updates require an explicit contract review, not a floating download
of another repository's workflows.

## Two release actions and separate unsigned checks

| Display name | File | Events and behavior |
|---|---|---|
| Bump version | `bump-version.yml` | Manual default-branch dispatch; `mode` is patch/minor/major, default patch. Validate and commit `VERSION`; no tag or release dispatch. |
| Build and Test | `test.yml` | Push, pull request, manual dispatch, reusable workflow. Build/test both platforms and validate same-run combined-package inputs. |
| Build | `release.yml` | Manual default-branch dispatch, no inputs. Preflight immutable source and version availability, invoke `test.yml`, assemble, sign/notarize, verify, smoke-test, then publish verified draft bytes as `vVERSION`. |

`VERSION` is the only version source. A bump does not start a release. Dispatch
release on the new revision; rerunning an old run still uses its original SHA.
The automatic GitHub token is sufficient: no PAT or tag-event chaining is
required. Repository rules must permit the bump to push to the default branch;
otherwise update `VERSION` through a reviewed PR.

**Bump version** and **Build** are the common release-facing names and roles.
The separate **Build and Test** workflow is safe unsigned validation, not a third
release action. The existing filenames remain stable for consumers.
Plugin tags retain the `v` prefix; component-specific `X.Y.Z` tag rules do not
change plugin tags. Build never accepts a copied version/tag/bump input or
checks out a moving branch instead of its selected immutable dispatch SHA.

The authorization job runs `scripts/publish_release.py --preflight` before
either native job can start. It checks clean source, immutable `HEAD:VERSION`,
SDK gitlink, dispatch SHA, default branch, existing releases and conflicting
tags using read-only API calls. API failures stop the build. A same-SHA orphan
tag is allowed, but an existing release (including a draft) is never reused.
Publication repeats these checks to cover changes since preflight and creates
the tag only after final distribution acceptance. No releases/assets are
overwritten and no tags are moved.

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
preserve unrelated repository rules. The managed attributes keep every generated
file LF, so a Windows checkout with `core.autocrlf` still passes the byte-for-byte
ownership check. The packaging tests run on both test jobs; only the macOS
executable-bit check is skipped on Windows, which cannot store it, and the macOS
assembly, signing and verification jobs still enforce it.

The existing example branch is the executable scaffold. Updating canonical
`main` alone does not update that scaffold. Its subsequent branch-targeted
change must generate these files, add `VERSION` and a `BNDL` Info.plist template,
wire CMake bundle versions to `VERSION`, and preserve greeting test behavior.
Do not claim example synchronization until its actual source and generated
files pass the same checks.

Read [SKILL.md](../SKILL.md) as the agent entrypoint. A consumer using this source
as a submodule must pin a reviewed commit and explicitly point its root
`AGENTS.md` to `<canonical-submodule>/SKILL.md`; do not rely on nested instruction
discovery. Run generation and `--check` from that pinned source. Keep tutorial
code and teaching prose outside generated ownership. Do not reuse a renamed
repository's old public name: pinned submodule URLs may depend on its redirect.

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
and notarized macOS, and explicitly unsigned Windows, with `dependencies: []`.
The only other dependency profile is the
[static third-party profile](#static-third-party-dependency-profile).
Unsupported platforms, signing policies or dependency profiles fail closed.
This is not an unsigned fallback. Adding another profile requires architecture,
dependency closure, license/provenance, packaging and signing acceptance tests;
do not weaken the baseline to accommodate it.

Invariants include immutable dispatch SHA and SDK gitlink, clean checkout,
immutable LF `VERSION`, same-run tested artifacts, portable exact layouts,
matching resource bytes, architecture/dependency checks and immutable licenses.
Reject symlinks, unsafe paths, case collisions, missing/additional/tampered
files and conflicting version tags.

The exact layouts ship one Mach-O and one `.4DX`, so both must load with only
operating-system libraries. macOS binaries may link only `/usr/lib` and
`/System/Library` and have no `LC_RPATH`. Windows binaries are parsed without
running external tools: the 4DX must be AMD64 PE32+, and every entry in its
import and delay-import tables must be a system DLL on the `package.py`
allowlist (for example `KERNEL32`, `USER32`, `GDI32`, `ADVAPI32`, `SHELL32`,
`OLE32`, `OLEAUT32`, `SHLWAPI`, `WS2_32`, `CRYPT32`, `BCRYPT`, and
`BCRYPTPRIMITIVES`, which the Rust standard library imports for `ProcessPrng`) or an
`api-ms-win-*`/`ext-ms-*` API set. Visual C++ runtime DLLs (`VCRUNTIME*`,
`MSVCP*`, `MSVCR*`, `CONCRT*`, `VCOMP*`, MFC and the debug `ucrtbased`) are
rejected explicitly: build with the static CRT (`/MT`). Missing, truncated or
malformed import tables fail closed. Staging, assembly and final inventory all
repeat this check.

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

## Static third-party dependency profile

Use this profile when a plugin links third-party libraries. They must be linked
**statically** into the single Mach-O and the single 4DX, so the exact layouts
and the system-only import checks above still apply. Shipping extra DLLs or
dylibs is not supported. A 4DX that imports a third-party DLL fails staging
instead of failing to load in 4D on users' machines.

```json
"dependencies": {
  "profile": "static",
  "windows": "vcpkg",
  "notices_dir": "THIRD-PARTY-NOTICES",
  "allowed_licenses": ["BSD-3-Clause", "LGPL-2.1-or-later", "MIT", "Zlib"],
  "copyleft_acknowledged": ["LGPL-2.1-or-later"],
  "components": {
    "glib": {"license": "LGPL-2.1-or-later", "notice": "glib.txt", "platforms": ["windows"]},
    "zlib": {"license": "Zlib", "notice": "zlib.txt", "platforms": ["macos", "windows"]}
  }
}
```

- **Components.** Each library linked into either binary is a component. On
  Windows its name is the vcpkg port name. `platforms` is a sorted subset of
  `macos`/`windows`. `vcpkg-*` helper ports are build tools, not components.
- **Licenses.** `license` is an SPDX expression (`AND`, `OR`, `WITH`,
  parentheses). Every license id must be in the sorted `allowed_licenses`;
  `NOASSERTION` is rejected.
- **Copyleft.** Any `GPL-*` or `LGPL-*` id used must be listed in
  `copyleft_acknowledged`, and that list must contain exactly the ids in use.
  Acknowledgment records that the maintainer accepts the obligations: GPL makes
  the whole plugin a covered work. Static LGPL linking requires offering users a
  way to relink modified library versions, by publishing the plugin source and
  its pinned build inputs or the object files. `AGPL-*` is always rejected.
- **Notices.** `notices_dir` holds exactly one committed, non-empty, LF file per
  component, typically the upstream license text plus a copyright notice. Extra,
  missing or empty files fail staging. Assembly copies them to
  `THIRD-PARTY-NOTICES/` in the payload. It also writes `THIRD-PARTY.json`
  recording components, licenses, acknowledgments, static linkage, the
  corresponding-source statement and, on Windows, the resolved vcpkg baseline,
  overrides, triplets and port versions. Final verification compares both
  against the source commit and build provenance.

### Windows: vcpkg (`"windows": "vcpkg"`)

The plugin commits a manifest-mode `vcpkg.json` at the repository root:

```json
{
  "name": "example",
  "dependencies": [{"name": "poppler", "default-features": false, "features": ["cairo", "glib", "zlib"]}],
  "builtin-baseline": "<40-hex vcpkg commit>",
  "overrides": [{"name": "brotli", "version": "1.2.1"}]
}
```

- **Pinning.** `builtin-baseline` must be a 40-character vcpkg commit. CI checks
  out exactly that commit of `microsoft/vcpkg` and resolves ports from its
  builtin registry. Raise the baseline to take routine updates.
- **Overrides.** Use `overrides` with an exact `version` (optional
  `port-version`) to pin a security fix ahead of the baseline, for example a
  CVE fix in a transitive dependency. Remove the override after the baseline
  catches up.
- **Forbidden inputs.** `vcpkg-configuration.json`, custom registries, overlay
  ports and fields other than `$schema`, `name`, `version*`, `port-version`,
  `description`, `homepage`, `license`, `supports`, `dependencies`, `overrides`
  and `builtin-baseline` fail closed.
- **Triplets.** The generator renders `vcpkg-triplets/x64-windows-static-release.cmake`
  (x64, static CRT, static libraries, release only) and the host triplet
  `x64-windows-release-host`. Do not edit them; `--check` detects drift. Many
  ports' installed headers already select static linkage (for example glib's
  `GLIB_STATIC_COMPILATION`). When a library does not, define its `*_STATIC`
  macro in CMake (see the plugin development guide).
- **CI.** The generated **Build and Test** Windows job:
  1. resolves the baseline with `scripts/package.py vcpkg-baseline`;
  2. restores the vcpkg binary cache with `actions/cache`, keyed by baseline and
     the hash of `vcpkg.json` and triplets;
  3. bootstraps vcpkg in `RUNNER_TEMP` and installs into `<build_dir>/vcpkg_installed`;
  4. saves the cache on a miss, including after a failed install, so finished
     packages are kept;
  5. configures CMake with the vcpkg toolchain and manifest install disabled.

  A cold build of a large closure can take hours (poppler with cairo and glib
  took about 2.5 hours, mostly MSYS tools), so the test job allows 360 minutes.
  Warm runs reuse the cache.
- **Closure.** Staging reads the installed vcpkg metadata. The set of
  target-triplet ports, excluding `vcpkg-*` helpers, must equal the declared
  Windows components. Each port's SPDX license must match its declared license
  unless vcpkg reports `NOASSERTION`. The closure and port versions are bound
  into the Windows input manifest and `THIRD-PARTY.json`. The PE import check
  independently proves that nothing is linked dynamically.

### macOS and `"windows": "none"`

The pipeline does not build macOS dependencies. Build them statically from the
plugin's own CMake, for example from pinned submodules as universal
`arm64;x86_64` static libraries. Homebrew dylibs are single-architecture and
dynamically linked, so they fail the closure check. The pipeline enforces the
system-only Mach-O closure and the notices. With `"windows": "none"`, Windows
components must likewise come from the plugin's CMake, and no vcpkg steps are
generated.

The smallest working case is
[4d-plugin-md4c](https://github.com/miyako/4d-plugin-md4c): one pinned submodule
(`md4c-src`), three C files compiled into a static target by the plugin's CMake on both
platforms, and one notice file:

```json
"dependencies": {
  "profile": "static",
  "windows": "none",
  "notices_dir": "THIRD-PARTY-NOTICES",
  "allowed_licenses": ["MIT"],
  "copyleft_acknowledged": [],
  "components": {
    "md4c": {"license": "MIT", "notice": "md4c.txt", "platforms": ["macos", "windows"]}
  }
}
```

A library whose linked files carry more than one license declares the conjunction and
lists every id: [4d-plugin-cmark-gfm](https://github.com/miyako/4d-plugin-cmark-gfm)
declares `"BSD-2-Clause AND MIT"` with `"allowed_licenses": ["BSD-2-Clause", "MIT"]`.
Count only what is linked: cmark-gfm's CC-BY-SA specification text ships in the
repository but not in the binary. The notice file can be the upstream `COPYING` with a
first line naming the pinned version and URL.

The baseline workflows are unchanged for `dependencies: []`.

### Private build-time dependency discovery

An imported target pointing at an `ExternalProject` byproduct does not necessarily
satisfy a downstream `find_package`. During a fresh Windows configure,
FindOpenSSL re-searched future, unbuilt static-library paths and overwrote output
variables; Boost.Redis returned early with an incomplete interface. Appending to
an unchecked `get_target_property` result then leaked
`_redis_dependencies-NOTFOUND.lib` to the linker
([failure run](https://github.com/miyako/4d-plugin-redis/actions/runs/43025019922)).

For private build dependencies, provide a project-owned package config or
dependency provider scoped to the consuming subproject. It must describe the
real pinned targets, version, components, static link closure and build ordering;
do not merely set `FOUND` to bypass a required dependency. Prevent accidental
system discovery/fallback. An existing imported target and a future library path
are not evidence that a package finder will accept them.

Before reading/modifying a dependency list, assert that the required targets
exist, the required property is set and nonempty (where required by that
library), and no value is `NOTFOUND` / ends in `-NOTFOUND`. Validate the expected
dependency interface, not just target existence; do not append to an unchecked
sentinel or silently replace an incomplete interface with an empty list.

The pinned [private OpenSSL config](https://github.com/miyako/4d-plugin-redis/blob/v0.1.2/redis/cmake/OpenSSLConfig.cmake.in),
[scoped consumer](https://github.com/miyako/4d-plugin-redis/blob/v0.1.2/redis/CMakeLists.txt)
and [configuration regressions](https://github.com/miyako/4d-plugin-redis/blob/v0.1.2/scripts/tests/test_cmake_dependencies.py)
are a concrete example, not new generator inputs or universal OpenSSL/Boost
template logic. For projects owning such dependencies, configure a fresh build
with no archives present and a deliberately failing system finder; the private
package must still resolve. Then supply a disabled/incomplete consumer interface
and require an actionable **configuration** failure, not a late linker failure.
Use forward-slash Windows `-D` paths, including paths forwarded to `try_compile`.
This repository's current overlay uses only the SDK/CoreGraphics, so dependency
regressions belong with the dependency-owning implementation, not a fake Boost
interface added to the baseline.

## Qualification and evidence

Keep a per-project matrix with source SHA, run/log link, exact host version/build,
OS/architecture, backend version/configuration, result and remaining gates. A
build target or minimum deployment setting is not runtime qualification.

| Layer | Establishes | Does not establish |
|---|---|---|
| Native units | Exercised C/C++ behavior, bounds and injected failure paths | Manifest tokens, SDK ownership or scheduler behavior |
| Portable/fake backend fixtures | Exact wire bytes, deadlines, TLS/failure outcomes and invalidation under controlled peers | Actual live-backend semantics or hosted-provider trust/routing |
| Offline tool4d | Exercised manifest/SDK/validation paths on the named runtime | Unexecuted live operations, CAS, BLOB round trips or worker cleanup |
| Live-backend host acceptance | Actual typed host calls, backend semantics and lifecycle paths explicitly asserted | Other runtimes, backends, termination modes or preemptive safety |
| Architecture/platform execution | Named binary executed on the named OS/host | Windows compilation is not Windows host qualification; Rosetta native tests are not Intel-host or x86_64 4D acceptance |
| Packaged-host smoke | Extracted package loads and its exercised host tests pass | Live integration if smoke is offline; production signing if seal is ad-hoc |
| Production acceptance | Verified Developer ID/runtime/timestamp, Accepted ZIP/DMG notarization, stapling/Gatekeeper and final bytes | Windows Authenticode or every end-user installation |

For CAS, typed BLOB preservation/clearing, worker ownership/termination or similar
claims, name the actual relevant host assertions; native-only evidence is not a
substitute. Keep the existing exact-`PASS` plus zero-exit harness and prove a
temporary negative assertion fails in the actual host, not only in a simulated
runner. The [development guide](../.github/instructions/plugin-dev.instructions.md#exit-behavior)
describes assertion/error-handler behavior.

For example, Redis's
[refreshed qualification record](https://github.com/miyako/4d-plugin-redis/blob/7775bbf1441f8ca4a167508f860f5b218fa4a572/docs/validation.md)
distinguishes actual authenticated Redis 7.2.9 + arm64 tool4d 21.0 text/BLOB/worker
acceptance from Windows native/fixtures/offline-host CI and Rosetta native runs.
Its Windows live-backend typed-host acceptance and Intel/x86_64 4D acceptance
remain open even after production macOS package acceptance.

## Credentials

Use the [canonical user-run credential instructions](../.github/instructions/plugin-dev.instructions.md#required-github-secrets-for-release).
They define the same six secrets used by the reference signing job and preserve
the `4d-plugin-release` local service. Never execute the credential helper as an
agent, read values, or modify installed skills as part of project generation.

## Offline checks and release handoff

The generation tests parse workflows with PyYAML (`requirements-dev.txt`); install
it first, for example in a virtual environment. `actionlint` is optional: its test
is skipped when the command is not installed.

```sh
python3 -m pip install -r requirements-dev.txt
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

After dispatching **Build**, check that the run's `headSha` is the commit
you meant to release before waiting on it. A dispatch sent right after `git push` can
resolve the branch to the previous commit:

```sh
gh run list --repo OWNER/REPO --workflow release.yml --limit 1 --json databaseId,headSha
git rev-parse HEAD
```

If they differ, cancel that run (`gh run cancel RUN_ID --repo OWNER/REPO`) and dispatch
again. The wrong run would otherwise test, sign and publish the previous revision.

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

### Post-release documentation handoff

After an authorized production release succeeds, refresh download/install
guidance, directly related translations and qualification evidence in the same
documentation change. Remove stale permanent "no release published" notices,
but retain still-unqualified cases. This checklist is not publication permission
and adds no release action beyond **Bump version / Build**; unsigned checks stay separate.

1. Verify the public release's tag/source, `draft:false`, `prerelease` status and
   actual assets. A successful draft-creation step is not public publication.
   Do not call a prerelease stable or infer publication from a green build.
2. Verify actual archive names and extracted bundle layout before writing
   installation instructions. Retain checksums, source/run provenance and
   final-byte verification; metadata checks alone do not accept archive contents.
3. Verify signature acceptance and the ZIP/DMG notarization receipts with
   `status: Accepted`, plus the required DMG stapling/Gatekeeper and packaged-host
   smoke results. Configured credentials are not completed signing; ad-hoc test
   sealing is not Developer ID production signing. macOS bundle signing does not
   sign the Windows 4DX: state Windows Authenticode status separately.
4. Record the released version, immutable source/run, exercised configurations
   and remaining gates; synchronize translated installation/qualification text.
   A production-signed offline smoke still does not qualify live Windows calls.

The [Redis 0.1.2 public release](https://github.com/miyako/4d-plugin-redis/releases/tag/v0.1.2)
illustrates the distinction: its
[production run](https://github.com/miyako/4d-plugin-redis/actions/runs/43036770788)
at `3e08ea357da57c85155066366b159cf692e8b2a3` succeeded, and publication was
separately confirmed (`draft:false`, `prerelease:false`, published
`2026-11-11T14:38:10Z`). Assets include `redis-0.1.2.zip`, `redis-0.1.2.dmg`,
`SHA256SUMS`, `PROVENANCE.json`, `zip-notarization.json` and
`dmg-notarization.json`; both receipts report `Accepted`. The combined
`redis.bundle` contains `Contents/MacOS/redis` and
`Contents/Windows64/redis.4DX`. Its
[documentation refresh](https://github.com/miyako/4d-plugin-redis/commit/7775bbf1441f8ca4a167508f860f5b218fa4a572)
replaced stale pre-release claims while keeping the unsigned Windows executable
and live-Windows-host qualification gap explicit. This historical evidence is
not a blanket claim about future releases.
