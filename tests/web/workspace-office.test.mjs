import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import test from 'node:test';
import {JSDOM} from 'jsdom';
import {mountWorkspaceOffice} from '../../src/agent_office/web/workspace-office.js';

const html = await readFile(new URL('../../src/agent_office/web/workspace-office.html', import.meta.url), 'utf8');
const remote = 'a'.repeat(32);
const workspace = {id: 'b'.repeat(32), name: 'AI Agent Office', folders: ['E:/프로젝트', 'E:/문서']};
const runId = 'c'.repeat(32);
const skillId = 'd'.repeat(32);
const hosts = [{id: 'local', name: '이 PC'}, {id: remote, name: '원격 연구실'}];
const settings = {
  delegation: true,
  developer: {name: '서진', engine: 'codex', skills: [skillId]},
  reviewer: {name: '정희', engine: 'antigravity', skills: []},
};
const run = {
  id: runId, workspace_id: workspace.id, task: '원인 확인', state: 'RUNNING', created_at: '2026-11-19T01:02:03Z',
  developer_engine: 'codex', reviewer_engine: 'antigravity', allow_edits: true, use_inbox: true, attempts: 1,
  assignment: {roles: {developer: settings.developer, reviewer: settings.reviewer}, delegation: true, note: '서진에게 작업을 맡겼습니다.'},
  lines: {developer: '오류 원인을 찾았고 테스트 중입니다.', reviewer: '작업 시작을 기다리고 있습니다.'},
  pending_count: 0,
};
const room = (runs = [run], values = settings) => ({workspace, settings: values, runs, engines: []});
const base = '/api/rooms/' + workspace.id;
const runPath = '/api/runs/' + run.id;

async function until(predicate) {
  for (let i = 0; i < 120; i++) {
    if (predicate()) return;
    await new Promise(resolve => setTimeout(resolve, 5));
  }
  assert.ok(predicate(), '작업실 응답 대기 시간 초과');
}

function mount(t, route, {query = '', storage = {}} = {}) {
  const dom = new JSDOM(html, {url: 'http://127.0.0.1:8766/' + query, pretendToBeVisual: true});
  const {window} = dom;
  const get = id => window.document.getElementById(id);
  const person = role => window.document.querySelector('.seat[data-person="' + role + '"]');
  for (const [key, value] of Object.entries(storage)) window.sessionStorage.setItem(key, value);
  if (!window.HTMLDialogElement.prototype.showModal) {
    window.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
    window.HTMLDialogElement.prototype.close = function () { this.open = false; this.dispatchEvent(new window.Event('close')); };
  }
  const calls = [];
  window.fetch = async (path, options) => {
    assert.equal(options.mode, 'same-origin');
    const body = options.body ? JSON.parse(options.body) : undefined;
    if (body) assert.equal(options.headers['X-Agent-Office'], 'studio');
    calls.push({path, body});
    const result = await route(path, body);
    if (result !== undefined) return result instanceof Response ? result : Response.json(result);
    if (path === '/api/hosts') return Response.json({hosts});
    if (path.endsWith('/workspaces')) return Response.json({workspaces: [workspace]});
    if (path.endsWith('/inbox')) return Response.json({requests: []});
    if (path.endsWith('/directions')) return Response.json({directions: []});
    assert.fail('unexpected fetch ' + path);
  };
  const dispose = mountWorkspaceOffice(window);
  const close = () => { dispose(); window.close(); };
  t.after(close);
  const submit = id => get(id).dispatchEvent(new window.Event('submit', {bubbles: true, cancelable: true}));
  return {window, get, person, calls, submit, close};
}

