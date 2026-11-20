import assert from 'node:assert/strict';
import test from 'node:test';
import {base, mount, room, run, runId, runPath, until, workspace} from './workspace-fixture.mjs';

test('an empty office puts the first assignment on the coordinator portrait', async t => {
  const {get, person} = mount(t, path => path === base ? room([]) : undefined);
  await until(() => !get('room').hidden);
  assert.equal(get('task-summary').hidden, true);
  assert.equal(person('coordinator').querySelector('.seat-start').hidden, false);
  person('coordinator').click();
  assert.equal(get('conversation').open, true);
  assert.equal(get('message').disabled, false);
});

test('the summary focuses the right answer, preserves its draft on refresh and opens a different permission only on click', async t => {
  const remoteRun = {...run, id: '7'.repeat(32), task: '다른 작업', pending_count: 1};
  const question = {id: '3'.repeat(32), run_id: runId, role: 'developer', engine: 'codex',
    prompt: '결과에 파일명을 표시할까요?', kind: 'product_decision', status: 'pending'};
  const permit = {...question, id: '4'.repeat(32), run_id: remoteRun.id, kind: 'permission', prompt: '파일 읽기 요청'};
  let others = [];
  const {get, window, calls} = mount(t, path => {
    if (path === base) return room([...others, {...run, pending_count: 1}]);
    if (path === runPath) return {run};
    if (path === runPath + '/inbox') return {requests: [question]};
    if (path === '/api/runs/' + remoteRun.id) return {run: remoteRun};
    if (path === '/api/runs/' + remoteRun.id + '/inbox') return {requests: [permit]};
    if (path === base + '/inbox') return {requests: [question, permit]};
  });
  await until(() => get('summary-action').textContent === '질문에 답하기');
  assert.match(get('summary-owner').textContent, /담당자 · 서진 · Codex/);
  assert.equal(get('summary-next').textContent, question.prompt);
  assert.equal(get('summary-facts').hidden, true);
  get('summary-action').click();
  const draft = () => get('office-thread').querySelector('textarea');
  assert.equal(window.document.activeElement, draft());
  draft().value = '작성 중';
  draft().dispatchEvent(new window.Event('input'));
  others = [remoteRun];
  get('refresh').click();
  await until(() => !get('summary-other').hidden);
  assert.equal(get('summary-title').textContent, run.task);
  assert.equal(draft().value, '작성 중');
  assert.equal(window.document.activeElement, draft());
  get('summary-other').querySelector('button').click();
  await until(() => get('requests').open && window.document.activeElement.dataset.request === remoteRun.id + ':' + permit.id);
  assert.equal(get('summary-title').textContent, remoteRun.task);
  assert.equal(window.document.activeElement.querySelector('[data-control=approve]').disabled, false);
  assert.equal(calls.some(call => call.body), false);
});

test('a plan alert goes directly to the current product choice without approving a plan', async t => {
  const choice = {id: 'q1', question: '기본 동작은?', options: ['수동', '자동']};
  const plan = {outcome: '정렬 개선', scope: ['목록'], tasks: [], criteria: [], rationale: [],
    questions: [choice], evidence: {revision: 'a'.repeat(40), paths: []}};
  const meeting = {status: 'awaiting_review', revision: 1, contributions: [], choices: [choice], answers: {},
    revisions: [], verification: [], plan};
  const {get, window, calls} = mount(t, path => {
    if (path === base) return room([{...run, meeting_status: meeting.status, pending_count: 1}]);
    if (path === runPath) return {run: {...run, meeting}};
  });
  await until(() => get('summary-action').textContent === '계획 확인하기' && !get('summary-action').disabled);
  get('summary-action').click();
  assert.equal(window.document.activeElement.id, 'meeting-choice-0-0');
  assert.equal(window.document.activeElement.checked, false);
  assert.equal(calls.some(call => call.body), false);
});

test('failed tests surface their recorded error before earlier work and open the matching evidence', async t => {
  const failure = {...run, state: 'FAILED', outcome: 'TEST_FAILED', developer_output: '구현 완료',
    test_output: 'FAILED tests/test_list.py::test_order - AssertionError\n1 failed in 0.21s'};
  const {get, window} = mount(t, path => {
    if (path === base) return room([failure]);
    if (path === runPath) return {run: failure};
  });
  await until(() => get('summary-facts').textContent.includes('1개 실패'));
  assert.equal(get('summary-owner').textContent, '테스트 실행기');
  assert.match(get('summary-result').textContent, /실패 기록 · tests\/test_list/);
  assert.doesNotMatch(get('task-summary').textContent, /구현 완료/);
  get('summary-action').click();
  assert.equal(window.document.activeElement, get('evidence-tests').querySelector('summary'));
});

test('terminal result opens the selected run even with another developer already at the desk', async t => {
  const finished = {...run, id: '8'.repeat(32), state: 'PLAN_ONLY', outcome: 'PLAN_ONLY', task: '로그 확인',
    developer_output: '필터의 기본값을 확인했습니다.', allow_edits: false};
  const {get, window} = mount(t, path => {
    if (path === base) return room([run, finished]);
    if (path === '/api/runs/' + finished.id) return {run: finished};
    if (path === runPath) return {run};
  }, {query: '?workspace=' + workspace.id + '&run=' + finished.id});
  await until(() => get('summary-result').textContent === finished.developer_output);
  get('summary-action').click();
  assert.equal(new URL(window.location).searchParams.get('run'), finished.id);
  assert.equal(get('chat-context').textContent, finished.task);
  assert.match(get('chat-log').textContent, /필터의 기본값/);
});
