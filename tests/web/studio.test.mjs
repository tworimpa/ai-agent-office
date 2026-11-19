import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import test from 'node:test';
import {JSDOM} from 'jsdom';
import {mountStudio} from '../../src/agent_office/web/studio.js';

const html = await readFile(new URL('../../src/agent_office/web/studio.html', import.meta.url), 'utf8');
const workspace = {id: 'a'.repeat(32), name: '주문 서비스', folders: ['/개발/화면', '/개발/규격']};
const runA = '1'.repeat(32), runB = '2'.repeat(32);
const remote = 'b'.repeat(32);
const hosts = [{id: 'local', name: '이 컴퓨터'}, {id: remote, name: '연구실'}];
const remoteAPI = '/api/hosts/' + remote;
const run = (id, state) => ({id, state, workspace_id: workspace.id, task: '주문 목록 확인'});
const stamp = '2026-11-19T00:00:00Z';
const catalog = {
  platform: 'win32', checked_at: stamp,
  engines: ['codex', 'antigravity', 'claude', 'gemini', 'cline'].map(engine => ({
    engine, installed: true, version: '1.2.3', probe_status: 'ok', headless_support: true,
    auth_status: 'authenticated', capabilities: {},
  })),
};

async function waitFor(predicate) {
  for (let i = 0; i < 100; i++) {
    if (predicate()) return;
    await new Promise(resolve => setTimeout(resolve, 5));
  }
  assert.ok(predicate(), '화면 상태 전환 시간 초과');
}

function mount(t, {workspaces = [workspace], runs = [], inbox = [], report, suffix = '', pending, pendingHost = 'local', route} = {}) {
  const dom = new JSDOM(html, {url: 'http://127.0.0.1:8766/' + suffix, pretendToBeVisual: true});
  const {window} = dom, doc = window.document;
  const key = 'agent-office.studio.pending' + (pendingHost === 'local' ? '' : '.' + pendingHost);
  if (pending) window.sessionStorage.setItem(key, JSON.stringify(pending));
  const requests = [];
  window.fetch = async (path, options) => {
    const method = options.method ?? 'GET';
    const body = options.body && JSON.parse(options.body);
    requests.push({path, method, body});
    if (method === 'POST') {
      assert.equal(options.headers['X-Agent-Office'], 'studio');
      assert.equal(options.headers['Content-Type'], 'application/json');
      assert.equal(options.mode, 'same-origin');
    }
    const override = route && await route(path, method, body);
    if (override) return override;
    if (path === '/api/hosts') return Response.json({hosts});
    if (path.startsWith(remoteAPI + '/')) path = '/api' + path.slice(remoteAPI.length);
    if (path === '/api/engines') return Response.json(catalog);
    if (path === '/api/return-report') return Response.json(report ?? {counts: {delegated: 0, questions: 0, permissions: 0}, delegated: [], active: []});
    if (path === '/api/inbox') return Response.json({requests: inbox.filter(row => row.status === 'pending')});
    if (path.endsWith('/inbox')) return Response.json({requests: inbox.filter(row => row.run_id === path.split('/')[3])});
    if (path.endsWith('/decision')) {
      const row = inbox.find(item => item.run_id === path.split('/')[3] && item.id === path.split('/')[5]);
      if (!row) return Response.json({}, {status: 404});
      Object.assign(row, {decision: body.action, answer: body.answer ?? null, status: 'decided'});
      return Response.json({request: row});
    }
    if (path === '/api/workspaces') {
      if (method === 'GET') return Response.json({workspaces});
      const added = {id: 'f'.repeat(32), name: body.name, folders: body.paths};
      workspaces.push(added);
      return Response.json({workspace: added}, {status: 201});
    }
    if (path === '/api/runs' && method === 'GET') return Response.json({runs});
    if (path === '/api/runs') {
      const added = {...body, state: 'QUEUED'};
      runs.unshift(added);
      return Response.json({run: added}, {status: 202});
    }
    const item = runs.find(r => r.id === path.split('/')[3]);
    if (!item) return Response.json({error: {code: 'NOT_FOUND'}}, {status: 404});
    if (method === 'POST') item.state = 'STOP_REQUESTED';
    return Response.json({run: item});
  };
  const dispose = mountStudio(window);
  t.after(() => { dispose(); dom.window.close(); });
  return {window, doc, get: id => doc.getElementById(id), requests};
}

test('register local workspace, choose primary and extra folders, start in planning mode', async t => {
  const {get, doc, window, requests} = mount(t, {workspaces: []});
  assert.equal(get('office-link').getAttribute('href'), '/office');
  await waitFor(() => get('registration').open);
  assert.ok(get('start').disabled);
  get('name').value = '한글 프로젝트';
  get('paths').value = '/개발/화면\n/개발/공통 규격';
  get('register').click();
  await waitFor(() => get('primary').options.length === 2);
  get('primary').value = '1';
  get('primary').dispatchEvent(new window.Event('change'));
  doc.querySelector('#extras input').click();
  get('task').value = '구조를 설명해 줘';
  get('start').click();
  await waitFor(() => !get('detail-body').hidden);
  const request = requests.find(r => r.path === '/api/runs' && r.method === 'POST').body;
  assert.match(request.id, /^[0-9a-f]{32}$/);
  assert.equal(request.allow_edits, false);
  assert.equal(request.use_inbox, true);
  assert.ok(!Object.hasOwn(request, 'subtasks'));
  assert.ok(!Object.hasOwn(request, 'role_skills'));
  assert.ok(!Object.hasOwn(request, 'delegation'));
  assert.equal(request.developer_engine, 'codex');
  assert.equal(request.reviewer_engine, 'antigravity');
  assert.deepEqual(request.indices, [1, 0]);
  assert.equal(get('request').textContent, '구조를 설명해 줘');
  assert.equal(new URL(window.location.href).searchParams.get('run'), request.id);
  assert.equal(get('run-state').textContent, '대기 중');
});

test('an ambiguous POST and page reload retain the same operation and edit choice', async t => {
  const first = mount(t, {route: (path, method) => {
    if (path === '/api/runs' && method === 'POST') throw new Error('lost response');
  }});
  await waitFor(() => !first.get('start').disabled);
  first.get('task').value = '코드 수정 및 검증';
  first.get('developer-engine').value = 'claude';
  first.get('reviewer-engine').value = 'cline';
  first.get('edit-mode').click();
  first.get('use-inbox').click();
  first.get('start').click();
  await waitFor(() => first.get('start-note').textContent.includes('같은 요청'));
  const sent = first.requests.find(r => r.method === 'POST').body;
  assert.equal(sent.allow_edits, true);
  assert.ok(!Object.hasOwn(sent, 'use_inbox'));
  assert.equal(sent.developer_engine, 'claude');
  assert.equal(sent.reviewer_engine, 'cline');
  assert.ok(first.get('run-fields').disabled);
  const saved = JSON.parse(first.window.sessionStorage.getItem('agent-office.studio.pending'));
  const second = mount(t, {pending: saved});
  await waitFor(() => second.get('task').value === sent.task);
  assert.equal(second.requests.filter(r => r.method === 'POST').length, 0);
  assert.equal(second.get('edit-mode').checked, true);
  assert.equal(second.get('use-inbox').checked, false);
  assert.equal(second.get('developer-engine').value, 'claude');
  assert.equal(second.get('reviewer-engine').value, 'cline');
  second.get('start').click();
  await waitFor(() => second.get('run-id').textContent === sent.id);
  assert.deepEqual(second.requests.find(r => r.method === 'POST').body, sent);
  assert.equal(second.window.sessionStorage.getItem('agent-office.studio.pending'), null);
  assert.equal(second.get('run-engines').textContent, '개발자 Claude Code · 리뷰어 Cline CLI');
});

