import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import test from 'node:test';
import {JSDOM} from 'jsdom';
import {mountVersions} from '../../src/agent_office/web/versions.js';

const html = await readFile(new URL('../../src/agent_office/web/versions.html', import.meta.url), 'utf8');
const remote = 'e'.repeat(32), time = '2026-11-19T06:00:00Z';
const sha = seq => seq.toString(16).padStart(40, '0');
function entry(seq, rollback = null) {
  return {seq, kind: rollback ? 'rollback' : seq > 1 ? 'promotion' : 'initial', sha: sha(rollback || seq),
    previous: seq > 1 ? sha(seq - 1) : null, target_seq: rollback, time: seq > 1 ? time : null};
}
const initialRows = Array.from({length: 26}, (_, i) => entry(26 - i));
function list(rows, offset) {
  return {available: true, current: rows[0], updated_at: time, items: rows.slice(offset, offset + 24),
    next_offset: rows.length > offset + 24 ? offset + 24 : null};
}
const patch = 'diff --git a/example b/example\n+<img src=absent>';
function comparison(rows, start, end) {
  const from = rows.find(r => r.seq === start), to = rows.find(r => r.seq === end);
  const files = from.sha === to.sha ? [] : [{path: '예시.txt', status: 'modified', before_mode: '100644', after_mode: '100755'}];
  return {from_seq: start, to_seq: end, from_sha: from.sha, to_sha: to.sha, total_files: files.length,
    files, diff: files.length ? patch : '', truncated: false};
}
async function until(predicate) {
  for (let i = 0; i < 200 && !predicate(); i++) await new Promise(resolve => setTimeout(resolve, 5));
  assert.ok(predicate(), '버전 화면이 갱신되지 않았습니다.');
}
function mount(t, {suffix = '?host=' + remote, route, rows = initialRows} = {}) {
  const {window} = new JSDOM(html, {url: 'http://localhost/versions' + suffix, pretendToBeVisual: true});
  const get = id => window.document.getElementById(id), requests = [];
  const fixture = {rows};
  const schedule = window.setTimeout.bind(window);
  window.setTimeout = (fn, delay) => {
    if (delay !== 6000) return schedule(fn, delay);
    fixture.poll = fn;
  };
  window.fetch = async (path, opts) => {
    assert.equal(opts.method || 'GET', 'GET');
    requests.push(path);
    if (path === '/api/hosts') return Response.json({hosts: [{id: 'local', name: '로컬'}, {id: remote, name: '원격'}]});
    const result = await route?.(path);
    if (result !== undefined) return result instanceof Response ? result : Response.json(result);
    const parts = path.split('/');
    return Response.json(parts.at(-2) === 'page' ? list(fixture.rows, Number(parts.at(-1))) :
      comparison(fixture.rows, Number(parts.at(-2)), Number(parts.at(-1))));
  };
  const stop = mountVersions(window);
  t.after(() => { stop(); window.close(); });
  const select = (id, seq) => {
    get(id).value = String(seq);
    get(id).dispatchEvent(new window.Event('change'));
  };
  return {window, get, fixture, requests, select};
}

test('an idle poll keeps an immutable comparison when the host confirms both versions', async t => {
  const {fixture, get, requests} = mount(t);
  await until(() => fixture.poll && get('diff').textContent === patch);
  const before = requests.length;
  await fixture.poll();
  assert.equal(requests.length, before + 2);
  assert.equal(requests.filter(path => path.includes('/compare/')).length, 1);
  get('refresh').click();
  await until(() => requests.filter(path => path.includes('/compare/')).length === 2);
});

