# ALS Harness 0.5.0 — 설치·사용·한계

ALS는 Codex를 대체하지 않습니다. 모델 선택과 `codex` 실행은 Codex가 하고,
Python/SQLite 하네스가 등록된 개발 작업의 상태·예약·검증·재시도·사용량을 관리합니다.
설치기·`als`·훅은 모델/API/배포를 자동 호출하지 않습니다. 별도 감독 AI도 없습니다.

## 설치 범위

| 범위 | 설치 | 하네스 | 상태 DB·검증 로그 | 훅 파일 |
|---|---|---|---|---|
| 전역 | `sh setup.sh` 또는 `als install --global` | `${ALS_HOME:-~/.local/share/als}/harness.py` | `${XDG_STATE_HOME:-~/.local/state}/als/<hash>/runs.sqlite`, `.../evidence/` | 없음 |
| 훅 전용 | `cd PROJECT && als install --hooks-only` | 전역 하네스 사용 | 전역과 같음 | `PROJECT/.codex/hooks.json` |
| 프로젝트 | `als install PROJECT` 또는 `sh install.sh PROJECT` | `PROJECT/.astra-luna-sol/harness.py` | `PROJECT/.astra-luna-sol/state/runs.sqlite`, `PROJECT/.astra-luna-sol/evidence/` | `PROJECT/.codex/hooks.json` |

`<hash>`는 메인 저장소 절대 경로의 SHA-256 앞 16자리입니다. `ALS_STATE_HOME`을 설정하면
`<hash>` 디렉터리의 부모가 그 경로로 바뀝니다. 같은 저장소의 워크트리와 하위 디렉터리는
모두 메인 저장소 하나로 매핑됩니다. 프로젝트 상태는 Git에서 무시되며 제거 후에도 남습니다.

훅 명령에는 프로젝트 절대 경로가 없습니다. 전역 훅은 `python3 "$HOME/.local/share/als/harness.py" hook`,
프로젝트 훅은 `git rev-parse --git-common-dir`로 저장소를 찾습니다. PreToolUse/PostToolUse 그룹은
`spawn_agent`에만 매칭하므로 다른 도구 호출에는 Python을 띄우지 않습니다. 설치 후 Codex에서
`/hooks`로 명령을 검토하고 **신뢰**해야 합니다. 설치기는 신뢰 절차를 우회하지 않습니다.

## `als` 사용

```bash
als doctor                 # 의존성·충돌, 이어서 현재 저장소의 하네스 상태
als run                    # codex --profile als (전역 설치 시)
als plan new /tmp/plan.json
als prepare --plan /tmp/plan.json > /tmp/prepared.json
als start --plan /tmp/prepared.json --session CODEX_SESSION_ID
als status --format text
als report --run RUN_ID --format md
```

`als <harness 명령>`은 `harness.py --project <메인 저장소> <명령>`과 같습니다. 프로젝트 설치가
있으면 그 하네스를, 없으면 전역 하네스를 씁니다. Git 저장소 밖에서는 현재 디렉터리를 프로젝트로
씁니다(`harness.py`를 직접 실행하면 `--project`를 요구). 아래 예시는 `als`로 적었고, 프로젝트 모드에서는
`python3 .astra-luna-sol/harness.py`로 바꿔도 됩니다.

단순 질문이나 한 줄 수정은 run을 만들 필요가 없습니다. **Observe가 기본**입니다.
Observe에서도 `complete`는 실패한 검증을 성공으로 인증하지 않지만, 훅이 도구 호출을
차단하거나 자동 계속 요청을 만들지는 않습니다.

### 계획 등록

명령은 셸 문자열이 아니라 `argv` 배열입니다. **실행될 명령을 검토하세요.** 테스트 코드도
부작용이 있을 수 있으며 검증기는 컨테이너가 아닙니다.

