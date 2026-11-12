---
name: native-plugin-pipeline
description: Create or update native 4D plugin workflows from pinned canonical templates, preserving source-only version bumps and immutable-source release acceptance.
---

# Native 4D plugin pipeline

Use this skill when creating or updating native-plugin build, version or release
workflows. Read [the pipeline contract](docs/plugin-pipeline.md) and
[development guide](.github/instructions/plugin-dev.instructions.md).
These tracked sources are canonical, not a floating copy of another plugin.

## Release UX

- **Bump version** selects patch/minor/major and commits source `VERSION` only.
  It never tags, builds, releases or dispatches another workflow.
- **Build** has no version, tag or bump input. It uses the immutable dispatch SHA
  on the configured default branch, preflights checked-in source/version and
  existing release/tag conflicts before native work, then tests, signs,
  notarizes and validates the final distribution and installed plugin.
- Only successful acceptance permits publication of `vVERSION` at the exact
  built SHA. Never overwrite releases/assets or move a tag. A same-SHA tag
  without a release may be reused. Tag pushes never publish automatically.
- **Build and Test** is a separate unsigned validation workflow, not a third
  release-facing action. Never turn its ad-hoc seals into production fallback.

The shared naming and roles apply to components too, but this skill implements
native plugins only. Plugin tags are `vX.Y.Z`; component tag/version conventions
are separate. Do not copy component publication logic into plugin workflows.

## Generate and verify

1. Use a reviewed immutable canonical revision, initializing its submodules.
   Edit a consumer-owned copy of `templates/plugin-pipeline/example.json`.
2. Run `python3 <canonical>/scripts/generate_plugin_pipeline.py --config
   <consumer-config> --output <consumer>` and repeat with `--check`.
   Keep generated workflows/helpers at their normal executable paths.
3. Use `--update-managed` only for bytes still owned by the prior manifest.
   Never hand-edit generated files or bypass drift detection.
4. Run the relevant tests in `scripts/tests`; validate rendered workflows with
   actionlint. Keep product code and explanations outside generated ownership.
5. Publication requires separate explicit authorization. Do not dispatch
   signing/release workflows, merge PRs, or change credentials as setup.

For consumers using this repository as a submodule, root `AGENTS.md` must name
the actual submodule path and instruct agents to read its `SKILL.md`. Run drift
checks using that exact pin, not a downloaded main branch. Preserve historical
Git objects, refs and public URLs; a repository rename/name-reuse is a separate
migration requiring a compatibility plan for pinned consumers.