test('coordinator assignment uses room defaults and sends a linked follow-up without creating a separate run', async t => {
  let assigned = null, direction = null, mode = 'RUNNING';
  const message = '<img src=x onerror=alert(1)> 로그인 검사';
  const {get, person, calls, submit, window} = mount(t, (path, body) => {
    if (path === base) return room(assigned ? [{...assigned, state: mode}] : []);
    if (path === base + '/assignments') {
      assert.match(body.id, /^[a-f0-9]{32}$/);
      assert.deepEqual(body, {id: body.id, message, allow_edits: true, delegation: null, indices: [0]});
      assigned = {...run, id: body.id, task: body.message};
      return {run: assigned};
    }
    if (assigned && path === '/api/runs/' + assigned.id) return {run: {...assigned, state: mode, developer_output: mode === 'PLAN_ONLY' ? '조회 결과' : ''}};
    if (assigned && path === '/api/runs/' + assigned.id + '/directions') {
      if (body) {
        assert.deepEqual(body, {id: body.id, role: 'developer', attempt: 1, message: 'Windows에서도 확인해 주세요.'});
        assert.match(body.id, /^[a-f0-9]{32}$/);
        direction = {...body, created_at: run.created_at, delivery: 'queued'};
        return {direction};
      }
      return {directions: direction ? [direction] : []};
    }
  });
  await until(() => get('room-title').textContent === workspace.name);
  person('coordinator').click();
  assert.equal(get('conversation').open, true);
  assert.match(get('run-delegation').options[0].textContent, /조정자가 결정/);
  get('message').value = message;
  get('allow-edits').checked = true;
  get('assignment-folders').querySelector('input').checked = true;
  submit('chat-form');
  await until(() => get('chat-log').textContent.includes('서진에게 작업을 맡겼습니다.'));
  assert.ok(get('chat-log').textContent.includes(message));
  assert.equal(get('chat-log').querySelector('img'), null);
  assert.equal(new URLSearchParams(window.location.search).get('run'), assigned.id);
  get('conversation').close();
  person('developer').click();
  await until(() => !get('send').disabled);
  assert.equal(get('chat-context').textContent, message);
  get('message').value = 'Windows에서도 확인해 주세요.';
  submit('chat-form');
  await until(() => get('chat-log').textContent.includes('전달 대기'));
  direction = {...direction, delivery: 'delivered'};
  mode = 'PLAN_ONLY';
  get('refresh').click();
  await until(() => get('chat-log').textContent.includes('전달됨') && get('ledger-label').textContent === '조회 완료');
  assert.equal(get('ledger-result').textContent.includes('조회 결과'), true);
  assert.equal(calls.filter(call => call.path === base + '/assignments').length, 1);
});

test('a lost assignment receipt preserves the exact request ID, choices and message across reload', async t => {
  let posted, attempts = 0, storedRun;
  const attemptsRoute = (path, body) => {
    if (path === base) return room(storedRun ? [storedRun] : []);
    if (path === base + '/assignments') {
      if (++attempts === 1) {
        posted = body;
        storedRun = {...run, id: body.id, task: body.message};
        throw new Error('lost after journal write');
      }
      assert.deepEqual(body, posted);
      if (attempts === 2) return Response.json({error: {code: 'NOT_FOUND', message: '등록된 경로가 없습니다.'}}, {status: 404});
      return {run: storedRun};
    }
    if (path === '/api/runs/' + storedRun?.id) return {run: storedRun};
  };
  const first = mount(t, attemptsRoute);
  await until(() => first.get('room-title').textContent === workspace.name);
  first.person('coordinator').click();
  first.get('message').value = '읽기 전용으로 리뷰해줘';
  first.get('run-delegation').value = 'false';
  first.submit('chat-form');
  await until(() => first.get('chat-note').textContent.includes('같은 요청을 다시 보내세요'));
  const storage = Object.fromEntries(Object.keys(first.window.sessionStorage).map(key => [key, first.window.sessionStorage.getItem(key)]));
  first.close();

  const second = mount(t, attemptsRoute, {storage});
  await until(() => second.get('room-title').textContent === workspace.name);
  second.person('coordinator').click();
  assert.equal(second.get('message').value, posted.message);
  assert.equal(second.get('message').readOnly, true);
  assert.equal(second.get('run-delegation').value, 'false');
  assert.equal(second.get('send').textContent, '같은 요청 다시 보내기');
  second.submit('chat-form');
  await until(() => second.get('chat-note').textContent.includes('같은 요청을 다시 보내세요'));
  assert.equal(second.get('message').readOnly, true);
  second.submit('chat-form');
  await until(() => second.get('chat-note').textContent === '작업을 배정했습니다.');
  assert.equal(second.get('message').readOnly, false);
  assert.equal(second.window.sessionStorage.length, 0);
  assert.equal(attempts, 3);
});

