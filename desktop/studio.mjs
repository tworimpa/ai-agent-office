import { spawn } from 'node:child_process'
import { closeSync, openSync } from 'node:fs'
import { access, mkdir, readFile, writeFile } from 'node:fs/promises'
import { homedir } from 'node:os'
import { delimiter, join } from 'node:path'
import { setTimeout as delay } from 'node:timers/promises'

const ID = /^[a-f0-9]{32}$/
const STUDIO_PAGES = new Set(['/', '/studio', '/office', '/improvements', '/versions'])
const MAX_RESPONSE_BYTES = 256 * 1024

export function localStudioOrigin(address) {
  const url = new URL(address)
  if (url.protocol !== 'http:' || !['127.0.0.1', 'localhost'].includes(url.hostname) ||
      url.username || url.password || url.pathname !== '/' || url.hash || url.search) {
    throw new Error('이 컴퓨터의 Studio 주소를 입력하세요. 예: http://127.0.0.1:8766')
  }
  url.hostname = '127.0.0.1'
  return url.origin
}

export function studioPage(address, origin) {
  try {
    const url = new URL(address, origin)
    return url.origin === origin && STUDIO_PAGES.has(url.pathname) && !url.username && !url.password
      ? url.pathname + url.search
      : null
  } catch {
    return null
  }
}

async function readJson(response) {
  if (!response.headers.get('content-type')?.startsWith('application/json') || !response.body) {
    throw new Error('Studio의 JSON 응답을 확인할 수 없습니다.')
  }
  const chunks = []
  let size = 0
  for await (const chunk of response.body) {
    size += chunk.length
    if (size > MAX_RESPONSE_BYTES) throw new Error('Studio 응답이 너무 큽니다.')
    chunks.push(chunk)
  }
  return JSON.parse(Buffer.concat(chunks).toString('utf8'))
}

async function api(origin, path, { optional = false, request = fetch } = {}) {
  const response = await request(origin + path, { signal: AbortSignal.timeout(2500), redirect: 'error' })
  if (optional && response.status === 404) {
    await response.body?.cancel()
    return null
  }
  if (!response.ok) {
    await response.body?.cancel()
    throw new Error(`Studio 연결을 확인하세요 (HTTP ${response.status}).`)
  }
  return readJson(response)
}

export async function checkStudio(address, expectedId = null, request = fetch) {
  let origin
  try {
    origin = localStudioOrigin(address)
  } catch {
    throw new Error('이 컴퓨터의 Studio 주소를 입력하세요. 예: http://127.0.0.1:8766')
  }
  let hosts, identity
  try {
    [hosts, identity] = await Promise.all([
      api(origin, '/api/hosts', { request }),
      api(origin, '/api/studio-identity', { optional: true, request }),
    ])
    if (!identity) {
      const managed = await api(origin, '/api/runtime-health', { optional: true, request })
      identity = managed && { journal_id: managed.journal_id }
    }
  } catch {
    throw new Error('Studio에 연결할 수 없습니다. 주소와 작업실이 실행 중인지 확인하세요.')
  }
  if (!Array.isArray(hosts?.hosts) || !hosts.hosts.some(host => host?.id === 'local' && typeof host.name === 'string') ||
      (identity && !ID.test(identity.journal_id))) {
    throw new Error('이 주소에서 AI Agent Office 작업실을 확인할 수 없습니다.')
  }
  if (expectedId && identity?.journal_id !== expectedId) {
    throw new Error('저장한 저널과 다른 작업실입니다. 등록할 주소를 다시 확인하세요.')
  }
  return { origin, journalId: identity?.journal_id ?? null }
}

export async function readTools(origin) {
  const [tools, engines] = await Promise.all([
    api(origin, '/api/studio-tools', { optional: true }),
    api(origin, '/api/engines'),
  ])
  if (!Array.isArray(engines?.engines)) throw new Error('작업실의 에이전트 상태를 읽을 수 없습니다.')
  return { git: tools?.git?.installed ?? null, engines: engines.engines }
}

export function validateRemotes(remotes) {
  if (!Array.isArray(remotes) || remotes.length > 20) throw new Error('원격 작업실 목록을 확인하세요.')
  const aliases = new Set()
  const journals = new Set()
  return remotes.map(row => {
    const { alias, port, journalId } = row
    if (typeof alias !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(alias) ||
        !Number.isInteger(port) || port < 1 || port > 65535 ||
        typeof journalId !== 'string' || !ID.test(journalId) ||
        aliases.has(alias.toLowerCase()) || journals.has(journalId)) {
      throw new Error('SSH 별칭, 포트와 저널 ID를 확인하세요. 같은 별칭이나 저널은 한 번만 등록할 수 있습니다.')
    }
    aliases.add(alias.toLowerCase())
    journals.add(journalId)
    return { alias, port, journalId }
  })
}

