# {name}

Tutorial has moved to [example](https://github.com/miyako/4d-plugin-tutorial/tree/example) branch.

Canonical [plugin development guidance](.github/instructions/plugin-dev.instructions.md#required-github-secrets-for-release) includes release-credential setup with the bundled user-run `scripts/set-release-secrets.sh` helper.

Canonical [native-plugin pipeline generation](docs/plugin-pipeline.md) is owned
here on `main`: tracked templates, controlled configuration, shared acceptance
helpers, and deterministic generation checks. Generated plugins use exactly
**Bump version**, **Build and Test**, and **Build and Release**; release is
explicitly dispatched and publishes `vVERSION` only after package verification.
The `example` branch remains the runnable scaffold and is updated separately;
the older `miyako-4d-plugin-tutorial` branch is historical, not a generation source.
