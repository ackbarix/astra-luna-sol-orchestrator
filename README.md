# Astra–Luna–Sol Orchestrator · 1.0.0

Codex에서 GPT 모델로 **프로젝트에 맞는 에이전트 팀(하네스)**을 만들고 운영하는 도구입니다.
[revfactory/harness](https://github.com/revfactory/harness) v2와 같은 방식이며, 모델만 GPT를 씁니다.

| 원본 (Claude) | ALS (GPT) | 맡는 일 |
|---|---|---|
| fable | **GPT-6 Astra** | 계획·조율·장기 자율 실행 |
| opus | **GPT-6.1 Sol** | 설계·코드 생성·복잡한 분석·교차 검증 |
| sonnet | **GPT-6 Luna** (항상 max) | 일상·절차적 작업 |

OpenAI/OpenCodex의 공식 배포물이 아닙니다.
참고: [donvito/codex-astra-luna-orchestrator](https://github.com/donvito/codex-astra-luna-orchestrator)

## 1. 설치 (3단계)

```bash
git clone https://github.com/ackbarix/astra-luna-sol-orchestrator.git
cd astra-luna-sol-orchestrator
sh setup.sh
```

설치가 끝나면 다음 단계가 출력됩니다. `als` 명령을 찾을 수 없다고 나오면, 출력된
`export PATH=...` 줄을 셸 설정 파일(`~/.bashrc` 등)에 추가하고 터미널을 다시 여세요.

```bash
als doctor        # 필요한 프로그램이 다 있는지 점검
```

`[FAIL]`이 있으면 그 줄의 안내대로 고친 뒤 다시 실행하세요. `[warn]`은 대개 무시해도 되지만,
`conflict:`로 시작하는 경고는 [6절](#6-문제가-생기면)을 보고 처리하세요.

**필요한 것:** Linux 또는 macOS, Python 3.9 이상, git, Codex CLI.
설치는 셸 설정 파일이나 기존 Codex 설정(`~/.codex/config.toml`)을 바꾸지 않습니다.
무엇이 설치될지 미리 보려면 `sh setup.sh --dry-run`을 실행하세요(아무것도 쓰지 않음).

## 2. 하네스 만들기 (핵심 기능)

프로젝트 폴더에서 Codex를 열고 요청하세요.

```bash
cd ~/work/my-app
als run
```

```text
$harness 이 프로젝트에 맞는 하네스 구성해줘
```

Astra가 프로젝트를 분석해 다음을 만듭니다.

- `.codex/agents/*.toml`: 이 프로젝트 전용 에이전트 (누가 일하는지)
- `.agents/skills/*/SKILL.md`: 에이전트가 따를 스킬과 오케스트레이터 (어떻게 일하는지)
- `AGENTS.md`: 호출 조건과 변경 이력만 담은 짧은 안내

Codex 샌드박스는 에이전트가 `.codex/`와 `.agents/`를 직접 고치지 못하게 막습니다. 그래서 Astra는 하네스를
`_workspace/harness-staging/`에 만든 뒤 아래 명령을 실행해 달라고 요청합니다(또는 승인을 요청해 직접 실행합니다).

```bash
als harness diff     # 바뀔 파일 확인
als harness apply    # 표준 위치에 반영 (덮어쓰는 파일은 백업)
```

반영한 뒤에는 Codex를 새로 시작해야 새 에이전트와 스킬을 읽습니다.

작업 흐름에 따라 세 가지 실행 방식을 고릅니다.

| 실행 방식 | 쓰는 도구 | 알맞은 작업 |
|---|---|---|
| 서브에이전트 위임 | Codex `spawn_agent` | 결과만 받으면 되는 병렬 작업 |
| 워크플로 조율 | `als-flow run 스크립트.py` | 처리 목록·검증·반복을 코드로 정할 수 있는 작업 |
| 지속형 에이전트 | `als-agent`, `als-task` | 같은 전문가와 대화를 이어 가야 하는 작업 |

쓰면서 생긴 피드백은 `$harness-evolve 하네스 회고해줘`로 에이전트·스킬에 반영합니다.
자세한 절차는 설치된 스킬(`~/.codex/skills/harness/`)에 있습니다.

## 3. 일반 사용

```bash
cd ~/work/my-app
als run           # Astra 설정으로 Codex 시작 (= codex --profile als)
```

작은 수정은 평소처럼 요청하면 됩니다. 여러 단계가 필요한 큰 작업은 스킬을 불러 주세요.

```text
$astra-luna-sol
로그인 기능에 비밀번호 재설정을 추가해. 작은 일은 직접 하고,
필요한 작업만 나눠 맡겨. 기존 변경은 보존하고 배포는 하지 마.
```

Codex 안에서 `/status`를 입력하면 Astra 모델이 쓰이는지 확인할 수 있습니다.

### (선택) 작업 관리 하네스

여러 워커가 같은 파일을 동시에 고치지 않게 막고, 선언한 테스트가 통과해야 완료로
인정받게 하려면 그 저장소에서 한 번만 실행하세요.

```bash
cd ~/work/my-app
als install --hooks-only
```

그다음 Codex에서 `/hooks`를 열어 ALS 훅을 검토하고 신뢰해 주세요.
저장소에는 `.codex/hooks.json` 하나만 생기고, 작업 기록은 저장소 밖에 저장됩니다.
계획을 등록하는 방법은 [하네스 가이드](docs/harness.md)에 있습니다.

## 4. 자주 쓰는 명령

| 명령 | 하는 일 |
|---|---|
| `als doctor` | 설치·의존성 점검 |
| `als run` | Astra 설정으로 Codex 시작 |
| `als install --hooks-only` | 현재 저장소에 하네스 훅 켜기 |
| `als uninstall --hooks-only` | 현재 저장소의 하네스 훅 끄기 |
| `als status --format text` | 진행 중인 관리 작업 보기 |
| `als usage` | 최근 세션의 토큰 사용량 |
| `als version` | 설치된 버전 |
| `als-flow run`, `status`, `ls` | 워크플로 실행·상태 (`_workspace/.flow/`) |
| `als-agent start`, `send`, `list`, `show` | 이름 있는 지속형 에이전트와 대화 |
| `als-task add`, `update`, `list` | 공유 작업 목록 (`_workspace/tasks.json`) |
| `als harness diff`, `apply` | 만든 하네스를 `.codex/`·`.agents/`에 반영 |

## 5. 업데이트와 제거

```bash
cd astra-luna-sol-orchestrator
git pull --ff-only
sh setup.sh                  # 업데이트 (다시 설치)

als uninstall --global       # 전체 제거
```

**0.2–0.4를 쓰던 저장소:** `als doctor`가 이전 프로젝트 설치를 알려 줍니다.
`als uninstall ~/work/my-app`으로 이전 설치를 지운 뒤, 필요하면 그 저장소에서
`als install --hooks-only`를 실행하세요.

## 6. 문제가 생기면

| 증상 | 해결 |
|---|---|
| `als: command not found` | `setup.sh`가 출력한 `export PATH=...` 줄을 셸 설정에 추가 |
| `als doctor`에 `[FAIL] codex-feature:hooks` | `codex features enable hooks` |
| `als doctor`에 `[FAIL] codex-feature:multi_agent` | `codex features enable multi_agent` |
| `als doctor`에 `conflict:` 경고 | 이전 프로젝트 설치가 남아 있음. 위 "0.2–0.4를 쓰던 저장소" 참고 |
| 설치·제거가 `REFUSED`로 멈춤 | 출력된 해결 명령을 그대로 실행 (사용자 파일은 덮어쓰지 않음) |

그 밖의 세부 사항은 다음 문서를 보세요.
- [설치·업그레이드·복구 상세](docs/install.md): 환경 변수, 전역/프로젝트 모드, 복구 플래그
- [하네스 가이드](docs/harness.md): 작업 계획, 수명 주기, 검증
- [효율 가이드](docs/efficiency.md): 위임 정책
- [검증 가이드](docs/validation.md): 사용량 리포트

## 역할 구성

| 역할 | 모델 / 추론 수준 | 하는 일 |
|---|---|---|
| HEAD | GPT-6 Astra / Medium | 총괄 |
| als_explorer · als_researcher · als_tester · als_worker · als_test_designer | GPT-6 Luna / Max | 탐색 · 자료 조사 · 테스트 실행 · 구현 · 테스트 설계 |
| als_hard_worker · als_reviewer_deep | GPT-6.1 Sol / High | 고난도 구현 · 고위험 검수 |
| als_reviewer | GPT-6.1 Sol / Medium | 독립 검수 |

모델과 추론 수준은 역할 파일이 고정합니다. 프롬프트로 역할의 추론 수준을 바꾸지 말고,
필요한 모델·추론 수준의 역할을 골라 맡기세요.

## 알아 둘 한계

- **보안 장치가 아닙니다.** 파일 예약은 협조를 전제로 한 충돌 방지이고, OS 수준 차단이 없습니다.
- 읽기 전용 역할의 "읽기 전용"은 지시일 뿐 강제되지 않습니다(하위 에이전트는 부모의 샌드박스를 물려받음).
- 훅에 오류가 나거나 신뢰되지 않으면 Codex는 막지 않고 계속 진행합니다.
- 토큰 예산은 새 작업을 줄이는 기준일 뿐 청구액 상한이 아닙니다.
- 이 릴리스는 유료 모델로 실측하지 않았습니다. 비용 절감이나 성능 향상은 보장하지 않습니다.

## 라이선스

[변경 기록](CHANGELOG.md). 본체는 MIT([LICENSE](LICENSE))입니다.
`harness/skills/`는 [revfactory/harness](https://github.com/revfactory/harness)(Apache-2.0)를 GPT용으로
고친 것이고([NOTICE](harness/NOTICE.md)), `scripts/token_usage.py`도 [Apache-2.0](licenses/Apache-2.0.txt)입니다.