test('legacy remote execution links find the workspace and retain the last progress during a disconnect', async t => {
  let disconnected = false;
  const target = '/api/hosts/' + remote;
  const {get, calls, person, window} = mount(t, path => {
    if (path === target + '/runs/' + runId) return {run};
    if (path === target + '/rooms/' + workspace.id) {
      return disconnected ? Response.json({}, {status: 503}) : room();
    }
  }, {query: '?host=' + remote + '&run=' + runId + '#detail'});
  await until(() => person('developer').querySelector('.seat-line').textContent === run.lines.developer);
  assert.equal(get('host').value, remote);
  assert.equal(new URLSearchParams(window.location.search).get('workspace'), workspace.id);
  assert.equal(get('detail-link').pathname, '/studio');
  assert.equal(get('detail-link').search, '?host=' + remote + '&run=' + runId);
  assert.ok(calls.every(call => call.path === '/api/hosts' || call.path.startsWith(target)));
  disconnected = true;
  get('refresh').click();
  await until(() => get('connection').textContent.includes('마지막 기록'));
  assert.equal(person('developer').querySelector('.seat-line').textContent, run.lines.developer);
  assert.equal(get('settings').disabled, true);
  disconnected = false;
  get('refresh').click();
  await until(() => get('connection').textContent === '원격 연구실 연결됨');
});

test('inbox distinguishes user-facing decisions, technical questions and tool approvals', async t => {
  const request = (kind, index) => ({
    id: String(index).repeat(32), kind, run_id: runId, role: 'developer', engine: 'codex',
    prompt: kind === 'product_decision' ? '<svg onload=alert(1)> 동작을 바꿀까요?' : '요청 내용', status: 'pending',
  });
  const requests = [request('question', 1), request('product_decision', 2), request('permission', 3)];
  const {get} = mount(t, (path, body) => {
    if (path === base) return room([{...run, pending_count: requests.filter(row => row.status === 'pending').length}]);
    if (path === runPath) return {run};
    if (path === base + '/inbox') return {requests: requests.filter(row => row.status === 'pending')};
    if (path === runPath + '/inbox') return {requests};
    if (path === runPath + '/inbox/' + requests[1].id + '/decision') {
      assert.deepEqual(body, {action: 'answer', answer: '기존 동작 유지'});
      requests[1] = {...requests[1], status: 'decided', decision: 'answer', answer: body.answer};
      return {request: requests[1]};
    }
  });
  await until(() => get('inbox-count').textContent === '3');
  get('inbox').click();
  await until(() => get('request-log').querySelectorAll('article').length === 3);
  const rows = Array.from(get('request-log').querySelectorAll('article'));
  assert.deepEqual(rows.map(row => row.querySelector('h3').textContent), ['기술 질문', '제품 동작 결정', '권한 승인 요청']);
  assert.equal(rows[1].querySelector('svg'), null);
  assert.equal(rows[2].querySelector('textarea'), null);
  assert.equal(rows[2].querySelector('button').textContent, '이번 요청만 승인');
  rows[1].querySelector('textarea').value = '기존 동작 유지';
  rows[1].querySelector('button').click();
  await until(() => get('request-log').textContent.includes('사용자 결정'));
  assert.ok(get('request-log').textContent.includes('기존 동작 유지'));
});

