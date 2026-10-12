# 실전 팀 구성 예시

각 예시는 작업에 맞는 실행 모드를 고르는 기준과 Harness v2 문법의 기본 형태를 보여 준다. 모드별 정의는 `execution-modes.md`, 워크플로 스크립트 작성법은 `workflow-recipes.md`에서 확인한다.

---

## 예시 1: 종합 조사 팀 — 워크플로 조율

**구성:** 분산·통합(팬아웃/팬인) + 적대적 검증

**선택 이유:** 조사할 관점을 미리 나열할 수 있고, 주장마다 검증하는 절차를 코드로 정할 수 있다.

```
[메인] 사전 조사: 조사 관점 확정(공식 자료·언론·커뮤니티·배경)
     → _workspace/00_flow_args.json에 {axes, topic, ws} 기록
     → als-flow run scripts/research.py
         phase '조사': flow.pipeline(axes, lambda axis: flow.agent(..., schema=FINDINGS))
         phase '검증': 주장별 적대적 검증 에이전트 실행 → 과반이 confirmed로 판정한 주장만 통과
         phase '종합': 누락 검토자 1명 실행 → 빠진 관점이 있으면 추가 조사
     → 메인이 반환된 구조화 결과로 종합 보고서 작성
```

조사 원칙과 구조화 출력 형식은 `.codex/agents/researcher.toml`에, 반박을 우선하는 검증 원칙은 `.codex/agents/fact-checker.toml`에 정의한다. 워크플로에서는 `agent_type`으로 두 에이전트를 지정한다.

서로 충돌하는 정보는 한쪽을 임의로 버리지 않는다. 반환 스키마에 출처와 함께 담는다.

## 예시 2: SF 소설 집필 팀 — 지속형 에이전트 중심의 혼합 모드

**구성:** 파이프라인 + 생성·검증

**선택 이유:** 세계관, 인물, 줄거리가 서로 어긋나지 않도록 실시간으로 조정해야 하므로 각 전문가가 이전 대화 맥락을 기억해야 한다. 반면 검토자는 서로 독립된 관점으로 결과만 전달하면 되므로 서브에이전트로 충분하다.

```
1단계(지속형 에이전트): als-agent start worldbuilder --type worldbuilder "..."
               + als-agent start character-designer --type character-designer "..."
               + als-agent start plot-architect --type plot-architect "..."
               → als-task add(세계관·인물·줄거리, 서로의 의존 관계 명시)
               → 리더가 중계: worldbuilder가 사회 구조 확정
                 → als-agent send character-designer "..." --from worldbuilder로 전달
                 → 인물의 직업군이 세계관과 충돌하면 als-agent send로
                   worldbuilder에 조정 요청
                 이전 맥락이 남아 있으므로 "아까 정한 계급 구조에서
                 상인 계층만 수정"처럼 일부만 고치라고 지시할 수 있다.
2단계(서브에이전트): spawn_agent(agent_type: "prose-stylist") 한 번 호출
               → _workspace/에 저장된 산출물 세 개를 읽고 집필
3단계(서브에이전트 병렬): science-consultant + continuity-manager를 한 턴에 spawn_agent로 각각 검토
4단계(지속형 에이전트): spawn_agent로 한 번만 호출한 prose-stylist에는 als-agent send를 보낼 수 없다.
                 2단계에서 als-agent start로 이름을 붙여 실행했다면 검토 결과를 반영하라고 지시할 수 있다.
                 수정이 반복될 것으로 보이면 처음부터 이름을 붙여 실행한다.
```

수정 요청을 다시 보낼 가능성이 있는 에이전트는 처음 실행할 때부터 `als-agent start`로 이름을 붙인다. 한 번만 호출한 에이전트는 이전 대화 맥락을 이어서 사용할 수 없다.

## 예시 3: 종합 코드 검토 — 워크플로 조율

**구성:** 분산·통합 + 적대적 검증

**선택 이유:** 보안·성능·구조·테스트처럼 검토 관점을 미리 정할 수 있고, 찾은 항목을 각각 다시 검증하는 절차도 코드로 표현할 수 있다.

