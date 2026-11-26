# Runtime packaging

From the repository root, run:

```powershell
.\dev\package-runtime.ps1
```

The script creates a versioned payload under `dist/` and a matching `.zip`:

```text
runtime/
├─ dcc_skill_runtime/  # Python MCP Runtime source
└─ requirements.txt    # Runtime dependencies
```

This is a standalone Runtime payload, not a complete Host Service/plugin
installer bundle. The script only packages the Runtime; it does not install it
or create its virtual environment. Pass `-OutputDirectory` to choose a new
output directory. The script refuses to overwrite an existing directory or
archive.

The GitHub Actions workflow validates Conventional Commit prefixes. `feat`
increments the minor version, `fix`/`perf`/`revert` increment the patch version,
and `!` or `BREAKING CHANGE:` increments the major version. Documentation,
tests, CI, refactors and chores validate but do not publish a new version. On
push to `main`, the workflow updates `pyproject.toml`, commits the new version,
and adds a `vMAJOR.MINOR.PATCH` tag. Protect `main` and require the CI check so
non-conforming commits cannot be merged.
