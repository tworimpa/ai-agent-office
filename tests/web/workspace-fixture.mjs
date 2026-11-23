import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {JSDOM} from 'jsdom';
import {mountWorkspaceOffice} from '../../src/agent_office/web/workspace-office.js';

const html = await readFile(new URL('../../src/agent_office/web/workspace-office.html', import.meta.url), 'utf8');
const remote = 'a'.repeat(32);
const workspace = {id: 'b'.repeat(32), name: 'AI Agent Office', folders: ['E:/프로젝트', 'E:/문서']};
const runId = 'c'.repeat(32);
const skillId = 'd'.repeat(32);
const hosts = [{id: 'local', name: '이 PC'}, {id: remote, name: '원격 연구실'}];
const settings = {
  delegation: true,
  developer: {name: '서진', engine: 'codex', skills: [skillId]},
  reviewer: {name: '정희', engine: 'antigravity', skills: []},
};
const run = {
  id: runId, workspace_id: workspace.id, task: '원인 확인', state: 'RUNNING', created_at: '2026-11-19T01:02:03Z',
  developer_engine: 'codex', reviewer_engine: 'antigravity', allow_edits: true, use_inbox: true, attempts: 1,
  assignment: {roles: {developer: settings.developer, reviewer: settings.reviewer}, delegation: true, note: '서진에게 작업을 맡겼습니다.'},
  lines: {developer: '오류 원인을 찾았고 테스트 중입니다.', reviewer: '작업 시작을 기다리고 있습니다.'},
  pending_count: 0,
};
const room = (runs = [run], values = settings) => ({workspace, settings: values, runs, engines: []});
const base = '/api/rooms/' + workspace.id;
const runPath = '/api/runs/' + run.id;

async function until(predicate) {
  for (let i = 0; i < 120; i++) {
    if (predicate()) return;
    await new Promise(resolve => setTimeout(resolve, 5));
  }
  assert.ok(predicate(), '작업실 응답 대기 시간 초과');
}

function mount(t, route, {query = '', storage = {}, preferences = {}, setupWindow} = {}) {
  const dom = new JSDOM(html, {url: 'http://127.0.0.1:8766/' + query, pretendToBeVisual: true});
  const {window} = dom;
  setupWindow?.(window);
  const get = id => window.document.getElementById(id);
  const person = role => window.document.querySelector('.seat[data-person="' + role + '"]');
  for (const [key, value] of Object.entries(storage)) window.sessionStorage.setItem(key, value);
  for (const [key, value] of Object.entries(preferences)) window.localStorage.setItem(key, value);
  if (!window.HTMLDialogElement.prototype.showModal) {
    window.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
    window.HTMLDialogElement.prototype.close = function () { this.open = false; this.dispatchEvent(new window.Event('close')); };
  }
  const calls = [];
  window.fetch = async (path, options) => {
    assert.equal(options.mode, 'same-origin');
    const body = options.body ? JSON.parse(options.body) : undefined;
    if (body) assert.equal(options.headers['X-Agent-Office'], 'studio');
    calls.push({path, body});
    const result = await route(path, body);
    if (result !== undefined) return result instanceof Response ? result : Response.json(result);
    if (path === '/api/hosts') return Response.json({hosts});
    if (path.endsWith('/workspaces')) return Response.json({workspaces: [workspace]});
    if (path.endsWith('/inbox')) return Response.json({requests: []});
    if (path.endsWith('/directions')) return Response.json({directions: []});
    assert.fail('unexpected fetch ' + path);
  };
  const dispose = mountWorkspaceOffice(window);
  const close = () => { dispose(); window.close(); };
  t.after(close);
  const submit = id => get(id).dispatchEvent(new window.Event('submit', {bubbles: true, cancelable: true}));
  return {window, get, person, calls, submit, close};
}

export {base, hosts, html, mount, remote, room, run, runId, runPath, settings, skillId, until, workspace};
