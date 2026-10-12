# NOTICE — Harness v2 스킬 (Codex + GPT 포팅)

이 디렉터리의 `skills/harness/`와 `skills/harness-evolve/`는 다음 원본을 바탕으로 한 파생물이다.

- 원본: revfactory/harness — <https://github.com/revfactory/harness>
- 원본 커밋: `92d9f1b`
- 원본 저작권: Copyright 2025 robin (원본 `LICENSE` 기재)
- 원본 라이선스: Apache License 2.0 (<https://www.apache.org/licenses/LICENSE-2.0>, 전문은 저장소의 `licenses/Apache-2.0.txt`)
- 변경: **modified for Codex + GPT by ALS** (Astra–Luna–Sol Orchestrator)

원본의 저작권 고지와 라이선스 조건은 그대로 유지된다. 이 파생물도 Apache License 2.0을 따른다.
이 포팅은 원본 저작자나 OpenAI의 공식 배포물이 아니다.

## 수정한 파일

| 포팅 파일 | 원본 파일 |
|-----------|-----------|
| `skills/harness/SKILL.md` | `skills/harness/SKILL.md` |
| `skills/harness/references/execution-modes.md` | `skills/harness/references/execution-modes.md` |
| `skills/harness/references/model-selection-guide.md` | `skills/harness/references/model-selection-guide.md` |
| `skills/harness/references/orchestrator-template.md` | `skills/harness/references/orchestrator-template.md` |
| `skills/harness/references/qa-agent-guide.md` | `skills/harness/references/qa-agent-guide.md` |
| `skills/harness/references/skill-testing-guide.md` | `skills/harness/references/skill-testing-guide.md` |
| `skills/harness/references/skill-writing-guide.md` | `skills/harness/references/skill-writing-guide.md` |
| `skills/harness/references/team-examples.md` | `skills/harness/references/team-examples.md` |
| `skills/harness/references/team-patterns.md` | `skills/harness/references/team-patterns.md` |
| `skills/harness/references/workflow-recipes.md` | `skills/harness/references/workflow-recipes.md` |
| `skills/harness-evolve/SKILL.md` | `skills/evolve/SKILL.md` (스킬 이름 `evolve` → `harness-evolve`) |

## 원본과의 차이

구조, 단계, 원칙, 점검표, 문구는 원본을 따른다. 실행 환경을 Claude Code + Claude 모델에서
Codex + GPT 모델로 바꾸는 데 필요한 부분만 고쳤다.

| 원본(Claude Code) | 포팅(Codex + GPT) |
|---|---|
| `.claude/agents/{name}.md` (YAML: name, description, model, tools) | `.codex/agents/{name}.toml` (name, description, model, model_reasoning_effort, sandbox_mode, developer_instructions) |
| `.claude/skills/{name}/SKILL.md` | `.agents/skills/{name}/SKILL.md` |
| `CLAUDE.md` (호출 조건 + 변경 이력) | `AGENTS.md` (호출 조건 + 변경 이력) |
| 모델 fable / opus / sonnet | gpt-6-astra (`medium`) / gpt-6.1-sol (`high`, 일상 검토 `medium`) / gpt-6-luna (항상 `max`) |
| `tools`에서 Edit·Write 제외 | `sandbox_mode = "read-only"` + 지시문의 읽기 전용 문장(하위 에이전트는 부모 sandbox를 물려받음) |
| `Agent` 도구, `subagent_type` | Codex `spawn_agent`, `agent_type` (동시 6, `max_depth = 1`) |
| `Workflow` JavaScript 스크립트 | `als-flow run SCRIPT.py` Python 스크립트 (`als_flow.Flow`) |
| `Agent(name:)`, `SendMessage`, `TaskCreate`/`TaskUpdate` | `als-agent start/send/list/show`, `als-task add/update/list` |
| v1 전환(`TeamCreate`, 실험 플래그) | ALS 0.5 이하 범용 역할과 `$astra-luna-sol` 재사용 |
| `/harness:evolve` | `$harness-evolve` |

그 밖에 다음을 추가하거나 바꿨다.

- 모든 SKILL과 생성 에이전트의 작업 원칙에 "검증은 한 번 하고 멈춘다"를 넣었다(ALS 사용자 규칙).
- 적대적 검증·심사위원단·Loop-until-dry 등 품질 패턴의 사용 조건(정확성이 중요하거나 넓은 조사를 요청할 때)을 명시했다.
- `workflow-recipes.md`에 「여러 기준으로 탐색 + 누락 검토자」 예시 절을 추가했다(원본은 패턴 표에만 있었다).
- Codex는 하위 에이전트의 재위임과 워크플로 중첩을 지원하지 않으므로 계층형 위임은 단계 평탄화로 바꿨다.
- 워크플로의 `isolation: 'worktree'`는 메인 에이전트가 미리 만든 git worktree를 `cwd=`로 넘기는 방식으로 바꿨다.
- 워크플로 입력(`args`)은 `_workspace/00_flow_args.json` 파일로 넘긴다.

## 아이디어 참고: oh-my-openagent

범용 하네스(`$astra-luna-sol`)의 두 규칙, 즉 "사용자에게 보이는 동작이 바뀌면 실제 사용 확인 한 번"과
"큰 작업은 구현 전에 계획 검토 한 번"은 code-yeongyu/oh-my-openagent의 codex-ultrawork(MIT) 지시문에서
착안했다. 코드나 문장은 복사하지 않았다.
