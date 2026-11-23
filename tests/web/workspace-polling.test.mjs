import assert from 'node:assert/strict';
import test from 'node:test';
import {base, mount, room, run, runId, runPath, until} from './workspace-fixture.mjs';

function pollingClock() {
  const timers = new Map();
  let next = -1;
  return {
    timers,
    setupWindow(win) {
      const set = win.setTimeout.bind(win), clear = win.clearTimeout.bind(win);
      win.setTimeout = (callback, delay, ...args) => {
        if (![3000, 20000].includes(delay)) return set(callback, delay, ...args);
        timers.set(next, {delay, callback});
        return next--;
      };
      win.clearTimeout = id => { if (!timers.delete(id)) clear(id); };
    },
    tick() {
      assert.equal(timers.size, 1);
      const [[id, {callback}]] = timers;
      timers.delete(id);
      return callback();
    },
  };
}

test('a restored window loads once while hidden and coalesces visible wake events', async t => {
  const clock = pollingClock();
  const restored = {...run, state: 'FAILED'};
  const roomData = {...room([restored]), github_supported: true};
  const github = {config: {enabled: true}, connections: [], status: {},
    issues: [{issue_id: 107, number: 7, title: '설정 복원', run_id: runId}]};
  const {window, calls, get} = mount(t, path => {
    if (path === base) return roomData;
    if (path === runPath) return {run: restored};
    if (path === base + '/github') return github;
  }, {setupWindow(win) {
    clock.setupWindow(win);
    Object.defineProperty(win.document, 'hidden', {value: true, configurable: true});
  }});
  await until(() => get('github-issues').querySelector('[data-issue="107"] button'));
  assert.equal(clock.timers.size, 0);
  const reads = calls.length;
  for (const event of ['online', 'focus', 'pageshow']) window.dispatchEvent(new window.Event(event));
  assert.equal(calls.length, reads);
  Object.defineProperty(window.document, 'hidden', {value: false, configurable: true});
  for (const event of ['pageshow', 'focus']) window.dispatchEvent(new window.Event(event));
  window.document.dispatchEvent(new window.Event('visibilitychange'));
  await until(() => clock.timers.size === 1);
  assert.equal(calls.filter(call => call.path === base).length, 2);
});

test('first GitHub read failure retries on the next visible tick in an idle restored room', async t => {
  const clock = pollingClock();
  let reads = 0;
  const {get} = mount(t, path => {
    if (path === base) return {...room([]), github_supported: true};
    if (path === base + '/github') {
      if (++reads === 1) throw Error('starting');
      return {config: {enabled: true}, connections: [], status: {}, issues: [{issue_id: 107, number: 7, title: '설정', run_id: runId}]};
    }
  }, {setupWindow: clock.setupWindow});
  await until(() => clock.timers.size && reads === 1);
  assert.equal([...clock.timers.values()][0].delay, 3000);
  await clock.tick();
  assert.ok(get('github-issues').querySelector('[data-issue="107"] button'));
  assert.equal([...clock.timers.values()][0].delay, 20000);
});

test('hiding and showing a loading window preserves the single in-flight discovery', async t => {
  const clock = pollingClock();
  let finishDiscovery;
  const {window, calls, get} = mount(t, path => {
    if (path === '/api/hosts') return new Promise(resolve => { finishDiscovery = resolve; });
    if (path === base) return room([]);
  }, {setupWindow: clock.setupWindow});
  await until(() => finishDiscovery);
  for (const hidden of [true, false]) {
    Object.defineProperty(window.document, 'hidden', {value: hidden, configurable: true});
    window.document.dispatchEvent(new window.Event('visibilitychange'));
    window.dispatchEvent(new window.Event('pageshow'));
    window.dispatchEvent(new window.Event('focus'));
  }
  finishDiscovery({hosts: [{id: 'local', name: '이 컴퓨터'}]});
  await until(() => !get('room').hidden && clock.timers.size === 1);
  assert.equal(calls.filter(call => call.path === '/api/hosts').length, 1);
  assert.equal(calls.filter(call => call.path === base).length, 1);
});

for (const supported of [false, true]) {
  test('visible active refresh uses ' + (supported ? 'one bundled request' : 'compatible individual reads'), async t => {
    const clock = pollingClock();
    let row = {...run};
    const snapshot = () => ({...room([row]), bundle_supported: supported, goal_supported: true});
    const {window, calls, get} = mount(t, path => {
      if (path === base) return snapshot();
      if (path === runPath) return {run: row};
      if (path === runPath + '/goal') return {goal: null};
      if (path.startsWith(base + '/bundle/')) return {room: snapshot(), run: {run: row}, goal: {goal: null},
        inbox: {requests: []}, directions: {directions: []}};
    }, {setupWindow: clock.setupWindow});
    await until(() => clock.timers.size === 1);
    calls.length = 0;
    await clock.tick();
    assert.equal(calls.length, supported ? 1 : 5);
    if (supported) assert.equal(calls[0].path, base + '/bundle/' + runId + '/live');
    const visibility = hidden => {
      Object.defineProperty(window.document, 'hidden', {value: hidden, configurable: true});
      window.document.dispatchEvent(new window.Event('visibilitychange'));
    };
    visibility(true);
    calls.length = 0;
    window.dispatchEvent(new window.Event('online'));
    assert.equal(clock.timers.size, 0);
    assert.equal(calls.length, 0);
    row = {...row, state: 'PLAN_ONLY'};
    visibility(false);
    await until(() => clock.timers.size === 1);
    assert.equal([...clock.timers.values()][0].delay, 20000);
    assert.equal(get('room').hidden, false);
  });
}

