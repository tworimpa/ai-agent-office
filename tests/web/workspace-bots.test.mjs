import assert from 'node:assert/strict';
import test from 'node:test';
import {base, mount, remote, room, until, workspace} from './workspace-fixture.mjs';

test('작업실에서 봇을 만들고 별도 동작으로 리뷰어에게 배정한다', async t => {
  const journal = 'e'.repeat(32), suffix = '?office_journal=' + journal;
  const assigned = {coordinator: 'office-coordinator', developer: 'office-developer', reviewer: 'office-reviewer'};
  const definitions = Object.entries(assigned).map(([role, id]) => ({role, id, name: id, builtin: true}));
  const created = {id: 'api-review', name: 'API 리뷰어', role: 'API 설계자', instructions: '<main> 변경을 검토'};
  let files = {'SOUL.md': created.instructions, 'MEMORY.md': ''};
  const {get, submit, window} = mount(t, (path, body) => {
    if (path === base) return {...room([]), journal_id: journal, bot_room_supported: true};
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
    if (path === base + '/bots/api-review/assets' + suffix) {
      if (body) {
        assert.deepEqual(body, {filename:'MEMORY.md', digest:'version1', content:'API 참고'});
        files = {...files, 'MEMORY.md':body.content};
      }
      return {id:created.id, digest:body ? 'version2' : 'version1', files};
    }
    if (path === base + '/bots/api-review/commands' + suffix) {
      assert.equal(body.message, 'API 사용성 분석');
      assert.equal(body.allow_edits, false);
      return {run:{id:body.id, workspace_id:workspace.id, assignment:{bots:{developer:{id:created.id}}}}};
    }
  });
  await until(() => !get('settings').disabled);
  get('bot-new').click();
  assert.ok(get('bot-create-dialog').open);
  get('bot-slug').value = created.id;
  get('bot-name').value = created.name;
  get('bot-title').value = created.role;
  get('bot-instructions').value = created.instructions;
  submit('bot-definition');
  await until(() => !get('bot-editor').hidden);
  assert.match(get('bot-roster').textContent, /API 설계자/);
  const memory = Array.from(get('bot-file-list').children).find(el => el.textContent === 'MEMORY.md');
  memory.click();
  get('bot-text').value = 'API 참고';
  get('bot-text').dispatchEvent(new window.Event('input'));
  submit('bot-editor');
  await until(() => get('bot-file-note').textContent.includes('저장됨'));
  get('bot-message').value = 'API 사용성 분석';
  get('bot-message').dispatchEvent(new window.Event('input'));
  submit('bot-command');
  await until(() => !get('bot-run-link').hidden);
  get('settings').click();
  get('role-bots').open = true;
  await until(() => !get('bot-role').disabled);
  get('bot-role').value = 'reviewer';
  get('bot-role').dispatchEvent(new window.Event('change'));
  get('bot-id').value = created.id;
  get('bot-id').dispatchEvent(new window.Event('change'));
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
