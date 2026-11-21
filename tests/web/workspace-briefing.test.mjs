import assert from 'node:assert/strict';
import test from 'node:test';
import {base, mount, room, run, runPath, until, workspace} from './workspace-fixture.mjs';

const dependent = {...run, id: 'e'.repeat(32), task: '이전 설정 검증', state: 'QUEUED'};
const independent = {...run, id: 'f'.repeat(32), task: '릴리스 노트', state: 'READY_FOR_HUMAN_REVIEW'};
const product = {id: '1'.repeat(32), prompt: '저장 형식은?', run_id: run.id, kind: 'product_decision', role: 'developer', status: 'pending'};
const permission = {...product, id: '2'.repeat(32), kind: 'permission', prompt: 'settings.py 파일 수정'};
const work = [run, dependent, independent].map(source => ({
  run_id: source.id, task: source.task, priority: 0, areas: ['settings.py'], dependencies: [], decisions: [],
  role: 'developer', stage: 'implement', active_stage: false, last_progress_at: '2026-11-21T03:30:00Z',
  state: source.state, progress: '첫 검사 기록', wait_reasons: [], coordination_revision: 2, editable: true,
}));
const digest = 'a'.repeat(64);
const snapshot = {
  workspace_id: workspace.id, observed_at: '2026-11-21T03:32:00Z', work, overlaps: [],
  decisions: [product, permission].map(q => ({run_id: q.run_id, request_id: q.id, role: q.role, task: run.task,
    prompt: q.prompt, kind: q === product ? 'product' : 'permission',
    impacts: q === product ? [{run_id: run.id, task: run.task, stage: 'start'}, {run_id: dependent.id, task: dependent.task, stage: 'implement'}] : []})),
  candidates: [{run_id: independent.id, task: independent.task, summary: '노트 작성', attempt: 1, digest,
    checks: [{stage: 'full', status: 'passed'}], manual_count: 1}],
};
const gi = '/api/runs/' + independent.id;
function fixture(overrides) {
  return (path, body) => {
    const response = overrides?.(path, body);
    if (response !== undefined) return response;
    if (path === base) return {...room([run, dependent, independent]), briefing_supported: true, goal_supported: true};
    if (path === base + '/briefing') return snapshot;
    if (path === base + '/inbox' || path === runPath + '/inbox') return {requests: [product, permission]};
    for (const item of [run, dependent, independent]) if (path === '/api/runs/' + item.id) return {run: item};
    if (path === gi + '/goal') return {goal: {run_id: independent.id, workspace_id: workspace.id, state: independent.state, best_attempt: 1,
      checks: [], events: [], candidates: [{attempt: 1, digest, eligible: true, status: 'passed', review_status: 'approved'}]}};
    if (path.endsWith('/goal')) return {goal: null};
  };
}

test('room briefing shows one product question and impacted stages, keeps permission separate and targets candidate evidence', async t => {
  const {get, calls, window} = mount(t, fixture(), {query: '?workspace=' + workspace.id + '&run=' + independent.id});
  const go = (id, kind) => get(id).querySelector('[data-action="' + kind + '"]');
  await until(() => go('brief-questions','answer') && get('goal-record').querySelector('[data-goal-attempt]'));
  assert.equal(get('task-summary').parentElement, get('selected-summary'));
  assert.equal(get('selected-summary').open, false);
  assert.equal(get('brief-progress').open, false);
  assert.equal(get('brief-questions').querySelectorAll('article').length, 1);
  assert.match(get('brief-questions').textContent, /저장 형식은.*이전 설정 검증 · 구현/s);
  assert.doesNotMatch(get('brief-questions').textContent, /파일 수정/);
  assert.match(get('brief-review').textContent, /전체 회귀 통과.*수동 확인 1건/);
  go('brief-review','candidate').click();
  await until(() => window.document.activeElement.dataset.goalDiff === '1');
  assert.equal(get('goal-record').querySelector('[data-goal-attempt]').open, true);
  go('brief-questions','answer').click();
  await until(() => window.document.activeElement.dataset.control === 'draft');
  assert.equal(window.document.activeElement.id, 'thread-answer-' + run.id + '-' + product.id);
  go('brief-permissions','permission').click();
  await until(() => get('requests').open && window.document.activeElement.dataset.request === run.id + ':' + permission.id);
  assert.equal(calls.filter(call => call.body).length, 0);
});

