# Astra–Luna–Sol Orchestrator · 0.4.0

프로젝트에서 `codex`로 실행하는 개인용 GPT 오케스트레이션과 경량 하네스입니다.
Astra Medium 총괄, Luna 실무, Sol 고난도 구현·독립 검수 구조를 유지하며,
**불필요한 호출·맥락 복제·과도한 병렬화를 줄이는 준비 계획과 실행 분석**을 추가했습니다.
실제 성능 향상이나 토큰 절감률은 아직 비교 측정하지 않았습니다.

참고 프로젝트: [donvito/codex-astra-luna-orchestrator](https://github.com/donvito/codex-astra-luna-orchestrator).
OpenAI/OpenCodex의 공식 배포물이 아닙니다.

## 0.4.0 네 가지 개선

| 우선순위 | 구현 |
|---|---|
| 단순 작업의 불필요한 호출 방지 | `prepare`가 선언된 난도·위험도로 HEAD 직접 처리/워커를 구분. 중복 작업과 총 호출 수 검사 |
| 맥락 전달 최소화 | `task-message`가 전체 파일 대신 관련 경로·확인 사실·제약·완료 조건을 생성. 전달 문구 8,000자 상한, 전체 맥락 상속 이유 확인 |
| 난도별 역할 선택 | Luna Medium 탐색·기본 검증, Luna Max 구현·복잡한 테스트, Sol High 어려운 작업. 역할 충돌은 설명된 override 필요 |
| 기록 기반 조정 | `analyze`로 HEAD 비중·캐시/비캐시·재시도 확인. 검토된 실제 작업이 충분할 때만 향후 계획의 예상치와 병렬 수 제안 |

`prepare` 기본 balanced는 동시 3개, 작성자 2개, 총 12회 생성입니다. 작은 작업에는 lean(1/1/6),
독립성이 확실한 큰 작업에는 throughput(6/2/24)을 선택합니다. 3개를 넘으면 `parallel_reason`이 필요합니다.
고위험 쓰기는 작성자 1명부터 시작합니다. **이 숫자는 초기 운영 정책이며 벤치마크로 확인된 최적값이 아닙니다.**

Codex 자체 상한 6개, Observe 기본, v1 모드 및 기존 역할 파일은 유지합니다. 새 정책은 `prepare`로
준비한 계획에만 적용하며, 기존 0.3.x 작업·진행 기록·계획을 소급 변경하지 않습니다.
Observe 훅은 경고를 반환하고, Guarded는 실제 지원·신뢰된 PreToolUse 경로에서만 거절합니다.
관리되지 않은 호출까지 모두 통제하는 별도 실행 엔진이나 보안 격리 도구가 아닙니다.

## 설치·업데이트

Linux/macOS, Git, Bash, **Python 3.11+**, Codex CLI, 이미 인증·설정된 OpenCodex가 필요합니다.
실행 중인 워커를 정리하고 새 세션에서 업데이트를 적용하세요. 설치기는 모델을 호출하거나
전역 인증·제공자 설정·운영 서비스를 변경하지 않습니다.

```bash
# 최초 다운로드만 필요. 기존 저장소가 있으면 clone하지 않습니다.
cd ~
git clone https://github.com/ackbarix/astra-luna-sol-orchestrator.git
```

이미 설치했다면 제거하지 않고 갱신합니다.

```bash
cd ~/astra-luna-sol-orchestrator
git pull --ff-only origin main
sh install.sh /data/reactive-shortterm --dry-run
# 변경 예정 내용을 확인한 뒤
sh install.sh /data/reactive-shortterm
sh install.sh /data/reactive-shortterm --check
cd /data/reactive-shortterm
cat .astra-luna-sol/VERSION
codex
```

목표 경로는 자신의 개발 프로젝트로 변경하세요. 프로젝트를 신뢰한 상태에서 `/status`로 Astra Medium,
`/hooks`로 ALS 명령과 활성·신뢰 상태를 확인합니다. 설치기는 신뢰 검토를 우회하지 않습니다.
하네스가 필요 없는 단순 수정은 그대로 요청하고, 큰 작업에는 `$astra-luna-sol`을 사용합니다.

```text
$astra-luna-sol
이번 작업을 balanced 준비 계획으로 관리해.
사소한 일은 직접 처리하고, 필요한 워커에게만 짧은 작업 계약을 전달해.
중요한 제약과 검증은 생략하지 말고, 결과와 사용량을 끝에 한 번 요약해.
기존 변경사항을 보존하고 배포는 하지 마.
```

## 모델 구성

| 역할 | 모델 / 추론 수준 |
|---|---|
| HEAD | GPT-6 Astra / Medium |
| als_explorer / als_researcher / als_tester | GPT-6 Luna / Medium |
| als_worker / als_test_designer | GPT-6 Luna / Max |
| als_hard_worker / als_reviewer_deep | GPT-6.1 Sol / High |
| als_reviewer | GPT-6.1 Sol / Medium |

역할 TOML이 모델·추론 수준을 고정합니다. 프롬프트로 Medium 역할을 Max라고 부르지 말고
맞는 역할을 선택해야 합니다. 위험한 작업을 토큰 절약만을 이유로 경량 모델에 넘기지 않습니다.

## 실사용과 분석

대부분의 계획·등록·작업 메시지 생성은 스킬을 읽은 HEAD가 CLI로 수행합니다.
상세 JSON과 명령은 [효율 운영 가이드](docs/efficiency.md), 기본 상태/검증/복구는
[하네스 가이드](docs/harness.md)를 보세요.

```bash
# 설정 및 실제 수신한 이벤트 진단 (모델 실행 없음)
python3 .astra-luna-sol/harness.py doctor --probe
# 등록된 작업 목록
python3 .astra-luna-sol/harness.py status
# 작업 하나의 짧은 진행 정보
python3 .astra-luna-sol/harness.py status --run 실제작업ID --compact
# 같은 작업의 사용량 관측·분석
python3 .astra-luna-sol/harness.py sync-usage --run 실제작업ID
python3 .astra-luna-sol/harness.py analyze --run 실제작업ID
# 기존 세션 전체 리포트도 유지
bash .astra-luna-sol/usage.sh --latest
```

분석 명령 자체는 모델을 호출하지 않습니다. 입력의 일부가 캐시이고 출력의 일부가 추론이므로
중복 합산하지 않습니다. 총토큰은 청구액/구독 크레딧과 같지 않습니다. 짧은 워커 메시지라도
시스템 지침·도구 정의·기본 컨텍스트 비용까지 없애지는 못합니다.

실측 조정은 동일 프로젝트·workload의 **검토된 production 작업 5개 이상**을 요구하고,
역할/난도/위험도별 표본이 충분할 때 추정치를 냅니다. 진단 테스트는 제외하며 실패·재시도는 포함합니다.
품질 기록은 사용자/HEAD의 명시적 평가이지 독립적인 자동 채점이 아닙니다.
`prepare --use-history`로 향후 계획에만 적용하며 사용자 예산을 자동 증액하거나 진행 중 설정을 바꾸지 않습니다.
지금까지의 연결 테스트 한두 건만으로 적정 토큰 예산이나 절감률을 확정하지 않습니다.

## 기존 기능과 안전성

SQLite 작업 상태, 자원 예약, 코드 fingerprint와 실제 검증 증거, 제한된 재시도 준비, 완료 검사를 유지합니다.
검증은 선언된 조건에 한정되고 모든 버그 부재를 보장하지 않습니다. 재시도는 새 모델을 자동 호출하지 않습니다.
새 작업의 종료 경계와 작업별 사용량 커서를 추가하여 다른 작업의 토큰 유입을 줄였습니다.
종료 이후 최종 HEAD 답변 등은 작업 합계에서 제외될 수 있고, 늦은 자식 기록은 경고와 함께 남습니다.
구형 종료 경계 없는 데이터는 미확인으로 표시합니다. 항상 관측된 하한이며 청구서가 아닙니다.

기존 Codex 설정과 사용자 훅·AGENTS.md를 보존합니다. 관리 파일 수정·심볼릭 링크·동시 변경은
덮어쓰지 않고 오류로 중단합니다. 0.3.1의 상태 보존 후 재설치 수정도 유지합니다.
`--yolo`는 승인·샌드박스를 생략하며 이 하네스를 보안 경계로 만들어주지 않습니다.
토큰 예산은 신규 예약의 소프트 제한이지 실행 중 모델이나 계정 청구의 하드 상한이 아닙니다.

```bash
# 제거할 때만; 상태·증거 데이터는 남습니다.
cd ~/astra-luna-sol-orchestrator
sh install.sh /data/reactive-shortterm --uninstall --dry-run
sh install.sh /data/reactive-shortterm --uninstall
```

[변경 기록](CHANGELOG.md). 자동 테스트는 합성 로그, 임시 Git 프로젝트 및 로컬 명령을 사용합니다.
사용자 계정의 유료 모델 성능 비교를 자동 실행하지 않습니다.
프로젝트 본체 MIT; 원본 기반 `scripts/token_usage.py`는 [Apache-2.0](licenses/Apache-2.0.txt).
