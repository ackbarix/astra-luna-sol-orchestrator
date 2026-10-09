# Changelog

## 0.2.0 — audit fixes and evidence-first operation

- Fix array-table TOML scope handling (`[[skills.config]]`); validate all unmanaged
  values before/after merging and validate byte-exact restoration before writing.
- Preserve original settings/permissions, read legacy manifests and repair legacy
  overlays on upgrade. Reject edited managed files, unsafe paths and symlinks.
- Preflight all changes and roll back ordinary write failures. Multi-file power-loss
  atomicity is not guaranteed; update from a clean/stable project state.
- Keep Astra Medium HEAD, Luna Max implementation, Sol 6.1 High hard work and Sol
  6.1 Medium review. Use Luna Medium for exploration/research/basic testing.
  Add explicit Luna Max test-design and read-only Sol High review roles.
- Pin V1 nesting depth to 1. Keep a six-child ceiling, typically 1–4 useful workers
  with 1–2 concurrent writers and explicit shared-resource ownership.
- Report usage per response/model/effort. Never assign legacy cumulative usage to
  the last model, add counters to response totals, or double-add reasoning/cache.
- Follow session lineage across dates. `--date` now selects ROOT start dates and
  retains full sessions; `--day` filters response timestamps in `--timezone`.
- `--latest` includes root-only sessions; `--latest-with-workers` retains the old
  delegated-session selection. Respect CODEX_HOME, report malformed/partial logs,
  support strict reporting, and separate confirmed usage from unknown counters.
- Add read-only runtime/config diagnostics, deliberate live smoke-test instructions,
  session-record verification, and a repeatable quality/time/usage comparison guide.
- No model performance gain, cost-saving percentage, billing equivalence, live
  serving identity or user-machine smoke-test success is claimed by this release.
