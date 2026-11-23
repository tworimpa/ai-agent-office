import { spawnSync } from 'node:child_process'
import { existsSync, lstatSync, mkdirSync, mkdtempSync, readdirSync, realpathSync, renameSync, rmSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const desktop = dirname(fileURLToPath(import.meta.url))
const project = resolve(desktop, '..')
const build = join(desktop, 'build')
const runtime = join(build, 'runtime')
const version = '3.13.15'
const uv = process.env.OFFICE_BUILD_UV || 'uv'
const windows = process.platform === 'win32'

function run(command, args) {
  const result = spawnSync(command, args, { cwd: project, stdio: 'inherit', windowsHide: true })
  if (result.error) throw result.error
  if (result.status !== 0) throw new Error(`${command} failed (exit ${result.status})`)
}

if (process.arch !== 'x64' || !['win32', 'linux'].includes(process.platform)) {
  throw new Error('Run the desktop build on Windows x64 or Linux x64.')
}
mkdirSync(build, { recursive: true })
if (realpathSync(build) !== build) throw new Error('Use a direct desktop build directory.')
const stage = mkdtempSync(join(build, 'runtime-stage-'))
try {
  run(uv, ['python', 'install', version, '--install-dir', stage, '--no-bin'])
  const installs = readdirSync(stage, { withFileTypes: true })
    .filter(entry => entry.isDirectory() && entry.name.startsWith(`cpython-${version}-`))
  if (installs.length !== 1) throw new Error('Expected one standalone CPython installation.')
  const source = join(stage, installs[0].name)
  const python = join(source, windows ? 'python.exe' : 'bin/python3')
  const site = join(source, windows ? 'Lib/site-packages' : 'lib/python3.13/site-packages')
  const install = ['pip', 'install', '--python', python, '--target', site, '--link-mode', 'copy']
  run(uv, [...install, '--requirement', join(desktop, 'runtime-requirements.txt')])
  run(uv, [...install, '--no-deps', project])
  run(python, ['-I', '-B', '-m', 'agent_office.desktop_runtime', '--help'])
  if (existsSync(runtime)) {
    if (!lstatSync(runtime).isDirectory() || realpathSync(runtime) !== runtime) {
      throw new Error('Refusing to replace a linked runtime directory.')
    }
    renameSync(runtime, join(stage, 'previous-runtime'))
  }
  try {
    renameSync(source, runtime)
  } catch (error) {
    const previous = join(stage, 'previous-runtime')
    if (existsSync(previous)) renameSync(previous, runtime)
    throw error
  }
} finally {
  if (realpathSync(stage) === stage && dirname(stage) === build) rmSync(stage, { recursive: true, force: true })
}
