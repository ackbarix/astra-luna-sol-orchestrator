# 설치·업그레이드·복구 상세

[README](../README.md)의 빠른 시작으로 충분하지 않을 때 보는 참고 문서입니다. 요구 사항과 환경 변수, 전역/프로젝트 설치 비교, `als` 명령 세부, 이전 버전에서의 업그레이드, 복구 플래그를 다룹니다.

## 요구 사항

설치 전에 바뀔 내용만 보려면 `sh setup.sh --dry-run`(전역) 또는 `als install PROJECT --dry-run`을
실행하세요. 아무것도 쓰지 않습니다.

전역 설치의 모델 기본값은 `$CODEX_HOME/als.config.toml` 프로필로 들어가며 `codex --profile als`
(`als run`)로 적용됩니다. 사용 중인 Codex 버전이 이 프로필을 읽는지는 Codex 안에서 `/status`로
모델이 GPT-6 Astra / Medium인지 확인하세요.

| 항목 | 조건 |
|---|---|
| OS / 셸 | Linux 또는 macOS, POSIX `sh` (dash/bash) |
| Python | 실행 3.9+. 프로젝트 모드의 `config.toml` 병합만 3.11+ 또는 `pip install tomli` 필요. `--global`은 불필요 |
| git | `git worktree` 지원(2.5+). `--path-format`을 쓰지 않고, 2.13 미만이 하위 디렉터리에서 내는 상대 `--git-common-dir`도 `--show-cdup`으로 보정 |
| Codex CLI | `codex` on PATH, `multi_agent` 기능 활성화 |
| OpenCodex (`ocx`) | 선택. `run.sh --configure-ocx`에만 필요 |

| 환경 변수 | 의미 |
|---|---|
| `ALS_PYTHON` | `setup.sh`·`install.sh`·`als`가 쓰는 인터프리터(기본 `python3`) |
| `ALS_HOME` | 전역 도구 위치(기본 `${XDG_DATA_HOME:-~/.local/share}/als`) |
| `ALS_BIN_DIR` | `als` 명령 위치(기본 `~/.local/bin`) |
| `ALS_DEPS_TIMEOUT` | `als doctor`의 외부 프로그램(`git`/`codex`/`ocx`) 프로브당 초(기본 10) |

전역 설치(`setup.sh`, `als install --global`) 때의 `ALS_HOME`과 `ALS_PYTHON`(절대 경로로 변환)은
설치된 `~/.local/bin/als`에 기본값으로 기록됩니다. 이후 변수를 지워도 `als version`·`doctor`·
`install`·`uninstall --global`이 같은 위치와 인터프리터를 씁니다. 실행 시 환경 변수가 있으면 그 값이
우선합니다. `ALS_BIN_DIR`·`CODEX_HOME`은 기록되지 않으므로 바꿔 설치했다면 이후에도 같은 값을 주세요.

## 설치 범위: 전역 vs 프로젝트

| | 전역 (`sh setup.sh`, `als install --global`) | 프로젝트 (`als install PROJECT`) |
|---|---|---|
| 도구 (`als-flow` 등) | `${ALS_HOME:-~/.local/share/als}` | `PROJECT/.astra-luna-sol/` |
| 역할·스킬 | `$CODEX_HOME/agents/als_*.toml`, `$CODEX_HOME/skills/` | `PROJECT/.codex/agents/`, `PROJECT/.agents/skills/` |
| 모델 기본값 | `$CODEX_HOME/als.config.toml` (`codex --profile als`) | `PROJECT/.codex/config.toml`에 가역 병합 |
| 정책 블록 | 없음 (`als harness init`으로 `AGENTS.md` 섹션 추가) | `AGENTS.md` (`--policy-file PATH`) |

- **전역 모드를 권장합니다.** 한 번 설치로 모든 저장소에서 `als run`을 쓰고, 저장소에는
  `als harness init`이 추가하는 `AGENTS.md` 섹션만 남습니다.
- **프로젝트 모드**는 팀이 같은 설정을 저장소에 커밋하거나, 그 프로젝트에서만 Astra를 기본
  모델로 쓰려 할 때 사용합니다. `sh install.sh PROJECT`와 같습니다.

## `als` 명령

```bash
als install [PROJECT|--global] [--dry-run --check --adopt --policy-file PATH]
als uninstall [PROJECT|--global] [--force-uninstall]
als uninstall --hooks-only         # 1.2 이전 릴리스가 이 저장소에 만든 훅 제거
als doctor [--no-probe] [--json]   # 의존성·설치 충돌 점검
als run [CODEX ARGS]               # 전역 설치면 codex --profile als, 아니면 codex
als harness init|diff|apply        # 범용 하네스 적용, 또는 만든 하네스 반영
als usage [--latest|--list|--root ID|--day YYYY-MM-DD|...]   # token_usage.py --cwd <현재 저장소>
als version
```

`PROJECT`를 생략하면 현재 저장소(워크트리면 메인 저장소)입니다. 하위 디렉터리에서 실행해도 됩니다.
`als usage`에 선택 옵션(`--list`, `--root`, `--latest`, `--latest-with-workers`, `--day`)이 없으면
`--latest`를 붙입니다.

