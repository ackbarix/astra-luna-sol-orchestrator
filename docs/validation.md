# 실행 증거와 품질·시간·사용량 비교

## 1. 서로 다른 세 가지 검증

- 설치 테스트: 파일 보존, TOML 병합/복원, 역할 설정. 모델 실행 증거가 아니다.
- 라이브 smoke: 역할을 실제로 선택하고 응답을 받았는지. 코딩 품질 벤치마크가 아니다.
- 작업 비교: 같은 요구사항과 코드에서 완성도·완료 시간·재작업·전체 사용량을 비교한다.

코드가 실제로 동작하지 않았는데 성공률·절감률을 주장하지 않는다. 역할 TOML이나 모델의
자기소개도 실제 서빙 모델의 증거가 아니다.

## 2. 라이브 smoke는 사용자 동의 후 별도로

smoke 지시문과 `check-runtime.py`의 설정 검사는 **프로젝트 모드 설치**(`als install PROJECT`)를
기준으로 합니다(`.codex/config.toml`, `.codex/agents/`, `.astra-luna-sol/VERSION`을 읽음).
전역 설치만 있는 저장소에서는 폐기 가능한 테스트 저장소에 프로젝트 모드로 설치해 수행하세요.

1. 사전 점검(모델 호출 없음):
   ```bash
   cd ~/work/smoke-fixture
   python3 .astra-luna-sol/check-runtime.py --probe
   python3 .astra-luna-sol/check-runtime.py --smoke-prompt > /tmp/als-smoke.txt
   ```
2. `codex --sandbox read-only`로 **새 세션**을 열고 `/tmp/als-smoke.txt` 지시를 붙여 넣습니다.
   애플리케이션은 수정하지 않지만 HEAD와 4개 워커 호출 비용이 발생합니다. 일반 작업의
   '필요한 워커만 호출' 원칙과 구분되는 의도적 검증입니다.
3. 세션 ID를 확인하고 사용량을 내보낸 뒤 대조합니다.
   ```bash
   python3 .astra-luna-sol/token_usage.py --list --cwd "$PWD" --limit 5
   python3 .astra-luna-sol/token_usage.py --root SESSION_ID --format json --strict > /tmp/als-smoke.json
   python3 .astra-luna-sol/check-runtime.py --report /tmp/als-smoke.json
   ```
   `check-runtime.py`는 문제가 있으면 종료 코드 2입니다. OpenCodex 요청 로그와도 대조합니다.

`check-runtime.py --report`는 **선택한 lineage의 `warnings`만** 판정에 씁니다. `index_warnings`와
smoke 세트 밖의 추가 역할은 `warnings` 메모로만 남기고 실패시키지 않습니다. Python 3.9/3.10에
`tomli`가 없으면 문자열·불리언·정수만 읽는 최소 TOML 대체 파서를 쓰고 그 사실을 경고합니다.

실행되지 않은 워커, 가용하지 않은 역할, 요청/실행 모델 차이, 로그 누락은 실패 또는
미확인으로 남긴다. 스레드 시작/종료 시간의 단순 중첩을 실제 병렬 계산으로 단정하지 않는다.

## 사용량 리포터 (`token_usage.py`)

`als usage ARGS`는 현재 저장소를 `--cwd`로 넘겨 `token_usage.py`를 실행합니다.
프로젝트 모드의 `usage.sh`도 같습니다. 세션 로그는 읽기만 합니다.

| 옵션 | 의미 |
|---|---|
| `--list` / `--root ID` / `--latest` / `--latest-with-workers` / `--day YYYY-MM-DD` | 선택 방식(하나 필수). `--root`는 고유 접두사, 모든 날짜의 lineage 포함 |
| `--date YYYY-MM-DD` | **root 시작일**로 세션을 고르고 lineage 전체를 유지 |
| `--day YYYY-MM-DD` | 그날 `--timezone` 기준 **응답 타임스탬프**만 집계. 그날 응답이 없는 스레드는 제외. `--date`와 함께 쓸 수 없음 |
| `--cwd DIR` | root 세션의 cwd가 DIR 또는 그 **하위 디렉터리**인 세션만 |
| `--include-archived` | `--sessions-dir` 옆의 `archived_sessions`도 검색(기본 `$CODEX_HOME/archived_sessions`) |
| `--include-events` | 스레드별 응답 이벤트 상세를 포함 |
| `--include-guardian` | guardian 스레드도 포함 |
| `--strict` | 선택한 리포트에 `warnings`가 있으면 종료 코드 3 |
| `--sessions-dir`, `--timezone`, `--limit`, `--format md\|json` | 입력 위치, 시간대, 목록 개수, 출력 형식 |

