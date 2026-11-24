import { createHash } from 'node:crypto'
import { createReadStream } from 'node:fs'
import { appendFile, lstat, readdir, readFile, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

const manifest = JSON.parse(await readFile(new URL('./package.json', import.meta.url), 'utf8'))
const checksumName = 'SHA256SUMS.txt'
const dist = fileURLToPath(new URL('./dist/', import.meta.url))
const hex256 = /^[a-f0-9]{64}$/
const gitSha = /^[a-f0-9]{40}$/

export function installerName(platform, version) {
  if (!/^\d+\.\d+\.\d+$/.test(version)) throw new Error('Invalid desktop version')
  if (platform === 'Windows') return `AI Agent Office Setup ${version}.exe`
  if (platform === 'Linux') return `ai-agent-office-desktop_${version}_amd64.deb`
  throw new Error(`Unsupported installer platform: ${platform}`)
}

async function hashFile(path) {
  const hash = createHash('sha256')
  for await (const chunk of createReadStream(path)) hash.update(chunk)
  return hash.digest('hex')
}

function checksumLine(name, digest) {
  return `${digest}  ${name}\n`
}

export async function buildChecksum(directory, name) {
  const entries = await readdir(directory, { withFileTypes: true })
  // Local builds can retain installers from prior releases in dist.
  const installers = entries.filter(entry => entry.name.toLowerCase() === name.toLowerCase())
  if (installers.length !== 1 || installers[0].name !== name || !installers[0].isFile()) {
    throw new Error(`Expected exactly one packaged installer: ${name}`)
  }
  if ((await lstat(join(directory, name))).size === 0) throw new Error('Empty installer')
  const checksum = entries.find(entry => entry.name.toLowerCase() === checksumName.toLowerCase())
  if (checksum && (!checksum.isFile() || checksum.name !== checksumName)) throw new Error('Invalid checksum file')
  const digest = await hashFile(join(directory, name))
  await writeFile(join(directory, checksumName), checksumLine(name, digest))
  return digest
}

export async function verifyInstaller(directory, name, expected) {
  if (!hex256.test(expected)) throw new Error('Missing or invalid build digest')
  const entries = await readdir(directory, { withFileTypes: true })
  if (entries.length !== 2 || !entries.every(entry => entry.isFile() && [name, checksumName].includes(entry.name))) {
    throw new Error(`Installer artifact must contain only ${name} and ${checksumName}`)
  }
  const digest = await hashFile(join(directory, name))
  if (digest !== expected || await readFile(join(directory, checksumName), 'utf8') !== checksumLine(name, digest)) {
    throw new Error('Installer or checksum differs from the build')
  }
}

function positiveId(value) {
  if (!/^[1-9]\d*$/.test(value) || !Number.isSafeInteger(Number(value))) throw new Error('Invalid Actions ID')
  return Number(value)
}

export function installerReceipt(env, version) {
  const name = installerName(env.RUNNER_OS, version)
  const artifactDigest = (env.ARTIFACT_DIGEST || '').replace(/^sha256:/, '')
  if (!gitSha.test(env.GITHUB_SHA) || !hex256.test(artifactDigest) || !hex256.test(env.INSTALLER_SHA256)) {
    throw new Error('Missing or invalid commit, artifact or installer digest')
  }
  return {
    schema_version: 1, platform: env.RUNNER_OS, version, run_id: positiveId(env.GITHUB_RUN_ID),
    run_attempt: positiveId(env.GITHUB_RUN_ATTEMPT), head_sha: env.GITHUB_SHA,
    artifact_id: positiveId(env.ARTIFACT_ID), artifact_digest: artifactDigest,
    installer_name: name, installer_sha256: env.INSTALLER_SHA256,
  }
}

// Rerunning only failed jobs can retain a successful desktop job from an earlier attempt.
export async function selectReceipt(directory, env, version) {
  const entries = await readdir(directory, { withFileTypes: true })
  const prefix = `desktop-receipt-${env.RUNNER_OS}-`
  const attempts = entries.map(entry => {
    if (!entry.isDirectory() || !entry.name.startsWith(prefix)) throw new Error('Unexpected receipt artifact')
    return positiveId(entry.name.slice(prefix.length))
  })
  if (!attempts.length) throw new Error('Missing desktop receipt')
  const attempt = Math.max(...attempts)
  if (attempt > positiveId(env.GITHUB_RUN_ATTEMPT)) throw new Error('Unexpected receipt attempt')
  const receipt = JSON.parse(await readFile(join(directory, prefix + attempt, 'receipt.json'), 'utf8'))
  if (receipt.schema_version !== 1 || receipt.platform !== env.RUNNER_OS || receipt.version !== version ||
      receipt.run_id !== positiveId(env.GITHUB_RUN_ID) || receipt.run_attempt !== attempt ||
      receipt.head_sha !== env.GITHUB_SHA || receipt.installer_name !== installerName(env.RUNNER_OS, version) ||
      !Number.isSafeInteger(receipt.artifact_id) || receipt.artifact_id <= 0 ||
      !hex256.test(receipt.artifact_digest) || !hex256.test(receipt.installer_sha256)) {
    throw new Error('Desktop receipt does not match the checked-out build')
  }
  return receipt
}

if (import.meta.main) {
  const env = process.env
  const version = manifest.version
  const platform = { linux: 'Linux', win32: 'Windows' }[process.platform]
  const name = installerName(platform, version)
  const [command = 'build', directory] = process.argv.slice(2)
  switch (command) {
    case 'build': {
      const digest = await buildChecksum(dist, name)
      if (env.GITHUB_OUTPUT) await appendFile(env.GITHUB_OUTPUT, `installer_name=${name}\ninstaller_sha256=${digest}\n`)
      break
    }
    case 'verify':
      await verifyInstaller(directory, name, env.INSTALLER_SHA256)
      break
    case 'receipt': {
      const receipt = installerReceipt(env, version)
      await verifyInstaller(directory, receipt.installer_name, receipt.installer_sha256)
      await writeFile(join(env.RUNNER_TEMP, 'receipt.json'), JSON.stringify(receipt, null, 2) + '\n', { flag: 'wx' })
      break
    }
    case 'select': {
      const receipt = await selectReceipt(directory, env, version)
      await appendFile(env.GITHUB_OUTPUT, `artifact_id=${receipt.artifact_id}\ninstaller_sha256=${receipt.installer_sha256}\n`)
      break
    }
    default:
      throw new Error(`Unknown installer checksum command: ${command}`)
  }
}
