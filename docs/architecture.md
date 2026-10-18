# Architecture

The public repository is a single Python package:

```text
src/argus/
  cli/         command dispatch
  config/      typed settings, profiles, validation
  core/        atomic files, paths, usage, shared utilities
  domain/      deterministic QA rules and finding identity
  pipeline/    extraction, evidence processing, model analysis
  operations/  local control, locks, review, rules, notifications
  profiles/    generic policy examples and prompt assets
  reporting/   JSON/Markdown/HTML rendering and local server
tests/         isolated Python and JavaScript regression tests
docs/          setup, behavior, and migration notes
tools/         maintenance and public-content checks
```

The extractor reads Jira/Zephyr metadata and evidence with a PAT. Evidence is
validated locally before decoding. The pipeline applies deterministic rules,
builds bounded model requests, parses responses, and records findings and usage.
The publisher writes local reports atomically. SQLite and filesystem adapters
hold operator state without a cloud database.

Model invocation goes through `pipeline/model_client.py`. This boundary owns
credential selection, bounded HTTP requests, response normalization, and shared
concurrency limits. Model evidence is untrusted input. Strict response parsing
and incomplete-analysis handling remain part of the audit pipeline.

Tests replace all external clients and block network connections. Distribution
builds use standard Hatchling and contain only the declared source/doc files.
