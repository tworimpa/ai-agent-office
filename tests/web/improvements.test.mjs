import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import test from 'node:test';
import {JSDOM} from 'jsdom';
import {mountImprovements} from '../../src/agent_office/web/improvements.js';

const html = await readFile(new URL('../../src/agent_office/web/improvements.html', import.meta.url), 'utf8');
const host = 'b'.repeat(32), base = 'a'.repeat(40), candidateId = 'c'.repeat(32), jobId = 'd'.repeat(32);
const remote = '/api/hosts/' + host + '/improvements';
const hosts = [{id: 'local', name: '이 컴퓨터'}, {id: host, name: '연구실'}];
const key = 'agent-office.improvements.pending.' + host;
const rows = Array.from({length: 25}, (_, i) => ({id: (i + 1).toString(16).padStart(32, '0'), request: '실행 ' + (i + 1), state: 'FAILED', outcome: 'TEST_FAILED'}));
const payload = {id: jobId, run_ids: [rows[0].id], base, engine: 'gemini'};
const stamp = '2026-11-19T02:00:00Z';
const view = {source_enabled: true, view: {available: true, stable: base, updated_at: stamp}};
const proposal = {rationale: '실행 기록 점검', expected: '실패 횟수 감소', patch: '+<img src=x> 제안'};
const job = {...payload, title: '실패 원인 개선', state: 'ready', message: ''};
const candidate = {id: candidateId, job_id: jobId, base, sha: 'f'.repeat(40), state: 'passed', validation: 'passed',
  title: '<img src=x> 개선', decision: null, image: 'sha256:' + '0'.repeat(64), run_ids: payload.run_ids, diff: '+<img src=x> 실제 변경',
  ...proposal, validation_output: '3 passed', exit_code: 0, history: [{time: stamp, event: 'validation_finished'}],
  diff_truncated: true, output_truncated: true, history_omitted: 2};

async function until(predicate) {
  for (let i = 0; i < 100 && !predicate(); i++) await new Promise(r => setTimeout(r, 5));
  assert.ok(predicate(), '개선안 화면 갱신 시간 초과');
}
function mount(t, {route, saved, onPoll, suffix = '?host=' + host} = {}) {
  const dom = new JSDOM(html, {url: 'http://127.0.0.1:8766/improvements' + suffix, pretendToBeVisual: true});
  const {window} = dom, doc = window.document, requests = [];
  if (onPoll) {
    const schedule = window.setTimeout.bind(window);
    window.setTimeout = (fn, delay) => delay === 6000 ? onPoll(fn) : schedule(fn, delay);
  }
  if (saved) window.sessionStorage.setItem(key, JSON.stringify(saved));
  window.fetch = async (path, options) => {
    const body = options.body && JSON.parse(options.body);
    const method = options.method || 'GET';
    requests.push({path, method, body});
    if (body) {
      assert.equal(options.headers['Content-Type'], 'application/json');
      assert.equal(options.headers['X-Agent-Office'], 'studio');
    }
    if (path === '/api/hosts') return Response.json({hosts});
    assert.ok(path.startsWith(remote), path);
    const suffix = path.slice(remote.length);
    let result = route && await route(suffix, method, body);
    if (result === undefined) {
      if (suffix === '') result = view;
      else if (suffix === '/runs/page/0') result = {items: rows.slice(0, 24), next_offset: 24};
      else if (suffix === '/runs/page/24') result = {items: rows.slice(24), next_offset: null};
      else if (suffix.endsWith('/page/0')) result = {items: [], next_offset: null};
      else result = Response.json({error: {code: 'NOT_FOUND'}}, {status: 404});
    }
    return result instanceof Response ? result : Response.json(result);
  };
  const dispose = mountImprovements(window);
  t.after(() => { dispose(); window.close(); });
  return {window, doc, get: id => doc.getElementById(id), requests};
}

