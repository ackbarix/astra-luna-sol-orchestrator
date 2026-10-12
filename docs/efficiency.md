# ALS 0.5.0 — 호출·맥락·추론 수준·실측 최적화

설정의 목표는 최저 토큰 숫자가 아니라 **검증된 작업 하나를 끝내는 전체 사용량과 시간**입니다.
Astra Medium 총괄, Luna Max 역할, Sol 6.1 High/Medium은 유지합니다. 별도 감독 AI,
자동 모델 평가, 클라우드 업로드는 추가하지 않습니다. 실제 절감률은 아직 측정하지 않았습니다.

## 적용 범위

`efficiency` 확장이 없는 기존 JSON 계획도 그대로 실행합니다(0.5부터 런타임 전용 task 키가
들어 있으면 거절). 새 제한은 `prepare`로 만든
`efficiency.version=1` 계획에만 적용합니다. 관리하지 않는 작업은 스킬의 지시를 따르며
코드가 모든 호출을 강제 차단하지는 않습니다. Observe는 경고, Guarded는 지원되고 신뢰된
PreToolUse 경로의 거절 결정입니다. 훅이 우회되거나 꺼져 있으면 강제 효과도 보장되지 않습니다.

아래 명령은 `als`로 적었습니다. 프로젝트 모드에서는 `python3 .astra-luna-sol/harness.py`와 같습니다.

### 기본 정책

- 사소한(low-risk + trivial) 작업은 HEAD가 직접 처리합니다. 독립 검토 등 특별한 이유가
  있으면 구체적인 `delegation_reason`으로 설명합니다. 테스트 이름만 붙여 무조건 호출하지 않습니다.
- 탐색/자료 조사/테스트 실행/일반 구현/테스트 설계는 각 Luna 역할(Max)을 고릅니다.
  복잡한 핵심 작업은 Sol High, 검수는 위험도에 따라 Sol Medium/High를 고릅니다.
- 읽기 전용 kind(exploration, research, review)는 난도·위험도와 관계없이 **항상 읽기 전용 역할**에
  남습니다. 복잡하거나 고위험인 탐색·조사에는 역할을 올리지 않고 준비된 task에
  `recommend_escalation` 메모(명시적 `als_reviewer_deep` override 또는 HEAD 검토 권고)를 붙입니다.
  이 값은 매번 다시 계산하며 입력으로 넣은 값은 무시합니다. 쓰기 가능한 역할로의 override는 거절합니다.
  (읽기 전용은 역할 지시이며, 하위 에이전트는 부모 샌드박스를 물려받습니다.)
- 역할이 이미 명시됐는데 권장 역할과 다르면 임의 변경하지 않고 설명된
  `role_override_reason`을 요구합니다. 고위험 작업의 경량 역할 하향은 거절합니다.
- `balanced`: 동시 3개, 작성자 2개, 총 12회 생성. `lean`: 1/1/6. `throughput`: 6/2/24.
  모두 시작값일 뿐 최적 벤치마크가 아닙니다. 3개를 넘기려면 `parallel_reason`이 필요합니다.
  고위험 쓰기가 있는 계획은 작성자 1명부터 운영합니다. Codex 전체 상한 6개는 바꾸지 않습니다.
- 프로필 값은 **실제 상한**입니다. 초안의 `efficiency.max_concurrent`·`max_writers`·`max_total_spawns`는
  상한을 낮출 수만 있고, 더 큰 값은 상한으로 잘리며 `caller_limits_capped`에 기록됩니다. 준비된 계획을
  손으로 고쳐 상한(또는 고위험 단일 작성자)을 넘기면 `start`의 `validate`가 거절합니다. `prepare`를 다시
  실행하세요.
- 신규 위임에는 명시 예산의 20%를 마무리 여유로 남깁니다. 실제 과금/생성 중 응답의
  하드 상한은 아니며, HEAD의 미래 사용량을 자동으로 중단하지 않습니다.
- 예산·추정·이력은 **counted tokens = 비캐시 입력 + 출력**으로 셉니다. 캐시 입력은 제외합니다.

## 계획 예시 — 준비 단계는 모델을 호출하지 않습니다

프로젝트에서 초안을 `/tmp/als-draft.json` 등에 저장합니다. 목표와 검증 명령은 실제 작업에
맞게 바꿔야 합니다. 아래 검증은 예시이며 설치기가 자동 실행하지 않습니다.

