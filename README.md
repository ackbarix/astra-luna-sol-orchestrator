# Astra–Luna–Sol Orchestrator · 0.3.1

프로젝트에서 **`codex`만 실행**하는 개인용 GPT 오케스트레이션입니다.
참고: [donvito/codex-astra-luna-orchestrator](https://github.com/donvito/codex-astra-luna-orchestrator).
OpenAI/OpenCodex의 공식 배포물이 아닙니다. 토큰 절감률이나 최고 성능은 보장하지 않습니다.

## 0.3.1: 재설치 보호 버그 수정

0.3.0 제거 시 작업 기록을 보호하려고 남긴 `.astra-luna-sol/.gitignore`가
이후 설치를 막는 문제를 해결했습니다. 작업 상태나 증거 디렉터리가 남아 있고,
해당 ignore 파일이 배포본과 완전히 같을 때만 재사용합니다. 수정되었거나
다른 사용자 파일은 기존처럼 덮어쓰지 않고 거부합니다.

## 0.3.0: 경량 하네스 추가

모델 구성과 `codex` 실행은 그대로 유지합니다. **SQLite 기반 작업 상태, 파일/공유 자원 예약,
증거 기반 완료 검사, 중단 후 대조·재개, 제한된 재시도 준비, 증분 토큰 예산 관측**이 추가됐습니다.
감독용 AI나 자체 모델 라우터는 추가하지 않았습니다.

```bash
cd ~/astra-luna-sol-orchestrator
git pull --ff-only
sh install.sh /data/reactive-shortterm --dry-run
sh install.sh /data/reactive-shortterm
sh install.sh /data/reactive-shortterm --check
cd /data/reactive-shortterm
codex
```

**새 세션의 `/hooks`에서 ALS 훅을 검토·신뢰하세요.** 설치기가 훅 신뢰를 우회하지 않습니다.
기존 hooks.json의 사용자 항목은 보존합니다. 인라인 `[hooks]`와 충돌하면 `--skip-hooks`로
CLI만 설치하거나 기존 훅 구성을 먼저 정리합니다. 상태·증거 데이터는 Git에 자동 게시하지 않습니다.

```text
$astra-luna-sol
이번 기능을 하네스로 관리해. 계획과 완료 조건을 등록하고,
독립 작업만 위임한 뒤 승인된 테스트의 실행 결과를 남겨줘.
```

Observe(기록·경고)가 기본입니다. Guarded는 실제 훅 콜백 확인 및 사용자 동의 후 선택합니다.
Guarded도 OS 샌드박스나 정확한 청구 상한은 아닙니다. `complete`의 명시적 요청에만 완료
검사를 적용하며, 누락 보완용 Stop 계속 요청은 run당 **최대 한 번**입니다. 무한 반복하지 않습니다.

```bash
python3 .astra-luna-sol/harness.py doctor --probe
python3 .astra-luna-sol/harness.py status
# RUN_ID는 실제 start 결과/상태에서 확인
python3 .astra-luna-sol/harness.py sync-usage --run RUN_ID
python3 .astra-luna-sol/harness.py report --run RUN_ID --format md
```

[하네스 설치·계획 JSON·워크플로·한계](docs/harness.md)를 먼저 읽으세요.
훅은 명령/모델을 자동 호출하지 않습니다. 검증 명령은 사용자가/HEAD가 선언한 argv를
명시적 `verify --execute`로 실행합니다. 테스트 실행에는 별도 OS 격리가 생기지 않습니다.
Git worktree 루트에서 사용하며, 서브모듈·대형 파일 등 지원 범위를 넘으면 인증 대신 오류를 반환합니다.

**검증 범위:** 임시 Git 프로젝트, 합성 Codex 훅/로그, 로컬 검사 명령, 설치/복원 회귀 테스트.
사용자 머신의 유료 Codex 워커·제공자 라우팅·비용 절감률은 아직 실측하지 않았습니다.
Codex 버전/관리 정책이 훅을 지원·허용해야 하며 `doctor`는 이를 완전히 인증하지 않습니다.

## 0.2.0에서 이어지는 설정·리포터

기존 버전의 `[[skills.config]]` 병합 오류를 수정했습니다. 관리 대상 외의 TOML
설정값이 하나라도 바뀌면 설치를 중단합니다. 기존 설치 기록을 읽어 업그레이드하고,
제거 시 기존 모델 설정과 사용자 지침을 복원합니다. 모델 조합을 뒤집기보다는
업무별 추론 수준과 중복 작업 방지 정책을 조정했습니다.

| 담당 | 모델 | 추론 수준 | 역할 이름 |
|---|---|---|---|
| 총괄 | GPT-6 Astra | Medium | HEAD |
| 탐색·자료 조사 | GPT-6 Luna | Medium | als_explorer / als_researcher |
| 기본 테스트 실행 | GPT-6 Luna | Medium | als_tester |
| 일반 구현 | GPT-6 Luna | Max | als_worker |
| 복잡한 테스트 설계 | GPT-6 Luna | Max | als_test_designer |
| 고난도 구현 | GPT-6.1 Sol | High | als_hard_worker |
| 독립 검수 | GPT-6.1 Sol | Medium | als_reviewer |
| 고위험 독립 검수 | GPT-6.1 Sol | High | als_reviewer_deep |

동시 **6개는 상한**입니다. 보통 1–4개의 유용한 독립 작업부터 시작하고, 동시 작성자는
1–2명부터 운영합니다. 같은 파일뿐 아니라 DB·포트·공통 테스트 데이터의 충돌도 피합니다.
별도 검수가 필요하지 않으면 검수 워커를 자동으로 만들지 않습니다.
Medium으로 낮춘 역할의 품질은 동일 작업 비교로 확인해야 합니다. 나빠진 경우에는
역할과 설정을 함께 조정해야 하며, 프롬프트만으로 고정된 역할의 effort를 바꾸지 마세요.

## 설치·업데이트

필요: Linux/macOS, Git, Bash, **Python 3.11+**, Codex CLI, 이미 인증·설정된 OpenCodex.
이 설치기는 모델을 호출하거나 전역 설정·인증을 자동으로 바꾸지 않습니다.

```bash
# 처음 받을 때만
git clone https://github.com/ackbarix/astra-luna-sol-orchestrator.git
cd astra-luna-sol-orchestrator

# 기존 사용자는 설치기 저장소에서 실행
git pull --ff-only
python3 --version
sh install.sh /data/reactive-shortterm --dry-run
sh install.sh /data/reactive-shortterm
sh install.sh /data/reactive-shortterm --check
```

대상 경로는 실제 개발 프로젝트로 바꾸세요. `--dry-run`은 쓰지 않습니다.
설치 후 해당 프로젝트가 Codex에서 신뢰된 상태인지 확인하고 **새 세션**을 실행합니다.

```bash
cd /data/reactive-shortterm
codex
```

Codex의 `/status`에서 Astra Medium을 확인합니다. 큰 작업에서 스킬이 선택되지 않으면
`$astra-luna-sol`을 명시하세요. 기존의 긴 지시문을 여러 개 추가로 붙이지 마세요.
모드와 카탈로그를 변경했으면 실행 중인 기존 세션이 아니라 새 세션에서 확인해야 합니다.

OpenCodex v1이 아직 설정되지 않았다면 아래 명령은 **전역 설정을 변경**합니다.
이미 해당 구성을 사용하는 경우 반복할 필요 없습니다.

```bash
ocx v2 mode v1
ocx v2 threads 6
ocx agent subagents set gpt-6-luna,gpt-6.1-sol,gpt-6-astra
ocx effort set --subagent max
```

`--subagent max`는 상한이며 모든 워커를 Max로 만들지는 않습니다. 제공자/모델 고정
추론 설정이 따로 있으면 역할의 요청값과 다를 수 있으므로 진단과 로그로 확인하세요.
전용 런처도 선택적으로 유지합니다: `bash .astra-luna-sol/run.sh`.
`--yolo`는 승인과 샌드박스를 모두 생략합니다. 별도 격리가 없는 환경에서 권장하지 않으며,
자식 역할에 `read-only`라고 적힌 것만으로 OS 수준 차단을 보장하지 않습니다.

## 설치 안전성·제한

기존 TOML의 모델·에이전트 기본값만 복원 가능한 블록으로 관리합니다. 일반 테이블과
배열 테이블을 구분하고, 관리 대상 외 모든 파싱 값의 보존 및 정확한 복원을 검사합니다.
다중 행 문자열, 지원하지 않는 인라인/dotted 관리 키 등은 수정 대신 거부합니다.
충돌 메시지가 나오면 파일을 지우거나 `--force`로 덮어쓰지 말고 원인을 먼저 확인하세요.

편집된 관리 파일, 경로 이탈, 심볼릭 링크, 쓰기 전 발견한 동시 변경은 거부합니다.
일반적인 쓰기 예외에는 본 작업의 쓰기를 롤백하지만, 전원 손실에 대한 다중 파일
원자성까지 보장하지는 않습니다. 기존 config 권한과 AGENTS.md 사용자 내용을 보존합니다.
애플리케이션 코드, 전역 Codex/OpenCodex 설정, 운영 서비스는 설치기에서 건드리지 않습니다.

## 토큰 사용량: 세션 전체와 하루를 분리

```bash
cd /data/reactive-shortterm
bash .astra-luna-sol/usage.sh --list
bash .astra-luna-sol/usage.sh --latest
# 정확한 비교에는 목록에서 확인한 세션 ID 사용
bash .astra-luna-sol/usage.sh --root 실제세션ID --format json > /tmp/als-session.json
# 응답 시각 기준 한국 날짜 하루 사용량
bash .astra-luna-sol/usage.sh --day 2026-10-08 --timezone Asia/Seoul
```

**0.2의 달라진 옵션 의미:**

- `--root`: 자정을 넘겨 생성된 자식까지 포함한 전체 세션.
- `--latest`: 워커 없는 세션도 포함하여 가장 최근 시작한 세션.
- `--latest-with-workers`: 이전처럼 워커가 있는 최근 시작 세션만 선택.
- `--date`: 파일 폴더 필터가 아니라 HEAD 시작 날짜 필터. 선택된 세션 전체는 유지.
- `--day`: 실제 응답 타임스탬프를 기준으로 하루만 집계. `--date`와 함께 쓰지 않음.
- `--timezone`: 날짜 경계, 기본 UTC. 한국 날짜는 Asia/Seoul.
- `--include-events`: JSON에 응답별 사용량도 포함. 원문 프롬프트/코드는 내보내지 않음.
- `--strict`: 누락·미분류·손상 등 경고가 있으면 종료 코드 3. 일반 읽기/인자 오류는 2.

기본 경로는 `$CODEX_HOME/sessions` 또는 `~/.codex/sessions`입니다. `--sessions-dir`로
다른 위치를 지정할 수 있습니다. 자식 스레드를 날짜 폴더로 잘라내지 않기 때문에
기록이 많으면 스캔 시간이 걸립니다. archived_sessions는 자동 합산하지 않습니다.

리포트는 **응답 시점의 모델 × 추론 수준 × 스레드**로 집계합니다. 같은 Sol 스레드에서
High→Medium으로 바뀌어도 분리됩니다. 중복 응답 ID는 다시 더하지 않으며, 응답 기록과
누적 카운터도 이중 합산하지 않습니다.

구형 누적 카운터에는 상속·재개된 과거 사용량이 섞일 수 있습니다. 따라서 마지막 모델에
몰아주거나 확정 합계에 더하지 않고 **미분류 스냅샷**으로 별도 표시합니다. 일부만 기록된
경우 확정 응답 합계는 전체 사용량의 하한일 수 있습니다. `complete`/`warnings`를 읽으세요.
캐시는 입력의 일부, 추론은 출력의 일부입니다. 각각 다시 더하면 안 됩니다.

이 자료는 **청구서·실제 제공자 라우팅·순수 작업 시간·병렬 실행의 증명은 아닙니다.**
같은 코드/요구사항에서 품질·완료 시간·실패와 재시도까지 비교하는 방법은
[검증·비교 가이드](docs/validation.md)를 참고하세요.

## 실행 점검: 비용 없는 확인과 유료 실험을 구분

```bash
# 설치된 설정/역할만 읽음
python3 .astra-luna-sol/check-runtime.py
# 버전과 OpenCodex 상태를 추가로 읽음. 모델 호출 없음.
python3 .astra-luna-sol/check-runtime.py --probe
# 실제 검증 지시문을 출력할 뿐, 실행하지 않음
python3 .astra-luna-sol/check-runtime.py --smoke-prompt
```

실제 워커 검증은 위 지시문을 **새로운 읽기 전용 Codex 세션**에서 사용자가 실행합니다.
4개 워커의 실제 호출은 사용량이 발생하므로 업데이트/진단 때 자동 실행하지 않습니다.
그 세션의 JSON 리포트를 만든 후 기록된 요청 모델·effort를 확인할 수 있습니다.

```bash
python3 .astra-luna-sol/check-runtime.py --report /tmp/als-session.json
ocx logs --follow
```

검사 성공은 설정과 **기록된 응답 식별값**이 일치한다는 뜻입니다. 실제 제공자 모델은
OpenCodex 요청 로그에서도 별도로 확인해야 합니다. 테스트 환경의 모델 품질 점수로
해석하지 마세요. 이 릴리스의 자동 테스트는 합성 로그와 설치 동작을 검사하며, 사용자
계정의 모델을 실행하지 않습니다.

## 제거

```bash
cd /data/astra-luna-sol-orchestrator
sh install.sh /data/reactive-shortterm --uninstall --dry-run
sh install.sh /data/reactive-shortterm --uninstall
```

원래 있던 config 설정은 복원하고 설치기가 새로 만든 파일만 제거합니다.
전역 OpenCodex 변경은 되돌리지 않습니다. 역할 파일을 수정한 경우 먼저 충돌을 해결하세요.

## 검증·라이선스

`python3 -m unittest discover -s tests -v`와 각 셸 파일의 문법 검사를 CI에서 수행합니다.
[변경 기록](CHANGELOG.md)을 참고하세요. 프로젝트 본체는 MIT, 원본에서 파생한
`scripts/token_usage.py`는 [Apache-2.0](licenses/Apache-2.0.txt)입니다. 설치된 리포터와
함께 해당 라이선스도 복사합니다.