test('a receipt missing the explicitly requested inbox mode does not confirm the request or clear its draft', async t => {
  const items = [];
  const {get, window, requests} = mount(t, {runs: items, route(path, method, body) {
    if (method === 'POST' && path === '/api/runs') {
      const item = {...body, use_inbox: false, state: 'RUNNING'};
      items.push(item);
      return Response.json({run: item}, {status: 202});
    }
  }});
  await waitFor(() => !get('start').disabled);
  get('task').value = '질문 응답 필요';
  get('start').click();
  await waitFor(() => get('start-note').textContent.includes('설정을 확인할 수 없습니다'));
  assert.equal(get('task').value, '질문 응답 필요');
  assert.ok(get('use-inbox').checked);
  assert.equal(window.sessionStorage.getItem('agent-office.studio.pending'), null);
  assert.equal(requests.filter(req => req.method === 'POST').length, 1);
});

test('reopening a run exposes literal evidence; stale responses cannot replace a new selection', async t => {
  let release, slow = false;
  const a = {...run(runA, 'FAILED'), developer_output: '<img src=x onerror=alert(1)>', test_output: 'pytest failed', review_output: '검토 기록', changes: ' M web/app.js', diff: '+수정',
    allow_edits: true, workspace: '/worktree/작업', folders: ['화면', '규격'], branch: 'agent-office/work'};
  const b = run(runB, 'PLAN_ONLY');
  const {get, doc, window} = mount(t, {runs: [b, a], suffix: '?run=' + runA, route: (path) => {
    if (slow && path === '/api/runs/' + runA) return new Promise(resolve => { release = () => resolve(Response.json({run: a})); });
  }});
  await waitFor(() => get('run-id').textContent === runA);
  assert.equal(get('run-state').textContent, '실패');
  assert.equal(get('test-command').hidden, false);
  assert.equal(get('context-path').textContent, a.workspace);
  assert.equal(get('context-branch').textContent, a.branch);
  assert.equal(get('run-engines').textContent, '개발자 Codex · 리뷰어 Antigravity');
  assert.equal(get('context-primary').textContent, a.folders[0]);
  assert.equal(get('context-extras').textContent, a.folders[1]);
  for (const id of ['developer_output', 'test_output', 'review_output', 'changes', 'diff']) assert.equal(get(id).textContent, a[id]);
  assert.equal(doc.querySelector('img'), null);
  assert.ok(get('stop').hidden);
  slow = true;
  get('refresh').click();
  await waitFor(() => release);
  doc.querySelector('[data-run-id="' + runB + '"]').click();
  await waitFor(() => get('run-id').textContent === runB);
  release();
  await new Promise(resolve => setTimeout(resolve, 5));
  assert.equal(get('run-id').textContent, runB);
  assert.equal(get('run-state').textContent, '계획 작성 완료');
  assert.equal(get('test-command').hidden, true);
  assert.equal(new URL(window.location.href).searchParams.get('run'), runB);
});

test('stop request and confirmed termination render as different states', async t => {
  const item = run(runA, 'TESTING');
  const {get, requests} = mount(t, {runs: [item]});
  await waitFor(() => get('run-state').textContent === '테스트 중');
  get('stop').click();
  await waitFor(() => get('run-state').textContent === '중지 요청 중');
  assert.deepEqual(requests.find(r => r.method === 'POST').body, {});
  assert.ok(get('stop').hidden);
  item.state = 'CANCELED';
  get('refresh').click();
  await waitFor(() => get('run-state').textContent === '중지됨');
});

test('a catalog refresh preserves the selected folders and enforces the UTF-8 request limit', async t => {
  const workspaces = [workspace];
  const {get, doc, window, requests} = mount(t, {workspaces});
  await waitFor(() => !get('start').disabled);
  get('primary').value = '1';
  get('primary').dispatchEvent(new window.Event('change'));
  doc.querySelector('#extras input').click();
  workspaces.push({...workspace, id: runB, name: '새 작업공간'});
  get('refresh').click();
  await waitFor(() => get('workspace').options.length === 2);
  assert.equal(get('primary').value, '1');
  assert.equal(doc.querySelector('#extras input:checked').value, '0');
  get('task').value = '한'.repeat(2731);
  get('start').click();
  assert.match(get('start-note').textContent, /8,192바이트/);
  assert.equal(requests.filter(r => r.method === 'POST').length, 0);
  get('task').value = '한'.repeat(2730) + 'ab';
  get('start').click();
  await waitFor(() => !get('detail-body').hidden);
  assert.equal(requests.filter(r => r.method === 'POST').length, 1);
});

test('detail distinguishes a staged failure, an interrupted setup and a cancellation', async t => {
  const item = {...run(runA, 'FAILED'), allow_edits: true, outcome: 'TEST_FAILED', attempts: 2};
  const {get} = mount(t, {runs: [item]});
  await waitFor(() => get('outcome').textContent === '테스트가 실패했습니다.');
  assert.equal(get('attempts').textContent, '개발 시도 2회');
  assert.equal(get('test-command').hidden, false);
  assert.equal(get('retry-note').hidden, false);
  Object.assign(item, {state: 'RECOVERY_REQUIRED', outcome: 'INTERRUPTED', attempts: 0, diagnostic: '작업 폴더를 준비하지 못했습니다.'});
  get('refresh').click();
  await waitFor(() => get('diagnostic').textContent === item.diagnostic);
  assert.ok(get('outcome').textContent.includes('중단'));
  assert.equal(get('attempts').textContent, '개발 시도 0회');
  assert.equal(get('test-command').hidden, true);
  assert.equal(get('retry-note').hidden, true);
  Object.assign(item, {state: 'CANCELED', outcome: 'CANCELED', diagnostic: ''});
  get('refresh').click();
  await waitFor(() => get('run-state').textContent === '중지됨');
  assert.ok(get('outcome').textContent.includes('중지 요청'));
  assert.equal(get('diagnostic').hidden, true);
});