```python
# 관점별 검토 → 찾은 항목마다 적대적 검증
# 전체 검토를 기다리지 않는다. 보안 검토가 끝나면 성능 검토가 진행 중이어도
# 보안 영역에서 찾은 문제를 바로 검증한다.
import json

from als_flow import Flow

FINDINGS = {'type': 'object', 'required': ['findings'], 'properties': {
    'findings': {'type': 'array', 'items': {'type': 'object',
        'required': ['title', 'file', 'evidence'], 'properties': {
            'title': {'type': 'string'}, 'file': {'type': 'string'},
            'evidence': {'type': 'string'}}}}}}
VERDICT = {'type': 'object', 'required': ['status', 'reason'], 'properties': {
    'status': {'type': 'string', 'enum': ['confirmed', 'refuted', 'uncertain']},
    'reason': {'type': 'string'}}}
DIMENSIONS = [
    {'key': 'security', 'prompt': '보안 관점에서 검토하라.'},
    {'key': 'perf', 'prompt': '성능 관점에서 검토하라.'},
    {'key': 'arch', 'prompt': '구조 관점에서 검토하라.'},
    {'key': 'test', 'prompt': '테스트 관점에서 검토하라.'},
]

flow = Flow(name='code-review', phases=['검토·검증'], workdir='.', concurrency=4)


def review(d):
    r = flow.agent(d['prompt'], label=f'검토:{d["key"]}', schema=FINDINGS)
    if r is None:
        return None
    verdicts = flow.parallel([
        (lambda f=f: flow.agent(
            '다음 발견을 검증하라. 근거가 충분하면 confirmed, 명백히 반박되면 refuted, '
            f'판단하기 어려우면 uncertain으로 판정하라: {json.dumps(f, ensure_ascii=False)}',
            label=f'검증:{f["file"]}', schema=VERDICT))
        for f in r['findings']])
    return [{**f, 'verdict': v} for f, v in zip(r['findings'], verdicts) if v is not None]


with flow.phase('검토·검증'):
    results = flow.pipeline(DIMENSIONS, review)
confirmed = [f for group in results if group is not None for f in group
             if f['verdict']['status'] == 'confirmed']
flow.done({'confirmed': confirmed})
```

검토자끼리 `als-agent send`로 발견을 공유하는 지속형 팀으로 만들 필요는 없다. 위 예시처럼 발견한 항목 한 건만으로 검증할 수 있으면 해당 항목의 근거를 프롬프트에 충분히 담아 곧바로 검증한다. 다른 관점의 발견과 비교해야 한다면 전체 검토 결과를 모은 뒤 동기화 장벽을 두고 검증한다. 설계 방향을 두고 토론해야 하는 등 실시간 대화가 꼭 필요한 경우에만 지속형 에이전트를 쓴다.

## 예시 4: 대규모 코드 마이그레이션 — 지속형 감독자 협업 또는 워크플로 조율

**구성:** 미리 나눌 수 있으면 분산·통합, 진행 중 다시 나눠야 하면 감독자

**선택 기준:** 작업 묶음을 미리 나눌 수 있는지에 따라 실행 모드를 고른다.

작업 묶음을 미리 정할 수 있으면 워크플로를 쓴다.

```python
import json
from pathlib import Path

from als_flow import Flow

# 사전 조사로 복잡도를 추정한 뒤 작업 묶음을 확정한다. 메인 에이전트는 실행 전에 묶음마다
# `git worktree add`로 격리 작업 트리를 만들고 그 절대 경로를 worktree에 적는다.
# 예: [{'id': 'b1', 'files': [...], 'worktree': '/abs/path/wt-b1'}, ...]
ARGS = json.loads(Path('_workspace/00_flow_args.json').read_text(encoding='utf-8'))
MIGRATION_RESULT = {'type': 'object', 'required': ['changedFiles'], 'properties': {
    'changedFiles': {'type': 'array', 'minItems': 1, 'uniqueItems': True,
                     'items': {'type': 'string', 'minLength': 1}}}}
MIGRATION_VERDICT = {'type': 'object', 'required': ['status', 'reason'], 'properties': {
    'status': {'type': 'string', 'enum': ['confirmed', 'refuted', 'uncertain']},
    'reason': {'type': 'string'}}}

flow = Flow(name='code-migration', phases=['전환·검증'], workdir='.', concurrency=4)


def migrate_then_verify(b):
    r = flow.agent(
        f'다음 작업 묶음을 마이그레이션하고 실제 변경 파일 목록을 반환하라: {", ".join(b["files"])}',
        agent_type='migrator', cwd=b['worktree'], label=f'전환:{b["id"]}',
        schema=MIGRATION_RESULT)
    if r is None:
        return None
    verdict = flow.agent(
        f'격리 작업 트리 {b["worktree"]}에서 마이그레이션 대상과 실제 변경 파일을 대조해 '
        f'누락과 오류를 검증하라. 원래 대상: {", ".join(b["files"])}. '
        f'실제 변경: {", ".join(r["changedFiles"])}',
        agent_type='qa-inspector', cwd=b['worktree'], label=f'검증:{b["id"]}',
        schema=MIGRATION_VERDICT)
    return {**r, 'worktreePath': b['worktree'], 'verdict': verdict}


with flow.phase('전환·검증'):
    migrated = flow.pipeline(ARGS['batches'], migrate_then_verify)
confirmed = [r for r in migrated if r is not None
             and r['verdict'] is not None and r['verdict']['status'] == 'confirmed']
flow.log(f'작업 묶음 {len(ARGS["batches"])}개 중 {len(confirmed)}개가 검증을 통과했습니다.')
flow.done({'confirmed': confirmed})
```

