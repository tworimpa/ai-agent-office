import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { runInNewContext } from 'node:vm'

const script = readFileSync(new URL('src/pin-journal.js', import.meta.url), 'utf8')
const key = 'ai-agent-office.rust.journal'
const accessKey = 'ai-agent-office.rust.access'
const id = 'a'.repeat(32)
const other = 'b'.repeat(32)

function load(address, storage, webkitGtk = false) {
  const location = new URL(address)
  const win = { fetch: async (input, options) => ({ input, options }) }
  win.top = win
  const sessionStorage = {
    getItem: k => storage.get(k) ?? null,
    setItem: (k, v) => storage.set(k, v),
    clear: () => storage.clear(),
  }
  const history = { replaceState: (_state, _title, url) => { location.href = url.href } }
  runInNewContext(script.replace('__WEBKIT_GTK__', String(webkitGtk)),
    { window: win, location, sessionStorage, history, Request, Headers, URL, URLSearchParams })
  return { fetch: win.fetch, location }
}

test('pin survives full page changes while preserving Studio fetch options and selection', async () => {
  const base = 'http://127.0.0.1:8766'
  const storage = new Map()
  const first = load(base + '/?workspace=abc#office-desktop-journal=' + id + '&access=bound&anchor=role', storage)
  assert.equal(first.location.href, base + '/?workspace=abc#role')
  const next = load(base + '/studio', storage)
  const init = { method: 'POST', body: '{}', headers: { 'X-Agent-Office': 'studio' } }
  const { input, options } = await next.fetch('/api/test', init)
  assert.equal(input, base + '/api/test?office_journal=' + id)
  assert.equal(options.headers.get('X-Agent-Office-Expected-Journal'), id)
  assert.equal(options.headers.get('X-Agent-Office'), 'studio')
  assert.equal(options.method, 'POST')
  assert.equal(options.body, '{}')
  assert.equal((await next.fetch('http://127.0.0.1:8767/api/hosts', init)).options, init)
})

test('replacing journal discards pending forms from the former journal only on change', async () => {
  const base = 'http://127.0.0.1:8766/#office-desktop-journal='
  const storage = new Map([[key, id], [accessKey, 'bound'], ['pending', 'form']])
  load(base + id + '&access=bound', storage)
  assert.equal(storage.get('pending'), 'form')
  load(base + other + '&access=bound', storage)
  assert.equal(storage.has('pending'), false)
  assert.equal(storage.get(key), other)
})

test('missing or malformed pin blocks Studio API calls; setup is unaffected', async () => {
  for (const fragment of ['', '#office-desktop-journal=bad']) {
    const page = load('http://127.0.0.1:8766/' + fragment, new Map())
    await assert.rejects(page.fetch('/api/hosts'), /저널 ID/)
  }
  const setup = load('tauri://localhost/index.html', new Map())
  assert.equal((await setup.fetch('/api/hosts')).options, undefined)
})

test('only Linux WebKit same-origin Studio API writes override the page referrer policy', async () => {
  const base = 'http://127.0.0.1:8766'
  const init = { method: 'POST', referrerPolicy: 'no-referrer' }
  for (const webkit of [true, false]) {
    const page = load(base + '/', new Map([[key, id], [accessKey, 'bound']]), webkit)
    const write = await page.fetch('/api/workspaces', init)
    assert.equal(write.options.referrerPolicy, webkit ? 'same-origin' : 'no-referrer')
    const request = new Request(base + '/api/test', { method: 'POST', body: 'hello', credentials: 'omit' })
    const rewritten = await page.fetch(request)
    assert.equal(rewritten.options.referrerPolicy, webkit ? 'same-origin' : undefined)
    assert.equal(rewritten.input.url, base + '/api/test?office_journal=' + id)
    assert.equal(await rewritten.input.text(), 'hello')
    assert.equal(rewritten.input.credentials, 'omit')
    assert.equal((await page.fetch('/api/status')).options.referrerPolicy, undefined)
    assert.equal((await page.fetch('/example', init)).options, init)
    assert.equal((await page.fetch('http://127.0.0.1:8767/api/test', init)).options, init)
  }
})

test('a confirmed legacy journal permits unqualified reads and rejects writes before fetch', async () => {
  const base = 'http://127.0.0.1:8766'
  const storage = new Map([[key, id], [accessKey, 'bound'], ['pending', 'old submission']])
  const page = load(base + '/#office-desktop-journal=' + id + '&access=read-only', storage)
  assert.equal(storage.has('pending'), false)
  const read = await page.fetch('/api/workspaces')
  assert.equal(read.input, '/api/workspaces')
  assert.equal(read.options.headers.get('X-Agent-Office-Expected-Journal'), id)
  assert.equal(read.options.redirect, 'error')
  for (const method of ['POST', 'PUT', 'PATCH', 'DELETE']) {
    await assert.rejects(page.fetch(new Request(base + '/api/test', { method })), /읽기 전용/)
  }
  const next = load(base + '/studio', storage)
  await assert.rejects(next.fetch('/api/test', { method: 'POST' }), /읽기 전용/)
  const unknown = load(base + '/', new Map([[key, id]]))
  await assert.rejects(unknown.fetch('/api/test'), /읽기 전용/)
})