test('a queued edit shows no executed test and the diff preserves the server preview as text', async t => {
  const item = {...run(runA, 'QUEUED'), allow_edits: true};
  const {get} = mount(t, {runs: [item]});
  await waitFor(() => get('run-state').textContent === '대기 중');
  assert.equal(get('test-command').hidden, true);
  Object.assign(item, {state: 'FAILED', outcome: 'DEVELOPER_FAILED', changes: '?? new-file.html\n?? image.png',
    diff: '+++ b/new-file.html\n+<img src=x>\n일부 새 파일은 변경 파일 목록과 작업 폴더에서 확인하세요.'});
  get('refresh').click();
  await waitFor(() => get('run-state').textContent === '실패');
  assert.equal(get('test-command').hidden, true);
  assert.equal(get('diff').textContent, item.diff);
  assert.equal(get('diff').querySelector('img'), null);
  item.diff = '+tracked change';
  get('refresh').click();
  await waitFor(() => get('diff').textContent === item.diff);
  assert.equal(get('diff').hidden, false);
});

test('host links discard previous run IDs and a remote host owns registration, start and stop', async t => {
  const {doc, get, window, requests} = mount(t, {suffix: '?host=' + remote, workspaces: []});
  await waitFor(() => get('registration').open && !get('register').disabled);
  const current = doc.querySelector('#hosts [aria-current=page]');
  assert.equal(get('office-link').getAttribute('href'), '/office?host=' + remote);
  assert.equal(get('improvements-link').getAttribute('href'), '/improvements?host=' + remote);
  assert.equal(get('versions-link').getAttribute('href'), '/versions?host=' + remote);
  assert.equal(current.textContent, '연구실');
  assert.equal(current.getAttribute('href'), '/?host=' + remote);
  assert.equal(get('hosts').querySelector('a').getAttribute('href'), '/');
  assert.match(get('paths-help').textContent, /연구실/);
  get('name').value = '원격 저장소';
  get('paths').value = '/srv/repos/app\n/srv/repos/app/docs';
  get('register').click();
  await waitFor(() => !get('start').disabled);
  doc.querySelector('#extras input').click();
  get('task').value = '문서 검토';
  get('developer-engine').value = 'gemini';
  get('reviewer-engine').value = 'codex';
  get('start').click();
  await waitFor(() => !get('stop').hidden);
  const added = requests.find(r => r.method === 'POST' && r.path === remoteAPI + '/runs');
  assert.deepEqual(added.body.indices, [0, 1]);
  assert.equal(added.body.developer_engine, 'gemini');
  assert.equal(added.body.reviewer_engine, 'codex');
  assert.equal(get('run-engines').textContent, '개발자 Gemini CLI · 리뷰어 Codex');
  assert.deepEqual(requests.find(r => r.method === 'POST' && r.path.endsWith('/workspaces')).body.paths,
    ['/srv/repos/app', '/srv/repos/app/docs']);
  assert.equal(window.location.search, '?host=' + remote + '&run=' + added.body.id);
  get('stop').click();
  await waitFor(() => get('run-state').textContent === '중지 요청 중');
  assert.ok(requests.some(r => r.method === 'POST' && r.path === remoteAPI + '/runs/' + added.body.id + '/stop'));
  assert.ok(requests.every(r => r.path === '/api/hosts' || r.path.startsWith(remoteAPI + '/')));
});

test('a remote run link retains the last detail during loss and reads the completed result on reconnect', async t => {
  const item = {...run(runA, 'TESTING'), developer_output: '구현 완료'};
  let disconnected = false;
  const {get, requests, window} = mount(t, {suffix: '?host=' + remote + '&run=' + runA, runs: [item], route: path => {
    if (disconnected && path.startsWith(remoteAPI + '/')) return Response.json({}, {status: 503});
  }});
  await waitFor(() => get('developer_output').textContent === '구현 완료');
  disconnected = true;
  get('refresh').click();
  await waitFor(() => get('connection').dataset.online === 'false');
  assert.equal(get('run-state').textContent, '테스트 중');
  assert.equal(get('detail-body').hidden, false);
  assert.match(get('detail-note').textContent, /마지막/);
  assert.ok(get('stop').disabled && get('start').disabled && get('register').disabled);
  Object.assign(item, {state: 'READY_FOR_HUMAN_REVIEW', test_output: '2 passed', diff: '+문서', review_output: '검토 완료'});
  disconnected = false;
  get('refresh').click();
  await waitFor(() => get('review_output').textContent === item.review_output);
  assert.equal(get('diff').textContent, item.diff);
  assert.equal(get('test_output').textContent, item.test_output);
  assert.ok(get('detail-note').hidden);
  assert.equal(window.location.search, '?host=' + remote + '&run=' + runA);
  assert.ok(requests.every(r => r.method === 'GET'));
});

test('an accepted remote operation with a lost response is recovered by host and ID after reload', async t => {
  const items = [run(runA, 'PLAN_ONLY')];
  let offline = false;
  const first = mount(t, {suffix: '?host=' + remote, runs: items, route: (path, method, body) => {
    if (path === remoteAPI + '/runs' && method === 'POST') {
      items.push({...body, state: 'PLAN_ONLY', developer_output: '완료'});
      offline = true;
    }
    if (offline && path.startsWith(remoteAPI + '/')) throw new Error('연결 중단');
  }});
  await waitFor(() => first.get('run-id').textContent === runA);
  first.get('task').value = '원격 작업 확인';
  first.get('developer-engine').value = 'cline';
  first.get('reviewer-engine').value = 'gemini';
  first.get('start').click();
  await waitFor(() => first.get('connection').dataset.online === 'false');
  const remoteKey = 'agent-office.studio.pending.' + remote;
  const saved = JSON.parse(first.window.sessionStorage.getItem(remoteKey));
  assert.equal(saved.id, items[1].id);
  const runLink = '?host=' + remote + '&run=' + saved.id;
  assert.equal(first.window.location.search, runLink);
  const local = mount(t, {pending: saved, pendingHost: remote});
  await waitFor(() => !local.get('start').disabled);
  assert.equal(local.get('task').value, '');
  assert.equal(local.get('developer-engine').value, 'codex');
  assert.ok(!local.requests.some(r => r.path.includes(saved.id)));
  assert.ok(local.window.sessionStorage.getItem(remoteKey));
  const recovered = mount(t, {suffix: runLink, pending: saved, pendingHost: remote, runs: items});
  await waitFor(() => recovered.get('developer_output').textContent === '완료');
  assert.equal(recovered.window.sessionStorage.getItem(remoteKey), null);
  assert.equal(recovered.get('run-id').textContent, saved.id);
  assert.equal(recovered.get('run-engines').textContent, '개발자 Cline CLI · 리뷰어 Gemini CLI');
  assert.ok(recovered.requests.some(r => r.path === remoteAPI + '/runs/' + saved.id));
  assert.ok(recovered.requests.every(r => r.method === 'GET'));
  offline = false;
  first.get('refresh').click();
  await waitFor(() => first.get('developer_output').textContent === '완료');
  assert.equal(first.get('run-id').textContent, saved.id);
  assert.equal(first.window.location.search, runLink);
  assert.equal(first.requests.filter(r => r.method === 'POST').length, 1);
});

