# ALS Harness 0.3.0 — 설치·사용·한계

ALS는 Codex를 대체하지 않습니다. 모델 선택과 기본 `codex` 실행은 0.2.0 그대로이고,
Python/SQLite가 등록된 개발 작업의 상태·예약·검증·재시도·사용량을 관리합니다.
모델/API/배포를 설치기나 훅이 자동 호출하지 않습니다. 별도 감독 AI도 없습니다.

## 시작

```bash
cd ~/astra-luna-sol-orchestrator
git pull --ff-only
sh install.sh /data/reactive-shortterm --dry-run
sh install.sh /data/reactive-shortterm
sh install.sh /data/reactive-shortterm --check
cd /data/reactive-shortterm
codex
```

Codex 안에서 `/hooks`로 **ALS 훅의 명령·동작을 검토하고 신뢰**해야 합니다.
신뢰 여부는 설치기가 설정하거나 우회하지 않습니다. 설치된 Codex 버전/관리 정책에 따라
훅이 지원되지 않거나 실행되지 않을 수 있습니다. `/status`에서 Astra Medium도 확인합니다.

```text
$astra-luna-sol
이 기능을 하네스로 관리해. 완료 조건과 계획을 먼저 등록하고,
독립적인 작업만 분배해. 승인된 테스트 결과를 남기고 미완료면 이유를 보고해.
```

단순 질문이나 한 줄 수정은 관리 run을 만들 필요가 없습니다. **Observe가 기본**입니다.
Observe에서도 명시적 `complete`는 실패한 검증을 성공으로 인증하지 않습니다.
다만 훅이 모델의 도구 호출을 차단하거나 자동 계속 요청을 만들지는 않습니다.

## 명령 인터페이스

실제 실행기는 `.astra-luna-sol/harness.py`입니다. 전역 `als` 명령은 설치하지 않습니다.

```bash
python3 .astra-luna-sol/harness.py doctor --probe
python3 .astra-luna-sol/harness.py status
python3 .astra-luna-sol/harness.py report --run RUN_ID --format md
```

`doctor --probe`는 버전/기능 목록만 읽습니다. 유료 모델을 실행하지 않습니다.
configured(설정 존재), observed(콜백 도착), trust(사용자 신뢰), blocking(차단 실효성)은
서로 다릅니다. 콜백만 봤다고 제공자 모델 또는 보안 경계가 검증됐다고 표시하지 않습니다.

### 계획 등록

HEAD 또는 사용자가 다음 형태의 JSON 계획을 작성합니다. 명령은 문자열 셸 코드가 아니라
`argv` 배열입니다. **실행될 명령을 검토하세요.** 테스트 코드 자체도 임의의 부작용을 가질 수
있으며 하네스 검증기는 별도의 컨테이너가 아닙니다.

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

`0` 예산은 미설정이며 무한 실행 권한이 아닙니다. 토큰·초 한도는 사용자 상황에 맞춰 정합니다.
토큰 예산은 입력+출력 기준 **소프트 실행 예산**으로 API 요금/구독 크레딧과 같지 않습니다.

```bash
python3 .astra-luna-sol/harness.py start --plan /tmp/als-plan.json --session CODEX_SESSION_ID
```

세션 ID는 SessionStart 훅 안내 또는 실제 Codex 상태에서 확인합니다. 식별자를 추측하지 않습니다.
`CODEX_THREAD_ID`가 실행 환경에 있으면 기본값으로 사용할 수 있지만, 없을 때는 `--session`을
명시하세요. 세션 없이 등록한 run은 CLI로 관리할 수 있지만 훅과 자동 연결되지 않습니다.
계획은 run 시작 때 고정됩니다. 큰 범위 변경은 기존 작업을 명시적으로 정리하고 새 run을 만듭니다.

### 워커와 연결

기존 `spawn_agent`에서 정확한 역할(`agent_type`)을 선택하고, 메시지 첫 줄에 다음을 넣습니다.
새로운 가짜 도구 인자를 추가하지 않습니다.