```json
{
  "goal": "로그인 오류 수정",
  "tasks": [
    {"id": "implement", "role": "als_worker", "write_paths": ["src/auth.py"],
     "resources": [], "depends_on": [], "checks": ["unit"], "estimate_tokens": 20000}
  ],
  "checks": [
    {"id": "unit", "argv": ["python3", "-m", "unittest", "discover", "-s", "tests"],
     "timeout": 120, "required": true}
  ],
  "criteria": [{"id": "behavior", "description": "요구된 로그인 동작과 오류 처리가 구현됨", "required": true}],
  "budget": {"tokens": 0, "seconds": 0}
}
```

- `start --plan -`은 stdin에서 계획을 읽습니다.
- `--session`을 생략하면 `CODEX_THREAD_ID`, 없으면 **최근 10분 안의 SessionStart 수신이 정확히 하나**일
  때 그 세션을 씁니다. 여러 개이거나 없으면 추측하지 않습니다(observe는 세션 없이 경고와 함께
  시작, guarded는 거부).
- 계획에는 런타임 전용 task 키(상태·시도 기록 등)를 넣을 수 없습니다. `.git`, `.astra-luna-sol`,
  `.codex` 아래는 작업 소유 경로로 예약되어 있습니다.
- `0` 예산은 미설정입니다. 토큰 예산은 **비캐시 입력 + 출력** 기준 소프트 예산이며 청구액이 아닙니다.

### 워커와 연결

`spawn_agent`에서 정확한 역할(`agent_type`)을 고르고 메시지 첫 줄에 `ALS_TASK=<task id>`를 넣습니다.
PreToolUse는 task 계약, 의존성, 파일/공유 자원, 총 6명·작성자 2명, 소프트 예산을 확인합니다.
PostToolUse 결과에서 `agent_id`를 연결하며, 연결하지 못하면 그 이유를 경고로 보여줍니다.
필요하면 실제 호출 기록으로 수동 연결합니다.

```bash
als bind --run RUN_ID --call-id TOOL_CALL_ID --thread REAL_AGENT_ID
```

하위 에이전트가 다시 `spawn_agent`를 호출하면 guarded는 거절, observe는 경고합니다(task를 예약하지
않음). 포크된 워크트리에서 실행된 자식의 이벤트도 부모 세션이 run에 연결되어 있으면 기록합니다.

## 작업 수명 주기

```bash
als finish --run RUN_ID --task implement --note "변경 내용 및 근거"
als verify --run RUN_ID                  # 명령 미리보기
als verify --run RUN_ID --execute        # 선언된 검사 실행
als accept --run RUN_ID --task implement
als assess --run RUN_ID --criterion behavior --actor HEAD --note "요구사항 확인 근거"
als complete --run RUN_ID
```

워커 종료 훅은 `awaiting_validation`일 뿐 완료가 아닙니다. 훅이 종료를 관측하지 못했으면
실제 중단을 확인한 뒤 `finish ... --workers-stopped`로 claim을 놓습니다.
`complete`가 성공한 run은 다시 running/blocked로 돌아가지 않습니다. 같은 코드에서 다시 호출하면
기록된 결과를 돌려주고, 코드가 바뀌었으면 새 run을 만들라고 거절합니다.

| 명령 | 효과 |
|---|---|
| `pause --run ID --reason R` | 진행 중 task를 `uncertain`으로 표시하고 **claim은 유지**. 검증 잠금 해제 |
| `cancel --run ID --reason R` | 영구 종료. claim을 가진 task를 `cancelled`로 바꾸고 **모든 claim 해제**. 검증 잠금 해제 |
| `resume --run ID [--workers-stopped] [--accept-drift] [--session S]` | 일시정지 run 재개. 작업 재실행 없음 |
| `retry --run ID --task T --approach A [--role R]` | 실패·중단된 task를 pending으로. 이전 결과 삭제 |

- pause/cancel은 코드 fingerprint 없이 동작하며 실행 중인 모델을 죽이지 않습니다. 실행 중 턴은
  Codex에서 사용자가 중단해야 합니다. 둘 다 이 run의 검사 프로세스 그룹이 **우리가 시작한 바로 그
  프로세스로 살아 있을 때만** SIGKILL합니다(결과의 `check_group_killed`).