test('an explicit selection after submission survives a delayed receipt', async t => {
  let release;
  const items = [run(runA, 'PLAN_ONLY')];
  const {doc, get, window} = mount(t, {suffix: '?host=' + remote, runs: items, route: (path, method, body) => {
    if (path === remoteAPI + '/runs' && method === 'POST') {
      const added = {...body, state: 'RUNNING'};
      items.push(added);
      return new Promise(resolve => { release = () => resolve(Response.json({run: added}, {status: 202})); });
    }
  }});
  await waitFor(() => get('run-id').textContent === runA);
  get('task').value = '새 실행';
  get('start').click();
  await waitFor(() => release);
  assert.equal(window.location.search, '?host=' + remote + '&run=' + items[1].id);
  doc.querySelector('[data-run-id="' + runA + '"]').click();
  release();
  await waitFor(() => get('start-note').textContent.includes('접수했습니다') && !get('detail-body').hidden);
  assert.equal(window.location.search, '?host=' + remote + '&run=' + runA);
  assert.equal(get('run-id').textContent, runA);
});

test('gateway failures cannot discard an ambiguous request or reissue it on reconnect', async t => {
  const pending = {id: runB, workspace_id: workspace.id, indices: [0], task: '재시도', allow_edits: false};
  let missingHost = false;
  const {get, window, requests} = mount(t, {suffix: '?host=' + remote, pending, pendingHost: remote,
    route: (path, method) => {
      if (path === remoteAPI + '/runs' && method === 'POST') missingHost = true;
      if (missingHost && path.startsWith(remoteAPI + '/')) return Response.json({error: {code: 'UNKNOWN_HOST'}}, {status: 404});
    },
  });
  await waitFor(() => get('start-note').textContent.includes('아직 접수'));
  get('start').click();
  await waitFor(() => get('connection').dataset.online === 'false');
  assert.equal(window.location.search, '?host=' + remote + '&run=' + runB);
  assert.deepEqual(JSON.parse(window.sessionStorage.getItem('agent-office.studio.pending.' + remote)), pending);
  missingHost = false;
  get('refresh').click();
  await waitFor(() => get('start-note').textContent.includes('아직 접수'));
  assert.equal(requests.filter(r => r.method === 'POST').length, 1);
});

test('a deliberate retry checks the remote run ID before sending the same request', async t => {
  const pending = {id: runB, workspace_id: workspace.id, indices: [0], task: '재시도', allow_edits: false};
  const {get, requests} = mount(t, {suffix: '?host=' + remote, pending, pendingHost: remote});
  await waitFor(() => get('start-note').textContent.includes('아직 접수'));
  assert.equal(get('developer-engine').value, 'codex');
  assert.equal(get('use-inbox').checked, false);
  assert.equal(get('reviewer-engine').value, 'antigravity');
  const before = requests.length;
  get('start').click();
  await waitFor(() => get('run-id').textContent === runB);
  assert.deepEqual(requests.slice(before, before + 2), [
    {path: remoteAPI + '/runs/' + runB, method: 'GET', body: undefined},
    {path: remoteAPI + '/runs', method: 'POST', body: pending},
  ]);
});

test('a slow remote catalog leaves role selection usable and separates CLI metadata from dated fixture evidence', async t => {
  let release, available = false;
  const remoteCatalog = structuredClone(catalog);
  remoteCatalog.platform = 'linux';
  const cline = remoteCatalog.engines.find(row => row.engine === 'cline');
  Object.assign(cline, {installed: false, probe_status: 'missing', version: null, headless_support: false, auth_status: 'unknown'});
  const gemini = remoteCatalog.engines.find(row => row.engine === 'gemini');
  Object.assign(gemini, {version: '4.5.6', protocol: 'acp', probe_status: 'pending', headless_support: false});
  const {get, window, requests} = mount(t, {suffix: '?host=' + remote, route: path => {
    if (path === remoteAPI + '/engines') {
      if (available) return Response.json(remoteCatalog);
      return new Promise(resolve => { release = () => resolve(Response.json(remoteCatalog)); });
    }
  }});
  await waitFor(() => release && !get('start').disabled);
  const picker = get('developer-engine');
  assert.deepEqual(Array.from(picker.options, o => o.value), ['codex', 'antigravity', 'claude', 'gemini', 'cline']);
  picker.value = 'cline';
  picker.focus();
  get('refresh').click();
  await waitFor(() => requests.filter(r => r.path === remoteAPI + '/runs').length > 1);
  assert.equal(requests.filter(r => r.path.endsWith('/engines')).length, 1);
  available = true;
  release();
  await waitFor(() => get('engine-status').textContent.includes('진단 중'));
  assert.match(get('engine-status').textContent, /연구실 · linux/);
  assert.match(get('engine-warning').textContent, /개발자 Cline CLI 실행 파일/);
  assert.equal(get('start').disabled, false);
  assert.doesNotMatch(get('engine-list').textContent, /통과/);
  picker.value = 'gemini';
  picker.dispatchEvent(new window.Event('change'));
  assert.equal(get('engine-warning').textContent, '');
  Object.assign(gemini, {probe_status: 'ok', headless_support: true,
    capabilities: {read: {status: 'passed', checked_at: stamp}, edit: {status: 'failed', checked_at: stamp},
      review: {status: 'not_checked', checked_at: null}}});
  get('refresh').click();
  await waitFor(() => get('engine-list').textContent.includes('읽기 통과'));
  const row = Array.from(get('engine-list').children).find(li => li.firstChild.textContent === 'Gemini CLI');
  assert.match(row.textContent, /4\.5\.6.*로그인 확인.*프로토콜 acp.*비대화형 옵션 확인/);
  assert.match(row.textContent, /읽기 통과 \([^)]+\) \/ 수정 실패 \([^)]+\) \/ 리뷰 미검증/);
  assert.equal(picker.value, 'gemini');
  assert.equal(window.document.activeElement, picker);
  Object.assign(cline, {installed: true, version: '9.9.9', probe_status: 'unsupported_version'});
  remoteCatalog.engines.find(row => row.engine === 'claude').probe_status = 'unknown_version';
  get('reviewer-engine').value = 'cline';
  get('refresh').click();
  await waitFor(() => get('engine-warning').textContent.includes('리뷰어 Cline CLI의 설치 버전'));
  assert.match(get('engine-list').textContent, /작업실에서 지원하지 않는 버전/);
  assert.match(get('engine-list').textContent, /CLI 버전 확인 실패/);
  assert.equal(get('start').disabled, false);
  assert.ok(requests.every(r => r.method === 'GET' && (r.path === '/api/hosts' || r.path.startsWith(remoteAPI + '/'))));
});

