# Contributing

Thanks for helping. A few rules keep the project steady.

- **Branches:** work on `feat/*`, `fix/*` or `docs/*`; open a pull request into `develop`. `main` is release-only and is promoted from `develop` after the full test run (unit, end-to-end, browser UI, Docker stack).
- **Tests:** `pip install -e ".[dev]" && playwright install chromium && pytest`. New behaviour needs a test. Bug fixes need a test that failed before the fix.
- **Findings never fail a build.** Ghost reports; it does not gate. Exit code 2 is only for configuration or tool errors.
- **Safety first.** Anything that can write to a target must pass the `allow_writes` and host checks in both the validator and the runner. Credentials stay in environment variables and in memory; never write them to disk or to reports.
- **Cases are data, not code.** Agents emit JSON case files; the runner executes them.
- **Style:** match the surrounding code (small functions, short docstrings that explain why, no commented-out code).

Found a gap by running Ghost on a real project? Add a line to `docs/research/real-world-runs.md` with the project, what happened and the fix.
