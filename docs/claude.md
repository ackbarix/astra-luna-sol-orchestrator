# Claude Code에서 ALS 쓰기 (0.6, 실험적)

Claude Code가 총괄(HEAD)을 맡고, 실무는 GPT 워커(Luna/Sol)가 맡는 구성입니다.
GPT는 Codex CLI(`codex exec`)로 **직접** 호출하므로 OpenCodex가 필요 없습니다.

| 역할 | 누가 |
|---|---|
| 요구사항 정리, 작업 분해, 통합, 최종 확인 | Claude Code (HEAD) |
| 탐색·구현·테스트·검수 | GPT 워커 (`als-gpt`가 역할 파일대로 실행) |
| 권한 통제, 병렬 실행, 백그라운드 작업 | Claude Code 기본 기능 |

Codex 쪽 하네스(SQLite, 훅 7개, `finish/verify/accept/complete` 절차)는 이 구성에서 쓰지 않습니다.

## 설치

```bash
cd astra-luna-sol-orchestrator
python3 claude/install_claude.py --dry-run   # 미리 보기
python3 claude/install_claude.py
codex login status                           # "Logged in" 이어야 함. 아니면: codex login --device-auth
als-gpt roles                                # 역할 8개가 보이면 정상
```

설치되는 것은 세 가지뿐이며, Claude Code·Codex·OpenCodex 설정은 바꾸지 않습니다.

- `~/.local/share/als/claude/` (`als_gpt.py`, 역할 파일, 매니페스트)
- `~/.local/bin/als-gpt`
- `~/.claude/skills/als/SKILL.md`

점검은 `--check`, 제거는 `--uninstall`로 합니다. 직접 만든 같은 이름의 파일이나 설치 후 수정한 파일은 덮어쓰지 않고 거부합니다.

## 사용

Claude Code에서 큰 작업을 맡길 때 `als` 스킬을 쓰라고 요청하세요. Claude가 작업을 나누고 다음처럼 워커를 부릅니다.

```bash
als-gpt run worker task.md        # 구현: 전용 worktree + 브랜치 als/<run>
als-gpt run reviewer task.md      # 검수: 읽기 전용, 현재 체크아웃
als-gpt ls                        # 이 저장소의 실행 목록
als-gpt show RUN                  # 보고서 다시 보기
als-gpt clean RUN                 # 병합 후 worktree 정리 (브랜치는 유지)
```

- **작성 역할**(worker, hard_worker, tester, test_designer)은 저장소 밖 전용 worktree에서 일합니다. 병렬로 돌려도 파일이 섞이지 않고, Claude가 diff를 검토한 뒤 병합합니다.
- **읽기 전용 역할**(explorer, researcher, reviewer, reviewer_deep)은 현재 체크아웃에서 읽기 전용 샌드박스로 실행됩니다.
- 모델·추론 수준·샌드박스는 역할 파일이 고정합니다. 호출하는 쪽에서 올리거나 풀 수 없습니다.
- 기록은 `~/.local/state/als/gpt-runs/<저장소 해시>/<run>/`에 남습니다: 프롬프트, 이벤트, 보고서, 사용량.

종료 코드는 다음과 같습니다.

| 코드 | 의미 |
|---|---|
| 0 | 완료 |
| 1 | 워커 실패 |
| 2 | 사용법·환경 오류 |
| 124 | 시간 초과 (`--timeout`, 기본 3600초) |

## 비용 감각

워커 한 번에 Codex 기본 지시문으로 약 1.2만 토큰(대부분 캐시)의 고정 비용이 듭니다. 실제 작은 탐색 작업은 입력 약 3.7만(캐시 2.5만)이었습니다. 그래서 한 줄 수정은 Claude가 직접 하고, 관련된 작은 일은 묶어서 한 번에 맡기는 것이 원칙입니다.

## 엔드포인트

`config.toml`에 OpenCodex 프록시가 설정돼 있어도 `als-gpt`는 네이티브 주소로 직접 호출합니다.

| Codex 로그인 방식 | 사용하는 주소 |
|---|---|
| ChatGPT | `https://chatgpt.com/backend-api/codex` |
| API 키 | `https://api.openai.com/v1` |

- `ALS_CODEX_BASE_URL=<url>`로 다른 주소를 지정할 수 있습니다.
- `ALS_CODEX_BASE_URL=`(빈 값)이면 `config.toml`의 주소를 그대로 씁니다.

## 계획과 작업 장부 (여러 세션에 걸친 작업만)

OMO 방식을 참고하되, 장부 필드는 고정합니다.

- **계획** `.als/plans/<이름>.md`: 쉬운 말 요약, 규모(Quick/Short/Medium/Large/XL), 위험도, 사용자가 확인할 결정, 작업과 완료 조건.
- **장부** `.als/work.json`: `work_id, plan, status, paused_reason, resume_authority, delivery_mode, tasks[{id, role, status, run, branch}]`. 이 외의 필드는 추가하지 않습니다.

## 한계

- 실험 단계입니다. 실제 GPT 호출로는 읽기 전용 탐색 1회만 확인했습니다. 쓰기 경로는 가짜 codex로만 테스트했습니다.
- 읽기 전용 샌드박스는 Codex 샌드박스에 의존합니다. 검수 후 `git status`로 변경이 없는지 확인하세요.
- 기존 Codex 하네스(0.5)와 비교한 비용·품질은 아직 측정하지 않았습니다.