test('diagnostic failure keeps the last host snapshot marked stale and recovers independently of run controls', async t => {
  let unavailable = true;
  const {get, requests} = mount(t, {route: path => {
    if (unavailable && path === '/api/engines') return Response.json({}, {status: 503});
  }});
  await waitFor(() => !get('start').disabled && get('engine-status').textContent.includes('불러오지 못했습니다'));
  assert.equal(get('engine-list').children.length, 0);
  get('reviewer-engine').value = 'claude';
  unavailable = false;
  get('refresh').click();
  await waitFor(() => get('engine-status').textContent.includes('win32'));
  assert.equal(get('engine-list').children.length, 5);
  assert.equal(get('engine-warning').textContent, '');
  unavailable = true;
  get('refresh').click();
  await waitFor(() => get('engine-status').textContent.includes('마지막으로 받은 진단'));
  assert.equal(get('engine-list').children.length, 5);
  assert.match(get('engine-warning').textContent, /최신 엔진 진단/);
  assert.equal(get('reviewer-engine').value, 'claude');
  assert.ok(requests.every(r => r.method === 'GET'));
});

test('unknown, empty and ambiguous host links fail closed without local lookups or mutations', async t => {
  for (const id of ['c'.repeat(32), '', 'local&host=' + remote]) {
    const {get, requests} = mount(t, {suffix: '?host=' + id + '&run=' + runA});
    await waitFor(() => get('connection').dataset.online === 'false');
    assert.ok(get('start').disabled && get('register').disabled);
    assert.equal(get('connection').textContent, '등록되지 않은 호스트');
    assert.ok(requests.every(r => r.path === '/api/hosts' && r.method === 'GET'));
  }
});

const question = () => ({id: 'd'.repeat(32), run_id: runA, role: 'developer', engine: 'codex', kind: 'question',
  prompt: '출력 형식을 지정해 주세요.\n<img src=x>', status: 'pending', decision: null, answer: null, created_at: stamp});
const permission = () => ({...question(), id: 'e'.repeat(32), run_id: runB, engine: 'cline', kind: 'permission',
  prompt: 'editor\n{"path":"<main>","text":"  들여쓰기"}'});

test('unavailable peer inbox preserves drafts and run controls and lets the operator explicitly start a legacy run', async t => {
  let unavailable = false;
  const q = question();
  const {get, requests} = mount(t, {
    suffix: '?host=' + remote, runs: [run(runA, 'RUNNING')], inbox: [q],
    route(path) {
      if (unavailable && path.endsWith('/inbox')) return Response.json({}, {status: 503});
    },
  });
  await waitFor(() => get('inbox-list').querySelector('textarea') && !get('stop').hidden);
  const draft = get('inbox-list').querySelector('textarea');
  draft.value = '입력 중인 답변';
  unavailable = true;
  get('refresh').click();
  await waitFor(() => draft.matches(':disabled') && get('history-inbox-note').textContent.includes('확인할 수 없습니다'));
  assert.equal(get('connection').dataset.online, 'true');
  assert.equal(draft.value, '입력 중인 답변');
  assert.ok(get('use-inbox').checked && get('start').disabled);
  assert.match(get('inbox-note').textContent, /요청함을 끄면/);
  assert.ok(!get('stop').disabled);
  get('stop').click();
  await waitFor(() => get('run-state').textContent === '중지 요청 중');
  get('task').value = '원격 구버전 작업';
  get('use-inbox').click();
  assert.ok(!get('start').disabled);
  get('start').click();
  await waitFor(() => get('request').textContent === '원격 구버전 작업');
  const post = requests.find(req => req.method === 'POST' && req.path === remoteAPI + '/runs');
  assert.ok(!Object.hasOwn(post.body, 'use_inbox'));
  assert.equal(requests.filter(req => req.method === 'POST' && req.path.endsWith('/decision')).length, 0);
  unavailable = false;
  get('refresh').click();
  await waitFor(() => !draft.matches(':disabled'));
  assert.equal(get('inbox-list').querySelector('textarea'), draft);
  assert.equal(draft.value, '입력 중인 답변');
});

test('remote inbox preserves a draft, focus and exact text while polling, and scopes each explicit decision', async t => {
  const q = question(), p = permission();
  const {doc, get, requests, window} = mount(t, {suffix: '?host=' + remote, runs: [run(runA, 'RUNNING'), run(runB, 'RUNNING')], inbox: [q, p]});
  await waitFor(() => get('inbox-list').children.length === 2);
  const [qc, pc] = get('inbox-list').children;
  assert.equal(qc.querySelector('pre').textContent, q.prompt);
  assert.equal(pc.querySelector('pre').textContent, p.prompt);
  assert.equal(get('inbox-list').querySelector('pre *'), null);
  assert.equal(pc.querySelector('textarea'), null);
  assert.equal(qc.querySelectorAll('button').length, 1);
  assert.equal(pc.querySelector('a').getAttribute('href'), '/?host=' + remote + '&run=' + runB + '#detail');
  const draft = qc.querySelector('textarea');
  draft.value = '  형식은 JSON\n한글 포함  ';
  get('refresh').click();
  draft.focus();
  await waitFor(() => requests.filter(r => r.path === remoteAPI + '/inbox').length === 2);
  assert.equal(doc.activeElement, draft);
  assert.equal(draft.labels[0].textContent, '답변 (최대 8,192바이트)');
  assert.deepEqual(requests.filter(r => r.method === 'POST'), []);
  pc.querySelector('a').click();
  await waitFor(() => get('run-id').textContent === runB);
  assert.equal(new URL(window.location.href).searchParams.get('run'), runB);
  qc.querySelector('button').click();
  await waitFor(() => get('inbox-list').children.length === 1);
  assert.deepEqual(requests.find(r => r.method === 'POST'), {
    path: remoteAPI + '/runs/' + runA + '/inbox/' + q.id + '/decision', method: 'POST', body: {action: 'answer', answer: draft.value},
  });
  pc.querySelector('button').click();
  await waitFor(() => get('history-inbox-list').textContent.includes('이번 요청 승인'));
  assert.deepEqual(requests.filter(r => r.method === 'POST')[1].body, {action: 'approve'});
  assert.equal(get('run-id').textContent, runB);
});

