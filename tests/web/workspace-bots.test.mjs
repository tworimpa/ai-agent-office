import assert from 'node:assert/strict';
import test from 'node:test';
import {base, mount, remote, room, until, workspace} from './workspace-fixture.mjs';

test('작업실에서 봇을 만들고 별도 동작으로 리뷰어에게 배정한다', async t => {
  const journal = 'e'.repeat(32), suffix = '?office_journal=' + journal;
  const assigned = {coordinator: 'office-coordinator', developer: 'office-developer', reviewer: 'office-reviewer'};
  const definitions = Object.entries(assigned).map(([role, id]) => ({role, id, name: id, builtin: true}));
  const created = {id: 'api-review', name: 'API 리뷰어', role: 'reviewer', instructions: '<main> 변경을 검토'};
  const {get, submit, window} = mount(t, (path, body) => {
    if (path === base) return {...room([]), journal_id: journal};
    if (path.endsWith('/skills')) return {skills: []};
    if (path === base + '/bots' + suffix) {
      if (body) { assert.deepEqual(body, created); definitions.push(created); }
      return {bots: definitions, assignments: assigned};
    }
    if (path === base + '/bots/assignment' + suffix) {
      assert.deepEqual(body, {role: 'reviewer', id: 'api-review'});
      assigned.reviewer = body.id;
      return {bots: definitions, assignments: assigned};
    }
  });
  await until(() => !get('settings').disabled);
  get('settings').click();
  get('role-bots').open = true;
  await until(() => !get('bot-create').disabled);
  get('bot-role').value = 'reviewer';
  get('bot-role').dispatchEvent(new window.Event('change'));
  assert.equal(get('bot-id').value, 'office-reviewer');
  get('bot-slug').value = created.id;
  get('bot-name').value = created.name;
  get('bot-instructions').value = created.instructions;
  submit('bot-definition');
  await until(() => !get('bot-assign').disabled);
  assert.equal(get('bot-id').value, created.id);
  assert.equal(get('bot-guidance').textContent, created.instructions);
  assert.match(get('bot-current').textContent, /office-reviewer/);
  submit('bot-assignment');
  await until(() => get('bot-current').textContent.includes(created.name));
  assert.ok(get('bot-assign').disabled);
});

test('SSH 작업실에서는 로컬 봇 관리 화면을 열지 않는다', async t => {
  const {get, calls} = mount(t, path => {
    if (path.endsWith('/rooms/' + workspace.id)) return {...room([]), journal_id: remote};
    if (path.endsWith('/skills')) return {skills: []};
  }, {query: '?host=' + remote + '&workspace=' + workspace.id});
  await until(() => !get('settings').disabled);
  get('settings').click();
  assert.ok(get('role-bots').hidden);
  assert.ok(!calls.some(call => call.path.includes('/bots')));
});
