import assert from 'node:assert/strict'
import { test } from 'node:test'
import { checkStudio, localStudioOrigin, studioPage, validateRemotes } from './studio.mjs'

const origin = 'http://127.0.0.1:8766'
const journal = '1'.repeat(32)
const response = (value, status = 200) => new Response(JSON.stringify(value), {
  status, headers: { 'content-type': 'application/json' },
})

test('Studio addresses stay on IPv4 loopback and page restoration stays within the Studio UI', () => {
  assert.equal(localStudioOrigin('http://localhost:8766/'), origin)
  for (const invalid of ['https://localhost:8766', 'http://192.168.0.2:8766', 'http://127.0.0.1:8766/api',
    'http://user@localhost:8766', 'file:///C:/index.html', origin + '/?request=other']) {
    assert.throws(() => localStudioOrigin(invalid), /Studio 주소/)
  }
  assert.equal(studioPage('/?workspace=' + journal, origin), '/?workspace=' + journal)
  assert.equal(studioPage(origin + '/api/runs', origin), null)
  assert.equal(studioPage('http://127.0.0.1:9080/', origin), null)
  assert.equal(studioPage('//example.org/', origin), null)
})

test('a stable Studio identity is checked against the saved journal and managed older Studio can reconnect', async () => {
  const request = async (url, options) => {
    assert.equal(options.redirect, 'error')
    if (url === origin + '/api/hosts') return response({ hosts: [{ id: 'local', name: '이 컴퓨터' }] })
    if (url === origin + '/api/runtime-health') return response({ journal_id: journal })
    return response({}, 404)
  }
  assert.deepEqual(await checkStudio(origin, journal, request), { origin, journalId: journal })
  await assert.rejects(checkStudio(origin, '2'.repeat(32), request), /저장한 저널과 다른/)
  await assert.rejects(checkStudio(origin, null, async () => response({ status: 'healthy' })), /작업실을 확인/)
})

test('remote config permits only SSH aliases and existing journal port and ID forms', () => {
  const remote = { alias: 'build-box', port: 8766, journalId: journal }
  assert.deepEqual(validateRemotes([remote]), [remote])
  for (const input of [[{ ...remote, alias: '-oProxyCommand' }],
    [{ ...remote, port: '8766' }], [remote, { ...remote, alias: 'another' }]]) {
    assert.throws(() => validateRemotes(input), /SSH 별칭/)
  }
})