test('a conflicting or lost decision receipt resolves from that run’s history even when another run is open', async t => {
  for (const conflict of [true, false]) {
    const p = permission();
    let offline = false;
    const {get, requests} = mount(t, {runs: [run(runA, 'RUNNING'), run(runB, 'RUNNING')], inbox: [p], route(path, method) {
      if (method === 'POST') {
        Object.assign(p, {status: 'delivered', decision: conflict ? 'approve' : 'reject'});
        if (conflict) return Response.json({}, {status: 409});
        offline = true;
        throw new Error('lost decision receipt');
      }
      if (offline && path !== '/api/hosts') throw new Error('offline');
    }});
    await waitFor(() => get('run-id').textContent === runA);
    const reject = get('inbox-list').querySelectorAll('button')[1];
    reject.click();
    if (!conflict) {
      await waitFor(() => get('connection').dataset.online === 'false');
      assert.ok(reject.matches(':disabled'));
      reject.click();
      offline = false;
      get('refresh').click();
    }
    await waitFor(() => get('decision-note').textContent.includes(conflict ? '이번 요청 승인' : '거절'));
    assert.match(get('decision-note').textContent, /작업자에게 전달됨/);
    const post = requests.findIndex(r => r.method === 'POST');
    assert.equal(requests[post].body.action, 'reject');
    assert.ok(requests.slice(post + 1).some(r => r.path === '/api/runs/' + runB + '/inbox'));
    assert.equal(requests.filter(r => r.method === 'POST').length, 1);
    assert.equal(get('run-id').textContent, runA);
  }
});

test('delayed request history cannot replace a new run’s expired decision and answer', async t => {
  const q = {...question(), status: 'expired', answer: '첫 번째 실행', decision: 'answer'};
  const p = {...permission(), status: 'expired', decision: 'reject'};
  let release;
  const {doc, get, requests} = mount(t, {runs: [run(runA, 'CANCELED'), run(runB, 'CANCELED')], inbox: [q, p],
    route(path) {
      if (path === '/api/runs/' + runA + '/inbox') return new Promise(resolve => {
        release = () => resolve(Response.json({requests: [q]}));
      });
    }});
  await waitFor(() => release);
  doc.querySelector('[data-run-id="' + runB + '"]').click();
  await waitFor(() => get('history-inbox-list').textContent.includes('만료됨 · 거절'));
  release();
  await new Promise(resolve => setTimeout(resolve, 5));
  assert.equal(get('history-inbox-list').children.length, 1);
  assert.ok(!get('history-inbox-list').textContent.includes(q.answer));
  assert.ok(requests.every(r => r.method === 'GET'));
});

test('away policy requires explicit topics and survives an ambiguous remote submission unchanged', async t => {
  const ui = mount(t, {suffix: '?host=' + remote, route(path, method) {
    if (method === 'POST') throw new Error('lost receipt');
  }});
  await waitFor(() => !ui.get('start').disabled && !ui.get('use-delegation').disabled);
  ui.get('task').value = '원격 위임 작업';
  assert.ok(!ui.get('use-delegation').checked && ui.get('delegation-fields').hidden);
  ui.get('use-delegation').click();
  assert.ok(ui.get('use-inbox').checked && ui.get('use-inbox').disabled);
  ui.get('start').click();
  assert.equal(ui.doc.activeElement, ui.get('topic-test_command'));
  assert.ok(ui.requests.every(req => req.method === 'GET'));
  ui.get('topic-python_version').click();
  ui.get('path-python_version').value = 'service/pyproject.toml';
  ui.get('specialist-engine').value = 'gemini';
  ui.get('verifier-engine').value = 'gemini';
  ui.get('start').click();
  await waitFor(() => ui.get('start-note').textContent.includes('같은 요청'));
  const sent = ui.requests.find(req => req.method === 'POST').body;
  const policy = {rules: [{topic: 'python_version', path: 'service/pyproject.toml'}], specialist_engine: 'gemini', verifier_engine: 'gemini'};
  assert.deepEqual(sent.delegation, policy);
  assert.equal(sent.use_inbox, true);
  const saved = JSON.parse(ui.window.sessionStorage.getItem('agent-office.studio.pending.' + remote));
  assert.deepEqual(saved, sent);
  const other = mount(t, {pending: saved, pendingHost: remote});
  await waitFor(() => !other.get('start').disabled);
  assert.ok(!other.get('use-delegation').checked);
  assert.equal(other.get('task').value, '');
  const reload = mount(t, {suffix: '?host=' + remote, pending: saved, pendingHost: remote});
  await waitFor(() => reload.get('start-note').textContent.includes('아직 접수'));
  assert.ok(reload.get('use-delegation').checked && reload.get('topic-python_version').checked);
  assert.equal(reload.get('path-python_version').value, 'service/pyproject.toml');
  assert.equal(reload.get('verifier-engine').value, 'gemini');
  assert.ok(reload.get('use-delegation').matches(':disabled'));
  assert.ok(!reload.get('topic-node_version').checked);
  reload.get('start').click();
  await waitFor(() => reload.get('start-note').textContent.includes('접수했습니다'));
  assert.deepEqual(reload.requests.filter(req => req.method === 'POST'), [{path: remoteAPI + '/runs', method: 'POST', body: sent}]);
  assert.ok(!reload.get('use-delegation').checked && !reload.get('topic-python_version').checked);
});

test('a peer with a missing or stale report still supports manual decisions and runs without silently dropping a policy', async t => {
  let unavailable = false;
  const q = question();
  const {get, requests} = mount(t, {suffix: '?host=' + remote, inbox: [q], runs: [run(runA, 'RUNNING')], route(path) {
    if (unavailable && path.endsWith('/return-report')) return Response.json({}, {status: 404});
  }});
  await waitFor(() => !get('use-delegation').disabled && !get('start').disabled);
  get('use-delegation').click();
  get('topic-test_command').click();
  get('task').value = '위임 가용성 확인';
  unavailable = true;
  get('refresh').click();
  await waitFor(() => get('report-note').textContent.includes('마지막'));
  assert.ok(get('use-delegation').checked && get('start').disabled);
  const draft = get('inbox-list').querySelector('textarea');
  draft.value = '직접 답변';
  get('inbox-list').querySelector('button').click();
  await waitFor(() => get('inbox-count').textContent === '0');
  get('use-delegation').click();
  assert.ok(get('use-delegation').disabled && !get('start').disabled);
  get('start').click();
  await waitFor(() => get('request').textContent === '위임 가용성 확인');
  const created = requests.find(req => req.method === 'POST' && req.path.endsWith('/runs')).body;
  assert.ok(created.use_inbox && !Object.hasOwn(created, 'delegation'));
});

test('a receipt changing a policy is explicit and does not trigger another submission', async t => {
  const {get, requests} = mount(t, {route(path, method, body) {
    if (method === 'POST') return Response.json({run: {...body, delegation: null}});
  }});
  await waitFor(() => !get('use-delegation').disabled && !get('start').disabled);
  get('task').value = '접수한 범위 확인';
  get('use-delegation').click();
  get('topic-test_command').click();
  get('start').click();
  await waitFor(() => get('start-note').textContent.includes('위임 범위가 요청과 다릅니다'));
  assert.ok(get('use-delegation').checked);
  assert.equal(get('task').value, '접수한 범위 확인');
  assert.equal(requests.filter(req => req.method === 'POST').length, 1);
});