test('the same coordination fields preview gates before saving, retain a draft during polling, and send no inferred gates', async t => {
  let saved;
  const {get, window, person, calls, submit} = mount(t, fixture((path, body) => {
    if (path === base + '/coordination/' + dependent.id && body) {
      saved = body; return {run_id: dependent.id, workspace_id: workspace.id, coordination: {...body, revision: 3}};
    }
  }));
  const adjust = () => get('brief-work').querySelector('[data-action="coordinate"][data-run="' + dependent.id + '"]');
  await until(adjust);
  get('brief-progress').open = true;
  adjust().click();
  get('coordination-priority').value = '4';
  get('coordination-areas').value = 'settings.py';
  get('coordination-areas').dispatchEvent(new window.Event('input'));
  assert.match(get('coordination-preview').textContent, /우선순위 4.*겹침 가능/s);
  assert.equal(get('coordination-fields').querySelectorAll('li').length, 0);
  get('coordination-source').value = run.id;
  get('coordination-source').dispatchEvent(new window.Event('change'));
  get('coordination-stage').value = 'implement';
  get('coordination-condition').value = 'product_decision';
  get('coordination-condition').dispatchEvent(new window.Event('change'));
  assert.ok(![...get('coordination-request').options].some(o => o.value === permission.id));
  get('coordination-request').value = product.id;
  get('coordination-reason').value = '선택한 형식으로 검증';
  get('coordination-add').click();
  get('coordination-priority').focus();
  const previous = calls.filter(call => call.path === base + '/briefing').length;
  get('refresh').click();
  await until(() => calls.filter(call => call.path === base + '/briefing').length > previous);
  assert.equal(window.document.activeElement.id, 'coordination-priority');
  assert.match(get('coordination-fields').textContent, /구현 전.*저장 형식은.*선택한 형식으로 검증/s);
  submit('coordination-form');
  await until(() => !get('coordination-dialog').open);
  assert.deepEqual(saved, {priority: 4, areas: ['settings.py'], dependencies: [{source_run: run.id, stage: 'implement',
    condition: 'product_decision', request_id: product.id, reason: '선택한 형식으로 검증'}], revision: 2});
  person('coordinator').click();
  get('allow-edits').click();
  assert.equal(get('assignment-coordination').hidden, false);
  get('assignment-areas').value = 'settings.py';
  get('message').value = '새 목표';
  submit('chat-form');
  await until(() => calls.some(c => c.path === base + '/assignments'));
  assert.deepEqual(calls.find(c => c.path === base + '/assignments').body.coordination, {priority: 0, areas: ['settings.py'], dependencies: []});
});

test('a stale board keeps the dated snapshot, disables actions, and refresh restores them', async t => {
  let unavailable = false;
  const {get} = mount(t, fixture(path => {
    if (path === base + '/briefing' && unavailable) throw Error('offline');
  }));
  const button = () => get('brief-questions').querySelector('button');
  await until(button);
  const time = get('brief-time').textContent;
  unavailable = true; get('refresh').click();
  await until(() => button().disabled);
  assert.equal(get('brief-time').textContent, time);
  assert.match(get('brief-note').textContent, /마지막 기록/);
  unavailable = false; get('refresh').click();
  await until(() => !button().disabled);
});

test('read-only intake accepts optional start gates and priority without adding edits, goal verification, or a permission action', async t => {
  const {get, window, person, calls, submit} = mount(t, fixture());
  await until(() => get('brief-questions').querySelector('button'));
  person('coordinator').click();
  assert.equal(get('assignment-coordination').hidden, false);
  assert.deepEqual([...get('assignment-stage').options].map(opt => opt.value), ['start']);
  assert.equal(get('allow-edits').checked, false);
  get('assignment-priority').value = '5';
  get('assignment-areas').value = 'settings.py';
  get('assignment-source').value = run.id;
  get('assignment-source').dispatchEvent(new window.Event('change'));
  get('assignment-condition').value = 'product_decision';
  get('assignment-condition').dispatchEvent(new window.Event('change'));
  get('assignment-request').value = product.id;
  get('assignment-reason').value = '결정 후 조사';
  get('assignment-add').click();
  get('message').value = '저장 형식을 읽고 보고해 주세요';
  submit('chat-form');
  await until(() => calls.some(c => c.path === base + '/assignments'));
  const writes = calls.filter(call => call.body);
  assert.equal(writes.length, 1);
  assert.equal(writes[0].body.allow_edits, false);
  assert.equal(writes[0].body.goal, undefined);
  assert.deepEqual(writes[0].body.coordination, {priority: 5, areas: ['settings.py'], dependencies: [
    {source_run: run.id, stage: 'start', request_id: product.id, condition: 'product_decision', reason: '결정 후 조사'}]});
});

test('changing to read-only preserves other draft fields and prompts to explicitly replace a later-stage gate', async t => {
  const {get, window, person, calls, submit} = mount(t, fixture());
  await until(() => get('brief-questions').querySelector('button'));
  person('coordinator').click();
  get('allow-edits').click();
  get('assignment-source').value = run.id;
  get('assignment-source').dispatchEvent(new window.Event('change'));
  get('assignment-stage').value = 'review'; get('assignment-reason').value = '검토 전 확인'; get('assignment-add').click();
  get('message').value = '관찰 결과 보고';
  get('allow-edits').click();
  submit('chat-form');
  assert.match(get('chat-note').textContent, /착수 전 조건/);
  assert.equal(calls.some(call => call.body), false);
  get('assignment-coordination').querySelector('.dependency-list button').click();
  submit('chat-form');
  await until(() => calls.some(c => c.path === base + '/assignments'));
  const body = calls.find(c => c.body).body;
  assert.equal(body.coordination, undefined);
  assert.equal(body.allow_edits, false);
});