test('behind an SSH host, defaults to the latest transition, pages and retains both selected versions', async t => {
  const {window, get, select, requests} = mount(t);
  await until(() => !get('comparison').hidden);
  assert.equal(get('hosts').querySelector('[aria-current]').textContent, '원격');
  assert.equal(get('studio-link').getAttribute('href'), '/?host=' + remote);
  assert.equal(get('office-link').getAttribute('href'), '/office?host=' + remote);
  assert.equal(get('improvements-link').getAttribute('href'), '/improvements?host=' + remote);
  assert.equal(get('current-sha').textContent, sha(26));
  assert.match(get('compare-note').textContent, /#25 → #26/);
  assert.match(get('files').textContent, /100644 → 100755/);
  assert.equal(get('diff').textContent, patch);
  assert.equal(window.document.querySelector('img,form'), null);
  assert.equal(get('history').children.length, 24);
  get('older').click();
  await until(() => get('history').children.length === 2);
  select('from-version', 1);
  await until(() => get('compare-note').textContent === '#1 → #26');
  get('newer').click();
  await until(() => get('history').children.length === 24);
  assert.equal(get('from-version').value, '1');
  select('to-version', 5);
  await until(() => get('compare-note').textContent === '#1 → #5');
  assert.ok(requests.every(path => path === '/api/hosts' || path.startsWith('/api/hosts/' + remote + '/versions/')));
  assert.ok(requests.some(path => path.endsWith('/compare/1/5')));
});

test('poll picks up rollback to a prior SHA, distinguishes repeated versions and supports reverse comparison', async t => {
  const {get, select, fixture, requests} = mount(t);
  await until(() => fixture.poll);
  fixture.rows = [entry(27, 25), ...initialRows];
  fixture.poll();
  await until(() => get('compare-note').textContent === '#26 → #27');
  assert.match(get('current-version').textContent, /#27 · 롤백/);
  assert.equal(get('current-sha').textContent, sha(25));
  assert.match(get('history').firstChild.textContent, /#27 · 롤백 · #25 복원/);
  select('from-version', 27);
  select('to-version', 26);
  await until(() => get('compare-note').textContent === '#27 → #26');
  select('to-version', 25);
  await until(() => get('compare-note').textContent === '#27 → #25');
  assert.ok(requests.at(-1).endsWith('/compare/27/25'));
  assert.equal(get('file-count').textContent, '변경 파일 0개');
  assert.match(get('diff').textContent, /변경된 파일이 없습니다/);
  fixture.rows.unshift({...entry(28), previous: sha(25)});
  fixture.poll();
  await until(() => get('current-sha').textContent === sha(28));
  assert.equal(get('from-version').value, '27');
  assert.equal(get('to-version').value, '25');
});

test('partial, missing, stale and mismatched comparisons are distinguishable from an empty diff', async t => {
  let compareOverride, pageOverride;
  const result = comparison(initialRows, 25, 26);
  const {get, fixture} = mount(t, {route: path => path.includes('/page/') ? pageOverride : compareOverride});
  await until(() => fixture.poll);
  compareOverride = {...result, diff: '', truncated: true, total_files: 2};
  get('refresh').click();
  await until(() => get('compare-note').textContent.includes('일부 자료'));
  assert.match(get('diff').textContent, /본문이 없습니다/);
  assert.match(get('file-count').textContent, /2개/);
  compareOverride = Response.json({}, {status: 503});
  get('refresh').click();
  await until(() => get('compare-note').textContent.includes('마지막으로 받은 결과'));
  assert.ok(!get('comparison').hidden);
  compareOverride = Response.json({}, {status: 404});
  get('refresh').click();
  await until(() => get('comparison').hidden);
  assert.equal(get('diff').textContent, '');
  assert.match(get('compare-note').textContent, /비교 자료를 확인할 수 없습니다/);
  compareOverride = {...result, from_sha: sha(99), diff: '다른 게시본'};
  get('refresh').click();
  await until(() => get('compare-note').textContent.includes('자료가 다릅니다'));
  assert.ok(get('comparison').hidden);
  assert.equal(get('diff').textContent, '');
  pageOverride = Response.json({}, {status: 503});
  get('refresh').click();
  await until(() => get('page-note').textContent.includes('마지막으로 받은 기록'));
  assert.equal(get('current-sha').textContent, sha(26));
  assert.equal(get('from-version').disabled, true);
  pageOverride = compareOverride = undefined;
  get('refresh').click();
  await until(() => get('diff').textContent === patch);
  assert.equal(get('page-note').textContent, '');
  pageOverride = {available: false, current: null, updated_at: null, next_offset: null, items: []};
  get('refresh').click();
  await until(() => get('current-version').textContent === '게시된 버전 자료 없음');
  assert.equal(get('current-sha').textContent, '');
  assert.equal(get('diff').textContent, '');
  assert.equal(get('history').children.length, 0);
  assert.equal(get('to-version').options.length, 0);
  pageOverride = undefined;
  get('refresh').click();
  await until(() => get('diff').textContent === patch);
});

test('late comparison from a previous host cannot repopulate its unpublished successor', async t => {
  let delayed, release;
  const wait = new Promise(resolve => { release = resolve; });
  const {get, window, select, requests} = mount(t, {route: async path => {
    if (path.endsWith('/compare/25/24')) {
      delayed = true;
      await wait;
      return {...comparison(initialRows, 25, 24), diff: '오래된 원격 결과'};
    }
    if (path === '/api/versions/page/0') return {available: false};
  }});
  await until(() => get('diff').textContent === patch);
  select('to-version', 24);
  await until(() => delayed);
  window.history.pushState(null, '', '/versions?host=local');
  window.dispatchEvent(new window.PopStateEvent('popstate'));
  await until(() => get('current-version').textContent === '게시된 버전 자료 없음');
  release();
  await new Promise(resolve => setTimeout(resolve, 10));
  assert.ok(requests.includes('/api/versions/page/0'));
  assert.ok(get('comparison').hidden);
  assert.equal(get('current-sha').textContent, '');
  assert.equal(get('history').children.length, 0);
  assert.equal(get('diff').textContent, '');
  assert.equal(get('current-version').textContent, '게시된 버전 자료 없음');
});

test('poll replaces an older offset and automatic pair after two unseen transitions return to the same SHA', async t => {
  const {get, fixture} = mount(t);
  await until(() => fixture.poll);
  get('older').click();
  await until(() => get('history').children.length === 2);
  fixture.rows = [entry(28, 26), entry(27), ...initialRows];
  fixture.poll();
  await until(() => get('compare-note').textContent === '#27 → #28');
  assert.equal(get('history').children.length, 24);
  assert.equal(get('current-sha').textContent, sha(26));
  assert.match(get('history').firstChild.textContent, /#28 · 롤백 · #26 복원/);
});

test('a slower comparison cannot replace the newer pair selected on the same host', async t => {
  let release, pending;
  const wait = new Promise(resolve => { release = resolve; });
  const {get, select} = mount(t, {route: async path => {
    if (path.endsWith('/compare/25/24')) {
      pending = true;
      await wait;
      return {...comparison(initialRows, 25, 24), diff: '늦게 도착한 이전 비교'};
    }
  }});
  await until(() => get('diff').textContent === patch);
  select('to-version', 24);
  await until(() => pending);
  select('to-version', 25);
  await until(() => get('file-count').textContent === '변경 파일 0개');
  release();
  await new Promise(resolve => setTimeout(resolve, 10));
  assert.equal(get('compare-note').textContent, '#25 → #25');
  assert.equal(get('diff').textContent, '변경된 파일이 없습니다.');
});

test('unknown or ambiguous host selectors issue only a host-directory request', async t => {
  for (const suffix of ['?host=no', '?host=' + remote + '&host=local', '?host=' + 'f'.repeat(32)]) {
    const {get, requests} = mount(t, {suffix});
    await until(() => get('page-note').textContent.includes('호스트를 찾을 수 없습니다'));
    assert.deepEqual(requests, ['/api/hosts']);
  }
});