test('active team children have individual, keyboard-accessible workspace links', async t => {
  const selected = {...run, team_parent_id: 'f'.repeat(32), visiting_roles: [{role: 'verifier', engine: 'claude'}]};
  const sibling = {...selected, id: '9'.repeat(32), task: '형제 작업', visiting_roles: []};
  const {window} = mount(t, path => path === base ? room([selected, sibling]) : path === runPath ? {run: selected} : undefined);
  await until(() => window.document.querySelectorAll('a[data-child]').length === 2);
  const link = window.document.querySelector('[data-child="' + sibling.id + '"]');
  assert.match(link.textContent, /형제 작업/);
  assert.equal(new URL(link.href).searchParams.get('run'), sibling.id);
  assert.match(window.document.querySelector('[data-visitor="verifier"]').textContent, /독립 검증자 · Claude Code/);
});

test('a plan and a replacement question on another run refresh even when its pending count stays one', async t => {
  const clock = pollingClock();
  const other = {...run, id: '8'.repeat(32), meeting_status: 'planning', pending_count: 1};
  const snapshot = () => ({...room([run, other]), bundle_supported: true, briefing_bundle_supported: true,
    briefing_supported: true});
  let decisions = [];
  const board = () => ({workspace_id: run.workspace_id, observed_at: run.created_at,
    decisions, work: [], overlaps: [], candidates: []});
  const {get, calls} = mount(t, path => {
    if (path === base) return snapshot();
    if (path === runPath) return {run};
    if (path === base + '/briefing') return board();
    if (path.startsWith(base + '/bundle/')) return {room: snapshot(), run: {run}, inbox: {requests: []},
      directions: {directions: []}, ...(path.endsWith('/live') ? {} : {briefing: board()})};
  }, {setupWindow: clock.setupWindow, query: '?workspace=' + run.workspace_id + '&run=' + runId});
  await until(() => clock.timers.size);
  calls.length = 0;
  other.meeting_status = 'awaiting_review';
  const plan = {kind: 'plan', run_id: other.id, request_id: 'plan:2', revision: 2, task: '별도 작업 계획',
    owner: '조정자', role: 'coordinator', prompt: '이 계획으로 진행할까요?', impacts: []};
  decisions = [plan];
  await clock.tick();
  assert.ok(get('brief-questions').querySelector('[data-action="plan"][data-run="' + other.id + '"]'));
  decisions = [{...plan, request_id: 'e'.repeat(32), kind: 'question', prompt: '추가 확인이 필요합니다.'}];
  await clock.tick();
  assert.ok(get('brief-questions').querySelector('[data-action="answer"][data-run="' + other.id + '"]'));
  assert.equal(get('brief-questions').querySelector('[data-action="plan"]'), null);
  assert.deepEqual(calls.map(c => c.path), Array(2).fill(base + '/bundle/' + runId + '/briefing'));
});

test('a completed GitHub sync can run again while the same goal waits for an answer', async t => {
  const clock = pollingClock();
  const row = {...run, pending_count: 1};
  const snapshot = {...room([row]), bundle_supported: true, goal_supported: true, github_supported: true};
  const goal = {id: runId, run_id: runId, workspace_id: row.workspace_id, state: 'RUNNING', request: row.task,
    policy: {active_seconds: 28800, max_attempts: 6, checks: []}, scope: [], criteria: [], checks: [], candidates: [],
    events: [], next_action: '작업을 계속하려면 운영자 응답이 필요합니다.'};
  let queued = false, syncs = 0;
  const github = () => ({config: {enabled: true, repository: 'fixture/settings'}, status: {queued},
    connections: [], issues: []});
  const request = {id: 'f'.repeat(32), run_id: runId, role: 'developer', kind: 'question',
    status: 'pending', prompt: '저장한 설정을 읽을까요?'};
  const inbox = {requests: [request]}, directions = {directions: []};
  const {get} = mount(t, path => {
    if (path === base) return snapshot;
    if (path === runPath) return {run: row};
    if (path === runPath + '/inbox') return inbox;
    if (path === runPath + '/goal') return {goal};
    if (path === base + '/github') return github();
    if (path === base + '/github/sync') {
      syncs++; queued = true; return {queued: true};
    }
    if (path.startsWith(base + '/bundle/')) return {room: snapshot, run: {run: row}, goal: {goal}, inbox,
      directions, ...(path.endsWith('/full') ? {github: github()} : {})};
  }, {setupWindow: clock.setupWindow});
  await until(() => clock.timers.size && !get('github-sync').disabled);
  assert.match(get('goal-next').textContent, /요청함에서 질문/);
  get('github-sync').click();
  await until(() => syncs === 1 && get('github-status').textContent.includes('가져오기 요청 처리 중'));
  assert.equal(get('github-sync').disabled, true);
  queued = false;
  await clock.tick();
  assert.equal(get('github-sync').disabled, false);
  get('github-sync').click();
  await until(() => syncs === 2);
});
