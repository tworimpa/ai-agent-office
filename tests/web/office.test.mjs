import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import test from 'node:test';
import {JSDOM} from 'jsdom';
import {mountOffice} from '../../src/agent_office/web/office.js';

const html = await readFile(new URL('../../src/agent_office/web/office.html', import.meta.url), 'utf8');
const remote = 'b'.repeat(32), parent = 'c'.repeat(32), child = 'd'.repeat(32);
const hosts = [{id: 'local', name: '이 컴퓨터'}, {id: remote, name: '연구실'}];
const remoteOffice = '/api/hosts/' + remote + '/office';
const stamp = '2026-11-19T01:02:03Z';
const agent = (fields = {}) => ({run_id: child, task: '결제 화면 검토', role: 'developer', engine: 'codex',
  run_state: 'RUNNING', activity: 'working', parent_id: null, parent_task: null, requests: [],
  request_count: 0, updated_at: stamp, ...fields});
const page = (agents = [], fields = {}) => ({generated_at: stamp, agents, offset: 0, next_offset: null,
  total_runs: new Set(agents.map(a => a.run_id)).size,
  counts: {active_runs: agents.length ? 1 : 0, questions: 0, permissions: 0}, ...fields});

async function until(predicate) {
  for (let i = 0; i < 100; i++) {
    if (predicate()) return;
    await new Promise(resolve => setTimeout(resolve, 5));
  }
  assert.ok(predicate(), '오피스 응답 대기 시간 초과');
}

function mount(t, route, query = '') {
  const dom = new JSDOM(html, {url: 'http://127.0.0.1:8766/office' + query, pretendToBeVisual: true});
  const {window} = dom, doc = window.document, calls = [];
  window.fetch = async (path, options) => {
    calls.push(path);
    assert.equal(options.method ?? 'GET', 'GET');
    assert.equal(options.mode, 'same-origin');
    const result = await route(path);
    if (result === undefined && path === '/api/hosts') return Response.json({hosts});
    assert.notEqual(result, undefined, 'unexpected fetch ' + path);
    return result instanceof Response ? result : Response.json(result);
  };
  const dispose = mountOffice(window);
  t.after(() => { dispose(); dom.window.close(); });
  return {window, doc, calls, get: id => doc.getElementById(id)};
}

test('all hosts keep native child links, two roles using the same engine and typed requests', async t => {
  const waiting = agent({parent_id: parent, parent_task: '주문 서비스 개선', activity: 'waiting', request_count: 4,
    requests: [{id: '1', kind: 'question', prompt: '<img src=x> 포함할까요?'}, {id: '2', kind: 'permission', prompt: '파일 저장 허용?'},
      {id: '3', kind: 'product_decision', prompt: '저장 화면을 바꿀까요?'}]});
  const reviewer = agent({role: 'reviewer', activity: 'queued', parent_id: parent});
  const history = agent({run_id: parent, run_state: 'CANCELED', activity: 'stopped'});
  const {get, doc, calls} = mount(t, path => {
    if (path === '/api/office') return page([agent()]);
    if (path === remoteOffice) return page([waiting, reviewer, history],
      {counts: {active_runs: 1, questions: 2, permissions: 1, product_decisions: 1}});
  });
  await until(() => doc.querySelectorAll('.station').length === 4);
  assert.deepEqual(calls.sort(), ['/api/hosts', remoteOffice, '/api/office'].sort());
  const [question, permission, product] = doc.querySelectorAll('.speech');
  assert.equal(question.textContent, '기술 질문<img src=x> 포함할까요?');
  assert.equal(permission.textContent, '권한 요청파일 저장 허용?');
  assert.equal(product.textContent, '제품 동작 결정저장 화면을 바꿀까요?');
  assert.equal(doc.querySelector('img'), null);
  const card = question.closest('a');
  assert.equal(card.getAttribute('href'), '/?host=' + remote + '&run=' + child + '#detail');
  assert.match(card.textContent, /주문 서비스 개선/);
  assert.match(card.textContent, /외 1건/);
  assert.equal(card.closest('[data-host]').dataset.host, remote);
  assert.match(card.closest('.zone').querySelector('h3').textContent, /응답이 필요한 자리/);
  assert.equal(doc.querySelector('[data-host=local] .station').getAttribute('href'), '/?run=' + child + '#detail');
  assert.match(doc.querySelector('[data-activity=queued]').textContent, /리뷰어 · Codex/);
  assert.match(doc.querySelector('[data-activity=stopped]').closest('.zone').textContent, /최근 실행 기록/);
  assert.equal(get('active-count').textContent, '2');
  assert.equal(get('question-count').textContent, '2');
  assert.equal(get('permission-count').textContent, '1');
  assert.equal(get('product-decision-count').textContent, '1');
  assert.equal(doc.querySelector('#host-filters [aria-current]').getAttribute('href'), '/office');
});