async function receipt(directory) {
  try {
    return JSON.parse(await readFile(join(directory, 'runtime.json'), 'utf8'))
  } catch (error) {
    if (error.code === 'ENOENT') return null
    throw error
  }
}

async function liveRuntime(directory) {
  const value = await receipt(directory).catch(() => null)
  if (!value || !ID.test(value.boot_id) || !ID.test(value.journal_id) ||
      !Number.isInteger(value.port) || value.port < 1 || value.port > 65535) return null
  const origin = `http://127.0.0.1:${value.port}`
  const live = await api(origin, '/api/desktop-runtime').catch(() => null)
  if (!live || live.boot_id !== value.boot_id || live.journal_id !== value.journal_id || live.port !== value.port) return null
  try {
    const remotes = live.remotes === undefined ? null : validateRemotes(live.remotes)
    return { origin, journalId: live.journal_id, bootId: live.boot_id, remotes }
  } catch {
    return null
  }
}

export class LocalStudio {
  constructor(directory, python) {
    this.directory = directory
    this.python = python
    this.starting = null
    this.stopping = null
  }

  running() {
    return liveRuntime(this.directory)
  }

  start(remotes = []) {
    this.starting ??= this.launch(validateRemotes(remotes)).finally(() => { this.starting = null })
    return this.starting
  }

  async launch(remotes) {
    if (this.stopping) await this.stopping
    await mkdir(this.directory, { recursive: true, mode: 0o700 })
    const active = await this.running()
    if (active) return active
    const args = ['-I', '-B', '-m', 'agent_office.desktop_runtime', '--data-dir', this.directory]
    for (const { alias, port, journalId } of remotes) {
      args.push('--remote', alias, String(port), journalId)
    }
    const env = { ...process.env, PYTHONUTF8: '1', PYTHONSAFEPATH: '1', PYTHONNOUSERSITE: '1' }
    for (const key of Object.keys(env)) {
      if (['PYTHONHOME', 'PYTHONPATH'].includes(key.toUpperCase())) delete env[key]
    }
    if (process.platform === 'linux') {
      // Desktop launches need the user CLI directory even when shell startup files are not read.
      const paths = (env.PATH || '').split(delimiter).filter(Boolean)
      const local = paths.filter(path => !/^\/mnt\/[a-z]\//i.test(path))
      const shared = paths.filter(path => !local.includes(path))
      const user = [join(homedir(), '.local/bin'), join(homedir(), '.npm-global/bin')]
      env.PATH = [...new Set([...local, ...user, ...shared])].join(delimiter)
    }
    const log = openSync(join(this.directory, 'startup.log'), 'w', 0o600)
    let child
    try {
      child = spawn(this.python, args, {
        cwd: this.directory, env, windowsHide: true, detached: true, stdio: ['ignore', log, log],
      })
    } finally {
      closeSync(log)
    }
    let failure
    child.once('error', error => { failure = error })
    child.unref()
    for (const deadline = Date.now() + 20000; Date.now() < deadline;) {
      const current = await this.running()
      if (current) return current
      if (failure?.code === 'ENOENT') throw new Error('포함된 실행기가 없습니다. 설치 파일로 앱을 다시 설치하세요.')
      if (failure || child.exitCode !== null || child.signalCode !== null) {
        throw new Error('로컬 작업실을 시작하지 못했습니다. 연결 설정이나 데이터 폴더의 startup.log를 확인하세요.')
      }
      await delay(175)
    }
    throw new Error('로컬 작업실 시작에 시간이 걸립니다. 잠시 후 다시 연결하거나 startup.log를 확인하세요.')
  }

  stop(bootId) {
    this.stopping ??= this.shutdown(bootId).finally(() => { this.stopping = null })
    return this.stopping
  }

  async shutdown(bootId) {
    const active = await this.running()
    if (!active || active.bootId !== bootId) return false
    const stop = join(this.directory, `stop-${active.bootId}`)
    try {
      await writeFile(stop, '', { flag: 'wx', mode: 0o600 })
    } catch (error) {
      if (error.code !== 'EEXIST') throw error
    }
    for (const deadline = Date.now() + 15000; Date.now() < deadline;) {
      const current = await receipt(this.directory)
      if (!current || current.boot_id !== bootId) {
        try {
          await access(stop)
        } catch (error) {
          if (error.code === 'ENOENT') return true
          throw error
        }
      }
      await delay(150)
    }
    throw new Error('로컬 작업실이 종료 중입니다. 잠시 후 상태를 다시 확인하세요.')
  }
}
