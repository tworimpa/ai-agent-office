import {execFileSync} from 'node:child_process'
import {copyFile, mkdir, mkdtemp, writeFile} from 'node:fs/promises'
import {tmpdir} from 'node:os'
import {delimiter,dirname,join} from 'node:path'
import {LocalStudio} from '../studio.mjs'

export async function prepare(electronExe) {
  const root = await mkdtemp(join(tmpdir(),'office-handoff-'))
  const repo = join(root,'project')
  const bin = join(root,'bin')
  const windows = process.platform === 'win32'
  const python = process.env.OFFICE_VERIFY_PYTHON ||
    join(dirname(electronExe),'resources','runtime',windows?'python.exe':'bin/python3')
  await mkdir(join(repo,'tests'),{recursive:true})
  await mkdir(bin)
  await writeFile(join(repo,'briefing-fixture.txt'),'Disposable cross-desktop settings fixture\n')
  await writeFile(join(repo,'.gitignore'),'__pycache__/\n.pytest_cache/\n')
  await writeFile(join(repo,'settings.py'),'import json\n\ndef restore(path):\n    return "light"\n')
  await writeFile(join(repo,'tests','test_settings.py'),[
    'from settings import restore', '',
    'def test_reopen_saved_theme(tmp_path):','    path = tmp_path / "settings.json"',
    '    path.write_text(\'{"theme":"dark"}\', encoding="utf8")','    assert restore(path) == "dark"', '',
    'def test_missing_default(tmp_path):','    assert restore(tmp_path / "missing") == "light"',''
  ].join('\n'))
  for (const args of [['init','-q','-b','main'],['add','.'],['-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-qm','settings fixture']]) {
    execFileSync('git',args,{cwd:repo,windowsHide:true})
  }
  // goal_worker imports its sibling briefing_worker only inside a marked disposable project.
  const source = join(bin,'goal_worker.py')
  await copyFile(new URL('../fixtures/goal_worker.py',import.meta.url),source)
  await copyFile(new URL('handoff-worker.py',import.meta.url),join(bin,'briefing_worker.py'))
  const quote = value => "'" + value.replaceAll("'","'\"'\"'") + "'"
  for (const engine of ['codex','agy']) {
    const script = windows ? '@"' + python + '" "' + source + '" ' + engine + ' %*\r\n' :
      '#!/bin/sh\nexec ' + quote(python) + ' ' + quote(source) + ' ' + engine + ' "$@"\n'
    await writeFile(join(bin,engine + (windows ? '.cmd' : '')),script,{mode:0o700})
  }
  const pathKey = Object.keys(process.env).find(key => key.toUpperCase() === 'PATH')
  process.env[pathKey] = bin + delimiter + process.env[pathKey]
  const studio = new LocalStudio(join(root,'studio'),python)
  return {root,repo,studio,python}
}
