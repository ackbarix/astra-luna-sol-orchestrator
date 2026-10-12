# Changelog

## 1.0.0 — Harness v2 for GPT

ALS now follows [revfactory/harness](https://github.com/revfactory/harness) v2; the only difference is
that GPT models run through Codex.

- New skills `harness` (team factory, phases 0-7, 9 references) and `harness-evolve`, ported from
  Harness v2 (Apache-2.0, see `harness/NOTICE.md`). They generate `.codex/agents/*.toml`,
  `.agents/skills/*/SKILL.md`, an orchestrator skill and an `AGENTS.md` pointer.
- Model tiers: fable → gpt-6-astra, opus → gpt-6.1-sol, sonnet → gpt-6-luna (always max).
- New runtime for the Claude-Code-only execution modes: `als-flow` (Workflow scripts in Python:
  `agent()`, `pipeline()`, `parallel()`, `phase()`, JSON schema results, resume, budget, call cap),
  `als-agent` (persistent named agents via `codex exec resume`) and `als-task` (shared task list).
  Verified with real GPT calls (schema results, remembered context).
- Verification: GPT verifies once and stops by default; the Harness quality patterns
  (adversarial verification, judge panel, loop-until-dry) are used only when correctness is
  critical or a thorough sweep is requested, as in the original.
- Every Luna role pinned to max; `als run` connects to the GPT endpoint directly (no OpenCodex).
- The installer ships all of the above (global and project mode).

## 0.5.0 — global install, `als` command, recovery and state-engine fixes

### Install and use
- `sh setup.sh` from a clone installs globally and puts `als` in `~/.local/bin`; it prints
  the PATH line and never edits shell rc files or `$CODEX_HOME/config.toml`.
- Global layout: harness in `${ALS_HOME:-~/.local/share/als}`, roles in
  `$CODEX_HOME/agents/als_*.toml`, skill in `$CODEX_HOME/skills/astra-luna-sol/`, model
  defaults in `$CODEX_HOME/als.config.toml` (`codex --profile als` / `als run`).
- Per-project hooks are opt-in: `cd PROJECT && als install --hooks-only`. Project mode
  (`als install PROJECT` / `sh install.sh PROJECT`) is unchanged in purpose.
- Hook commands are relocatable (no absolute project path) and PreToolUse/PostToolUse
  match only `spawn_agent`.
- New `als` subcommands: `install`, `uninstall`, `doctor`, `run`, `plan new`, `usage`,
  `version`, and passthrough of every harness command for the current repository.
- Global-scope state lives in `${XDG_STATE_HOME:-~/.local/state}/als/<hash>/`, keeping
  repositories clean.

### Dependencies
- Runtime floor is Python 3.9. The installer needs 3.11+ (or `tomli`) only to merge a
  project `config.toml`; `--global` needs no TOML parser.
- No `git --path-format`, so older git works.
- `als doctor` checks python, git, sqlite3, codex and its `hooks`/`multi_agent` features,
  optional `ocx`, PATH, and project/global conflicts; text by default, `--json` for JSON.
- If `python3` on PATH is missing or older than 3.9, hooks record the installer's absolute
  interpreter.

### Recovery
- `--uninstall` succeeds when managed files are already missing; `--force-uninstall`
  backs up drifted files to `backup-<UTC>/` first; `--adopt` reclaims manifest-less
  byte-identical files; `--policy-file PATH` chooses the policy file.
- `--check` exit codes: 0 ok, 1 not installed, 2 drift/refused, 4 outdated.
- `hooks.json` is restored byte-exact on uninstall/rollback.

### State engine
- `cancel` releases claims; stale or killed verification locks are detected and cleared,
  and a check process group is killed only when it is still our exact process.
- `resume` keeps finished (`awaiting_validation`) work and refreshes `last_snapshot`, so
  `--accept-drift` is rarely needed; `retry` clears the previous result.
- Completed runs never flip back to running/blocked.
- Nested repositories, gitlinks and non-UTF-8 file names no longer break snapshots; claim
  release no longer needs a snapshot.
- Task-level checks may run while other runs are active, provided this run holds no write
  claim. `.codex` is a reserved path. Redaction covers more secret shapes.

### Hooks and usage
- Child-origin spawns are denied (guarded) or warned (observe); forked-worktree child
  events are kept; deny reasons are never empty; unchanged receipts are not rewritten;
  `bind` failures are surfaced.
- Oversized rollout lines are skipped with a warning; unbound threads keep their usage;
  session_meta is stored minimally; truncation is detected; the sessions base is stored.
- After a SessionEnd pause the SessionStart context prints the exact resume command.
- `start --plan -` reads stdin; `--session` is auto-detected from exactly one recent
  SessionStart receipt; `status --format text`.

### Policy and reporters
- `token_usage.py`: lineage-scoped warnings with unrelated files in `index_warnings`,
  robust identity coercion, `--day` skips threads idle on that day, `--cwd` includes
  subdirectories, new `--include-archived`.
- Profile caps are real caps; exact duplicate signatures include checks, constraints,
  risk, complexity and role.
- History calibration counts failures as failures and uses counted tokens.
- `check-runtime.py` judges only the selected lineage and has a minimal TOML fallback.

### Profiles and CI
- `als_test_designer` and `als_reviewer_deep` rewritten with their own mandates; read-only
  is stated as an instruction (subagents inherit the parent sandbox); shorter AGENTS snippet.
- CI: ubuntu/macOS × Python 3.11/3.14, a Python 3.9 runtime-only job, dash/sh/bash syntax
  checks and install/setup end-to-end tests.

### Breaking or behaviour changes
- `setup.sh`/`--global` is the recommended install; a 0.2–0.4 project install alongside a
  global one shadows the global roles (`als doctor` warns: `conflict:agent-roles`).
- Soft token budgets and history estimates count **uncached input + output** (cached input
  no longer counts), so the same budget admits more work than in 0.4.
- Run reports are `schema_version` 2; `analyze --report-file` accepts 1 or 2 (with `workers`).
- Read-only kinds (exploration, research, review) never route to write-capable roles;
  complex/high-risk ones get a `recommend_escalation` note instead.
- Caller `efficiency.max_*` values can only lower profile caps; `validate` rejects edited
  prepared plans above them.
- Plans that contain runtime-only task keys are rejected; `.codex/...` cannot be a write path.
- Child agents may not spawn agents (denied in guarded mode).
- Hook PreToolUse/PostToolUse groups now match only `spawn_agent`.

### Round-3 runtime fixes
- Runs started inside a linked `git worktree` record `run.worktree`; snapshots, `verify`
  (cwd) and evidence freshness use it, state stays keyed by the main project, and hook events
  from any worktree of the same repository are handled.
- HEAD rollout cursors start at the last complete line at `start`, `resume --session` and
  SessionStart of a session with a run; the per-call sync budget rotates least-recently-synced
  first.
- `start` without `--session` picks a session only when exactly one had any hook event in
  the last 10 minutes, always warns with its id, and never auto-picks in guarded mode.
- `doctor` reads the project `.codex/hooks.json` then `$CODEX_HOME/hooks.json` in both scopes,
  prints optional items as `warn`, exits 1 only when a required dependency fails, and accepts
  `--no-probe`.
- State DB uses WAL with 5 s (CLI) / 2 s (hooks) busy timeouts; a relative `ALS_STATE_HOME` is
  an error and a relative `XDG_STATE_HOME` is ignored.
- `verify` kills the check group before reaping its leader; 0.4 locks without a process start
  time expire after the check timeout (or 3600 s) + 60 s.
- Calibration treats failed/cancelled/interrupted attempts alike and samples only completed
  tasks; `spawn_name` no longer accepts `Agent`; unprepared drafts get a `prepare` hint.
- `token_usage.py --day` without `--cwd` is incomplete when a rollout cannot be attributed;
  the missing sessions directory error names the path. `check-runtime.py` works from a global
  install (`smoke-test.txt` beside the script, `$CODEX_HOME/als.config.toml` and `agents/`).

### Round-4 runtime fixes
- File claims, the writer cap, the final-verify active-worker gate and the verification lock
  apply per linked worktree; `resources` stay global. A run whose worktree was removed or
  moved fails with "Run worktree missing or moved (...); cancel the run" and must be cancelled.
- Session auto-pick skips ended sessions (last event SessionEnd), but they still count, so
  an ended plus an active session means nothing is picked.
- `doctor`: when `codex features list` cannot run, feature items are unknown (`ok: null`,
  `[unk ]`) and never fail doctor; a timeout says "codex did not answer within Ns; raise
  ALS_DEPS_TIMEOUT". New `harness.py doctor --no-deps`; `status --run ID --format text`.
- Project-scope SessionStart hints use `als` when it is on PATH, otherwise the absolute
  interpreter and harness path; the project-scope resume command always uses the absolute
  form, so it runs from subdirectories and linked worktrees.
- Fingerprints exclude untracked `__pycache__/` and `*.pyc` files, and checks run with
  `PYTHONDONTWRITEBYTECODE=1`.

### Rounds 5–11 (audit convergence and CI)
- Live runs whose linked worktree was moved or removed keep their claims counting in every
  worktree until cancelled, so `git worktree move` cannot bypass file exclusivity.
- Process liveness is fail-safe: verification locks record the owner's uid; an owner that
  cannot be inspected (Codex Seatbelt sandbox, another uid) keeps the lock. A pid missing
  from macOS `ps` counts as exited only when it is still signalable (a zombie); on Linux a
  same-uid owner whose pid now belongs to another user is stale. SIGKILL of a check group
  needs positive identity (readable, same start, not a zombie).
- Installer remedies carry a non-default `ALS_HOME`; a reinstall blocked by another ALS
  install's `als` names that install, and this install's own `als` suggests `--adopt`.
- The test suite is hermetic: no host `codex`/`ocx` is executed, inherited `ALS_*` and
  `CODEX_THREAD_ID` are ignored, and it passes on Linux and macOS (Python 3.9 runtime job,
  3.11 and 3.14).

### Round-3 and round-4 installer and `als` fixes
- Upgrading a 0.2/0.4 project install records the ALS directories it created, so a later
  uninstall removes them when empty; `[features] hooks = true` is no longer mistaken for
  inline hooks; `--check`/`--uninstall` of an installer-created `config.toml` work without a
  TOML parser.
- Hook commands work from submodules, with an exported `CDPATH`, with `HOME` unset (global
  form `python3 "${HOME:-/abs/home}/.local/share/als/harness.py" hook`) and with git older
  than 2.13 from a subdirectory (a relative `--git-common-dir` is resolved with
  `--show-cdup`); a missing project harness exits 0. Older hook forms are recognised and
  replaced on reinstall (`--check` reports them as outdated, exit 4).
- A project install (with or without `--skip-hooks`) over `--hooks-only` hooks is refused
  with the working remedy `als uninstall --hooks-only`.
- `install.sh`/`setup.sh` honour `ALS_PYTHON`; the global install records the `ALS_HOME`
  and `ALS_PYTHON` it used in the installed `als` (the environment still overrides), and
  errors name the configured interpreter.
- `als doctor --json` prints one `{"deps", "harness"}` object; exit 0 ok, 1 required
  failure, 2 usage/state error. `--no-probe` skips only `codex features list`; probes are
  bounded by `ALS_DEPS_TIMEOUT` (default 10 s). Against a 0.2–0.4 project install it skips
  that harness and prints an upgrade/uninstall hint instead of failing; with a harness that
  supports `--no-deps` the dependency report is printed once.
- `als usage` without a selection option reports `--latest`; `als plan new` writes a
  `python3 -B` check so verification does not trip on new `__pycache__` files.
- Documented: run state under `~/.local/state/als/<hash>/` survives uninstall (delete it by
  hand), and 0.3/0.4 manifests restore `hooks.json` JSON-equal rather than byte-equal.

### Not validated
- No paid model or live Codex session was run for this release. Tests use synthetic logs,
  temporary Git projects and local commands; no performance or savings claim is made.

## 0.4.0 — bounded delegation, context contracts and reviewed calibration

- Add versioned opt-in plan preparation: trivial low-risk tasks run directly in HEAD;
  exact duplicate scopes, conflicting role selections and unsafe downgrades are rejected.
- Preserve existing Astra/Luna/Sol role pins; use task kind, declared complexity/risk
  and explained overrides rather than pretending narration changes model effort.
- Add bounded reference-only worker contracts, character limits and explicit reasons
  for full-parent context when the real tool exposes fork_context. Do not inject
  unsupported arguments, rewrite permissions, ingest whole files or truncate failures.
- Add lean/balanced/throughput profiles, per-run total spawn allowances, conservative
  concurrent writers, HEAD resource reservations and 20% explicit-budget finalization margin.
- Add compact status, per-run usage analysis and caller-reviewed production measurement.
  Calibration needs five distinct comparable reviewed production runs, includes failures
  and retries, excludes diagnostics and uncertain usage, and affects only future plans.
- Estimates and parallel suggestions are provisional heuristics, not benchmark optima,
  billing guarantees or permission to raise user budgets. No extra supervisor model.
- Separate usage cursors per run, record terminal boundaries, retain late child usage
  with warnings, freeze new terminal run elapsed time and preserve observed worker end time.
- Keep 0.3.x plans/states compatible and unchanged in policy. Add regression coverage
  for direct execution, role gates, context contracts, history eligibility and accounting.

## 0.3.1 — safe reinstallation after preserved harness state

- Permit reinstallation after 0.3.0 uninstall kept an unchanged
  `.astra-luna-sol/.gitignore` to protect persistent state/evidence.
- Reclaim only byte-identical ignore files when the manifest is absent and
  runtime state/evidence exists; edited, unrelated and symlinked paths
  continue to fail closed.
- Add regression cases for uninstall → reinstall, runtime preservation,
  modified leftovers and unowned identical files without runtime state.

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
