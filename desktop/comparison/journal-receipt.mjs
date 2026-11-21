import {createHash} from 'node:crypto'
import {createReadStream} from 'node:fs'

// A stopped fixture Studio has checkpointed its SQLite files. Stream the two
// files so the uninstall check can detect a change without opening that journal.
export async function sha256(file) {
  const hash = createHash('sha256')
  for await (const chunk of createReadStream(file)) hash.update(chunk)
  return hash.digest('hex')
}
