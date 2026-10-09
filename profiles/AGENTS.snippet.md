## Astra–Luna–Sol orchestration (0.2)

For substantive work that benefits from delegation, use `$astra-luna-sol`.
Astra Medium owns requirements, architecture and integration. Use exact named
roles: Luna Medium for exploration/research/basic test execution; Luna Max for
implementation and difficult test design; Sol 6.1 High for hard implementation;
Sol 6.1 Medium for independent review, or the explicit High reviewer for high-risk
changes. These are requested settings, not claims of serving identity.

Six workers is a concurrency ceiling, never a target. Begin with 1–4 useful
independent tasks; normally only 1–2 simultaneous writers. Assign exclusive file
and shared-resource ownership. Avoid repeated exploration, duplicate tests,
waiting reviewers and full-context handoffs. Do trivial work directly. No child
spawns children. Preserve user edits and permission boundaries.

Report actual spawns and checks briefly; never invent model routing, usage or
passing tests. User instructions and the active tool schema take precedence.
