## Astra–Luna–Sol coding orchestration

This repository uses the project-scoped `$astra-luna-sol` skill for substantive work that benefits from delegation. Apply the skill when appropriate; it does not require spawning agents for trivial fixes.

- Root HEAD: `gpt-6-astra` at `medium` — planning, architecture, prioritization and final integration.
- General worker/explorer/tester/researcher: `gpt-6-luna` at `max` — bounded work and cost-effective parallelism.
- Hard worker: `gpt-6.1-sol` at `high` — complex feature implementation and difficult debugging.
- Independent reviewer: `gpt-6.1-sol` at `medium` — risk-based review, not automatic review for every change.
- Up to six concurrent subagents, but only when tasks are independent and useful.
- Keep a single writer for each file; preserve existing user changes.
- Do not fabricate delegation, model routing, tool results or test successes.
- Treat the active Codex tool schema and user instructions as authoritative when they conflict with an illustrative example.

If a worker is spawned, disclose requested model/effort and the verifiable result concisely. Distinguish a requested model from the actual upstream model when that cannot be observed.
