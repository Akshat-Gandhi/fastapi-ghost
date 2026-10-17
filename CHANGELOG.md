# Changelog

## 0.1.0

First public version.

- Pipeline: discover the OpenAPI document (also at custom paths), scan the source for raise sites with a call graph, build a contract and drift report, generate cases, validate them, run them, explain failures, report.
- Authentication: static headers, token in the URL, or a scripted login (`[login]`).
- Stateful runs: unique values, setup/captures/teardown chains, optional reset hook.
- Web app with accounts, admin user management, encrypted project headers, Docker Compose stack with Postgres.
- Claude Code layer: five subagents, seven skills, and `/ghost-api`.
- Results are labelled in plain language (including *open to everyone* and *inconclusive*).
