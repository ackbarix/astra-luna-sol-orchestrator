---
name: als
description: Delegate bounded coding work to ALS GPT workers (Luna/Sol via the Codex CLI) while Claude Code stays the head. Use for multi-part tasks where independent pieces can run in parallel; do small work directly.
---
# ALS for Claude Code (0.6)

You are the head: requirements, decomposition, integration and the final check stay with you.
GPT workers run through `als-gpt`, which pins each role's model, effort and sandbox and
talks to Codex directly (no OpenCodex). Each worker call costs ~12k mostly-cached input tokens
of fixed overhead, so bundle small related pieces into one task; never delegate a one-line edit.

## Roles

| Role | Model / effort | Use for |
|---|---|---|
| explorer, researcher | Luna medium, read-only | locating code, call paths, primary-source facts |
| tester | Luna medium | running existing tests, reproductions |
| worker | Luna max | bounded implementation |
| test_designer | Luna max | hard regression tests, failure analysis |
| hard_worker | Sol 6.1 high | intrinsically complex implementation/debugging |
| reviewer / reviewer_deep | Sol 6.1 medium / high, read-only | independent review (deep: security, state, concurrency, data) |

Pick the role by the work, not by prompting effort. `als-gpt roles` prints the pins.

## Running a worker

1. Write a short task file: objective, the files/symbols to read first, what may change,
   constraints, and the acceptance check. References, not pasted files.
2. `als-gpt run worker task.md` (use the Bash tool with `run_in_background` for parallel
   independent tasks; at most 1-2 writers at once, read-only roles can fan out).
3. Writer roles get their own worktree and branch `als/<run>` - parallel writers cannot
   collide. Read-only roles run in place.
4. Read the report it prints (`als-gpt show RUN` later). Review the branch diff yourself,
   run the acceptance check once, then merge or cherry-pick and `als-gpt clean RUN`.
   A failed or timed-out worker: read its stderr, change the task or the role; do not
   resend an identical task.

## Verification: proportional to risk

- Docs, wording, trivial edits: no extra verification.
- Normal change: run the relevant tests once after integrating.
- State, concurrency, security, data loss, irreversible operations: tests plus one
  independent `reviewer_deep` pass on the final diff. Not before the code stabilizes.
- Do not re-review unchanged code, and do not add verifier/monitor steps per task.
- Read-only roles may still write (the sandbox is not a guarantee everywhere): after a
  review, `git status` must show no new changes.

## Plans and the work ledger (only for multi-session or multi-wave work)

- Plan: `.als/plans/<name>.md` with a plain-language TL;DR, effort band
  (Quick / Short / Medium / Large / XL), risk, decisions the user should check, then tasks
  with acceptance checks.
- Ledger: `.als/work.json` with exactly these fields, nothing else:
  `work_id, plan, status (active|paused|done), paused_reason, resume_authority,
  delivery_mode, tasks: [{id, role, status (todo|running|review|done|dropped), run, branch}]`.
  Update it when a task changes state. After a pause, resume only on the authority it names.
- Keep `.als/` out of commits unless the user wants plans versioned.

## Stop and ask the user when

- requirements are ambiguous in a way that changes the design;
- the same task failed twice with different approaches;
- the next step deploys, deletes data, spends money beyond the agreed scope, or pushes/merges.

Report at the end: what was delegated (run ids, roles), what you verified and how, and what
remains open. Do not claim a worker's checks passed unless its report shows it ran them.