test('selected run retains its original policy and can end delegation outside the host report window', async t => {
  const policy = {rules: [{topic: 'node_version', path: 'ui/package.json'}], specialist_engine: 'cline', verifier_engine: 'gemini'};
  const item = {...run(runB, 'RUNNING'), delegation: policy, delegation_active: true};
  const {get, requests} = mount(t, {runs: [item], suffix: '?host=' + remote + '&run=' + runB, route(path, method) {
    if (path.endsWith('/return-report')) return Response.json({}, {status: 503});
    if (path.endsWith('/delegation/stop') && method === 'POST') {
      item.delegation_active = false;
      return Response.json({run_id: item.id, active: false});
    }
  }});
  await waitFor(() => get('run-delegation-status').textContent === '위임 중' && !get('run-delegation-stop').disabled);
  assert.match(get('run-delegation-roles').textContent, /전문가 Cline CLI · 독립 검증자 Gemini CLI/);
  const rule = get('run-delegation-rules').textContent;
  assert.match(rule, /Node\.js 버전은 무엇인가요\? · ui\/package\.json/);
  assert.equal(get('delegation-active').children.length, 0);
  get('run-delegation-stop').click();
  await waitFor(() => get('run-delegation-status').textContent === '위임 종료');
  assert.ok(get('run-delegation-stop').hidden);
  assert.equal(get('run-delegation-rules').textContent, rule);
  assert.deepEqual(requests.filter(req => req.method === 'POST'), [{path: remoteAPI + '/runs/' + runB + '/delegation/stop', method: 'POST', body: {}}]);
});

test('remote return report links the original run, quotes literal evidence and ends delegation only on explicit request', async t => {
  const item = {...question(), task: '버전 확인', prompt: '이 프로젝트가 요구하는 Python 버전은 무엇인가요?', answer: '>=3.11',
    status: 'delivered', source: 'delegate', decision: 'answer', decided_at: stamp, topic: 'python_version',
    specialist_engine: 'codex', verifier_engine: 'antigravity', run_state: 'PLAN_ONLY', outcome: 'PLAN_ONLY',
    evidence: {path: 'web/pyproject.toml', line: 12, quote: '<b>requires-python = ">=3.11"</b>', revision: 'c'.repeat(40)}};
  item.delegation_details = {topic: item.topic, specialist_engine: item.specialist_engine,
    verifier_engine: item.verifier_engine, evidence: item.evidence};
  const q = {...question(), id: 'f'.repeat(32), run_id: runB};
  const p = permission();
  const report = {counts: {delegated: 1, questions: 1, permissions: 1}, delegated: [item], active: [{run_id: runB, task: '계속 작업'}]};
  const {get, doc, requests} = mount(t, {suffix: '?host=' + remote, runs: [run(runB, 'RUNNING'), run(runA, 'PLAN_ONLY')],
    inbox: [item, q, p], report, route(path, method) {
      if (path === remoteAPI + '/runs/' + runB + '/delegation/stop' && method === 'POST') {
        report.active = [];
        return Response.json({run_id: runB, active: false});
      }
    }});
  await waitFor(() => get('inbox-list').children.length === 2 && get('delegation-decisions').children.length === 1);
  assert.match(get('report-note').textContent, /연구실 · 대리 답변 1건 · 답변 대기 1건 · 권한 승인 대기 1건/);
  const details = get('delegation-decisions').querySelector('details');
  details.open = true;
  assert.equal(details.querySelector('summary').textContent, item.prompt);
  assert.equal(details.querySelector('pre').textContent, item.evidence.quote);
  assert.equal(details.querySelector('pre *'), null);
  for (const text of ['대리 답변 · >=3.11', 'web/pyproject.toml 12행', item.evidence.revision,
    '전문가 Codex', '독립 검증자 Antigravity', '계획 작성을 마쳤습니다.']) assert.ok(details.textContent.includes(text));
  assert.match(details.querySelector('a').href, new RegExp('host=' + remote + '&run=' + runA));
  details.querySelector('a').click();
  await waitFor(() => get('history-inbox-list').textContent.includes('대리 결정'));
  assert.ok(get('history-inbox-list').textContent.includes(item.evidence.revision));
  assert.equal(get('history-inbox-list').querySelectorAll('pre')[1].textContent, item.evidence.quote);
  assert.equal(get('run-id').textContent, runA);
  const draft = get('inbox-list').querySelector('textarea');
  draft.value = '작성 중';
  q.delegation_reason = '사전 위임한 질문과 정확히 일치하지 않습니다.';
  get('refresh').click();
  draft.focus();
  await waitFor(() => get('inbox-list').textContent.includes(q.delegation_reason));
  assert.equal(doc.activeElement, draft);
  assert.equal(draft.value, '작성 중');
  assert.ok(details.open && details.isConnected);
  assert.ok(requests.every(req => req.method === 'GET'));
  get('delegation-active').querySelector('button').click();
  await waitFor(() => get('delegation-stop-note').textContent.includes('종료했습니다'));
  assert.equal(get('delegation-active').children.length, 0);
  assert.deepEqual(requests.filter(req => req.method === 'POST'), [
    {path: remoteAPI + '/runs/' + runB + '/delegation/stop', method: 'POST', body: {}},
  ]);
  assert.equal(get('inbox-list').querySelectorAll('[data-kind="permission"] button').length, 2);
});

const skillV1 = {id: 'c'.repeat(32), name: '검증 기준', version: 1, digest: 'a'.repeat(64)};
const skillV2 = {...skillV1, id: 'd'.repeat(32), version: 2};

test('host skill registration, literal preview and pagination retain assigned immutable versions', async t => {
  const page = [skillV2];
  const {get, doc, requests, window} = mount(t, {suffix: '?host=' + remote, route(path, method, body) {
    if (path === remoteAPI + '/skills' && method === 'GET') return Response.json({skills: page, next_cursor: skillV2.id});
    if (path === remoteAPI + '/skills' && method === 'POST') {
      assert.deepEqual(body, {name: '검증 기준', instructions: '새 지침'});
      const skill = {...skillV2, id: 'e'.repeat(32), version: 3};
      page.unshift(skill);
      return Response.json({skill}, {status: 201});
    }
    if (path === remoteAPI + '/skills/before/' + skillV2.id) return Response.json({skills: [skillV1], next_cursor: null});
    if (path === remoteAPI + '/skills/' + skillV1.id) return Response.json({skill: {...skillV1, instructions: '<img src=x> 원본'}});
  }});
  await waitFor(() => !get('skill-entry').disabled);
  doc.querySelector('[name=developer_skill]').click();
  get('older-skills').click();
  await waitFor(() => get('older-skills').hidden);
  get('inspect-skill').value = skillV1.id;
  get('inspect-skill').dispatchEvent(new window.Event('change'));
  await waitFor(() => get('skill-preview').textContent.includes('원본'));
  assert.equal(get('skill-preview').children.length, 0);
  doc.querySelector('[name=reviewer_skill][value="' + skillV1.id + '"]').click();
  get('skill-name').value = '검증 기준';
  get('skill-text').value = '새 지침';
  get('save-skill').click();
  await waitFor(() => get('skill-note').textContent.includes('v3'));
  assert.ok(doc.querySelector('[name=developer_skill][value="' + skillV2.id + '"]').checked);
  assert.ok(doc.querySelector('[name=reviewer_skill][value="' + skillV1.id + '"]').checked);
  get('task').value = '역할별 지침 적용';
  get('start').click();
  await waitFor(() => requests.some(row => row.method === 'POST' && row.path.endsWith('/runs')));
  assert.deepEqual(requests.find(row => row.method === 'POST' && row.path.endsWith('/runs')).body.role_skills,
    {developer: [skillV2.id], reviewer: [skillV1.id]});
  assert.ok(requests.every(row => row.path === '/api/hosts' || row.path.startsWith(remoteAPI + '/')));
});