- resume은 `reserved/running/uncertain` task가 있으면 `--workers-stopped`(옛 워커가 멈췄다는 확인)를
  요구하고 그 task를 failed/interrupted로 바꿉니다. 이미 끝난 `awaiting_validation` task는 그대로
  두어 `finish`·`accept`할 수 있습니다.
- resume은 코드가 `last_snapshot`과 다를 때만 `--accept-drift`를 요구합니다. `verify`가 끝날 때마다
  `last_snapshot`을 갱신하므로, 검증 후 손대지 않았다면 보통 필요 없습니다. checkout/reset/stash/commit은
  하지 않습니다. 취소·완료된 run은 재개할 수 없습니다.
- Codex SessionEnd는 run을 자동 pause합니다. 다음 SessionStart 안내에 정확한 resume 명령
  (전역: `als resume --run ... --session ...`)이 표시되지만 자동 실행하지 않습니다.
- retry는 새 모델을 실행하지 않습니다. 최초 포함 3회, 코드 진척 없는 반복 실패·동일 설명은 거절하고,
  고위험 task를 경량 역할로 낮추거나 읽기 전용 역할에 쓰기 소유를 넘기지 않습니다.

## 검증과 잠금

`verify --execute`는 종료 코드·시간초과·로그 해시·코드 fingerprint를 기록합니다. 검사 중 코드가
바뀌면 실패로 기록하고 이후 검사를 멈춥니다. 로그는 로컬 0600, 64KiB 제한이며 알려진 비밀 형태를
마스킹하지만 완전한 제거를 보장하지 않습니다. 통과해도 모든 버그가 없다는 뜻은 아닙니다.

- **전역 검증 잠금:** 한 번에 하나의 verify만 워크트리를 소유합니다. 잠금에는 verify 프로세스
  PID·시작 시각·부팅 ID와 실행 중인 검사의 프로세스 그룹이 기록됩니다. 프로세스가 사라졌거나
  PID가 재사용됐거나 재부팅된 잠금은 **stale**로 판단해 다음 verify/complete/resume 때 자동 해제하고
  `verification_lock_cleared` 이벤트를 남깁니다.
- verify가 도는 중 run이 pause/cancel되면 그 결과는 증거로 채택되지 않습니다.
- **최종 검사**(`--check` 없음): 어떤 미완료 run에도 활성 워커나 쓰기 claim이 없어야 합니다.
- **task 검사**(`--check ID`): **이 run**이 쓰기 claim을 갖고 있지 않으면 됩니다. 다른 run의 작성자나
  읽기 전용 워커가 활동 중이어도 실행되며, 동시 코드 변경은 전후 fingerprint로 실패 처리됩니다.

## Guarded 모드

Observe 세션에서 SessionStart / PreToolUse / PostToolUse / Stop 콜백을 실제로 확인한 뒤,
**새 run**을 명시적으로 시작합니다.

```bash
als start --plan /tmp/prepared.json --session CODEX_SESSION_ID --mode guarded --ack-hook-limits
```

guarded는 등록된 세션의 `spawn_agent` 호출에만 거절 결정을 반환하며 거절 사유는 비어 있지
않습니다(빈 사유는 Codex가 무시해 fail open되기 때문). `complete` 요청이 미충족일 때만 run당 최대
한 번 Stop 계속 요청을 만듭니다. 훅은 전체 도구·네트워크·파일 접근을 막는 보안 시스템이 아니고,
DB/훅 장애 시 Codex는 도구를 계속 실행합니다. `--yolo`는 하네스를 강화하지 않습니다.

## 사용량과 소프트 예산

```bash
als sync-usage --run RUN_ID
als report --run RUN_ID --format md     # JSON은 schema_version 2
als usage --latest
```

등록된 root/child 세션 로그만 증분으로 읽습니다(기본 `$CODEX_HOME/sessions`, run에 기록된 세션
경로 기준). 너무 큰 로그 줄은 건너뛰고 경고하며, 잘린 로그를 감지합니다. 아직 연결되지 않은
스레드의 사용량도 버리지 않습니다. 로그가 없으면 미측정이지 0이 아닙니다.
70%는 경고, 90%는 새 작업 직렬화, 100%는 신규 예약 거절입니다. 진행 중 응답·다른 세션·청구
요금은 즉시 제한하지 못합니다.