test('workspace settings persist role names, engines, skills and delegation independently of a running assignment', async t => {
  let current = settings;
  const {get, person, submit} = mount(t, (path, body) => {
    if (path === '/api/skills') return {skills: [{id: skillId, name: '테스트 규칙', version: 2}]};
    if (path === base && body) {
      current = body;
      return {settings: current};
    }
    if (path === base) return room([run], current);
    if (path === runPath) return {run};
  });
  await until(() => !get('settings').disabled);
  get('settings').click();
  await until(() => get('developer-skills').textContent.includes('테스트 규칙'));
  get('developer-name').value = '민수';
  get('developer-engine').value = 'gemini';
  get('room-delegation').checked = false;
  submit('settings-form');
  await until(() => !get('room-settings').open);
  assert.deepEqual(current, {
    delegation: false,
    developer: {name: '민수', engine: 'gemini', skills: [skillId]},
    reviewer: settings.reviewer,
  });
  assert.equal(person('developer').querySelector('.seat-name').textContent, '서진');
  person('coordinator').click();
  assert.match(get('run-delegation').options[0].textContent, /나에게 질문/);
});

test('developer chat preserves the draft and focus, then records the answer in the same conversation', async t => {
  let question = {id: '1'.repeat(32), kind: 'question', run_id: runId, role: 'developer', engine: 'codex',
    prompt: '어떤 테스트를 먼저 실행할까요?', status: 'pending'};
  let directions = [];
  const permission = {id: '2'.repeat(32), kind: 'permission', run_id: runId, role: 'developer', engine: 'codex',
    prompt: '이 폴더의 작업을 허용할까요?', status: 'pending'};
  const answer = '핵심 검증부터 실행해 주세요.';
  const {get, person, window} = mount(t, (path, body) => {
    if (path === base) return room([{...run, pending_count: 2}]);
    if (path === runPath) return {run};
    if (path === runPath + '/directions') return {directions};
    if (path === runPath + '/inbox') return {requests: [question, permission]};
    if (path === base + '/inbox') return {requests: [question, permission].filter(row => row.status === 'pending')};
    if (path === runPath + '/inbox/' + question.id + '/decision') {
      assert.deepEqual(body, {action: 'answer', answer});
      question = {...question, decision: 'answer', answer, status: 'decided'};
      return {request: question};
    }
  });
  await until(() => get('room-title').textContent === workspace.name);
  person('developer').click();
  const draft = () => get('chat-log').querySelector('[data-control="draft"]');
  await until(() => draft() && !draft().disabled);
  draft().value = answer;
  draft().dispatchEvent(new window.Event('input', {bubbles: true}));
  draft().focus();
  draft().setSelectionRange(2, 4);
  directions = [{id: '3'.repeat(32), role: 'developer', message: 'Windows 검증 추가', delivery: 'delivered'}];
  get('refresh').click();
  await until(() => get('chat-log').textContent.includes('Windows 검증 추가'));
  assert.equal(draft().value, answer);
  assert.equal(window.document.activeElement, draft());
  assert.equal(draft().selectionStart, 2);
  assert.equal(draft().selectionEnd, 4);
  get('chat-log').querySelector('[data-control="answer"]').click();
  await until(() => get('chat-request-note').textContent === '결정을 기록했습니다.' &&
    get('chat-log').textContent.includes(answer) && !draft());
  assert.equal(get('conversation').open, true);
  assert.equal(get('requests').open, false);
  const permissionAction = get('chat-log').querySelector('[data-control="inbox"]');
  assert.ok(permissionAction);
  assert.equal(get('chat-log').querySelector('[data-control="approve"]'), null);
  permissionAction.click();
  await until(() => get('request-log').querySelector('[data-control="approve"]'));
  assert.equal(window.document.activeElement.dataset.request, runId + ':' + permission.id);
  assert.equal(get('conversation').open, false);
  assert.equal(get('requests').open, true);
});