```json
{
  "goal": "선택한 모듈의 오류 수정과 검증",
  "workload": "bounded-fix",
  "purpose": "production",
  "tasks": [{
    "id": "fix", "objective": "이미 재현한 입력 검증 오류를 수정",
    "kind": "implementation", "complexity": "bounded", "risk": "normal",
    "context_files": ["src/validation.py"],
    "known_facts": ["HEAD가 오류 위치를 확인했으므로 전체 저장소 재조사는 필요 없음"],
    "constraints": ["기존 공개 API를 바꾸지 않음"],
    "acceptance": ["잘못된 입력 거절 및 기존 정상 입력 처리 유지"],
    "write_paths": ["src/validation.py"], "resources": [], "depends_on": [],
    "checks": ["unit"], "estimate_tokens": 0
  }],
  "checks": [{"id": "unit", "argv": ["python3", "-B", "-m", "unittest", "discover", "-s", "tests"], "timeout": 120}],
  "criteria": [{"id": "behavior", "description": "요구된 입력 동작을 실제 결과로 확인"}],
  "budget": {"tokens": 0, "seconds": 0}
}
```

```bash
als prepare --plan /tmp/als-draft.json > /tmp/als-prepared.json
# 위 명령이 성공했을 때만 준비된 계획을 검토하고 등록
als start --plan /tmp/als-prepared.json --session 실제세션ID
als task-message --run 실제작업ID --task fix
```

`kind`: exploration, research, test_execution, test_design, implementation, review.
`complexity`: trivial, bounded, complex. `risk`: low, normal, high.
분류는 호출자가 제공하는 정보이며 하네스가 코드 난도를 자동 이해하는 것은 아닙니다.
같은 목표·범위·완료 조건을 복제한 계획은 합치도록 거절합니다. 중복 판정은 정확한 일치만 봅니다:
kind, 정규화한 objective, context_files, write_paths, resources, **checks, constraints**, acceptance,
**risk, complexity, role**이 모두 같아야 중복입니다. 하나라도 다르면 별개의 task입니다.
검수는 구현과 다른 kind입니다.

준비된 계획의 HEAD 작업도 자원 예약을 거칩니다.

```bash
als begin-head --run 실제작업ID --task 직접처리작업ID
# 승인된 작업을 HEAD가 수행한 다음, 실제 결과를 finish로 기록
```

이후 finish → 승인된 verify --execute → accept → assess → complete 흐름은 기존과 같습니다.
완료/취소한 새 작업의 경과 시간은 종료 시점에 고정합니다. cancel은 모델 프로세스 중단 명령이
아닙니다. 취소 뒤에도 실행 중인 연결 워커의 사용량이 도착하면 기록하고 경고합니다.

## 전달 맥락 제한

`task-message`는 소스 파일을 읽어 붙이지 않습니다. 파일 경로(12개 이하), 확인된 사실,
필수 제약·완료 조건을 전달하며 8,000 **문자**를 넘으면 범위를 다시 나누도록 거절합니다.
이것은 토큰 수가 아닙니다. 내용을 몰래 자르지 않습니다. 워커의 2,000문자 요약은 지시이며
API 출력 상한이 아닙니다. 중요한 실패는 숨기지 않고 상세 증거 위치를 참조합니다.

PreToolUse가 실제로 전달한 `fork_context=true`에는 `full_context_reason`을 요구합니다.
도구가 이 필드를 제공하지 않으면 임의 인자를 넣지 않고 `not_exposed_or_unspecified`로
기록합니다. 사용자 승인 없이 hook updatedInput으로 설정을 덮어쓰지 않습니다.
짧은 메시지만으로 기본 시스템 지침·도구 정의·프로젝트 설정까지 없애는 것은 아닙니다.

## 사용량 분석 — 자동으로 기록을 조작하지 않습니다

```bash
als status --run 실제작업ID --compact
als sync-usage --run 실제작업ID
als analyze --run 실제작업ID
# 보관한 run report JSON(schema_version 1 또는 2, `workers` 배열 필수)도 로컬 분석 가능
als report --run 실제작업ID > /tmp/als-run-report.json
als analyze --report-file /tmp/als-run-report.json
```

입력/캐시 입력/출력/추론, 비캐시 입력, HEAD 비중, 응답·재시도 횟수, 전달 메시지 길이와
전체 맥락 상속 요청을 구분합니다. 원문 메시지 대신 길이·해시만 시도 기록에 보관합니다.
캐시는 입력에 포함되고 추론은 출력에 포함됩니다. 총토큰은 비용/크레딧과 동일하지 않습니다.
실패한 시도도 제외하지 않습니다. 기록이 없으면 0원이 아니라 미측정입니다.

