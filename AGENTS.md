# Native plugin workflow source

Read [SKILL.md](SKILL.md) before creating or updating plugin workflows.
It is the canonical entrypoint even when this repository is a nested submodule.
Consumer repositories must explicitly direct their root agent instructions to
the pinned submodule's `SKILL.md`; nested instructions are not auto-discovery.

Keep existing public paths and Git history available for pinned consumers.
Generate workflows from `templates/plugin-pipeline` with
`scripts/generate_plugin_pipeline.py`; never maintain a competing inline recipe.
The release-facing display names are **Bump version** and **Build**.
The separate unsigned **Build and Test** workflow is not a release action.
See [docs/plugin-pipeline.md](docs/plugin-pipeline.md) for all acceptance gates.