test('developer chat reads the latest decision state before sending an explicit answer', async t => {
  const question = {id: '1'.repeat(32), kind: 'product_decision', run_id: runId, role: 'developer', engine: 'codex',
    prompt: '기존 저장 흐름을 바꿀까요?', status: 'pending'};
  let changed = false;
  const {get, person, calls} = mount(t, path => {
    if (path === base) return room([{...run, pending_count: 1}]);
    if (path === runPath) return {run};
    if (path === runPath + '/inbox') return {requests: [changed ? {...question, prompt: '기존 저장 형식을 바꿀까요?'} : question]};
  });
  await until(() => get('room-title').textContent === workspace.name);
  person('developer').click();
  const button = () => get('chat-log').querySelector('[data-control="answer"]');
  await until(() => button() && !button().disabled);
  get('chat-log').querySelector('textarea').value = '현재 동작 유지';
  changed = true;
  button().click();
  await until(() => get('chat-request-note').textContent.includes('요청이 변경되었습니다.'));
  assert.equal(calls.some(call => call.path.endsWith('/decision')), false);
});

test('stopping an active remote run shows a stop request until termination is confirmed', async t => {
  let state = 'RUNNING';
  const prefix = '/api/hosts/' + remote;
  const {get, calls} = mount(t, (path, body) => {
    if (path === prefix + '/rooms/' + workspace.id) return room([{...run, state}]);
    if (path === prefix + '/runs/' + runId) return {run: {...run, state}};
    if (path === prefix + '/runs/' + runId + '/stop') {
      assert.deepEqual(body, {});
      state = 'STOP_REQUESTED';
      return {run: {...run, state}};
    }
  }, {query: '?host=' + remote + '&workspace=' + workspace.id + '&run=' + runId});
  await until(() => !get('stop-run').hidden && !get('stop-run').disabled);
  get('stop-run').click();
  await until(() => get('stop-note').textContent === '중지 요청을 접수했습니다.');
  assert.equal(get('stop-run').hidden, true);
  assert.equal(get('ledger-label').textContent, '중지 요청 중');
  state = 'CANCELED';
  get('refresh').click();
  await until(() => get('ledger-label').textContent === '중지됨');
  assert.equal(calls.filter(call => call.path.endsWith('/stop')).length, 1);
});

test('portrait chat exposes the evidence and delivery state of completed decisions', async t => {
  const receipt = (kind, id, status, fields = {}) => ({
    id: String(id).repeat(32), kind, run_id: runId, role: 'developer', engine: 'codex',
    prompt: '요청 ' + id, status, ...fields,
  });
  const revision = 'd'.repeat(40);
  const rows = [
    receipt('question', 1, 'delivered', {decision: 'answer', answer: '테스트 규칙을 사용하세요.', source: 'delegate',
      delegation_details: {specialist_engine: 'codex', verifier_engine: 'antigravity',
        evidence: {path: 'docs/test.md', line: 7, quote: '테스트 규칙은 <b>원문</b>', revision}}}),
    receipt('permission', 2, 'decided', {decision: 'reject'}),
    receipt('product_decision', 3, 'expired'),
  ];
  const {get, person} = mount(t, path => {
    if (path === base) return room();
    if (path === runPath) return {run};
    if (path === runPath + '/inbox') return {requests: rows};
  });
  await until(() => get('room-title').textContent === workspace.name);
  person('developer').click();
  await until(() => get('chat-log').textContent.includes('대리 결정 근거'));
  const log = get('chat-log');
  assert.match(log.textContent, /조정자 대리 결정/);
  assert.match(log.textContent, /에이전트에게 전달됨/);
  assert.match(log.textContent, /거절 · 결정 접수됨/);
  assert.match(log.textContent, /만료됨/);
  assert.ok(log.textContent.includes('docs/test.md 7행 · 기준 커밋 ' + revision));
  assert.ok(log.textContent.includes('테스트 규칙은 <b>원문</b>'));
  assert.match(log.textContent, /전문가 · Codex \/ 독립 검증자 · Antigravity/);
  assert.equal(log.querySelector('b'), null);
});