완료 후 다른 일을 한 HEAD의 토큰을 이전 작업에 추가하지 않도록 새 작업에 종료 경계를
기록합니다. 따라서 complete 이후의 최종 답변 등은 run 집계에서 제외될 수 있습니다.
연결된 자식의 늦은 결과는 별도 경고와 함께 남깁니다. 순수 활성 시간/청구서가 아닌 하한입니다.
작업별 커서를 분리해 동일 Codex 세션을 여러 관리 작업에서 순차 재사용해도 기록이 섞이지
않도록 합니다. 구형 종료 시점 없는 기록은 미확인으로 표시하며 수치를 소급 수정하지 않습니다.

## 실측 기반 조정 — 표본이 없으면 추측하지 않음

실제 개발이 끝나고 결과·실패·기록 범위를 확인한 뒤만 등록합니다. pass는 completed 작업에만
허용합니다. 실패한 작업도 취소·정리 후 fail로 남겨 선택 편향을 줄입니다.

```bash
als measure --run 실제작업ID \
  --workload bounded-fix --quality pass --ack-recorded-only
als analyze --workload bounded-fix
```

같은 프로젝트·workload, 동일 역할/난도/위험도이며 사용자/HEAD가 검토한 production 작업이
5개 이상 있어야 추정치를 제공합니다. 진단용 작업(purpose=diagnostic)은 제외합니다. 동일 run의
여러 작업을 5번 실험한 것처럼 세지 않습니다.

이력에서 run을 빼는 기준은 **사용량 무결성 문제**뿐입니다.
- run 경고 중 `no response ID`, `not_reported:cached_input_tokens`, `reported_total_differs`로
  시작하는 것은 무해한 것으로 보고 허용합니다. 그 밖의 경고가 있으면 제외합니다.
- claim이 남은 task, HEAD 세션 사용량 없음, 캐시 > 입력 같은 손상된 기록, 요청한 모델·effort와
  기록된 신원이 다른 스레드, 사용량 없이 **성공한** 시도는 제외 사유입니다.
- 스레드에 연결되지 않았거나 사용량이 없는 **실패** 시도는 제외하지 않고 비용 0의 실패로 셉니다.
- 품질 `fail`인 run도 실패로 그대로 셉니다(성공만 고르는 선택 편향 방지).
이 품질 표시는 호출자의 판단 기록이지 독립 평가나 인증된 사용자 신원이 아닙니다.

권고는 같은 역할/kind/난도/위험도 묶음별 counted tokens(비캐시 입력 + 출력)의 관측 P80에
25% 여유를 더한 추정치, 그리고 실패·재시도에 따른 병렬 제한 축소입니다. 실패 비율 40% 이상이면 1,
10% 초과 또는 재시도 run 20% 초과면 2, 그 밖에 실패·재시도가 하나라도 있으면 3으로 제안합니다.
실패·재시도가 전혀 없으면 프로필의 동시 수를 **줄이지 않습니다**.
정확한 실제 소비량이나 인과적인 성능 향상을 보장하지 않습니다. 관측 이력으로 모델을
자동 하향하거나 병렬 상한을 높이지 않습니다. 명시적으로 새 계획에만 적용합니다.

```bash
als prepare --plan /tmp/als-draft.json \
  --profile balanced --use-history > /tmp/als-prepared.json
```

`budget.tokens=0`은 미설정입니다. 실제 사용량이 없다는 뜻이 아닙니다. 명시 예산이 있으면
워커 예상치를 입력하거나 검토된 이력을 사용해야 합니다. 예산 자체는 자동 증액하지 않습니다.
예상 잔액은 이미 관측된 워커 사용량을 제외해 이중 예약을 줄이지만, 아직 기록되지 않은
진행 중 사용량은 알 수 없습니다. 가격표/결제 경로를 추측해 달러 값을 표시하지 않습니다.

## 검증과 한계

단위·통합 테스트는 합성 기록과 임시 Git 프로젝트입니다. 사용자의 운영 코드나 유료 모델을
테스트 과정에서 실행하지 않습니다. 고위험 검수와 필요한 테스트는 토큰 절감을 이유로 없애지
않습니다. 이전 설치·완료·재설치 보호 기능도 유지합니다.

설계 참고(2026-10-11 확인):
- https://developers.openai.com/codex/multi-agent/
- https://developers.openai.com/codex/hooks/
- https://github.com/openai/codex/blob/main/codex-rs/core/src/tools/handlers/multi_agents/spawn.rs

공식 도구 스키마·역할 우선순위가 실제 설치 버전과 다르면 명시적으로 확인해야 합니다.