```text
ALS_TASK=implement
지정된 범위 안에서 로그인 오류를 수정하고, 실제 변경과 검증 결과를 보고해.
```

PreToolUse는 task 계약, 의존성, 파일/공유 자원, 총 6명·작성자 2명, 소프트 예산을 확인합니다.
PostToolUse 결과에 명확한 `agent_id`가 있으면 연결합니다. 다른 형태의 결과는 추측하지 않고
예약을 유지합니다. 필요한 경우 실제 호출 기록으로 수동 `bind`를 수행합니다.

```bash
python3 .astra-luna-sol/harness.py bind --run RUN_ID --call-id TOOL_CALL_ID --thread REAL_AGENT_ID
```

워커 종료 훅은 `awaiting_validation`일 뿐 완료가 아닙니다. 결과를 실제로 검토한 다음 기록합니다.

```bash
python3 .astra-luna-sol/harness.py finish --run RUN_ID --task implement --note "변경 내용 및 근거"
# 훅이 종료를 관측하지 못한 경우에만, 실제 중단을 확인한 뒤 --workers-stopped 추가
python3 .astra-luna-sol/harness.py verify --run RUN_ID          # 명령 미리보기
python3 .astra-luna-sol/harness.py verify --run RUN_ID --execute
python3 .astra-luna-sol/harness.py accept --run RUN_ID --task implement
python3 .astra-luna-sol/harness.py assess --run RUN_ID --criterion behavior --actor HEAD --note "요구사항 확인 근거"
python3 .astra-luna-sol/harness.py complete --run RUN_ID
```

`assess`는 명시적 판단 기록이며 사람의 신원이나 객관적인 정답을 인증하지 않습니다.
`verify`는 종료 코드·시간초과·로그 해시·코드 fingerprint를 기록합니다. 검증 중 코드가 바뀌면
통과로 인증하지 않으며, 이후 코드가 바뀌어도 결과는 stale로 처리합니다. 로그는 로컬 0600,
64KiB 제한, 알려진 토큰 패턴을 마스킹하지만 완전한 비밀정보 제거를 보장하지 않습니다.
완료 검사는 선언한 조건에 한정됩니다. 테스트가 통과해도 모든 버그가 없다는 뜻은 아닙니다.

## Guarded 모드

먼저 Observe 세션에서 SessionStart / PreToolUse / PostToolUse / Stop 콜백을 실제로 확인합니다.
그다음 **별도 새 run**을 다음처럼 명시적으로 시작할 수 있습니다.

```bash
python3 .astra-luna-sol/harness.py start --plan /tmp/als-plan.json --session CODEX_SESSION_ID --mode guarded --ack-hook-limits
```

guarded는 등록된 세션의 지원되는 spawn_agent 호출에 한해 거절 결정을 반환합니다.
`complete`를 요청했는데 조건이 미충족일 때만 **run당 최대 한 번** Stop 계속 요청을 만듭니다.
`stop_hook_active`가 참이거나 예산이 소진되면 더 돌리지 않고 blocked로 인계합니다.
일반 응답·질문·취소에 대해서는 계속 요청을 만들지 않습니다.

**중요:** 훅은 전체 도구/네트워크/파일 접근을 봉쇄하는 보안 시스템이 아닙니다.
파일 예약은 협력적 충돌 제어일 뿐, 셸 쓰기를 OS에서 차단하지 않습니다.
데이터베이스/훅 장애 시 명시적 CLI 완료는 오류로 끝나고, 훅은 실패를 알리지만 Codex 자체는
도구를 계속 실행할 수 있습니다. 상태를 고치기 전 완료 인증이나 자동 복구를 신뢰하지 마세요.
`--yolo`는 하네스를 강화하지 않고 Codex 승인을 해제합니다.

## 재시도·중단·재개

```bash
python3 .astra-luna-sol/harness.py finish --run RUN_ID --task implement --failed --workers-stopped --error-code test_failure --note "확인한 실패 원인"
python3 .astra-luna-sol/harness.py retry --run RUN_ID --task implement --approach "이전과 다른 해결 방법" --role als_hard_worker
python3 .astra-luna-sol/harness.py pause --run RUN_ID --reason "사용자 중단"
python3 .astra-luna-sol/harness.py resume --run RUN_ID --workers-stopped --accept-drift --session NEW_SESSION_ID
```