`als doctor`는 의존성과 설치 충돌(`als_harness.deps`)을 점검합니다. 저장소의 `.codex/hooks.json`에
1.2 이전 ALS 훅이 남아 있으면 제거 방법을 함께 알려 줍니다.

- `--no-probe`: `codex features list`만 생략하므로 `codex-feature:*` 항목이 빠집니다.
  `git --version`, `codex --version`, `ocx --version`은 그대로 실행합니다(각 `ALS_DEPS_TIMEOUT`초).
- `codex features list`를 실행할 수 없으면 기능 항목은 알 수 없음(`ok: null`, 텍스트 `[unk ]`)이며
  doctor를 실패시키지 않습니다. 시간 초과면 `codex did not answer within Ns; raise ALS_DEPS_TIMEOUT`.
- `--json`: stdout에 객체 하나 `{"deps": {"ok", "items": [{"name", "found", "required", "ok", "hint"}]}}`.
- 종료 코드: 0 필수 항목 정상(경고 허용), 1 필수 의존성 실패, 2 사용법 오류(알 수 없는 옵션, 인터프리터 없음).

검증·사용량 리포트는 [검증 가이드](validation.md)를 보세요.

## 1.1 이하에서 업그레이드

1.2에서 작업 관리 하네스(훅, 파일 예약, 완료 인증, `als start`·`status`·`verify` 등)를 없앴습니다.

- **전역 설치:** `sh setup.sh`(재설치)가 `$ALS_HOME`의 하네스 파일을 지웁니다.
- **프로젝트 설치:** `als install PROJECT`(재설치)가 `.astra-luna-sol/`의 하네스 파일과
  `.codex/hooks.json`의 ALS 훅 그룹을 지웁니다. 사용자 훅은 그대로 둡니다.
- **`--hooks-only` 훅:** 그 저장소에서 `als uninstall --hooks-only`를 실행하세요. 그 전까지 남은 훅은
  실행할 하네스가 없어 아무 일도 하지 않습니다. 등록된 저장소가 남아 있으면 `als install --global --check`가
  구버전(4)으로 알려 줍니다.
- **실행 상태:** `${XDG_STATE_HOME:-~/.local/state}/als/`와 프로젝트의 `.astra-luna-sol/state`·`evidence`는
  사용자 데이터라 지우지 않습니다. 필요 없으면 직접 지우세요.

## 0.2–0.4 프로젝트 설치에서 업그레이드

이전 버전은 프로젝트 모드만 있었습니다. 전역 설치 후 `als doctor`가 프로젝트의
`.codex/agents/als_*.toml`이 전역 역할을 가린다고(`conflict:agent-roles`) 또는 버전이 다르다고
(`conflict:versions`) 경고합니다. 하나만 고르세요.

```bash
cd ~/astra-luna-sol-orchestrator && git pull --ff-only
als uninstall ~/work/my-app --dry-run   # 전역으로 옮기려면: 프로젝트 설치 제거
als uninstall ~/work/my-app
# 또는 프로젝트 모드 유지: 이 릴리스로 재설치
als install ~/work/my-app
```

제거해도 `.astra-luna-sol/state`·`evidence`와 그 ignore 규칙은 남습니다.
0.3/0.4 매니페스트에는 설치 전 `hooks.json` 원본 바이트가 없어서, 그 설치를 업그레이드하면
`hooks.json`은 JSON 내용으로는 같지만 바이트(들여쓰기·키 순서)까지 같지는 않을 수 있습니다.

## 복구와 점검 플래그

| 플래그 | 의미 |
|---|---|
| `--check` | 종료 코드 0 정상, 1 미설치, 2 변경(drift)·거부, 4 구버전(이 릴리스로 재설치) |
| `--dry-run` | 계획만 출력, 쓰기 없음 |
| `--uninstall` | ALS 파일 제거. 이미 없는 파일은 괜찮고, 수정된 파일이 있으면 거부 |
| `--force-uninstall` | 변경된 파일을 `backup-<UTC>/`(프로젝트: `.astra-luna-sol/`, 전역: `$ALS_HOME/`)에 백업 후 매니페스트 경로만 제거 |
| `--adopt` | 매니페스트가 없어졌을 때 이 릴리스와 바이트 단위로 같은 파일을 다시 소유 |
| `--policy-file PATH` | 프로젝트 모드에서 정책 블록을 넣을 저장소 내 파일(기본 `AGENTS.md`) |
| `--skip-hooks` | 호환용으로만 받습니다. 1.2는 훅을 설치하지 않습니다 |

관리 파일 수정·심볼릭 링크는 덮어쓰지 않고 중단하며 해결 명령을 출력합니다. 일반 쓰기
실패는 롤백하고, `hooks.json`은 바이트 단위로 원복합니다(0.5 매니페스트 기준, 0.3/0.4는 위 참고).
전원 차단 원자성은 보장하지 않습니다.
