# 워크플로 활용 예시 — 스크립트 기본 틀과 주의할 점

하네스를 워크플로 조율 모드(모드 A)로 만들 때 조율 스킬에 넣을 수 있는 스크립트 패턴을 설명한다. 스크립트는 Python으로 작성해 조율 스킬의 `scripts/`에 두고 `als-flow run SCRIPT.py`로 실행한다.

---

## 목차

1. [기본 틀](#1-기본-틀)
2. [분산 실행(팬아웃) + 적대적 검증](#2-분산-실행팬아웃--적대적-검증)
3. [심사위원단](#3-심사위원단)
4. [새 항목이 없을 때까지 반복(Loop-until-dry)](#4-새-항목이-없을-때까지-반복loop-until-dry)
5. [여러 기준으로 탐색 + 누락 검토자](#5-여러-기준으로-탐색--누락-검토자)
6. [토큰 예산 연동 반복](#6-토큰-예산-연동-반복)
7. [사용자 정의 유형 + 구조화 출력](#7-사용자-정의-유형--구조화-출력)
8. [주의할 점(검증 목록)](#8-주의할-점검증-목록)

이 패턴들은 산출물의 정확성이 중요하거나 사용자가 "철저히", "전수"처럼 넓은 조사를 요청했을 때 쓴다. 그 밖에는 에이전트 수와 반복 횟수를 작게 유지한다.

---

## 1. 기본 틀

실행 가능한 전체 스크립트는 `Flow(...)` 정의로 시작한다. `name`과 `phases`에는 리터럴만 쓰고, `with flow.phase(...)`에 쓴 제목은 `phases`의 항목과 정확히 일치해야 한다. 스크립트에 넘길 입력(항목 목록, 타임스탬프 등)은 메인 에이전트가 실행 전에 `_workspace/00_flow_args.json`에 써 둔다.

```python
import json
from pathlib import Path

from als_flow import Flow

ARGS = json.loads(Path('_workspace/00_flow_args.json').read_text(encoding='utf-8'))
COLLECT_SCHEMA = {'type': 'object', 'required': ['summary'],
                  'properties': {'summary': {'type': 'string'}}}

flow = Flow(name='domain-task', phases=['수집', '검증'], workdir='.',
            budget_tokens=None, concurrency=4)


def main():
    with flow.phase('수집'):
        raw = flow.pipeline(ARGS['items'], lambda item: flow.agent(
            f'...{item}...', label=f'수집:{item}', schema=COLLECT_SCHEMA))
        collected = [r for r in raw if r is not None]
        flow.log(f'{len(ARGS["items"])}건 중 {len(collected)}건을 수집했습니다.')
    with flow.phase('검증'):
        result = collected  # ...
    return {'result': result}


flow.done(main())  # 최종 결과가 메인 에이전트에 전달된다
```

**원칙:**
- **기본적으로 `flow.pipeline()`을 쓰고, 동기화 장벽이 꼭 필요할 때만 `flow.parallel()`을 쓴다.** 다음 단계에서 이전 단계의 결과 **전체**를 서로 비교해야 할 때만 동기화 장벽이 필요하다. 중복 제거, 전체 결과가 0건일 때의 조기 종료, "다른 발견과 비교"하라는 프롬프트가 이에 해당한다.
- 항목 목록의 값을 스크립트에 직접 적지 않는다. 가능하면 메인 에이전트가 사전 조사로 확정한 목록을 입력 파일의 `items`로 전달한다.
- 타임스탬프가 필요하면 입력 파일의 `now`로 전달한다. 스크립트 안에서 `time.time()`이나 `datetime.now()`로 프롬프트를 만들지 않는다.
- 중간에 끝내야 하는 경우가 있으므로 본문을 `main()`에 두고, 반환한 값을 `flow.done()`에 넘긴다.

## 2. 분산 실행(팬아웃) + 적대적 검증

각 관점에서 검토 결과를 수집한 뒤, 결과를 하나씩 검증한다. 다른 관점의 조사가 끝날 때까지 기다리지 않고, 한 관점의 조사가 끝나는 즉시 해당 관점에서 나온 결과를 검증한다. 검토와 검증이 한 `pipeline` 안에서 이어지므로 단계 하나로 묶고, 호출은 `label`로 구분한다.

```python
import json
from pathlib import Path

from als_flow import Flow

ARGS = json.loads(Path('_workspace/00_flow_args.json').read_text(encoding='utf-8'))
FINDINGS = {'type': 'object', 'required': ['findings'], 'properties': {
    'findings': {'type': 'array', 'items': {'type': 'object',
        'required': ['title', 'file', 'evidence'], 'properties': {
            'title': {'type': 'string'}, 'file': {'type': 'string'},
            'evidence': {'type': 'string'}}}}}}
VERDICT = {'type': 'object', 'required': ['status', 'reason'], 'properties': {
    'status': {'type': 'string', 'enum': ['confirmed', 'refuted', 'uncertain']},
    'reason': {'type': 'string'}}}
LENSES = ['정확성', '보안', '재현성']

flow = Flow(name='review-fanout-verify', phases=['검토·검증'], workdir='.', concurrency=4)


def verify(f, verifiers):
    # 검증 에이전트마다 다른 관점을 맡긴다. 과반이 confirmed로 판정해야 통과한다.
    verdicts = flow.parallel([
        (lambda i=i: flow.agent(
            f'{LENSES[i % len(LENSES)]} 관점에서 다음 검토 결과를 반박할 근거를 찾아라. '
            '근거가 충분하면 confirmed, 명백히 반박되면 refuted, 판단하기 어려우면 '
            f'uncertain으로 판정하라: {json.dumps(f, ensure_ascii=False)}',
            label=f'검증:{f["file"]}:{i}', schema=VERDICT))
        for i in range(verifiers)])
    votes = sum(1 for v in verdicts if v is not None and v['status'] == 'confirmed')
    return {**f, 'confirmed_votes': votes, 'verifiers': verifiers,
            'passed': votes * 2 > verifiers}


def review_then_verify(d, verifiers):
    review = flow.agent(d['prompt'], label=f'검토:{d["key"]}', schema=FINDINGS)
    if review is None:
        return None
    checked = flow.parallel([(lambda f=f: verify(f, verifiers)) for f in review['findings']])
    return [c for c in checked if c is not None]


def main():
    verifiers = ARGS.get('verifiers', 1)  # 철저한 검증을 요청받으면 3
    with flow.phase('검토·검증'):
        # 예: [{'key': 'security', 'prompt': '...'}, {'key': 'perf', 'prompt': '...'}]
        results = flow.pipeline(ARGS['dimensions'], lambda d: review_then_verify(d, verifiers))
    done = [r for r in results if r is not None]
    skipped = len(results) - len(done)
    checked = [f for group in done for f in group]
    confirmed = [f for f in checked if f['passed']]
    flow.log(f'전체 {len(checked)}건 중 {len(confirmed)}건이 검증을 통과했습니다. '
             f'검토에 실패한 관점: {skipped}개')
    return {'confirmed': confirmed}


flow.done(main())
```

기본값은 발견한 항목 한 건마다 적대적 검증 에이전트 한 명이다. 검증을 더 엄격하게 하려면 입력 파일의 `verifiers`를 3으로 정해 세 명을 실행한다. 이 가운데 두 명 이상이 근거가 충분하다고 `confirmed`로 판정한 항목만 통과시킨다. 각 에이전트에 정확성, 보안, 재현성처럼 서로 다른 검증 기준을 주면 같은 기준으로 반복해서 검사할 때보다 더 다양한 문제를 찾을 수 있다.

## 3. 심사위원단

가능한 해법이 많은 설계 작업에 쓰는 패턴이다. 서로 독립적인 시안 N개를 만든 뒤 병렬로 심사하고, 심사 결과를 바탕으로 최종안을 만든다.

```python
import json
import math
from pathlib import Path

from als_flow import Flow

ARGS = json.loads(Path('_workspace/00_flow_args.json').read_text(encoding='utf-8'))
SCORE = {'type': 'object', 'required': ['scores'], 'properties': {
    'scores': {'type': 'array', 'items': {'type': 'object',
        'required': ['index', 'score', 'strengths'], 'properties': {
            'index': {'type': 'integer'}, 'score': {'type': 'number'},
            'strengths': {'type': 'string'}}}}}}

flow = Flow(name='design-judge-panel', phases=['시안', '심사', '종합'], workdir='.', concurrency=4)


def main():
    angles = ARGS.get('angles') or ['MVP를 우선하는', '위험 관리를 우선하는', '사용자 경험을 우선하는']
    judge_count = ARGS.get('judge_count', 3)

    with flow.phase('시안'):
        drafts = flow.parallel([
            (lambda a=a: flow.agent(f'{a} 관점에서 독립적으로 시안을 작성하라: {ARGS["brief"]}',
                                    label=f'시안:{a}'))
            for a in angles])
        drafts = [d for d in drafts if d is not None]
        flow.log(f'시안 {len(angles)}개 중 {len(drafts)}개를 만들었습니다.')
        if not drafts:
            return {'error': '완성된 시안이 없습니다.', 'drafts': []}

    with flow.phase('심사'):  # 동기화 장벽 필요: 모든 시안을 서로 비교해야 한다
        listing = '\n---\n'.join(f'[{i}] {d}' for i, d in enumerate(drafts))
        judged = flow.parallel([
            (lambda j=j: flow.agent(
                f'심사위원 {j + 1}로서 다음 {len(drafts)}개 시안을 채점하라:\n{listing}',
                label=f'심사:{j}', schema=SCORE))
            for j in range(judge_count)])
        judged = [r for r in judged if r is not None]
        flow.log(f'심사위원 {judge_count}명 중 {len(judged)}명의 결과를 받았습니다.')
        if not judged:
            return {'error': '완료된 심사 결과가 없습니다.', 'drafts': drafts}

    with flow.phase('종합'):
        ranked = []
        for i in range(len(drafts)):
            scores = [x['score'] for r in judged for x in r['scores']
                      if x['index'] == i and math.isfinite(x['score'])]
            if scores:
                ranked.append({'index': i, 'average': sum(scores) / len(scores)})
        if not ranked:
            return {'error': '유효한 시안별 점수가 없습니다.', 'drafts': drafts, 'judged': judged}
        winner = max(ranked, key=lambda x: x['average'])['index']
        final_draft = flow.agent(
            '가장 높은 점수를 받은 시안을 바탕으로 최종안을 작성하라. 다른 시안의 장점도 필요한 부분에 반영하라.\n'
            f'최고점 시안:\n{drafts[winner]}\n심사평:\n{json.dumps(judged, ensure_ascii=False)}',
            label='종합')
        if final_draft is None:
            flow.log('최종안을 작성하지 못했습니다.')
            return {'error': '최종안을 작성하지 못했습니다.', 'drafts': drafts,
                    'judged': judged, 'winner': winner}
        return {'final': final_draft, 'winner': winner}


flow.done(main())
```

심사위원 프롬프트에 번호를 넣는 이유는 같은 프롬프트를 여러 번 보내도 서로 다른 호출로 다루게 하기 위해서다. 프롬프트가 완전히 같으면 캐시에서 같은 결과를 받을 수 있다.

## 4. 새 항목이 없을 때까지 반복(Loop-until-dry)

찾아야 할 항목이 몇 개인지 미리 알 수 없는 작업에 쓴다. 새로운 항목이 K회 연속으로 나오지 않을 때까지 탐색을 반복한다.

4~7절의 코드는 1절의 기본 틀에 넣는 함수다. 전체 스크립트로 사용할 때는 맨 앞에 `Flow(...)`를 두고, 사용하는 단계 제목과 스키마(`FINDINGS`, `VERDICT`)를 함께 정의한다.

```python
def finding_key(finding):
    return json.dumps([finding.get('file', ''), finding.get('title', ''),
                       finding.get('evidence', '')], ensure_ascii=False)


def find_until_dry(finders, dry_limit=2):
    seen, confirmed = set(), []
    if not finders:
        return {'confirmed': confirmed, 'error': '탐색 기준이 없습니다.'}
    dry, round_no = 0, 0
    while dry < dry_limit:
        round_no += 1
        known = [json.loads(k)[1] for k in sorted(seen)]  # 이미 찾은 항목의 제목
        with flow.phase('탐색'):
            found = flow.parallel([
                (lambda f=f: flow.agent(
                    f'{f["prompt"]}\n({round_no}회차) 이미 찾은 항목은 제외하라: '
                    f'{json.dumps(known, ensure_ascii=False)}',
                    label=f'탐색:{f["key"]}:{round_no}', schema=FINDINGS))
                for f in finders])
        found = [b for r in found if r is not None for b in r['findings']]
        # 중복 여부는 seen을 기준으로 판단한다. confirmed를 기준으로 삼으면 안 된다.
        fresh = [b for b in found if finding_key(b) not in seen]
        if not fresh:
            dry += 1
            continue
        dry = 0
        seen.update(finding_key(b) for b in fresh)
        with flow.phase('검증'):
            judged = flow.parallel([
                (lambda b=b: flow.agent(
                    '다음 검토 결과를 검증하라. 근거가 충분하면 confirmed, 명백히 반박되면 refuted, '
                    f'판단하기 어려우면 uncertain으로 판정하라: {json.dumps(b, ensure_ascii=False)}',
                    label=f'검증:{b.get("file", "")}', schema=VERDICT))
                for b in fresh])
        confirmed += [b for b, v in zip(fresh, judged)
                      if v is not None and v['status'] == 'confirmed']
        flow.log(f'확정된 항목은 지금까지 {len(confirmed)}건이며, '
                 f'이번 반복에서 {len(fresh)}건을 새로 찾았습니다.')
    return {'confirmed': confirmed}
```

`confirmed`를 기준으로 중복을 제거하면 검증에서 기각된 항목이 매회 다시 나타나므로 반복이 끝나지 않는다. 이미 한 번이라도 발견한 항목을 담는 `seen`을 기준으로 중복을 제거해야 한다. 회차 번호와 이미 찾은 항목을 프롬프트에 넣는 이유는 같은 프롬프트가 캐시에서 같은 결과를 돌려주면 둘째 회차부터 새 항목이 나올 수 없기 때문이다.

## 5. 여러 기준으로 탐색 + 누락 검토자

한 가지 탐색 기준만으로는 빠짐없이 찾을 수 없을 때 컨테이너·내용·개체·시간처럼 서로 다른 기준으로 탐색하는 에이전트를 병렬로 실행한다. 결과를 합치기 직전에는 빠진 내용만 찾는 누락 검토자 한 명을 둔다. 누락 검토자가 찾은 항목은 다음 탐색 작업에 넣는다.

```python
GAPS = {'type': 'object', 'required': ['gaps'], 'properties': {
    'gaps': {'type': 'array', 'items': {'type': 'object',
        'required': ['kind', 'detail'], 'properties': {
            'kind': {'type': 'string', 'enum': ['unused_axis', 'unverified_claim', 'unread_source']},
            'detail': {'type': 'string'}}}}}}


def sweep_axes(topic, axes):
    # 예: axes = [{'key': 'container', 'prompt': '디렉터리·모듈 단위로'},
    #             {'key': 'content', 'prompt': '내용 패턴으로'}, ...]
    with flow.phase('탐색'):
        found = flow.pipeline(axes, lambda a: flow.agent(
            f'{a["prompt"]} 다음 주제를 조사하라: {topic}',
            label=f'탐색:{a["key"]}', schema=FINDINGS))
    missing = [a['key'] for a, r in zip(axes, found) if r is None]
    findings = [b for r in found if r is not None for b in r['findings']]
    flow.log(f'탐색 기준 {len(axes)}개 중 {len(axes) - len(missing)}개가 완료되었습니다. '
             f'실패한 기준: {missing}')
    return findings, missing


def completeness_critic(topic, axes, findings):
    with flow.phase('누락 검토'):
        review = flow.agent(
            '새 항목을 찾지 마라. 다음 결과에서 빠진 것만 찾아라. 어떤 탐색 기준을 쓰지 않았는가, '
            '검증하지 않은 주장은 무엇인가, 읽지 않은 자료는 무엇인가?\n'
            f'주제: {topic}\n사용한 기준: {[a["key"] for a in axes]}\n'
            f'결과: {json.dumps(findings, ensure_ascii=False)}',
            label='누락 검토', schema=GAPS)
    if review is None:
        flow.log('누락 검토 결과를 받지 못했습니다. 최종 보고서에 누락 검토 미실시를 적는다.')
        return []
    return review['gaps']
```

## 6. 토큰 예산 연동 반복

사용자가 "+500k"처럼 토큰 예산을 지정한 세션에서는 남은 예산에 따라 탐색 범위를 자동으로 조절할 수 있다. 예산은 입력 파일로 받아 `Flow(budget_tokens=...)`에 넘긴다. 예산이 없는 무제한 실행에서는 반복을 멈출 기준이 없으므로, 조건문에서 반드시 예산이 지정됐는지 먼저 확인한다.

```python
def explore_with_budget(budget_tokens):
    findings, rounds = [], 0
    while budget_tokens is not None and flow.budget_remaining() > 50_000:
        rounds += 1
        r = flow.agent(f'다음 탐색 작업을 진행하라. ({rounds}회차)',
                       label=f'탐색:{rounds}', schema=FINDINGS)
        if r is not None:
            findings += r['findings']
        flow.log(f'지금까지 {len(findings)}건을 찾았고, '
                 f'남은 토큰 예산은 {round(flow.budget_remaining() / 1000)}k입니다.')
    return findings


# 작업 규모를 미리 정하는 방식: fleet = budget_tokens // 100_000 if budget_tokens else 3
```

## 7. 사용자 정의 유형 + 구조화 출력

하네스가 만든 `.codex/agents/{name}.toml` 정의를 워크플로에서 그대로 사용한다.

```python
def inspect_draft():
    return flow.agent(
        '_workspace/02_draft.md 파일을 검증하고 발견한 항목을 반환하라',
        agent_type='qa-inspector',  # .codex/agents/qa-inspector.toml
        schema=FINDINGS,            # 사용자 정의 유형도 지정한 스키마에 맞는 결과를 반환
        label='검증:02_draft')      # 모델과 effort는 TOML 정의가 정한다
```

- `agent_type`을 생략하면 기본 서브에이전트를 사용한다. 이때만 `model`, `effort`, `sandbox`를 직접 지정할 수 있다.
- 모델은 각 단계의 업무 특성에 따라 고른다. 반복적인 수집·변환 단계에는 `model='gpt-6-luna', effort='max'`, 깊이 있는 검증·심사·설계 단계에는 `model='gpt-6.1-sol', effort='high'`, 전체 계획·장기 실행 단계에는 `model='gpt-6-astra', effort='medium'`을 쓴다. 검증 단계에서 추론 강도를 높이려면 검증 역할의 TOML에 `model_reasoning_effort = "high"`를 둔다. 자세한 기준은 `model-selection-guide.md`를 참고한다.
- 여러 에이전트가 파일을 동시에 수정해 충돌할 수 있다면 메인 에이전트가 미리 `git worktree add`로 작업 트리를 만들고 `cwd=`로 각 에이전트에 넘긴다. 작업 트리를 준비하는 데 비용이 드므로 실제로 충돌할 가능성이 있을 때만 쓴다.

```python
def flow_agent_examples():
    collect = flow.agent('로그를 형식에 맞게 정리하라', model='gpt-6-luna', effort='max',
                         sandbox='read-only', label='정리')
    design = flow.agent('정리한 결과로 설계안을 비판하라', model='gpt-6.1-sol', effort='high',
                        label='비판')
    return collect, design
```

## 8. 주의할 점(검증 목록)

하네스의 6-2단계에서 워크플로 스크립트를 검증할 때 아래 목록을 확인한다.

| 주의할 점 | 나타나는 문제 | 예방법 |
|------|------|------|
| `Flow(...)`의 `name`·`phases`에 변수나 연산 사용 | 실행을 식별하고 재개하기 어렵다. | `name`과 `phases`에는 리터럴만 넣는다. |
| 문법 오류 | 스크립트를 실행할 수 없다. | 실행 전에 `python3 -m py_compile SCRIPT.py`로 한 번 확인한다. |
| 프롬프트에 `time.time()` / `datetime.now()` / `random` 사용 | 프롬프트가 실행마다 달라져 `--resume`이 캐시를 쓰지 못한다. | 타임스탬프와 시드는 입력 파일로 전달하고, 무작위성이 필요하면 인덱스에 따라 프롬프트를 다르게 작성한다. |
| `None` 거르기 누락 | 실패한 에이전트가 반환한 `None` 때문에 다음 단계가 중단된다. | `flow.parallel()`이나 `flow.pipeline()`의 결과를 사용하기 전에 `[r for r in results if r is not None]`을 적용한다. |
| 불필요한 동기화 장벽 | 빨리 끝난 에이전트가 느린 에이전트를 기다리므로 전체 실행 시간이 늘어난다. | 결과 전체를 서로 비교할 필요가 없다면 `flow.pipeline()`으로 다시 작성한다. |
| `phase` 제목 불일치 | 진행 상황이 의도한 묶음으로 표시되지 않는다. | `flow.phase()` 호출과 `phases`의 항목을 일치시킨다. |
| 병렬로 실행되는 함수 안에서 `flow.phase()` 사용 | 진행 상황을 표시할 묶음이 서로 충돌한다. | 단계는 병렬 호출 바깥에서 열고, 병렬 함수 안의 호출은 `label`로 구분한다. |
| 같은 프롬프트를 여러 번 호출 | 캐시에서 같은 결과를 받아 심사위원·검증자·반복 회차가 사실상 한 번만 실행된다. | 번호, 관점, 회차를 프롬프트에 넣는다. |
| `confirmed`를 기준으로 중복 제거 | 기각된 항목이 다시 나타나 반복이 끝나지 않는다. | `seen` 집합을 기준으로 중복을 제거한다. |
| 토큰 예산 반복에 예산 지정 여부 확인 누락 | 무제한 실행에서 호출 횟수가 실행당 상한(1,000건)에 이를 때까지 계속 실행된다. | `while budget_tokens is not None and ...` 조건을 사용한다. |
| `agent_type`과 `model`·`effort`·`sandbox`를 함께 지정 | 허용되지 않는 호출이다. | 사용자 정의 유형의 모델과 권한은 TOML에서 정한다. 다른 설정이 필요하면 `agent_type` 없이 호출한다. |
| Luna를 `effort='max'` 외의 값으로 호출 | Luna 고정 규칙을 어긴다. | `model='gpt-6-luna'`에는 항상 `effort='max'`를 준다. |
| 처리 범위를 알리지 않고 일부만 처리 | 상위 N개만 처리하고도 "전부 완료"했다고 보고한다. | 처리하지 않은 항목 수를 `flow.log()`에 명시한다. |
| 결과를 진단할 때 실행 기록 미확인 | 캐시에서 가져온 빈 결과를 성공으로 잘못 판단한다. | 완료된 실행을 진단하기 전에 `als-flow status`와 `_workspace/.flow/<run_id>/`의 기록을 확인한다. |
| 스크립트를 임시 위치에 작성 | 다음 실행이나 재개에서 같은 스크립트를 찾지 못한다. | 조율 스킬의 `scripts/`에 저장하고 그 경로로 실행한다. 고칠 때는 그 파일을 수정한 뒤 `--resume RUN_ID`로 다시 실행한다. |