retry는 task를 pending으로 되돌릴 뿐 새 모델을 자동 실행하지 않습니다. 최초 실행 포함 최대
3회, 같은 코드·오류의 반복 실패나 동일한 재시도 설명은 제한합니다. 중단된 옛 워커를
새 워커와 동시에 실행하지 않도록 실제 종료를 확인해야 합니다. 원격 부작용은 자동 재시도하지 않습니다.

resume은 브랜치·HEAD·코드를 대조하고 새 작업을 준비할 뿐, checkout/reset/stash/commit을
수행하지 않습니다. `--accept-drift`는 실제 변경을 검토한 뒤에만 사용하세요.
`cancel`은 영구 취소 상태이며 자동 재개할 수 없습니다. CLI pause/cancel은 실행 중인 모델을
물리적으로 죽이지 않습니다. 실행 중 턴은 Codex에서 사용자가 중단해야 합니다.
SessionEnd는 자동 pause를 기록합니다. Interrupt 수신기는 구현했지만 Codex 버전별 지원이
달라 기본 hooks.json에는 설치하지 않습니다. 예기치 않은 강제 종료는 resume 시 재조정합니다.

## 사용량과 소프트 예산

```bash
python3 .astra-luna-sol/harness.py sync-usage --run RUN_ID
python3 .astra-luna-sol/harness.py report --run RUN_ID --format md
bash .astra-luna-sol/usage.sh --latest
```

등록된 root/child JSONL만 증분 읽고 기존 0.2 토큰 정규화 코드를 사용합니다.
기본 경로는 `$CODEX_HOME/sessions`. 로그가 제공되지 않으면 unknown/lower-bound이며 0원으로
해석하지 않습니다. 캐시를 입력에, 추론을 출력에 다시 더하지 않습니다. run 이전 기록은 제외합니다.
70%는 경고 단계, 90%는 새로운 작업 직렬화, 100%는 신규 예약 거절 조건입니다.
진행 중 응답·다른 세션·제공자 재시도·청구 요금은 이 장치가 즉시 제한하지 못합니다.
확정 사용량과 예약 추정량을 구분하며, 고난도 작업을 끝낼 여유는 사용자 예산에 포함해야 합니다.

## 설치·제거와 데이터

기존 hooks.json의 사용자 그룹은 보존합니다(논리값 보존, JSON 서식은 재정렬될 수 있음).
ALS가 넣은 정확한 그룹만 업데이트/제거합니다. 인라인 `[hooks]`가 있으면 자동 혼합하지 않고
거부하므로 먼저 정리하거나 `install.sh TARGET --skip-hooks`로 CLI만 설치하세요.
사용자가 편집한 ALS 그룹은 덮어쓰지 않습니다. 훅 신뢰 우회 옵션을 사용하지 않습니다.

state/runs.sqlite와 evidence/는 Git에서 무시하며 자동 푸시·삭제하지 않습니다.
하네스 제거 후에도 이 데이터와 ignore 규칙은 남습니다. 데이터 삭제는 사용자가 별도로 결정합니다.
로컬 SQLite 한 프로젝트에 최적화했으며, 공유 네트워크 파일시스템용 분산 잠금은 아닙니다.
Git worktree 루트에서 관리합니다. 서브모듈·매우 큰 저장소·지원하지 않는 스냅샷에는 검증을 거부합니다.

## 검증 범위

유닛 테스트는 합성 훅 이벤트·로그, 임시 Git 프로젝트, 실제 로컬 검사 명령만 사용합니다.
사용자의 Codex 빌드/제공자에서 유료 워커를 실행한 결과가 아닙니다. Stop/PreToolUse의
실제 지원과 신뢰 상태는 사용자 환경에서 확인해야 합니다. 모델 성능이나 절감률은 보장하지 않습니다.
공식 연결 계약: https://developers.openai.com/codex/hooks (2026-10-11 확인).
