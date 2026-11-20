import assert from 'node:assert/strict';
import test from 'node:test';
import {mount, remote, until, workspace} from './workspace-fixture.mjs';

test('first local folder click suggests an editable name and appends additional selections', async t => {
  const {get, window} = mount(t, path => {
    if (path === '/api/workspaces') return {workspaces: []};
  });
  const selections = [['C:\\프로젝트\\도구'], ['C:\\프로젝트\\도구', 'D:\\문서'], []];
  window.officeFolders = {select: async () => selections.shift()};
  await until(() => !get('empty-register').hidden);
  get('empty-register').click();
  await until(() => get('workspace-name').value === '도구');
  assert.match(get('register-host').textContent, /이 PC/);
  get('workspace-name').value = '내 도구';
  get('workspace-name').dispatchEvent(new window.Event('input'));
  get('pick-folders').click();
  await until(() => get('workspace-paths').value.includes('문서'));
  get('pick-folders').click();
  assert.equal(get('workspace-name').value, '내 도구');
  assert.deepEqual(get('workspace-paths').value.split('\n'), ['C:\\프로젝트\\도구', 'D:\\문서']);
});

test('SSH workspace uses paths entered for the selected host even in the Desktop viewer', async t => {
  const {get, window, calls, submit} = mount(t, (path, body) => {
    if (path === '/api/hosts/' + remote + '/workspaces') {
      return body ? Response.json({error: {message: '등록할 폴더를 확인하세요.'}}, {status: 400}) : {workspaces: []};
    }
  }, {query: '?host=' + remote});
  window.officeFolders = {select: () => assert.fail('원격 호스트에서 로컬 폴더를 선택했습니다.')};
  await until(() => !get('empty-register').hidden);
  get('empty-register').click();
  assert.ok(get('pick-folders').hidden);
  assert.match(get('register-host').textContent, /원격 연구실/);
  get('workspace-paths').value = '/srv/작업실\n/srv/설계';
  get('workspace-paths').dispatchEvent(new window.Event('input'));
  submit('register-form');
  await until(() => get('register-note').textContent.includes('등록할 폴더'));
  assert.deepEqual(calls.at(-1), {path: '/api/hosts/' + remote + '/workspaces', body: {
    name: '작업실', paths: ['/srv/작업실', '/srv/설계'],
  }});
});

test('missing saved workspace keeps its target and can be retried', async t => {
  let attempts = 0;
  const {get, window} = mount(t, path => {
    if (path === '/api/workspaces') return {workspaces: []};
    if (path === '/api/rooms/' + workspace.id) {
      attempts++;
      return Response.json({error: {message: '작업공간을 찾을 수 없습니다.'}}, {status: 404});
    }
  }, {query: '?workspace=' + workspace.id});
  await until(() => !get('empty-retry').hidden);
  assert.match(get('empty-note').textContent, new RegExp(workspace.id));
  assert.equal(window.location.search, '?workspace=' + workspace.id);
  get('empty-retry').click();
  await until(() => attempts === 2);
});