`warnings`와 `index_warnings`는 다릅니다.
- `warnings`: **선택한 lineage**의 파일에서 나온 문제(손상·잘림·누락 메타데이터, 너무 큰 줄 등).
  완전성 판단과 `--strict`에 영향을 줍니다.
- `index_warnings`: 선택 밖의 관련 없는 로그 파일, 또는 `archived_sessions_dir_missing` 같은 색인
  진단입니다. 보고만 하고 완전성이나 `--strict`에 영향을 주지 않습니다.
- 아카이브를 검색하지 않았는데 root 메타데이터가 없으면
  `archived_sessions_not_scanned:missing_member_may_be_archived`가 붙습니다. `--include-archived`로 다시 확인하세요.

## 3. 최초 비교안

A(기존): Astra Medium 총괄, Luna 전체 Max, Sol High 구현/Medium 검수.
B(0.2): Astra Medium 총괄, Luna 구현 Max, 탐색/자료조사/기본 테스트 Medium,
필요한 경우 전용 깊은 테스트/검수 역할. 동시 상한은 둘 다 6.

동시에 HEAD·병렬 수·프롬프트를 모두 바꾸지 않는다. '절감 확정'이 아니라 가설 검증이다.
별도의 폐기 가능한 worktree/테스트 프로젝트에서 기존 설치본 A와 새 설치본 B를 준비한다.
관리 파일을 수동 변경하면 무결성 검사에 걸릴 수 있으므로 운영 프로젝트를 실험에 쓰지 않는다.

대표 작업 3종(단일 파일 수정, 여러 파일 기능, 복잡한 버그)을 동일한 시작 커밋과 프롬프트로
각 2회씩 비교하면 12회다. 순서를 번갈아 수행하고 Codex/OpenCodex 버전·캐시 상태·동시
세션 여부를 기록한다. 실제 데이터베이스/주문/배포가 아니라 fixture만 사용한다.

| 작업/회차 | 구성 | 요구사항·테스트 통과 | 전체 완료 시간 | 재작업 횟수 | HEAD 사용량 | 워커별 모델/effort 사용량 | 누락/미분류 |
|---|---|---|---|---|---|---|---|
| 예시 작업 | A 또는 B | 실제 확인 | 실제 측정 | 실패 포함 | 캐시/비캐시/출력 분리 | 모든 재시도 포함 | warnings 기록 |

기록 JSON에는 코드/프롬프트 원문을 내보내지 않지만 디렉터리와 세션 메타데이터가 포함된다.
공유 전 경로를 확인한다. 실제 개발 소스나 세션 원문을 GitHub에 자동 업로드하지 않는다.

## 4. 채택 기준

품질과 요구사항 충족이 유지되면서 비용 또는 완료 시간이 좋아지는 구성을 채택한다.
Medium 탐색에서 재조사·누락이 늘면 그 역할은 Max를 유지하거나 계약 범위를 개선한다.
낮은 단가, 적은 토큰, 빠른 첫 응답만으로 전체 작업 효율을 판단하지 않는다.

API 기준 비용은 캐시/비캐시/출력 단가와 부가비용을 적용해야 한다. ChatGPT 구독 한도는
API 가격과 동일하지 않다. 검증되지 않은 가격을 이 리포터에 고정하지 않았다.
미분류 누적 로그가 큰 세션은 모델별 비용 비교에서 제외하거나 보수적 하한으로만 해석한다.

## 공식 참고

- https://developers.openai.com/codex/multi-agent/
- https://developers.openai.com/codex/config-reference/
- https://developers.openai.com/api/docs/guides/reasoning
- https://developers.openai.com/api/docs/guides/token-counting

확인 시점: 2026-10-09. 문서와 CLI 기능은 변경될 수 있으므로 로컬 버전/실제 도구가 우선한다.
