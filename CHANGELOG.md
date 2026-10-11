# Changelog

## 0.3.0 — cooperative harness, observe first

- Add persistent SQLite runs/tasks/attempts and atomic cross-task path/resource reservations.
- Keep Astra/Luna/Sol roles, six-child ceiling, two-writer admission and explicit dependency gates.
- Add explicit argv verification with time/output bounds, private redacted logs, code fingerprints, stale-evidence detection and separate caller assessments.
- Distinguish worker stop from task completion, and code completion from human/serving-model identity.
- Add manual pause/cancel/reconciliation and bounded retry preparation; no automatic model dispatch or destructive replay.
- Integrate project Codex command hooks without replacing user groups or bypassing trust. Observe default; guarded opt-in after callback receipts.
- Only explicit incomplete completion requests may trigger one Stop continuation per run. Interrupt handler supported but not installed by default due to version variance.
- Incrementally consume registered session response logs using the existing token normalizer. Soft token/time budgets; no billing or inflight hard cap.
- Preserve runtime state/evidence on uninstall and ignore it in Git. Existing inline hook conflicts fail before writes; CLI-only install available.
- Add hook/CLI/installer/concurrency/failure regression tests. No paid model smoke test or guaranteed cost reduction.

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
