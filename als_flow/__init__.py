"""als_flow: Claude Code team-factory primitives rebuilt on the Codex CLI.

| Claude Code                         | here                                   |
|-------------------------------------|----------------------------------------|
| `Workflow` script, `agent()`,       | `from als_flow import Flow`; run with  |
| `pipeline()`, `parallel()`, phases, | `als-flow run SCRIPT.py`               |
| `resumeFromRunId`, token budget     | (`--resume RUN_ID`)                    |
| `Agent(name:)` + `SendMessage`      | `als-agent start/send` (codex resume)  |
| `TaskCreate` / `TaskUpdate`         | `als-task add/update/list`             |

State lives in `_workspace/` at the git top level (`ALS_WORKSPACE` overrides).
"""
from .flow import Flow

__all__ = ['Flow']
