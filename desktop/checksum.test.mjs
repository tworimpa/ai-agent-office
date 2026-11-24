import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { test } from 'node:test'
import { buildChecksum, installerName, installerReceipt, selectReceipt, verifyInstaller } from './checksum.mjs'

const version = '0.9.0'
const contents = Buffer.from([0, 0xff, 0x10])
const digest = createHash('sha256').update(contents).digest('hex')
const env = {
  RUNNER_OS: 'Windows', GITHUB_RUN_ID: '123', GITHUB_RUN_ATTEMPT: '3', GITHUB_SHA: 'c'.repeat(40),
  ARTIFACT_ID: '456', ARTIFACT_DIGEST: 'a'.repeat(64), INSTALLER_SHA256: digest,
}

async function fixture(t) {
  const dir = await mkdtemp(join(tmpdir(), 'office-installer-'))
  t.after(() => rm(dir, { recursive: true, force: true }))
  return dir
}

for (const [platform, name] of [['Windows', 'AI Agent Office Setup 0.9.0.exe'],
  ['Linux', 'ai-agent-office-desktop_0.9.0_amd64.deb']]) {
  test(`${platform} build and downloaded installer must match by filename, checksum and bytes`, async t => {
    const directory = await fixture(t)
    assert.equal(installerName(platform, version), name)
    await assert.rejects(buildChecksum(directory, name), /exactly one/)
    await writeFile(join(directory, name), '')
    await assert.rejects(buildChecksum(directory, name), /Empty installer/)
    await writeFile(join(directory, name), contents)
    const checksum = join(directory, 'SHA256SUMS.txt')
    assert.equal(await buildChecksum(directory, name), digest)
    assert.equal(await readFile(checksum, 'utf8'), `${digest}  ${name}\n`)
    await verifyInstaller(directory, name, digest)

    await writeFile(checksum, `${digest}  ${name}\n${digest}  other\n`)
    await assert.rejects(verifyInstaller(directory, name, digest), /differs/)
    await writeFile(join(directory, name), 'different')
    await buildChecksum(directory, name)
    await assert.rejects(verifyInstaller(directory, name, digest), /differs/)
    await writeFile(join(directory, 'extra.exe'), contents)
    await buildChecksum(directory, name)
    await assert.rejects(verifyInstaller(directory, name, digest), /only/)
    await rm(join(directory, name))
    await assert.rejects(buildChecksum(directory, name), /exactly one/)
  })
}

test('receipt identifies exact upload, run, attempt, source and build', () => {
  assert.deepEqual(installerReceipt({ ...env, ARTIFACT_DIGEST: 'sha256:' + env.ARTIFACT_DIGEST }, version), {
    schema_version: 1, platform: 'Windows', version, run_id: 123, run_attempt: 3, head_sha: env.GITHUB_SHA,
    artifact_id: 456, artifact_digest: env.ARTIFACT_DIGEST, installer_name: 'AI Agent Office Setup 0.9.0.exe',
    installer_sha256: digest,
  })
  for (const [key, value] of Object.entries({ ARTIFACT_ID: '', ARTIFACT_DIGEST: 'ff', GITHUB_RUN_ID: '0',
    GITHUB_RUN_ATTEMPT: '9007199254740992', GITHUB_SHA: 'main', INSTALLER_SHA256: '', RUNNER_OS: 'macOS' })) {
    assert.throws(() => installerReceipt({ ...env, [key]: value }, version))
  }
})

test('Rust retries use the newest successful desktop receipt from this run and platform', async t => {
  const dir = await fixture(t)
  for (const attempt of [1, 10, 3]) {
    const path = join(dir, 'desktop-receipt-Windows-' + attempt)
    await mkdir(path)
    await writeFile(join(path, 'receipt.json'), JSON.stringify(installerReceipt({
      ...env, GITHUB_RUN_ATTEMPT: String(attempt), ARTIFACT_ID: String(100 + attempt),
    }, version)))
  }
  const later = { ...env, GITHUB_RUN_ATTEMPT: '11' }
  assert.equal((await selectReceipt(dir, later, version)).artifact_id, 110)
  await assert.rejects(selectReceipt(dir, env, version), /attempt/)
  await assert.rejects(selectReceipt(dir, { ...later, GITHUB_RUN_ID: '345' }, version), /does not match/)
  await assert.rejects(selectReceipt(dir, { ...later, GITHUB_SHA: 'd'.repeat(40) }, version), /does not match/)
  await assert.rejects(selectReceipt(dir, { ...later, RUNNER_OS: 'Linux' }, version), /Unexpected receipt/)
  await assert.rejects(selectReceipt(dir, later, '0.9.1'), /does not match/)
})
