# Astra–Luna–Sol Orchestrator

**프로젝트에서 `codex`만 실행해 사용하는 개인용 GPT 멀티에이전트 오케스트레이션**

참고 프로젝트: [donvito/codex-astra-luna-orchestrator](https://github.com/donvito/codex-astra-luna-orchestrator).
독립 제작한 프로젝트이며 OpenAI 또는 OpenCodex의 공식 배포물이 아닙니다.

## 역할과 원칙

| 역할 | 모델 / 노력 | 담당 |
| --- | --- | --- |
| HEAD | GPT-6 Astra / medium | 판단, 계획, 통합 및 최종 검증 |
| 병렬 실무 | GPT-6 Luna / max | 탐색, 작은·중간 구현, 테스트, 자료 조사 |
| 고난도 구현 | GPT-6.1 Sol / high | 복잡한 개발과 디버깅 |
| 독립 리뷰 | GPT-6.1 Sol / medium | 고위험 변경의 독립 검증 |

**최대 6개**는 동시 워커 한도이지 호출 목표가 아닙니다. 단순 작업에는 워커를 불필요하게 호출하지 않습니다.
별도 역할 파일(`.codex/agents/als_*.toml`)과 스킬(`$astra-luna-sol`)이 역할을 설정합니다.

## 요구 사항

Linux/macOS, Bash, Git, **Python 3.11+**, Codex CLI, 설정된 [OpenCodex](https://github.com/lidge-jun/opencodex)(`ocx`).
Codex가 해당 프로젝트를 신뢰해야 프로젝트 내 설정이 적용됩니다. 설치 스크립트는 Codex, OpenCodex 또는 인증 정보를 대신 설치하지 않습니다.

## 설치 및 업데이트

```bash
git clone https://github.com/ackbarix/astra-luna-sol-orchestrator.git
cd astra-luna-sol-orchestrator

# 기존 설정에 어떤 파일을 추가/변경할지 먼저 확인
sh install.sh /data/reactive-shortterm --dry-run

# 실제 설치, 설치 무결성 확인
sh install.sh /data/reactive-shortterm
sh install.sh /data/reactive-shortterm --check
```

이미 clone해서 사용 중이라면:

```bash
cd /data/astra-luna-sol-orchestrator
git pull --ff-only
sh install.sh /data/reactive-shortterm --dry-run
sh install.sh /data/reactive-shortterm
```

설치기는 **기존 `.codex/config.toml`을 안전하게 병합**합니다. 모델과 워커 기본값만 관리 구간에 삽입하고 원래 모델 관련 설정을 복원용 주석으로 저장합니다. 나머지 설정, 승인 정책, 샌드박스 설정은 그대로 유지합니다. 다시 설치하거나 제거하면 관리 구간만 갱신·복원하고, 기존 `AGENTS.md`의 사용자 지침도 보존합니다.

다만 여러 줄 문자열, 인라인·중첩 `[agents]` 등 안전하게 합치기 어려운 TOML은 **실패하도록 설계**했습니다. 그런 경우 원본 파일을 변경하지 않으므로, 충돌 내용을 확인하고 수동 병합하거나 명시적인 런처를 사용하세요.

기존 버전에서 이미 관리 중이던 `.codex/config.toml`은 자동으로 설치 기록 형식을 변환하며, 제거하면 설치기가 만든 파일만 삭제합니다. 설치된 파일을 직접 수정했다면 덮어쓰거나 삭제하지 않고 충돌을 알려줍니다.

## 실행: 원본 프로젝트처럼 `codex`만 사용

OpenCodex는 1회 설정이 필요합니다. 이미 v1과 해당 모델들이 설정됐다면 건너뛰세요.

```bash
ocx v2 mode v1
ocx v2 threads 6
ocx agent subagents set gpt-6-luna,gpt-6.1-sol,gpt-6-astra
ocx effort set --subagent max
```

이후에는 해당 프로젝트에서 **그냥 Codex**를 실행합니다.

```bash
cd /data/reactive-shortterm
codex
```

`/status`에서 **GPT-6 Astra / medium**인지 확인하세요. 큰 작업에서 스킬이 자동 선택되지 않으면 Codex에 `$astra-luna-sol`을 입력하세요.
전용 런처는 여전히 선택적으로 사용할 수 있습니다.

```bash
bash .astra-luna-sol/run.sh
bash .astra-luna-sol/run.sh --dry-run
```

**주의:** `--yolo`는 승인 질문뿐 아니라 Codex 샌드박스도 끄므로, 사용자가 별도로 격리한 신뢰할 수 있는 개발 환경에서만 사용하세요.

## 토큰 사용량 분석 — 원본 프로젝트에서 가져온 기능

원본 저장소의 `scripts/token_usage.py`를 Apache-2.0 조건에 따라 수정·포함했습니다. **세션 기록을 읽기만 하며 변경하지 않습니다.**

개발 프로젝트 안에서 바로 사용합니다.

```bash
cd /data/reactive-shortterm

# 이 프로젝트의 Codex 세션 목록
bash .astra-luna-sol/usage.sh --list

# 이 프로젝트에서 마지막으로 워커를 호출한 세션
bash .astra-luna-sol/usage.sh --latest

# 날짜를 제한해서 빠르게 확인
bash .astra-luna-sol/usage.sh --latest --date 2026-10-08

# 세션 ID 앞부분으로 지정
bash .astra-luna-sol/usage.sh --root 01a09b

# 자동화용 JSON
bash .astra-luna-sol/usage.sh --latest --format json
```

리포트에는 **스레드/역할/모델/요청된 추론 수준/응답 수/캐시되지 않은 입력/캐시 입력/출력/추론 토큰/총토큰**과 모델별 합계가 표시됩니다. 추론 토큰은 보통 출력 토큰에 포함되므로 다시 더하지 마세요.

**중요:** 이는 `~/.codex/sessions`에 저장된 값이며 실제 과금액·계정별 사용 한도 차감량·OpenCodex 제공자 측 최종 라우팅을 보증하지 않습니다. 오래된 기록에는 응답별 토큰 로그가 없어 마지막 누적값을 추정에 사용할 수 있습니다. `--latest`는 워커가 생성된 가장 최근 세션을 찾으므로 워커가 없는 세션은 `--list`와 `--root`로 확인하세요.

원본 사용법: [Token Usage 가이드](https://github.com/donvito/codex-astra-luna-orchestrator/blob/main/guides/token-usage.md).

## 실제 라우팅 확인

```bash
ocx v2 status
ocx agent status
ocx logs --follow
```

Codex 내 `/status`는 HEAD 설정을 확인합니다. 실제 서브에이전트 실행 여부와 요청 모델은 스레드와 OpenCodex 로그에서 따로 확인해야 합니다.

## 제거

```bash
cd /data/astra-luna-sol-orchestrator
sh install.sh /data/reactive-shortterm --uninstall --dry-run
sh install.sh /data/reactive-shortterm --uninstall
```

기존 설정의 사용자 소유 값은 복원합니다. **전역 OpenCodex 설정은 자동으로 되돌리지 않습니다.**

## 라이선스

프로젝트 본체는 MIT입니다. 원본 기반 `scripts/token_usage.py`는 별도의 Apache-2.0 라이선스(`licenses/Apache-2.0.txt`)를 따릅니다.
