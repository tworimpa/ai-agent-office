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

async function waitFor(predicate) {
  for (let i = 0; i < 100; i++) {
    if (predicate()) return;
    await new Promise(resolve => setTimeout(resolve, 5));
  }
  assert.ok(predicate(), '화면 상태 전환 시간 초과');
}

function mount(t, {workspaces = [workspace], runs = [], suffix = '', pending, pendingHost = 'local', route} = {}) {
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
  first.get('edit-mode').click();
  first.get('start').click();
  await waitFor(() => first.get('start-note').textContent.includes('같은 요청'));
  const sent = first.requests.find(r => r.method === 'POST').body;
  assert.equal(sent.allow_edits, true);
  assert.ok(first.get('run-fields').disabled);
  const saved = JSON.parse(first.window.sessionStorage.getItem('agent-office.studio.pending'));
  const second = mount(t, {pending: saved});
  await waitFor(() => second.get('task').value === sent.task);
  assert.equal(second.requests.filter(r => r.method === 'POST').length, 0);
  assert.equal(second.get('edit-mode').checked, true);
  second.get('start').click();
  await waitFor(() => second.get('run-id').textContent === sent.id);
  assert.deepEqual(second.requests.find(r => r.method === 'POST').body, sent);
  assert.equal(second.window.sessionStorage.getItem('agent-office.studio.pending'), null);
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
  get('start').click();
  await waitFor(() => !get('stop').hidden);
  const added = requests.find(r => r.method === 'POST' && r.path === remoteAPI + '/runs');
  assert.deepEqual(added.body.indices, [0, 1]);
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
  assert.ok(!local.requests.some(r => r.path.includes(saved.id)));
  assert.ok(local.window.sessionStorage.getItem(remoteKey));
  const recovered = mount(t, {suffix: runLink, pending: saved, pendingHost: remote, runs: items});
  await waitFor(() => recovered.get('developer_output').textContent === '완료');
  assert.equal(recovered.window.sessionStorage.getItem(remoteKey), null);
  assert.equal(recovered.get('run-id').textContent, saved.id);
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
  const before = requests.length;
  get('start').click();
  await waitFor(() => get('run-id').textContent === runB);
  assert.deepEqual(requests.slice(before, before + 2), [
    {path: remoteAPI + '/runs/' + runB, method: 'GET', body: undefined},
    {path: remoteAPI + '/runs', method: 'POST', body: pending},
  ]);
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
