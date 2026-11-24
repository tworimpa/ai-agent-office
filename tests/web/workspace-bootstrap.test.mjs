import assert from 'node:assert/strict';
import test from 'node:test';
import {JSDOM} from 'jsdom';
import {startWorkspace} from '../../src/agent_office/web/workspace-bootstrap.js';

function tab(t) {
  const dom = new JSDOM('<p id="connection"></p><p id="empty-note"></p><button id="empty-retry" hidden></button>',
    {url: 'http://localhost/?workspace=42'});
  t.after(() => dom.window.close());
  let reloads = 0;
  return {
    document: dom.window.document, sessionStorage: dom.window.sessionStorage,
    location: {reload() { ++reloads; }},
    get reloads() { return reloads; },
  };
}

// Use the module loader: the entry parses successfully, its dependency cannot load.
const brokenGraph = () => import('data:text/javascript,import%20%22file%3A%2F%2F%2Fmissing-office-fixture%2Fdependency.js%22');

test('a failed dependency reloads once, subsequent failures expose manual retry until a successful mount', async t => {
  const win = tab(t);
  const button = win.document.getElementById('empty-retry');
  await startWorkspace(win, brokenGraph);
  assert.equal(win.reloads, 1);
  assert.equal(button.hidden, true);
  await startWorkspace(win, brokenGraph);
  assert.equal(win.reloads, 1);
  assert.equal(win.document.documentElement.dataset.workspaceBoot, 'module-error');
  assert.equal(button.hidden, false);
  button.click();
  assert.equal(win.reloads, 2);
  let mounts = 0;
  await startWorkspace(win, async () => ({mountWorkspaceOffice: () => { ++mounts; }}));
  assert.equal(mounts, 1);
  assert.equal(win.document.documentElement.dataset.workspaceBoot, 'mounted');
  assert.equal(win.sessionStorage.length, 0);
});

test('an unavailable tab store and exceptions in mount never trigger an automatic reload', async t => {
  const win = tab(t);
  const unavailable = {...win, get sessionStorage() { throw Error('blocked'); }};
  await startWorkspace(unavailable, brokenGraph);
  assert.equal(win.reloads, 0);
  assert.equal(win.document.getElementById('empty-retry').hidden, false);
  await assert.rejects(startWorkspace(win, async () => ({mountWorkspaceOffice() { throw Error('mount'); }})), /mount/);
  assert.equal(win.document.documentElement.dataset.workspaceBoot, 'mount-error');
  assert.equal(win.reloads, 0);
});