test('remote outage preserves the last dated snapshot while the local host refreshes', async t => {
  let offline = false;
  const {get, doc} = mount(t, path => {
    if (path === remoteOffice) return offline ? Response.json({}, {status: 503}) : page([agent({task: '원격 기록'})]);
    if (path === '/api/office') return page([agent({task: offline ? '로컬 새 업무' : '로컬 이전 업무'})]);
  });
  const host = () => doc.querySelector('[data-host="' + remote + '"]');
  await until(() => get('active-count').textContent === '2');
  offline = true;
  get('refresh').click();
  await until(() => host().dataset.stale === 'true' && get('hosts').textContent.includes('로컬 새 업무'));
  assert.match(host().textContent, /연결 확인 실패 · 마지막 기록/);
  assert.match(host().textContent, /원격 기록/);
  assert.equal(get('active-count').textContent, '1');
  offline = false;
  get('refresh').click();
  await until(() => get('active-count').textContent === '2' && host().dataset.stale === 'false');
});

test('manual refresh supersedes late responses and unchanged data preserves keyboard focus', async t => {
  let resolveOld, latest = false;
  const {get, doc, calls, window} = mount(t, path => {
    if (path !== remoteOffice) return;
    if (!latest) return new Promise(resolve => { resolveOld = resolve; });
    return page([agent({task: '최신 배정'})]);
  }, '?host=' + remote);
  await until(() => resolveOld);
  latest = true;
  get('refresh').click();
  await until(() => doc.querySelector('.station'));
  const focused = doc.querySelector('.station');
  focused.focus();
  resolveOld(page([agent({task: '이전 배정'})]));
  await new Promise(resolve => setTimeout(resolve, 10));
  assert.match(focused.textContent, /최신 배정/);
  get('refresh').click();
  await until(() => calls.filter(p => p === remoteOffice).length === 3);
  assert.equal(doc.activeElement, focused);
  assert.equal(get('studio-link').getAttribute('href'), '/?host=' + remote);
  assert.equal(get('improvements-link').getAttribute('href'), '/improvements?host=' + remote);
  assert.equal(get('versions-link').getAttribute('href'), '/versions?host=' + remote);
  Object.defineProperty(doc, 'hidden', {configurable: true, value: true});
  doc.dispatchEvent(new window.Event('visibilitychange'));
  const before = calls.length;
  get('refresh').click();
  assert.equal(calls.length, before);
  Object.defineProperty(doc, 'hidden', {configurable: true, value: false});
  doc.dispatchEvent(new window.Event('visibilitychange'));
  await until(() => calls.length > before + 1);
  assert.ok(calls.every(p => p === '/api/hosts' || p === remoteOffice));
});

test('office pagination remains host-bound and preserves the selected page on refresh', async t => {
  const {get, doc, calls} = mount(t, path => {
    if (path === '/api/office') return page([agent()], {next_offset: 24, total_runs: 25});
    if (path === '/api/office/page/24') return page([agent({task: '이전 실행'})], {offset: 24, total_runs: 25});
  }, '?host=local');
  await until(() => doc.querySelector('[data-key=next]'));
  doc.querySelector('[data-key=next]').click();
  await until(() => get('hosts').textContent.includes('이전 실행'));
  get('refresh').click();
  await until(() => calls.filter(p => p === '/api/office/page/24').length === 2);
  doc.querySelector('[data-key=prev]').click();
  await until(() => !get('hosts').textContent.includes('이전 실행'));
  assert.ok(!calls.includes(remoteOffice));
});

test('when the visible run window shrinks the current host returns to its first page', async t => {
  let first = true;
  const {doc, get} = mount(t, path => {
    if (path === '/api/office/page/24') return page([], {offset: 24, total_runs: 1});
    if (path !== '/api/office') return;
    if (!first) return page([agent({task: '남은 실행'})]);
    first = false;
    return page([agent()], {next_offset: 24, total_runs: 25});
  }, '?host=local');
  await until(() => doc.querySelector('[data-key=next]'));
  doc.querySelector('[data-key=next]').click();
  await until(() => get('hosts').textContent.includes('남은 실행'));
  assert.equal(doc.querySelector('[data-key=next]'), null);
});

test('all-host polling limits concurrent snapshots and covers each configured host', async t => {
  const directory = [...hosts, ...Array.from({length: 6}, (_, i) =>
    ({id: String(i).repeat(32), name: '호스트 ' + i}))];
  let active = 0, peak = 0, finished = 0;
  mount(t, async path => {
    if (path === '/api/hosts') return {hosts: directory};
    active++;
    peak = Math.max(peak, active);
    await new Promise(resolve => setTimeout(resolve, 12));
    active--;
    finished++;
    return page();
  });
  await until(() => finished === directory.length);
  assert.equal(peak, 4);
});

test('registered empty host and nonexistent or ambiguous host selection have distinct states', async t => {
  const {get, calls} = mount(t, path => path === '/api/office' ? page() : undefined, '?host=local');
  await until(() => get('active-count').textContent === '0');
  assert.match(get('hosts').textContent, /배정된 작업이 없습니다/);
  assert.ok(!calls.includes(remoteOffice));
  for (const query of ['?host=nonexistent', '?host=local&host=' + remote]) {
    const invalid = mount(t, () => undefined, query);
    await until(() => invalid.get('notice').textContent.includes('찾을 수 없습니다'));
    assert.deepEqual(invalid.calls, ['/api/hosts']);
    assert.equal(invalid.get('active-count').textContent, '—');
  }
});
