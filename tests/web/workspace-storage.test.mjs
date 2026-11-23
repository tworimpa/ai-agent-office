import assert from 'node:assert/strict';
import test from 'node:test';
import {base, mount, remote, room, runId, until, workspace} from './workspace-fixture.mjs';

test('정리 전 사용자가 대상을 고르고 원격 저널과 커밋을 함께 전송한다', async t => {
  const path = '/api/hosts/' + remote + base.slice(4);
  const head = 'a'.repeat(40), retained = 'e'.repeat(32);
  let mutation;
  const {get, submit, calls} = mount(t, (url, body) => {
    if (url === path) return {...room([]), storage_supported: true, journal_id: remote};
    if (url.endsWith('/skills')) return {skills: []};
    if (url === path + '/storage') {
      const source = {workspace_id: workspace.id, journal_id: remote};
      if (body) {
        mutation = body;
        return {...source, run_id: body.run_id, outcome: 'removed'};
      }
      return {...source, entries: [
        ...mutation ? [] : [{run_id: runId, head, bytes: 1024, reasons: []}],
        {run_id: retained, head, bytes: 4, reasons: ['checkpoint']},
      ]};
    }
  }, {query: '?host=' + remote + '&workspace=' + workspace.id});
  await until(() => !get('settings').disabled);
  get('settings').click();
  assert.equal(get('worktree-storage').hidden, false);
  assert.equal(calls.filter(call => call.path.endsWith('/storage')).length, 0);
  get('storage-preview').click();
  await until(() => get('storage-' + retained));
  assert.equal(get('storage-' + retained).disabled, true);
  assert.match(get('storage-entries').textContent, /체크포인트/);
  assert.equal(get('storage-clean').disabled, true);
  get('storage-' + runId).click();
  assert.equal(get('storage-clean').disabled, false);
  submit('storage-form');
  await until(() => get('storage-note').textContent.includes('정리했습니다'));
  assert.deepEqual(mutation, {journal_id: remote, run_id: runId, head});
  assert.equal(get('storage-' + runId), null);
});
