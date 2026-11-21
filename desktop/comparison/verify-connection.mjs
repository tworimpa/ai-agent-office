// Exercise a real Studio on a reused port and open its original journal after removing the comparator.
import assert from 'node:assert/strict'
import {spawn} from 'node:child_process'
import {once} from 'node:events'
import {mkdir, mkdtemp, readFile, realpath, writeFile} from 'node:fs/promises'
import {dirname, join, resolve} from 'node:path'
import {tmpdir} from 'node:os'
import {createInterface} from 'node:readline'
import {launch, until} from './drivers.mjs'
import {finishVerification} from '../verify-lifecycle.mjs'

const [mode, electronPath, rustPath, output] = process.argv.slice(2)
if (!output || !['connect', 'survivor', 'rust-survivor'].includes(mode)) throw Error('Usage: verify-connection.mjs connect|survivor|rust-survivor ELECTRON RUST EVIDENCE')
const evidence = resolve(output)
await mkdir(evidence, {recursive:true})
const manifest = join(evidence, 'identity.json')
const previous = mode !== 'connect' && JSON.parse(await readFile(manifest, 'utf8'))
const root = previous ? previous.root : await mkdtemp(join(tmpdir(), 'office-compare-journal-'))
const electron = mode === 'rust-survivor' ? electronPath : await realpath(electronPath)
const python = process.env.OFFICE_COMPARISON_PYTHON || join(dirname(electron),'resources','runtime',
  process.platform === 'win32' ? 'python.exe' : 'bin/python3')
const serve = [
  'from agent_office.studio_server import StudioServer',
  'from pathlib import Path',
  'import json, sys, threading',
  'with StudioServer(Path(sys.argv[1]), int(sys.argv[2])) as server:',
  '    thread = threading.Thread(target=server.serve_forever)',
  '    thread.start()',
  '    print(json.dumps([server.server_port, server.journal_id]), flush=True)',
  '    try: sys.stdin.read()',
  '    finally: server.shutdown(); thread.join()'
].join('\n')
let active, page, failure
async function start(journal, port = 0) {
  const child = spawn(python,['-I','-B','-c',serve,join(root,journal + '.sqlite3'),String(port)],
    {stdio:['pipe','pipe','inherit'],windowsHide:true})
  const exit = once(child, 'exit')
  active = {child,exit}
  const lines = createInterface({input:child.stdout})
  try {
    const [line] = await Promise.race([once(lines,'line'), exit.then(() => { throw Error('Test Studio exited during startup') })])
    const [port, id] = JSON.parse(line)
    return {port,id,origin:'http://127.0.0.1:' + port}
  } finally { lines.close() }
}
async function stop() {
  if (!active) return
  const {child,exit} = active
  active = null
  child.stdin.end()
  assert.equal((await exit)[0],0)
}
async function close() {
  if (page) { const current = page; page = null; await current.close() }
}
async function setupWindow() {
  await page.selectWindow(url => /^(?:tauri:\/\/localhost|https?:\/\/tauri\.localhost)(?:\/|$)/.test(url))
  await page.ready('#address')
}
async function choose(studio) {
  await page.run(origin => {
    const input = document.querySelector('#address')
    input.value = origin
    input.dispatchEvent(new Event('input',{bubbles:true}))
    input.form.requestSubmit(document.querySelector('#connect'))
  },studio.origin)
  await page.ready('#confirm')
  assert.ok((await page.run(() => document.querySelector('#identity').textContent)).includes(studio.id))
  await page.activate('#confirm')
  await room(studio)
}
async function room(studio) {
  await page.selectWindow(url => url.startsWith(studio.origin + '/'))
  const journal = await page.run(async () => (await (await fetch('/api/studio-identity')).json()).journal_id)
  assert.equal(journal,studio.id)
}
try {
  const first = await start('original')
  if (mode === 'connect') {
    const path = join(root,'workspace')
    await mkdir(path)
    const registration = await fetch(first.origin + '/api/workspaces',{method:'POST',headers:{
      'Content-Type':'application/json',Origin:first.origin,'X-Agent-Office':'studio'
    },body:JSON.stringify({name:'설치 확인',paths:[path]})})
    assert.equal(registration.status,201,await registration.clone().text())
    const workspace = (await registration.json()).workspace.id
    const profile = join(root,'rust-profile')
    page = await launch('rust',rustPath,profile)
    await setupWindow()
    await choose(first)
    await close()
    page = await launch('rust',rustPath,profile)
    await room(first)
    await stop()
    await setupWindow()
    await until(() => page.run(() => document.querySelector('#status').textContent.includes('연결할 수 없습니다')),'offline notice')
    const other = await start('replacement',first.port)
    assert.notEqual(other.id,first.id)
    await until(() => page.run(() => document.querySelector('#status').textContent.includes('다른 작업실')),'replacement notice')
    await page.screenshot(join(evidence,'changed-journal.png'))
    await choose(other)
    await close()
    await writeFile(manifest,JSON.stringify({root,original:first.id,replacement:other.id,workspace},null,2))
  } else {
    assert.equal(first.id,previous.original)
    const kind = mode === 'survivor' ? 'electron' : 'rust'
    page = await launch(kind, kind === 'rust' ? rustPath : electron, join(root,kind+'-survivor-profile'))
    if (kind === 'electron') {
      await page.activate('#connection-options > summary')
      await page.fill('#address',first.origin)
      await page.activate('#connect button')
      await room(first)
    } else {
      await setupWindow()
      await choose(first)
    }
    await page.ready('#workspaces a[href*="' + previous.workspace + '"]')
    await page.screenshot(join(evidence,kind+'-after-remove.png'))
    await writeFile(join(evidence,kind+'-survivor.json'),JSON.stringify({original:first.id,workspace:previous.workspace},null,2))
  }
} catch (error) {
  failure = error
  if (page) {
    const screen = await page.run(() => ({
      url:location.href, address:document.querySelector('#address')?.value, text:document.body.innerText.slice(0,1500)
    })).catch(() => null)
    console.error('Connection verification stopped:', screen)
    await page.screenshot(join(evidence,'connection-failed.png')).catch(() => {})
  }
} finally {
  await finishVerification(failure,close,stop)
}