test('evidence selection survives paging, caps at eight, and retries only the same host and request after a lost receipt', async t => {
  const first = mount(t, {route(path, method) {
    if (method === 'POST') throw Error('lost receipt');
  }});
  const {doc, get} = first;
  await until(() => get('runs').children.length === 24);
  assert.equal(get('generate').disabled, true);
  assert.equal(get('studio-link').getAttribute('href'), '/?host=' + host);
  assert.equal(get('versions-link').getAttribute('href'), '/versions?host=' + host);
  assert.equal(get('hosts').querySelector('[aria-current]').textContent, '연구실');
  for (const box of [...doc.querySelectorAll('[name=run]')].slice(0, 8)) box.click();
  assert.equal(doc.querySelectorAll('[name=run]:checked').length, 8);
  assert.equal(doc.querySelectorAll('[name=run]:disabled').length, 16);
  doc.querySelector('[name=run]').click();
  get('runs-pages').lastChild.click();
  await until(() => get('runs').textContent.includes('실행 25'));
  doc.querySelector('[name=run]').click();
  assert.equal(get('selection-count').textContent, '8개 선택 · 최대 8개');
  get('engine').value = 'gemini';
  get('generate').click();
  get('generate').click();
  await until(() => get('submit-note').textContent.includes('같은 요청'));
  const sent = first.requests.filter(r => r.method === 'POST');
  assert.equal(sent.length, 1);
  assert.match(sent[0].body.id, /^[0-9a-f]{32}$/);
  assert.equal(sent[0].path, remote + '/jobs');
  assert.deepEqual(sent[0].body.run_ids, [...rows.slice(1, 8), rows[24]].map(row => row.id));
  const saved = JSON.parse(first.window.sessionStorage.getItem(key));
  assert.deepEqual(sent[0].body, saved);

  let accepted;
  const second = mount(t, {saved, route(path, method, body) {
    if (method === 'POST') { accepted = {...body, state: 'generating', title: '', message: ''}; return {job: accepted}; }
    if (path === '/jobs/' + saved.id && accepted) return {job: accepted, studio_run_ids: payload.run_ids, candidate_id: null, proposal: null, error: null};
  }});
  await until(() => second.get('submit-note').textContent.includes('아직 접수'));
  assert.ok(second.get('evidence').disabled && second.get('engine').disabled);
  assert.equal(second.requests.filter(r => r.method === 'POST').length, 0);
  second.get('generate').click();
  await until(() => second.get('detail-body').textContent.includes('생성 중'));
  assert.deepEqual(second.requests.filter(r => r.method === 'POST').map(r => r.body), [saved]);
  assert.equal(second.window.sessionStorage.getItem(key), null);
  assert.equal(second.get('generate').disabled, true);
});

test('a previously accepted ambiguous request is recovered with GET even when new generation is disabled', async t => {
  const {get, requests, window} = mount(t, {saved: payload, route(path) {
    if (!path) return {...view, source_enabled: false};
    if (path === '/jobs/' + jobId) return {job, studio_run_ids: [], candidate_id: candidateId, error: null, proposal};
  }});
  await until(() => get('detail-body').textContent.includes('엔진이 제안한 패치'));
  assert.ok(get('source-note').textContent.includes('소스가 설정되지'));
  assert.ok(get('generate').disabled);
  assert.equal(window.sessionStorage.getItem(key), null);
  assert.equal(get('detail-body').querySelector('pre').textContent, proposal.patch);
  assert.equal(get('detail-body').querySelector('ul a'), null);
  assert.ok(get('detail-body').textContent.includes(rows[0].id + ' · CLI 실행'));
  assert.ok(requests.every(req => req.method === 'GET'));
});