test('the source keeps the latest product answer separate from the version used by a dependent and safely retries a correction', async t => {
  const earlier = {source_run: run.id, request_id: product.id, prompt: product.prompt, revision: 1, answer: '수동'};
  let current = {...earlier, revision: 2, answer: '자동', reason: '기존 설정 지원'};
  let first;
  const {get, submit, calls, window} = mount(t, fixture((path, body) => {
    if (path === base + '/briefing') return {...snapshot, decision_options: [current], decisions: [],
      work: work.map(row => row.run_id !== dependent.id ? row : {...row, decisions: [earlier],
        dependencies: [{source_run: run.id, request_id: product.id, condition: 'product_decision', stage: 'verify', reason: '형식 확인'}]})};
    if (path === base + '/decision-revisions/' + product.id && body) {
      if (!first) { first = body; throw Error('lost receipt'); }
      assert.deepEqual(first, body);
      current = {...current, revision: 3, answer: body.answer, reason: body.reason};
      return {workspace_id: workspace.id, decision: current};
    }
  }));
  const source = () => get('brief-work').querySelector('[data-source-request]');
  await until(source);
  assert.match(get('brief-work').textContent, /사용한 버전 1.*현재 결정 2 · 자동/);
  get('brief-work').querySelector('[data-action="source"]').click();
  assert.equal(window.document.activeElement, source().querySelector('summary'));
  source().querySelector('[data-action="revise"]').click();
  assert.match(get('revision-impact').textContent, /이전 설정 검증 · 검증/);
  assert.equal(calls.some(call => call.body), false);
  get('revision-answer').value = '기본값 유지'; get('revision-reason').value = '명시적인 동의 필요';
  submit('decision-revision-form');
  await until(() => get('revision-note').textContent);
  submit('decision-revision-form');
  await until(() => !get('decision-revision-dialog').open && source().textContent.includes('현재 결정 3'));
  assert.equal(first.revision, 2);
  assert.equal(get('brief-work').textContent.includes('사용한 버전 1'), true);
});

test('a reused product answer is attributed to the source in both the conversation and request history', async t => {
  const targetPath = '/api/runs/' + dependent.id;
  const reused = {...product, run_id: dependent.id, id: '3'.repeat(32), answer: '자동', decision: 'answer', status: 'delivered',
    source: 'operator', shared_decision: {source_run: run.id, request_id: product.id, prompt: product.prompt, answer: '자동', revision: 2}};
  const {get, calls} = mount(t, fixture(path => path === targetPath + '/inbox' ? {requests: [reused]} : undefined),
    {query: '?workspace=' + workspace.id + '&run=' + dependent.id});
  const thread = get('office-thread');
  await until(() => thread.textContent.includes('선행 목표의 사용자 결정 적용'));
  assert.equal([...thread.querySelectorAll('[data-owner="user"]')].some(item => item.textContent.includes('자동')), false);
  assert.match(thread.textContent, /자동.*원래 제품 결정 · 버전 2.*원본 요청/s);
  assert.equal(thread.querySelector('[data-control="answer"]'), null);
  get('inbox').click();
  const item = () => get('request-log').querySelector('[data-request="' + dependent.id + ':' + reused.id + '"]');
  await until(item);
  assert.match(item().textContent, /선행 목표의 사용자 결정 적용.*원래 제품 결정 · 버전 2/s);
  assert.match(item().querySelector('a').href, new RegExp(run.id));
  assert.equal(item().querySelector('button,textarea'), null);
  assert.equal(calls.some(call => call.body), false);
});

test('thirteen visible work records count ten active jobs and three closed, and overlap never becomes a wait reason', async t => {
  const workRows = [...Array(13)].map((_,index) => ({...work[0], run_id: String(index+1).padStart(32,'0'),
    task: '목표 ' + index, state: index < 7 ? 'RUNNING' : index < 10 ? 'QUEUED' : 'READY_FOR_HUMAN_REVIEW'}));
  const {get} = mount(t, fixture(path => path === base + '/briefing' ? {...snapshot, work: workRows, decisions: [],
    overlaps: [{left: workRows[0].run_id, right: workRows[1].run_id, area: 'settings.py', reason: '지정한 경로가 겹침'}]} : undefined));
  await until(() => get('brief-work-count').textContent === '진행 7 · 대기 3 · 종료 3');
  assert.match(get('brief-overlaps').textContent, /settings.py.*자동 보류 없음/);
  assert.doesNotMatch(get('brief-work').textContent, /대기 사유|선행 조건 ·/);
});