test('a selected developer run cannot mask another run actively assigned to the reviewer', async t => {
  const reviewId = 'e'.repeat(32);
  const reviewing = {...run, id: reviewId, task: '별도 검토 작업', state: 'REVIEWING',
    lines: {...run.lines, reviewer: '변경 사항을 검토하고 있습니다.'}};
  const {get, person, window} = mount(t, path => {
    if (path === base) return room([run, reviewing]);
    if (path === runPath) return {run};
    if (path === '/api/runs/' + reviewId) return {run: reviewing};
  });
  await until(() => get('room-title').textContent === workspace.name);
  assert.match(person('developer').textContent, /원인 확인/);
  assert.match(person('reviewer').textContent, /별도 검토 작업/);
  person('reviewer').click();
  assert.equal(get('chat-context').textContent, '별도 검토 작업');
  assert.equal(new URLSearchParams(window.location.search).get('run'), reviewId);
});

test('a deep link cannot read another workspace’s directions or issue inbox decisions', async t => {
  const outside = {...run, workspace_id: 'e'.repeat(32)};
  const ownRun = {...run, id: 'f'.repeat(32), pending_count: 1};
  const pending = {run_id: runId, id: '1'.repeat(32), role: 'developer', kind: 'permission', status: 'pending', prompt: '다른 작업실의 요청'};
  const ownPending = {...pending, run_id: ownRun.id, prompt: '이 작업실의 요청'};
  const {get, person, calls} = mount(t, path => {
    if (path === base) return room([ownRun]);
    if (path === runPath) return {run: outside};
    if (path === base + '/inbox') return {requests: [ownPending, pending]};
    if (path === '/api/inbox') return {requests: [pending]};
  }, {query: '?workspace=' + workspace.id + '&run=' + runId});
  await until(() => get('detail-status').textContent === '이 실행은 선택한 작업공간에 속하지 않습니다.');
  assert.equal(get('detail-link').hidden, true);
  assert.equal(get('stop-run').hidden, true);
  person('coordinator').click();
  assert.equal(get('conversation').open, true);
  assert.ok(!get('chat-log').textContent.includes(pending.prompt));
  get('conversation').close();
  get('inbox').click();
  await until(() => get('request-log').textContent.includes(ownPending.prompt));
  assert.ok(!get('request-log').textContent.includes(pending.prompt));
  assert.ok(!calls.some(call => call.path.startsWith(runPath + '/')));
  assert.ok(!calls.some(call => call.path === '/api/inbox'));
});

test('additional directions stay bound to the attempt visible when the portrait conversation opened', async t => {
  let attempt = 1;
  const {get, person, calls, submit} = mount(t, (path, body) => {
    if (path === base) return room([{...run, attempts: attempt}]);
    if (path === runPath) return {run: {...run, attempts: attempt}};
    if (path === runPath + '/directions' && body) {
      assert.equal(body.attempt, 2);
      return {direction: {...body, delivery: 'queued'}};
    }
  });
  await until(() => get('room-title').textContent === workspace.name);
  person('developer').click();
  await until(() => !get('send').disabled);
  get('message').value = '첫 시도용 지시';
  attempt = 2;
  get('refresh').click();
  await until(() => get('chat-note').textContent.includes('실행 시도가 바뀌었습니다.'));
  assert.equal(get('message').disabled, true);
  submit('chat-form');
  assert.ok(!calls.some(call => call.path.endsWith('/directions') && call.body));
  get('conversation').close();
  person('developer').click();
  await until(() => !get('send').disabled);
  assert.equal(get('message').value, '');
  get('message').value = '두 번째 시도에 확인할 지시';
  submit('chat-form');
  await until(() => calls.some(call => call.path.endsWith('/directions') && call.body?.attempt === 2));
});

