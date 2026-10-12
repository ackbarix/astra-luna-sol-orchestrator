---
name: astra-luna-sol
description: Cost-aware coding delegation for substantial independent work; skip trivial edits and simple questions.
---
# Astra–Luna–Sol — finish the work, not the orchestration ceremony

HEAD is Astra Medium. Keep architecture, priorities and integration here. Simple
lookups/one-line edits are direct work: do not create a run or worker just to show
activity. Do not repeat work already delegated or ask a stronger model to redo a
successful result. Bundle related small tasks. Reuse a relevant worker for follow-up.

## Routing (task metadata, not model self-identification)

| Task | Role | Requested effort |
|---|---|---|
| Bounded search / primary-source research | als_explorer / als_researcher | Luna Max |
| Existing checks and simple reproductions | als_tester | Luna Max |
| Bounded implementation | als_worker | Luna Max |
| Difficult test design/failure analysis | als_test_designer | Luna Max |
| Complex implementation/analysis | als_hard_worker | Sol 6.1 High |
| Valuable independent review | als_reviewer | Sol 6.1 Medium |
| High-risk security/state/data review | als_reviewer_deep | Sol 6.1 High |

Do not lower safety-critical reasoning to save tokens. Route obviously hard work
directly; never manufacture a Luna failure. Roles pin effort in TOML. A different
role requires an explained override; prose does not change runtime model settings.

## Managed work

Read `efficiency-guide.md` for the plan schema only when needed (project install:
`.astra-luna-sol/`; global install: `${ALS_HOME:-~/.local/share/als}/`).
CLI (below abbreviated HARNESS): `python3 .astra-luna-sol/harness.py` in a project
install, or `als <command>` after a global install (`als plan new FILE` drafts a plan).

1. Draft goal, checks/criteria and tasks with objective, kind, complexity, risk,
   acceptance, relevant context_files, write_paths/resources/dependencies. No raw
   logs or full files in plans. Preserve meaningful constraints. `prepare --plan
   DRAFT --profile balanced` prints the validated plan; review it, then `start
   --plan PREPARED --session ACTUAL_ID`. Do not guess IDs or invoke diagnostic
   models. Use observe unless the user explicitly authorizes guarded.
2. Trivial/low-risk tasks route to executor=head. For mixed managed work use
   `begin-head --run ID --task TASK` to reserve writes, do the work, then finish.
   A wholly trivial request needs no harness run. No fake child IDs for HEAD work.
3. For a worker, use `task-message --run ID --task TASK`. It starts with
   `ALS_TASK=TASK` and carries bounded references/conditions. Select the task's
   exact agent_type. If the active tool supports fork_context, prefer false;
   otherwise omit it and report inheritance unverified. Full parent context needs
   full_context_reason. Never invent tool fields or bypass trust/approval rules.
4. Default balanced admission: up to 3 useful workers, 2 writers (1 for high-risk
   writes). Six remains the Codex ceiling. Throughput above 3 needs explicit
   parallel_reason, independent tasks and shared DB/port/fixture ownership.
   Never start final review before implementation stabilizes or run duplicate builds.
5. After actual worker termination, `finish` with concise evidence. Use
   --workers-stopped only when termination was really confirmed. Execute only
   authorized checks with `verify --execute`; accept tasks and assess criteria on
   actual evidence. `complete` must not certify missing/failed/stale checks.
6. Use `status --run ID --compact` during work. Report final facts once. Put long
   artifacts in evidence and reference them; material failures must not be omitted
   to meet a length preference. Never generate extra agents just for reporting.

## Measurement and limits

Use analyze for recorded cached/uncached input, output, HEAD overhead and retries.
Raw token totals are NOT fees; cache and reasoning are subsets, not extra tokens.
After a real terminal production run, measure only after reviewing quality and
coverage. Caller assessments are not independent grades. History tuning needs
at least 5 comparable reviewed production runs; smoke tests are excluded.
`prepare --use-history` may use reviewed estimates for FUTURE plans only. It
never raises a user budget or edits a live run. Report insufficient evidence.
No automated price assumptions, hidden supervisor model, benchmark or saving claim.
