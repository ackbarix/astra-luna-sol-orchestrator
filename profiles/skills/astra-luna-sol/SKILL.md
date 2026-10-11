---
name: astra-luna-sol
description: Coordinate substantive coding work when bounded delegation improves correctness or elapsed time. Skip trivial localized edits and simple questions.
---
# Astra–Luna–Sol 0.3: cost-aware execution, evidence-based verification

This is a decision policy, not a scheduler or a guaranteed optimal benchmark.
Actual runtime settings and user authorization remain authoritative.
The parent is Astra Medium; a child reading this skill is NOT another HEAD.

## Roles (choose the exact configured role)

| Role | Requested model / effort | Work |
|---|---|---|
| als_explorer | gpt-6-luna / medium | Bounded read-only code and call-path mapping |
| als_researcher | gpt-6-luna / medium | Focused primary-source API/version research |
| als_tester | gpt-6-luna / medium | Existing tests, reproduction, basic validation |
| als_worker | gpt-6-luna / max | Bounded general implementation and fixes |
| als_test_designer | gpt-6-luna / max | Difficult regression-test design/failure analysis |
| als_hard_worker | gpt-6.1-sol / high | Intrinsically complex implementation/debugging |
| als_reviewer | gpt-6.1-sol / medium | Independent read-only review when valuable |
| als_reviewer_deep | gpt-6.1-sol / high | High-risk security/state/concurrency/data review |

Role TOML pins efforts; do not ask a Medium role to become Max through narration.
If the actual tool cannot select a role/model, report it rather than inventing
fields. Use the explicit deeper role when warranted; do not set model-wide effort
pins that collapse Sol High and Medium into the same configuration.

## Delegation gate and work contracts

The HEAD decides requirements, architecture, dependency order and completion.
Handle very small tasks directly. Delegate meaningful independent work, not
individual file lookups or one-line explanations. Bundle related small tasks and
reuse a relevant worker for follow-ups instead of repeatedly creating contexts.
Route obviously hard work straight to Sol High; do not manufacture Luna failures.
If blocked, inspect the cause before retrying. Do not repeat identical failed
instructions. Narrow the task, change the approach or escalate with evidence.

Every contract states objective, relevant context/files, exclusive write scope,
shared resources, acceptance tests and concise deliverables. Avoid handing every
worker full conversation history. Ask for conclusions, paths/symbols, diffs,
commands and actual results—not whole files, long raw logs or repeated plans.
HEAD must not repeat a delegated investigation in parallel; use waiting time on
independent decisions or integration preparation. Check critical evidence rather
than redoing every worker's entire analysis.

## Parallelism and resource discipline

Six concurrent children is a ceiling, not a target. Usually 1–4 useful workers;
5–6 only for truly independent work with available runtime resources.
Prefer broad parallel read-only tasks. Begin with only 1–2 simultaneous writers.
Avoid same-file writes AND shared port/database/schema/fixture conflicts.
Agree on interfaces first and serialize dependent steps. Assign one owner to
full builds/integration suites; never duplicate them across workers blindly.
Children do not create children; max_depth=1 is pinned for the tested V1 setup.

Do not spawn a final reviewer while implementation is still changing. Review is
not a mandatory escalation rung. Low-risk changes may use focused tests without
an extra reviewer; security, data integrity and consequential state changes need
independent scrutiny, using als_reviewer_deep when Medium is insufficient.
Do not duplicate a successful Luna implementation through Sol merely for prestige.

## Completion and measurement

Integrate results, inspect the final diff, and run/coordinate relevant tests.
Resolve required worker failures before declaring completion. Preserve the user's
worktree. Do not deploy, place orders, delete operational data or transmit secrets
without the user's separate authorization. YOLO is not a safety boundary.

Conclude with a short Orchestration Summary: actual thread IDs/roles, REQUESTED
model/effort, completed/failed tasks, real checks and remaining risks. Mark serving
identity or concurrency unverified unless execution logs prove them. Do not spawn
workers merely to write this report. If none were useful, say why in one line.

Use recorded per-response usage, including retries and HEAD work. Compare quality,
completion time and rework, not raw token count alone. Cached input is part of
input and reasoning is part of output; don't add either twice. A session log is
not an invoice. Legacy/unclassified counters must not be assigned to the final
model. Tune Medium vs Max using the same tasks; no savings percentage is promised.

## Opt-in managed runs (ALS 0.3 harness)

For substantial tasks when the user asks for harness management, use the installed
`.astra-luna-sol/harness.py` CLI. Read `.astra-luna-sol/harness-guide.md` when needed.
Hooks and their trust must be available; never claim they are active from prose.

1. Write a bounded JSON plan with goal, tasks/roles, dependencies, exclusive write
   paths/shared resources, declared argv checks, and acceptance criteria. Use only
   tests/actions authorized for this task; do not create costs merely to prove setup.
2. Start in observe mode and explicitly bind the actual session ID supplied by the
   SessionStart hook (`start --plan FILE --session ID`). Never guess a session ID.
   Guarded mode is user opt-in after real callback checks; don't silently enable it.
3. Use the real spawn tool's `agent_type` role and begin each delegated message with
   a line `ALS_TASK=<task-id>`. This links actual tool/agent IDs to the plan without
   inventing a new tool argument. A rejected/uncorrelated spawn isn't successful work.
4. After the worker really stops, record `finish --run ID --task TASK --note ...`.
   If termination isn't observed, inspect the real worker before using
   `--workers-stopped`. Don't release a live writer merely to pass a check.
5. Run authorized declared checks using `verify --run ID --execute`, then `accept`
   tasks whose result/checks are adequate. Criteria are separate explicit `assess`
   records (actor HEAD); do not present them as authenticated human approval.
6. `complete --run ID` certifies only declared/current evidence. If checks fail,
   the dependency isn't done, or evidence is stale, fix only the required authorized
   scope, or pause/report the blocker. Don't loop blindly or rewrite tests to win.
7. Resume reconciles code and uncertain workers; it never resets/stashes user work.
   Retry prepares a pending task but does NOT spawn another model. Maximum 3 attempts.
8. Use `sync-usage` and `report` for records. Reporting/observation never needs an LLM.

For trivial work, use ordinary ALS without starting a harness run. This does not
force all role types or a particular worker count. A persistent state/claim is not
an OS permission boundary; cancellation needs the actual Codex turn stopped too.