적대적 검증 에이전트에는 마이그레이션 결과가 있는 격리 작업 트리 경로를 반드시 전달한다. 워크플로가 끝나면 메인 에이전트가 `confirmed` 결과의 `worktreePath`를 하나씩 확인해 변경을 기준 브랜치에 병합하거나 필요한 커밋만 선별 적용한다. 충돌이 생기면 다음 작업 트리를 합치기 전에 해결하고, 통합 테스트를 통과한 뒤에만 다음 변경을 적용한다. `refuted`나 `uncertain` 결과는 병합하지 않고 누락 사유를 보고한다.

진행 상황에 따라 작업을 다시 나눠야 하면 지속형 에이전트를 쓴다.

```
리더가 als-task add로 작업 묶음 등록(의존 관계 포함)
→ als-agent start migrator-1 --type migrator "..." (migrator-2, migrator-3도 같은 방식, 동시 실행 최대 6)
→ als-task list / als-agent show로 완료를 확인할 때마다 결과 확인
→ 실패한 작업은 als-agent send로 원인을 확인한 뒤 als-task update로 다시 배정
→ 모두 끝나면 통합 테스트
```

## 예시 5: 웹툰 제작 — 지속형 생성자와 단발 검토자

**구성:** 생성·검증

**선택 이유:** 생성자 한 명과 검토자 한 명만 필요하다. 검토 결과를 생성자에게 최대 두 번 돌려보내면 되므로 가벼운 혼합 모드로 충분하다.

```
1단계: als-agent start artist --type artist "..." → 패널 생성 → _workspace/panels/
2단계: spawn_agent(agent_type: "webtoon-reviewer", 지시: "패널을 검토하라") 한 번 호출 → PASS/FIX/REDO 판정
       → _workspace/review_report.md
3단계: REDO 판정을 받은 패널만 als-agent send artist "..."로 재생성 지시
       최대 두 번 반복한다. artist가 이전 맥락을 기억하므로
       "3번 패널의 구도만 수정"처럼 범위를 좁혀 지시할 수 있다.
재시도 방침: 두 번 수정해도 통과하지 못하면 미해결 상태와 원인을 사용자에게 알린다.
             전체 패널의 50% 이상이 REDO이면 사용자에게 프롬프트 수정을 제안한다.
```

---

## 산출물 저장 방식

- **에이전트 정의:** `프로젝트/.codex/agents/{name}.toml`에 만든다. 핵심 역할, 작업 원칙, 입력·출력 규칙, 재호출 방법, 오류 처리, 협업 방법을 반드시 적는다. 지속형 에이전트에는 통신 규칙을, 워크플로에서 쓸 에이전트에는 구조화 출력 형식을 추가한다.
- **스킬:** `프로젝트/.agents/skills/{name}/SKILL.md`에 만들고, 필요하면 `references/`와 `scripts/`를 둔다.
- **오케스트레이터:** 실행 모드를 반드시 적는다. `orchestrator-template.md`의 템플릿을 사용한다.
- **중간 산출물:** `_workspace/{phase}_{agent}_{artifact}.{ext}` 형식으로 저장하고 검증이 끝난 뒤에도 남긴다.