test('candidate detail follows terminal publication and stale states without exposing browser decisions', async t => {
  let current = {...candidate}, status = view, offline = false, poll;
  const {doc, get, requests} = mount(t, {onPoll(fn) { poll = fn; }, suffix: '?host=' + host + '&job=' + jobId, route(path) {
    if (offline) throw Error('offline');
    if (!path) return status;
    if (path === '/jobs/' + jobId) return {job, studio_run_ids: [], candidate_id: candidateId, error: null, proposal};
    if (path === '/candidates/page/0') return {items: [current], next_offset: null};
    if (path === '/candidates/' + candidateId) return {candidate: current, studio_run_ids: payload.run_ids};
  }});
  await until(() => get('detail-body').textContent.includes('제안한 패치'));
  get('detail-body').querySelector('[data-kind=candidate]').click();
  await until(() => get('detail-body').textContent.includes('검증 이미지'));
  assert.ok(get('detail-body').textContent.includes(candidate.diff));
  assert.ok(get('detail-body').textContent.includes('전체 출력은 운영자 터미널'));
  assert.ok(get('detail-body').textContent.includes('이전 기록 2건'));
  assert.ok(get('detail-body').textContent.includes('전체 diff는 운영자 터미널'));
  assert.equal(get('detail-body').querySelector('ul a').getAttribute('href'), '/?host=' + host + '&run=' + rows[0].id + '#detail');
  assert.equal(get('detail-body').querySelector('button,form,img'), null);
  assert.equal(doc.querySelector('img'), null);
  current = {...current, state: 'approved', decision: 'approved', history: [...current.history, {event: 'approved', time: stamp}]};
  await until(() => poll);
  poll();
  await until(() => get('detail-body').querySelector('ol').textContent.includes('승인'));
  assert.ok(get('candidates').textContent.includes('운영자 승인'));
  status = {...view, view: {available: false, stable: null, updated_at: null}};
  get('refresh').click();
  await until(() => get('published').textContent.includes('갱신 실패'));
  assert.ok(get('detail-body').textContent.includes(candidate.diff));
  assert.ok(get('detail-note').textContent.includes('마지막'));
  assert.ok(get('generate').disabled);
  offline = true;
  get('refresh').click();
  await until(() => get('host-error').textContent.includes('마지막'));
  assert.ok(get('candidates').textContent.includes('승인'));
  assert.ok(requests.every(row => row.method === 'GET'));
});

test('unknown and ambiguous host selectors never read or submit to another host', async t => {
  for (const suffix of ['?host=' + 'e'.repeat(32), '?host=' + host + '&host=local', '?host=bogus']) {
    const {get, requests} = mount(t, {suffix});
    await until(() => get('host-error').textContent.includes('호스트를 찾을 수 없습니다'));
    assert.ok(get('generate').disabled);
    assert.deepEqual(requests.map(row => row.path), ['/api/hosts']);
  }
});

test('a verified candidate from before rollback is stale even when the published SHA matches again', async t => {
  const earlier = {...candidate, stale_base: true};
  const {get} = mount(t, {suffix: '?host=' + host + '&candidate=' + candidateId, route(path) {
    if (path === '/candidates/page/0') return {items: [earlier], next_offset: null};
    if (path === '/candidates/' + candidateId) return {candidate: earlier, studio_run_ids: []};
  }});
  await until(() => get('detail-body').textContent.includes('기준 변경'));
  assert.match(get('candidates').textContent, /검증 통과.*기준 변경/);
  assert.doesNotMatch(get('detail-body').textContent, /결정 대기/);
});

test('a gateway 404 retains the saved payload and does not submit a duplicate when the original receipt reappears', async t => {
  let returned = false;
  const {get, requests, window} = mount(t, {saved: payload, route(path) {
    if (path === '/jobs/' + jobId) {
      if (!returned) return Response.json({error: {code: 'UNKNOWN_HOST'}}, {status: 404});
      return {job, studio_run_ids: [], candidate_id: null, proposal, error: null};
    }
  }});
  await until(() => get('submit-note').textContent.includes('이전 요청의 접수 여부를 확인할 수 없습니다.'));
  get('generate').click();
  await until(() => get('submit-note').textContent.includes('접수 응답을 확인할 수 없습니다.'));
  assert.deepEqual(JSON.parse(window.sessionStorage.getItem(key)), payload);
  returned = true;
  get('generate').click();
  await until(() => get('submit-note').textContent.includes('접수했습니다'));
  assert.equal(window.sessionStorage.getItem(key), null);
  assert.ok(requests.every(row => row.method === 'GET'));
});