test('parallel drafts pin separate goals, folders, engines and roles across a lost remote receipt', async t => {
  const skills = path => path === remoteAPI + '/skills' && Response.json({skills: [skillV1]});
  const first = mount(t, {suffix: '?host=' + remote, route(path, method) {
    if (path === remoteAPI + '/runs' && method === 'POST') throw new Error('연결 끊김');
    return skills(path);
  }});
  await waitFor(() => !first.get('use-parallel').disabled && !first.get('start').disabled);
  first.doc.querySelector('#extras input').click();
  for (const role of ['developer', 'reviewer', 'specialist', 'verifier']) first.doc.querySelector('[name=' + role + '_skill]').click();
  first.get('use-parallel').click();
  const [a, b] = first.get('subtasks').children;
  a.querySelector('textarea').value = '화면 상태 점검';
  b.querySelector('textarea').value = '규격 테스트 점검';
  b.querySelector('[value="0"]').click();
  b.querySelector('[value="1"]').click();
  b.querySelector('[data-role=developer]').value = 'gemini';
  b.querySelector('[data-role=reviewer]').value = 'cline';
  first.get('add-subtask').click();
  first.get('subtasks').lastChild.querySelector('button').click();
  first.get('task').value = '주문 기능 점검';
  first.get('start').click();
  await waitFor(() => first.get('start-note').textContent.includes('같은 요청'));
  const key = 'agent-office.studio.pending.' + remote;
  const saved = JSON.parse(first.window.sessionStorage.getItem(key));
  assert.deepEqual(saved.subtasks, [
    {task: '화면 상태 점검', indices: [0], developer_engine: 'codex', reviewer_engine: 'antigravity'},
    {task: '규격 테스트 점검', indices: [1], developer_engine: 'gemini', reviewer_engine: 'cline'},
  ]);
  assert.deepEqual(Object.keys(saved.role_skills), ['developer', 'reviewer', 'specialist', 'verifier']);
  let available = false;
  const second = mount(t, {pending: saved, pendingHost: remote, suffix: '?host=' + remote, route(path, method, body) {
    if (path === remoteAPI + '/skills') return available ? skills(path) : Response.json({}, {status: 404});
    if (path === remoteAPI + '/runs' && method === 'POST') return Response.json({run: {...body, kind: 'parallel',
      subtasks: body.subtasks.map((child, i) => ({...child, id: [runA, runB][i], parent_id: body.id, state: 'QUEUED'}))}});
  }});
  await waitFor(() => second.get('subtasks').children.length === 2);
  assert.ok(second.get('run-fields').disabled && second.get('start').disabled);
  assert.equal(second.get('subtasks').lastChild.querySelector('[data-role=developer]').value, 'gemini');
  assert.equal(second.get('subtasks').lastChild.querySelector('input:checked').value, '1');
  assert.equal(second.doc.querySelectorAll('#role-skills :checked').length, 4);
  assert.equal(second.requests.filter(r => r.method === 'POST').length, 0);
  available = true;
  second.get('refresh').click();
  await waitFor(() => !second.get('start').disabled);
  assert.equal(second.requests.filter(r => r.method === 'POST').length, 0);
  second.get('start').click();
  await waitFor(() => !second.window.sessionStorage.getItem(key));
  assert.deepEqual(second.requests.filter(r => r.method === 'POST').map(r => r.body), [saved]);
});

test('parent reload exposes individual failed evidence, pinned skills and links to child and original run', async t => {
  const childId = '3'.repeat(32);
  const a = {base_revision: 'a'.repeat(40), ...run(runB, 'READY_FOR_HUMAN_REVIEW'), parent_id: runA, kind: 'subtask',
    indices: [0], task: '화면', developer_engine: 'codex', reviewer_engine: 'antigravity', role_skills: {developer: [skillV1.id]}};
  const b = {...a, id: childId, state: 'FAILED', outcome: 'TEST_FAILED', task: '규격', indices: [1], review_output: '',
    test_output: '1 failed', changes: 'M test.py', diff: '+<img src=x>', diagnostic: '테스트 회귀',
    role_skills: {developer: [skillV1.id]}, role_skill_snapshots: {developer: [skillV1]}};
  const parent = {...run(runA, 'FAILED'), task: '독립 배정', kind: 'parallel', base_revision: 'a'.repeat(40), subtasks: [a, b],
    role_skills: {developer: [skillV1.id]}};
  const {get, doc, window, requests} = mount(t, {runs: [parent, a, b], suffix: '?host=' + remote + '&run=' + runA});
  await waitFor(() => get('child-results').children.length === 2);
  assert.match(get('group-revision').textContent, /a{40}/);
  assert.match(get('child-results').lastChild.textContent, /규격 · 실패/);
  assert.ok(get('developer_output').parentElement.hidden);
  assert.ok(!requests.some(r => r.path.endsWith(runA + '/inbox')));
  const card = get('child-results').lastChild;
  card.open = true;
  await waitFor(() => card.textContent.includes('1 failed'));
  assert.match(card.textContent, /검증 기준 · v1/);
  assert.match(card.textContent, /테스트 회귀/);
  assert.match(card.textContent, /변경 내용 diff\+<img src=x>/);
  assert.equal(doc.querySelector('img'), null);
  card.querySelector('a').click();
  await waitFor(() => get('run-id').textContent === childId);
  assert.ok(get('group-result').hidden);
  assert.ok(!get('developer_output').parentElement.hidden);
  get('parent-link').firstChild.click();
  await waitFor(() => get('run-id').textContent === runA);
  assert.equal(new URL(window.location.href).searchParams.get('host'), remote);
  assert.equal(new URL(window.location.href).searchParams.get('run'), runA);
  assert.equal(requests.filter(r => r.method === 'POST').length, 0);
  // With the original assignment intact, a changed role assignment in the child detail must hide its evidence.
  b.role_skills = {developer: [skillV2.id]};
  const changed = get('child-results').lastChild;
  changed.open = true;
  await waitFor(() => changed.textContent.includes('배정이 원래 요청과 달라'));
  assert.ok(!changed.textContent.includes('1 failed'));
});
