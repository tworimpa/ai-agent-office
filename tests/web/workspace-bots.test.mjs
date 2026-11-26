import assert from 'node:assert/strict';
import test from 'node:test';
import {base, mount, remote, room, until, workspace} from './workspace-fixture.mjs';

test('작업실에서 봇 관리 클라이언트로 이동하고 사용자 역할을 실행 자리에 배정한다', async t => {
  const journal = 'e'.repeat(32), suffix = '?office_journal=' + journal;
  const assigned = {coordinator:'office-coordinator', developer:'office-developer', reviewer:'office-reviewer'};
  const bots = Object.entries(assigned).map(([role,id]) => ({id,role,name:id,builtin:true}));
  bots.push({id:'research',role:'사용자 조사',name:'다온',builtin:false});
  const {get, submit, window} = mount(t, (path, body) => {
    if (path === base) return {...room([]), journal_id:journal};
    if (path.endsWith('/skills')) return {skills:[]};
    if (path === base+'/bots/assignment'+suffix) {
      assert.deepEqual(body,{id:'research',role:'reviewer'});
      assigned.reviewer = body.id;
    }
    if (path.startsWith(base+'/bots')) return {bots,assignments:assigned};
  });
  await until(() => !get('settings').disabled);
  assert.ok(get('bot-client-link').href.endsWith('/bots?workspace='+workspace.id));
  get('settings').click();get('role-bots').open=true;
  await until(() => !get('bot-role').disabled);
  get('bot-role').value='reviewer';get('bot-role').dispatchEvent(new window.Event('change'));
  get('bot-id').value='research';get('bot-id').dispatchEvent(new window.Event('change'));
  submit('bot-assignment');
  await until(() => get('bot-current').textContent.includes('다온'));
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
