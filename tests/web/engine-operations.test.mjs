import assert from 'node:assert/strict';
import test from 'node:test';
import {JSDOM} from 'jsdom';
import {watchProgress} from '../../src/agent_office/web/workspace-progress.js';
import {engineMaintenance} from '../../src/agent_office/web/engine-maintenance.js';

test('progress follows the selected run and journal, hides stale rows and closes background streams', () => {
  const {window} = new JSDOM('<p id="provider-progress" hidden></p>', {pretendToBeVisual:true});
  const opened = [];
  window.EventSource = class extends window.EventTarget {
    constructor(url) { super(); this.url = url; opened.push(this); }
    close() { this.closed = true; }
  };
  const doc = window.document, panel = watchProgress(doc, window);
  const target = {id:'run', active:true, url:'/pinned/stream'};
  const row = {id:2, engine:'cline',label:'도구 응답',at:'2026-01-01T12:00:00Z'};
  panel.select(target);
  opened[0].onmessage({data:JSON.stringify(row)});
  panel.select({...target, latest:{...row,id:1,label:'이전 작업'}});
  const node = doc.querySelector('p');
  assert.match(node.textContent, /도구 응답/);
  Object.defineProperty(doc,'hidden',{value:true,configurable:true});
  doc.dispatchEvent(new window.Event('visibilitychange'));
  assert.equal(opened[0].closed,true);
  panel.select({id:'other',active:false,url:null});
  opened[0].onmessage({data:JSON.stringify({...row,id:4})});
  assert.equal(node.hidden,true);
  panel.dispose();
  window.close();
});

test('version lookup supplies a journal-bound operator command with no browser mutation', async () => {
  const {window} = new JSDOM();
  const calls = [], journal = 'a'.repeat(32);
  const group = engineMaintenance(window.document, 'cline', async (path,body) => {
    calls.push([path,body]);
    return {engine:'cline',journal_id:journal, installed:'3.0.75', latest:'3.0.76', update_available:true, update_policy:'pinned'};
  });
  assert.deepEqual(calls,[]);
  await group.querySelector('button').onclick();
  assert.deepEqual(calls, [['/engines/cline/check',{}]]);
  assert.equal(group.querySelectorAll('button').length,1);
  assert.equal(group.querySelector('code').textContent,
    'agent-office update-engine cline --from-version 3.0.75 --to-version 3.0.76 --journal '+journal);
  window.close();
});