## 종료 코드

| 명령 | 코드 |
|---|---|
| `install.sh` / `als install --check` | 0 정상, 1 미설치, 2 변경·거부, 4 구버전 |
| `install.sh` / `als install` (그 외) | 0 성공, 2 거부(사전 점검 실패·수정된 관리 파일 등) |
| `als doctor` | 필수 의존성이 없으면 1(필수 항목 `FAIL`), 선택 항목은 `warn`만 |
| `token_usage.py` / `als usage` | 0 정상, 1 해당 세션 없음, 2 인자·세션 디렉터리 오류, 3 `--strict`에서 경고 |
| `check-runtime.py` | 0 정상, 2 문제 발견 |

## 문제 해결 (`als doctor`)

| 항목 | 의미와 조치 |
|---|---|
| `python` | 실행 Python이 3.9 미만. 3.9+를 설치하거나 `ALS_PYTHON=/path/to/python3` |
| `python-installer` (선택) | TOML 파서 없음. 프로젝트 `config.toml` 병합에만 필요: Python 3.11+ 또는 `pip install tomli` |
| `python3-on-path` | 훅은 PATH의 `python3`를 실행. 3.9+를 PATH 앞에 두거나 재설치해 절대 경로를 기록 |
| `git` | 없음/2.5 미만. 포크된 워커의 `git worktree`와 훅의 저장소 탐색에 필요 |
| `sqlite3` | Python에 sqlite3 모듈 없음. 전체 Python 빌드를 설치 |
| `codex` | `npm i -g @openai/codex` 후 PATH 확인 |
| `codex-feature:hooks` / `codex-feature:multi_agent` | `codex features enable hooks` / `codex features enable multi_agent` (`--no-probe`면 생략) |
| `ocx` (선택) | `run.sh --configure-ocx`에만 필요 |
| `als-on-path` (선택) | `export PATH="$HOME/.local/bin:$PATH"`를 셸 프로필에 추가 |
| `conflict:agent-roles` (선택) | 프로젝트 `.codex/agents/als_*.toml`이 전역 역할을 가림. `als uninstall` 또는 `als uninstall --global`로 하나만 유지 |
| `conflict:versions` (선택) | 프로젝트·전역 버전 불일치. 오래된 쪽을 `als install` 또는 `als install --global`로 재설치 |

하네스 부분은 범위(project/global), 상태 DB 경로, 훅 파일 구성 여부와 관측된 이벤트를 보여줍니다.
configured(설정 존재), observed(콜백 도착), trust(사용자 신뢰), blocking(차단 실효성)은 서로 다르며,
콜백을 봤다고 서빙 모델이나 보안 경계가 검증된 것은 아닙니다.

그 밖의 흔한 오류:
- `Not inside a git project` — 전역 `harness.py`를 직접 실행할 때. 저장소(하위 디렉터리·워크트리 가능)
  안에서 실행하거나 `--project PATH`.
- `Another verification owns the worktree` — 다른 verify가 실행 중. 죽은 프로세스의 잠금이면 자동 해제됩니다.
- `Confirm old workers/verification process stopped` — 옛 워커 중단을 확인한 뒤 `resume --workers-stopped`.
- `Worktree/HEAD differs` — 변경을 검토한 뒤에만 `resume --accept-drift`.
- `DRIFT` / `REFUSED` (설치기) — 출력된 명령대로 확인 후 `--adopt` 또는 `--force-uninstall`.

## 검증 범위

유닛 테스트는 합성 훅 이벤트·로그, 임시 Git 프로젝트, 실제 로컬 검사 명령만 사용합니다.
사용자의 Codex 빌드/제공자에서 유료 워커를 실행한 결과가 아닙니다. 하네스는 로컬 단일
파일시스템용이며 네트워크 파일시스템의 분산 잠금이 아닙니다.
공식 연결 계약: https://developers.openai.com/codex/hooks (2026-10-11 확인).
