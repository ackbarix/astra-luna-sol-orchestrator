# Astra–Luna–Sol Orchestrator · 0.5.0

Codex용 개인 GPT 오케스트레이션(역할·스킬)과 경량 작업 하네스입니다.
Astra Medium이 총괄하고 Luna가 실무, Sol이 고난도 구현·독립 검수를 맡습니다.
OpenAI/OpenCodex의 공식 배포물이 아닙니다. 참고 프로젝트:
[donvito/codex-astra-luna-orchestrator](https://github.com/donvito/codex-astra-luna-orchestrator).

## 빠른 시작

```bash
git clone https://github.com/ackbarix/astra-luna-sol-orchestrator.git
cd astra-luna-sol-orchestrator && sh setup.sh   # 전역 설치 + ~/.local/bin/als
als doctor                                      # python3·git·sqlite3·codex 기능 점검
als run                                         # = codex --profile als
# 선택: 하네스 훅이 필요한 프로젝트에서만
cd ~/work/my-app && als install --hooks-only
```

`setup.sh`는 셸 rc 파일과 `$CODEX_HOME/config.toml`을 수정하지 않습니다. `~/.local/bin`이
PATH에 없으면 추가할 `export PATH=...` 한 줄만 출력합니다. 변경 예정 내용만 보려면
`sh setup.sh --dry-run`. 설치기와 `als`는 모델을 호출하지 않습니다.

Codex 안에서는 `/status`로 Astra Medium을, `/hooks`로 ALS 훅 명령을 검토·신뢰합니다.
신뢰 절차는 설치기가 우회하지 않습니다. 작은 수정은 그대로 요청하고, 큰 작업에만 스킬을 씁니다.

```text
$astra-luna-sol
이번 작업을 balanced 준비 계획으로 관리해. 사소한 일은 직접 처리하고,
필요한 워커에게만 짧은 작업 계약을 전달해. 기존 변경을 보존하고 배포는 하지 마.
```

## 요구 사항

| 항목 | 조건 |
|---|---|
| OS / 셸 | Linux 또는 macOS, POSIX `sh` (dash/bash) |
| Python | 실행 3.9+. 프로젝트 모드의 `config.toml` 병합만 3.11+ 또는 `pip install tomli` 필요. `--global`은 불필요 |
| git | `git worktree` 지원(2.5+). `--path-format`을 쓰지 않고, 2.13 미만이 하위 디렉터리에서 내는 상대 `--git-common-dir`도 `--show-cdup`으로 보정 |
| Codex CLI | `codex` on PATH, `hooks`·`multi_agent` 기능 활성화 |
| OpenCodex (`ocx`) | 선택. `run.sh --configure-ocx`에만 필요 |

PATH의 `python3`가 3.9 미만이거나 없으면 설치기는 자신을 실행한 인터프리터의 절대 경로를
훅 명령에 기록합니다.

| 환경 변수 | 의미 |
|---|---|
| `ALS_PYTHON` | `setup.sh`·`install.sh`·`als`가 쓰는 인터프리터(기본 `python3`) |
| `ALS_HOME` | 전역 하네스 위치(기본 `${XDG_DATA_HOME:-~/.local/share}/als`) |
| `ALS_BIN_DIR` | `als` 명령 위치(기본 `~/.local/bin`) |
| `ALS_DEPS_TIMEOUT` | `als doctor`의 외부 프로그램(`git`/`codex`/`ocx`) 프로브당 초(기본 10) |

전역 설치(`setup.sh`, `als install --global`) 때의 `ALS_HOME`과 `ALS_PYTHON`(절대 경로로 변환)은
설치된 `~/.local/bin/als`에 기본값으로 기록됩니다. 이후 변수를 지워도 `als version`·`status`·
`install`·`uninstall --global`이 같은 위치와 인터프리터를 씁니다. 실행 시 환경 변수가 있으면 그 값이
우선합니다. `ALS_BIN_DIR`·`CODEX_HOME`은 기록되지 않으므로 바꿔 설치했다면 이후에도 같은 값을 주세요.

## 역할 구성

| 역할 | 모델 / 추론 수준 |
|---|---|
| HEAD | GPT-6 Astra / Medium |
| als_explorer / als_researcher / als_tester | GPT-6 Luna / Medium |
| als_worker / als_test_designer | GPT-6 Luna / Max |
| als_hard_worker / als_reviewer_deep | GPT-6.1 Sol / High |
| als_reviewer | GPT-6.1 Sol / Medium |

역할 TOML이 모델·추론 수준을 고정합니다. 프롬프트로 Medium 역할을 Max라고 부르지 말고
맞는 역할을 고르세요. 읽기 전용 역할(explorer/researcher/reviewer 계열)의 읽기 전용은
**지시**입니다. Codex 문서에 따르면 하위 에이전트는 부모 세션의 샌드박스를 물려받습니다.

## 설치 범위: 전역 vs 프로젝트

| | 전역 (`sh setup.sh`, `als install --global`) | 프로젝트 (`als install PROJECT`) |
|---|---|---|
| 하네스 | `${ALS_HOME:-~/.local/share/als}` | `PROJECT/.astra-luna-sol/` |
| 역할·스킬 | `$CODEX_HOME/agents/als_*.toml`, `$CODEX_HOME/skills/astra-luna-sol/` | `PROJECT/.codex/agents/`, `PROJECT/.agents/skills/` |
| 모델 기본값 | `$CODEX_HOME/als.config.toml` (`codex --profile als`) | `PROJECT/.codex/config.toml`에 가역 병합 |
| 훅 | 없음. 필요하면 `als install --hooks-only` | `PROJECT/.codex/hooks.json` (`--skip-hooks`로 생략) |
| 정책 블록 | 없음 | `AGENTS.md` (`--policy-file PATH`) |
| 실행 상태 | `${XDG_STATE_HOME:-~/.local/state}/als/<hash>/` | `PROJECT/.astra-luna-sol/state/`, `evidence/` |

- **전역 모드를 권장합니다.** 한 번 설치로 모든 저장소에서 `als run`을 쓰고, 저장소에는
  (훅을 원할 때의) `.codex/hooks.json`만 남습니다. 상태 DB와 검증 로그는 저장소 밖에 둡니다.
- **프로젝트 모드**는 팀이 같은 설정을 저장소에 커밋하거나, 그 프로젝트에서만 Astra를 기본
  모델로 쓰려 할 때 사용합니다. `sh install.sh PROJECT`와 같습니다.
- 훅 명령에는 **프로젝트** 절대 경로가 없습니다. PreToolUse/PostToolUse는 `spawn_agent`에만 매칭합니다.
  - 전역(`--hooks-only`): `python3 "${HOME:-/home/me}/.local/share/als/harness.py" hook`.
    `/home/me` 자리는 설치 때의 홈 절대 경로입니다. HOME이 설정되어 있으면 그 값을, Codex가 HOME 없이
    훅을 실행하면 기록된 경로를 씁니다. `ALS_HOME`이 홈 밖이거나 홈 경로에 따옴표가 필요한 문자가
    있으면 하네스 절대 경로를 그대로 적습니다(`python3 '/opt/als/harness.py' hook`).
  - 프로젝트: `git rev-parse --git-common-dir`(필요하면 `--show-cdup`로 보정)로 메인 저장소를 찾아
    `.astra-luna-sol/harness.py`를 실행하는 한 줄 셸 명령입니다. 하네스가 없거나 저장소 밖이면
    조용히 0으로 끝납니다. 전체 문자열은 [하네스 가이드](docs/harness.md#설치-범위)에 있습니다.

## `als` 명령

```bash
als install [PROJECT|--global] [--hooks-only --dry-run --check --adopt --skip-hooks --policy-file PATH]
als uninstall [PROJECT|--global] [--hooks-only] [--force-uninstall]
als doctor [--no-probe] [--json]   # 의존성·충돌 점검 후 현재 프로젝트 하네스 점검
als run [CODEX ARGS]               # 전역 설치면 codex --profile als, 아니면 codex
als plan new FILE                  # 최소 계획 JSON 템플릿 (검사: python3 -B -m unittest ...)
als usage [--latest|--list|--root ID|--day YYYY-MM-DD|...]   # token_usage.py --cwd <현재 저장소>
als status|start|prepare|verify|finish|complete|pause|resume|cancel|report ...   # 하네스 명령 전달
als version
```

`PROJECT`를 생략하면 현재 저장소(워크트리면 메인 저장소)입니다. 하네스 명령은 프로젝트
설치가 있으면 그것을, 없으면 전역 하네스를 사용합니다. 하위 디렉터리에서 실행해도 됩니다.
`als usage`에 선택 옵션(`--list`, `--root`, `--latest`, `--latest-with-workers`, `--day`)이 없으면
`--latest`를 붙입니다.

`als doctor`는 먼저 의존성(`als_harness.deps`)을 출력하고, 저장소 안이면 이어서 하네스 doctor를
실행합니다(하네스가 `--no-deps`를 지원하면 의존성을 중복 출력하지 않도록 넘깁니다).

- `--no-probe`: `codex features list`만 생략하므로 `codex-feature:*` 항목이 빠집니다.
  `git --version`, `codex --version`, `ocx --version`은 그대로 실행합니다(각 `ALS_DEPS_TIMEOUT`초).
- `codex features list`를 실행할 수 없으면 기능 항목은 알 수 없음(`ok: null`, 텍스트 `[unk ]`)이며
  doctor를 실패시키지 않습니다. 시간 초과면 `codex did not answer within Ns; raise ALS_DEPS_TIMEOUT`.
- `--json`: stdout에 객체 하나 `{"deps": {...}, "harness": {...} | null}`. `deps`는
  `{"ok", "items": [{"name", "found", "required", "ok", "hint"}]}`, `harness`는 하네스 doctor의 JSON이며
  저장소 밖이면 `null`, 0.5 미만 프로젝트 설치면 `{"outdated_project_install": VERSION, "message"}`입니다.
- 종료 코드: 0 필수 항목 정상(경고 허용), 1 필수 의존성 또는 하네스 필수 항목 실패, 2 사용법 오류
  (알 수 없는 옵션, 인터프리터 없음)나 하네스 상태 오류.
- 0.2–0.4 프로젝트 설치(`.astra-luna-sol/VERSION`이 0.5 미만이거나 없음)는 그 하네스를 실행하지 않고
  ``project install X is outdated: run `als install` (upgrade) or `als uninstall` ``을 출력합니다.
  의존성이 정상이면 0입니다.

```bash
als plan new /tmp/plan.json            # 목표·작업·검사를 실제 내용으로 수정
als prepare --plan /tmp/plan.json > /tmp/prepared.json
als start --plan /tmp/prepared.json    # 또는 --plan - (stdin)
als status --format text
```

상세 흐름은 [하네스 가이드](docs/harness.md), 준비 정책은 [효율 가이드](docs/efficiency.md),
검증·사용량 리포트는 [검증 가이드](docs/validation.md)를 보세요.

## 0.2–0.4 프로젝트 설치에서 업그레이드

이전 버전은 프로젝트 모드만 있었습니다. 전역 설치 후 `als doctor`가 프로젝트의
`.codex/agents/als_*.toml`이 전역 역할을 가린다고(`conflict:agent-roles`) 또는 버전이 다르다고
(`conflict:versions`) 경고합니다. 하나만 고르세요.

```bash
cd ~/astra-luna-sol-orchestrator && git pull --ff-only
als uninstall ~/work/my-app --dry-run   # 전역으로 옮기려면: 프로젝트 설치 제거
als uninstall ~/work/my-app
cd ~/work/my-app && als install --hooks-only   # 훅이 필요하면
# 또는 프로젝트 모드 유지: 이 릴리스로 재설치
als install ~/work/my-app
```

제거해도 `.astra-luna-sol/state`·`evidence`와 그 ignore 규칙은 남습니다. 전역 모드는
프로젝트 상태를 읽지 않으므로, 진행 중인 run은 업그레이드 전에 끝내거나 취소하세요.
이전 훅(절대 경로 + `--project`)은 재설치 때 ALS 그룹으로 인식되어 교체됩니다.
0.3/0.4 매니페스트에는 설치 전 `hooks.json` 원본 바이트가 없어서, 그 설치를 업그레이드한 뒤
제거하면 `hooks.json`은 JSON 내용으로는 같지만 바이트(들여쓰기·키 순서)까지 같지는 않을 수 있습니다.

## 복구와 점검 플래그

| 플래그 | 의미 |
|---|---|
| `--check` | 종료 코드 0 정상, 1 미설치, 2 변경(drift)·거부, 4 구버전(이 릴리스로 재설치) |
| `--dry-run` | 계획만 출력, 쓰기 없음 |
| `--uninstall` | ALS 파일 제거. 이미 없는 파일은 괜찮고, 수정된 파일이 있으면 거부 |
| `--force-uninstall` | 변경된 파일을 `backup-<UTC>/`(프로젝트: `.astra-luna-sol/`, 전역: `$ALS_HOME/`)에 백업 후 매니페스트 경로만 제거 |
| `--adopt` | 매니페스트가 없어졌을 때 이 릴리스와 바이트 단위로 같은 파일을 다시 소유 |
| `--policy-file PATH` | 프로젝트 모드에서 정책 블록을 넣을 저장소 내 파일(기본 `AGENTS.md`) |
| `--skip-hooks` | 프로젝트 모드에서 훅 없이 설치(기존 ALS 훅 그룹만 제거). `--hooks-only` 훅이 있으면 거부 |

관리 파일 수정·심볼릭 링크는 덮어쓰지 않고 중단하며 해결 명령을 출력합니다. 일반 쓰기
실패는 롤백하고, `hooks.json`은 바이트 단위로 원복합니다(0.5 매니페스트 기준, 0.3/0.4는 위 참고).
전원 차단 원자성은 보장하지 않습니다.

`--hooks-only` 훅(전역 하네스 호출)이 있는 프로젝트에 프로젝트 설치를 하면 `--skip-hooks`를 주어도
거부합니다. 그 훅은 전역 상태에 기록하므로 프로젝트 DB의 run과 세션이 연결되지 않기 때문입니다.
먼저 `als uninstall --hooks-only`를 실행하세요.

**제거 후 남는 실행 상태.** 전역·`--hooks-only` 범위의 run 상태와 검증 로그는
`${XDG_STATE_HOME:-~/.local/state}/als/<hash>/`(`ALS_STATE_HOME`을 썼다면 그 아래)에 있고,
`als uninstall --global`/`--hooks-only`는 이를 지우지 않습니다. 필요 없으면 직접 지우세요.

```bash
als doctor --no-probe --json | grep -m1 '"state_path"'   # 이 저장소의 상태 DB(<hash>/runs.sqlite)
rm -rf "${XDG_STATE_HOME:-$HOME/.local/state}/als/<hash>"   # 한 저장소
rm -rf "${XDG_STATE_HOME:-$HOME/.local/state}/als"          # 모든 저장소
```

## 한계

- **보안 경계가 아닙니다.** OS 샌드박스가 없고, 파일·자원 예약은 협력적 충돌 제어입니다.
- 읽기 전용 역할은 지시일 뿐 강제되지 않습니다(부모 샌드박스 상속).
- 훅은 실패 시 열린 상태(fail open)입니다. 훅이 꺼졌거나 신뢰되지 않았거나 오류가 나면 Codex는
  도구를 계속 실행합니다. Guarded 모드의 거절도 지원·신뢰된 PreToolUse 경로에서만 유효합니다.
- 토큰 예산은 신규 예약의 소프트 제한(비캐시 입력 + 출력)이며 청구액 상한이 아닙니다.
- 이 릴리스는 **유료 모델로 라이브 검증하지 않았습니다.** 자동 테스트는 합성 로그·임시 Git
  프로젝트·로컬 명령만 사용합니다. 성능 향상이나 토큰 절감률은 측정하지 않았습니다.
- `codex --profile als`가 `$CODEX_HOME/als.config.toml`을 읽는 동작은 사용 중인 Codex 버전에서
  `/status`로 확인하세요.

## 라이선스

[변경 기록](CHANGELOG.md). 프로젝트 본체 MIT([LICENSE](LICENSE)); 원본 기반
`scripts/token_usage.py`는 [Apache-2.0](licenses/Apache-2.0.txt).
