import assert from 'node:assert/strict';
import test from 'node:test';
import {base, mount, remote, room, until, workspace} from './workspace-fixture.mjs';

test('GitHub 설정과 동기화가 선택한 원격 실행 호스트를 사용한다', async t => {
  const root = '/api/hosts/' + remote;
  const path = root + base.replace('/api', '');
  const connection = {id: 'selected', name: '사내 GitHub', web_url: 'https://git.example'};
  const repo = {id: 13, full_name: 'team/sample', default_branch: 'trunk'};
  let config = null;
  const {get, window, submit, calls} = mount(t, (url, body) => {
    if (url === path) return {...room([]), github_supported: true};
    if (url === root + '/skills') return {skills: []};
    if (url === path + '/github') {
      if (body) config = body;
      return {connections: [connection], issues: [], status: {}, config};
    }
    if (url === path + '/github/repos/selected') return {repositories: [repo]};
    if (url === path + '/github/test') return {diagnostic: {ok: true, version: '3.20', api_url: 'https://git.example/api/v3'}};
    if (url === path + '/github/sync') return {queued: true};
  }, {query: '?host=' + remote + '&workspace=' + workspace.id});
  await until(() => !get('settings').disabled);
  get('settings').click();
  get('github-settings').open = true;
  await until(() => get('github-repo').options.length === 2);
  get('github-repo').value = '13';
  get('github-repo').dispatchEvent(new window.Event('change'));
  get('github-folder').value = '1';
  get('github-test').click();
  await until(() => get('github-diagnostic').textContent.includes('3.20'));
  assert.deepEqual(calls.find(c => c.path.endsWith('/github/test')).body,
    {connection_id: 'selected', repository_id: 13, folder_index: 1, branch: 'trunk'});
  get('github-actors').value = '23, 42';
  get('github-answers').value = '23';
  get('github-enabled').checked = true;
  submit('github-form');
  await until(() => config?.enabled);
  assert.deepEqual(config.allowed_actors, [23, 42]);
  assert.deepEqual(config.product_actors, []);
  assert.equal(config.draft_pr, false);
  await until(() => !get('github-sync').disabled);
  get('github-sync').click();
  await until(() => calls.some(c => c.path === path + '/github/sync' && c.body));
});

test('연결 등록이 없어도 설정을 열고 안내를 확인할 수 있다', async t => {
  const {get, calls} = mount(t, path => {
    if (path === base) return {...room([]), github_supported: true};
    if (path === base + '/github') return {config: null, connections: [], issues: [], status: {}};
    if (path.startsWith('/api/skills')) return {skills: []};
  });
  await until(() => !get('settings').disabled);
  get('settings').click();
  get('github-settings').open = true;
  await until(() => get('github-settings-note').textContent.includes('연결이 없습니다'));
  assert.ok(get('github-form').hidden);
  assert.ok(get('github-inbox').hidden);
  assert.ok(!calls.some(c => c.path.includes('/github/repos/')));
});

test('범위 검토와 중복 가능성이 있는 댓글 재시도는 별도 사용자 결정을 기다린다', async t => {
  const event = 'c'.repeat(64);
  const issue = {issue_id: 29, number: 6, title: '설정 복원', state: 'open', status: 'waiting_for_user', events: [],
    scope_pending: event, previous_scope: {title: '복원', body: '설정 한 개'}, proposed_scope: {title: '복원', body: '<b>전체 설정</b>'},
    publish_state: 'comment_ack_pending'};
  const {get, calls} = mount(t, (path, body) => {
    if (path === base) return {...room([]), github_supported: true};
    if (path === base + '/github') return {config: {enabled: true}, status: {}, issues: [issue], connections: []};
    if (path === base + '/github/issues/29') return {issue, next_cursor: null};
    if (path === base + '/github/decision' && body) return {ok: true};
    if (path === base + '/github/retry-post' && body) return {ok: true};
  });
  const list = get('github-issues');
  await until(() => list.textContent.includes('이슈 기록 및 최초 요청'));
  assert.ok(!list.textContent.includes('기존 범위 유지'));
  [...list.querySelectorAll('button')].find(b => b.textContent === '이슈 기록 및 최초 요청').click();
  await until(() => list.textContent.includes('기존 범위 유지'));
  assert.match(list.textContent, /설정 한 개/);
  assert.match(list.textContent, /<b>전체 설정<\/b>/);
  assert.equal(list.querySelector('b'), null);
  assert.equal(list.querySelector('details').open, false);
  assert.equal(calls.some(c => c.body !== undefined), false);
  const button = name => [...list.querySelectorAll('button')].find(b => b.textContent === name);
  button('기존 범위 유지').click();
  await until(() => !button('원본 확인 후 댓글 게시 재시도').disabled);
  assert.deepEqual(calls.find(c => c.path.endsWith('/decision')).body, {issue_id: 29, event_id: event, action: 'reject'});
  list.querySelector('details').open = true;
  button('원본 확인 후 댓글 게시 재시도').click();
  await until(() => calls.some(c => c.path.endsWith('/retry-post')));
  assert.match(calls.find(c => c.path.endsWith('/retry-post')).body.id, /^[a-f0-9]{32}$/);
});

test('다른 페이지의 이슈와 과거 이벤트를 보고 원래 목록으로 돌아간다', async t => {
  const first = {issue_id: 29, number: 6, title: '최근 이슈'};
  const older = {issue_id: 23, number: 3, title: '이전 이슈'};
  const state = (issue, next_cursor) => ({config: {enabled: true}, status: {}, issues: [issue], connections: [], next_cursor});
  const read = (detail, next_cursor) => ({issue: {...older, events: [{id: 'e', kind: 'note', detail}]}, next_cursor});
  const {get, calls} = mount(t, path => {
    if (path === base) return {...room([]), github_supported: true};
    if (path === base + '/github') return state(first, 23);
    if (path === base + '/github/page/23') return state(older, null);
    if (path === base + '/github/issues/23') return read('최근 변경', 50);
    if (path === base + '/github/issues/23/page/50') return read('최초 이벤트', null);
  });
  const list = get('github-issues'), button = text => [...list.querySelectorAll('button')].find(b => b.textContent === text);
  await until(() => list.textContent.includes('최근 이슈'));
  get('github-next').click();
  await until(() => list.textContent.includes('이전 이슈'));
  button('이슈 기록 및 최초 요청').click();
  await until(() => list.textContent.includes('최근 변경'));
  button('다음 기록 페이지').click();
  await until(() => list.textContent.includes('최초 이벤트'));
  button('이전 기록 페이지').click();
  await until(() => list.textContent.includes('최근 변경'));
  get('github-prev').click();
  await until(() => list.textContent.includes('최근 이슈'));
  assert.ok(!list.textContent.includes('최근 변경'));
  assert.equal(calls.some(c => c.body), false);
});