test('a legacy run may show questions while withholding new additional directions', async t => {
  const legacy = {...run, assignment: null};
  const request = {id: '1'.repeat(32), run_id: runId, role: 'developer', engine: 'codex', kind: 'question',
    status: 'pending', prompt: '기존 실행에서 묻는 질문'};
  const {get, person} = mount(t, path => {
    if (path === base) return room([legacy]);
    if (path === runPath) return {run: legacy};
    if (path === runPath + '/inbox') return {requests: [request]};
  });
  await until(() => get('room-title').textContent === workspace.name);
  person('developer').click();
  await until(() => get('chat-log').querySelector('[data-control="answer"]')?.disabled === false);
  assert.equal(get('send').hidden, true);
  assert.match(get('chat-note').textContent, /작업실에서 새로 맡긴 작업/);
});

test('workspace history pages deduplicate against fresh polling and keep older links usable', async t => {
  const older = {...run, id: 'e'.repeat(32), task: '이전 작업', state: 'PLAN_ONLY'};
  const cursor = 'h-' + runId;
  const next = 'h-' + older.id;
  let latest = run;
  const {get, calls, window} = mount(t, path => {
    if (path === base) return {...room([latest]), next_cursor: cursor};
    if (path === base + '/history/' + cursor) {
      return {runs: [{...run, task: '겹치는 오래된 사본'}, older], next_cursor: next};
    }
    if (path === base + '/history/' + next) return {runs: [], next_cursor: null};
    if (path === runPath) return {run: latest};
    if (path === '/api/runs/' + older.id) return {run: older};
  });
  await until(() => !get('history-older').hidden && !get('history-older').disabled);
  get('history-older').click();
  await until(() => get('run-list').querySelectorAll('a').length === 2);
  assert.ok(!get('run-list').textContent.includes('겹치는 오래된 사본'));
  latest = {...latest, task: '최신 상태의 새 제목'};
  get('refresh').click();
  await until(() => get('run-list').textContent.includes(latest.task));
  assert.equal(get('run-list').querySelectorAll('a').length, 2);
  get('run-list').querySelectorAll('a')[1].click();
  await until(() => get('ledger-heading').textContent === older.task);
  assert.equal(new URLSearchParams(window.location.search).get('run'), older.id);
  get('history-older').click();
  await until(() => get('history-older').hidden);
  assert.deepEqual(calls.filter(call => call.path.includes('/history/')).map(call => call.path),
    [base + '/history/' + cursor, base + '/history/' + next]);
});

test('an in-flight history page never populates a different host’s workspace', async t => {
  const cursor = 'a-' + runId;
  const remoteRun = {...run, id: 'e'.repeat(32), task: '원격 작업'};
  const foreignPage = {...run, id: 'f'.repeat(32), task: '로컬 작업의 이전 기록'};
  const prefix = '/api/hosts/' + remote;
  let resolveHistory;
  const {get, window} = mount(t, path => {
    if (path === base) return {...room(), next_cursor: cursor};
    if (path === runPath) return {run};
    if (path === base + '/history/' + cursor) return new Promise(resolve => { resolveHistory = resolve; });
    if (path === prefix + '/rooms/' + workspace.id) return {...room([remoteRun]), next_cursor: null};
    if (path === prefix + '/runs/' + remoteRun.id) return {run: remoteRun};
  });
  await until(() => !get('history-older').hidden && !get('history-older').disabled);
  get('history-older').click();
  await until(() => resolveHistory);
  get('host').value = remote;
  get('host').dispatchEvent(new window.Event('change', {bubbles: true}));
  await until(() => get('run-list').textContent.includes(remoteRun.task));
  resolveHistory({runs: [foreignPage], next_cursor: null});
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(get('history-older').hidden, true);
  assert.equal(get('run-list').querySelectorAll('a').length, 1);
  assert.ok(!get('run-list').textContent.includes(foreignPage.task));
});
