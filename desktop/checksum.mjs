import { createHash } from 'node:crypto'
import { createReadStream } from 'node:fs'
import { readdir, readFile, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

const dist = fileURLToPath(new URL('./dist/', import.meta.url))
const { version } = JSON.parse(await readFile(new URL('./package.json', import.meta.url), 'utf8'))
const suffix = process.platform === 'win32' ? ` ${version}.exe` : `_${version}_amd64.deb`
const installer = (await readdir(dist)).find(name => name.endsWith(suffix))
if (!installer) throw new Error(`Installer for version ${version} is missing.`)
const hash = createHash('sha256')
for await (const chunk of createReadStream(join(dist, installer))) hash.update(chunk)
await writeFile(join(dist, 'SHA256SUMS.txt'), `${hash.digest('hex')}  ${installer}\n`)
