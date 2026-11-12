# 4D plugin pipeline and development guide

English | [日本語](README.ja.md)

Start with the canonical [agent skill](SKILL.md) and
[pipeline generation guide](docs/plugin-pipeline.md).
The historical [example](https://github.com/miyako/4d-plugin-tutorial/tree/example)
branch is not synchronized with this contract; do not copy its release recipes.

Canonical [plugin development guidance](.github/instructions/plugin-dev.instructions.md#required-github-secrets-for-release) includes release-credential setup with the bundled user-run `scripts/set-release-secrets.sh` helper.

Canonical [native-plugin pipeline generation](docs/plugin-pipeline.md) is owned
here on `main`: tracked templates, controlled configuration, shared acceptance
helpers, and deterministic generation checks. The two release-facing actions are
**Bump version** (source-only) and **Build** (no version/tag input).
The separate **Build and Test** workflow provides unsigned checks.
Build preflights the immutable dispatch revision and version availability before
native work, and publishes `vVERSION` only after package verification.
The `example` branch remains the runnable scaffold and is updated separately;
the older `miyako-4d-plugin-tutorial` branch is historical, not a generation source.

The **Canonical checks** workflow tests/lints this source repository without
building, signing or publishing plugins.
