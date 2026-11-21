import assert from 'node:assert/strict'
import {spawn, spawnSync} from 'node:child_process'
import {writeFile} from 'node:fs/promises'
import {createServer} from 'node:net'
import {setTimeout as delay} from 'node:timers/promises'
import {_electron as electron} from 'playwright'
import {closeDesktop} from '../verify-lifecycle.mjs'

export async function until(read, label, milliseconds = 30000) {
  const deadline = Date.now() + milliseconds
  let error
  while (Date.now() < deadline) {
    try {
      const value = await read()
      if (value) return value
    } catch (cause) { error = cause }
    await delay(150)
  }
  throw new Error('Timed out waiting for ' + label, {cause: error})
}

async function freePort() {
  const listener = createServer()
  await new Promise((resolve, reject) => listener.on('error', reject).listen(0, '127.0.0.1', resolve))
  const {port} = listener.address()
  await new Promise(resolve => listener.close(resolve))
  return port
}

async function electronDriver(executablePath, profile) {
  const app = await electron.launch({executablePath, args:['--user-data-dir=' + profile], chromiumSandbox:true})
  let page = await app.firstWindow()
  return {
    async selectWindow(match) {
      page = await until(() => app.windows().find(window => match(window.url())), 'Electron Studio window')
    },
    run: (fn, ...args) => page.evaluate('(' + fn.toString() + ')(...' + JSON.stringify(args) + ')'),
    async fill(selector, value) { await page.locator(selector).fill(value) },
    key: value => page.keyboard.press(value),
    size: width => page.setViewportSize({width,height:840}),
    screenshot: path => page.screenshot({path}),
    close: () => closeDesktop(app.process(), () => app.close())
  }
}

async function rustDriver(executable, profile) {
  const port = await freePort()
  let native = await freePort()
  while (native === port) native = await freePort()
  const args = ['--port', String(port), '--native-port', String(native)]
  if (process.env.TAURI_NATIVE_DRIVER) args.push('--native-driver', process.env.TAURI_NATIVE_DRIVER)
  const windows = process.platform === 'win32'
  const child = spawn(process.env.TAURI_DRIVER || 'tauri-driver', args, {
    stdio:['ignore','inherit','inherit'],windowsHide:true,detached:!windows
  })
  let launchError
  child.on('error', error => { launchError = error })
  async function stopDriver() {
    if (!child.pid || child.exitCode !== null || child.signalCode !== null) return
    // The native driver is a child of the proxy; both belong to this temporary session.
    if (windows) {
      const stopped = spawnSync('taskkill',['/PID',String(child.pid),'/T','/F'],{windowsHide:true})
      if (stopped.error) throw stopped.error
    } else {
      try { process.kill(-child.pid,'SIGTERM') } catch (error) { if (error.code !== 'ESRCH') throw error }
    }
    await until(() => child.exitCode !== null || child.signalCode !== null,'tauri-driver shutdown',7000)
  }
  async function request(method, path, body) {
    if (launchError) throw launchError
    if (child.exitCode !== null) throw Error('tauri-driver exited with ' + child.exitCode)
    const response = await fetch('http://127.0.0.1:' + port + path, {
      method, headers:{'Content-Type':'application/json'}, body:body && JSON.stringify(body),signal:AbortSignal.timeout(45000)
    })
    const data = await response.json()
    if (!response.ok || data.value?.error) throw Error(method + ' ' + path + ': ' + JSON.stringify(data.value))
    return data.value
  }
  let session
  const command = (method, path, body) => request(method, '/session/' + session + path, body)
  const run = (fn, ...args) => command('POST','/execute/sync',{script:'return (' + fn.toString() + ')(...arguments)',args})
  const keys = {Enter:'\uE007',Tab:'\uE004',Escape:'\uE00C',Space:' '}
  try {
    await until(() => request('GET','/status'), 'tauri-driver')
    const result = await request('POST','/session',{capabilities:{alwaysMatch:{
      browserName:'wry','tauri:options':{application:executable,args:['--profile-dir',profile]}
    }}})
    session = result.sessionId
    assert.ok(session)
    return {
      run,
      async selectWindow(match) {
        await until(async () => {
          for (const handle of await command('GET','/window/handles')) {
            await command('POST','/window',{handle})
            if (match(await command('GET','/url'))) return true
          }
        }, 'Rust Studio window')
      },
      async fill(selector, value) {
        const result = await command('POST','/element',{using:'css selector',value:selector})
        const base = '/element/' + result['element-6066-11e4-a52e-4f735466cecf']
        await command('POST',base + '/clear',{})
        await command('POST',base + '/value',{text:value,value:Array.from(value)})
      },
      async key(value) {
        assert.ok(Object.hasOwn(keys,value))
        const key = keys[value]
        await command('POST','/actions',{actions:[{type:'key',id:'keyboard',actions:[{type:'keyDown',value:key},{type:'keyUp',value:key}]}]})
      },
      size: width => command('POST','/window/rect',{width,height:840}),
      async screenshot(path) { await writeFile(path,Buffer.from(await command('GET','/screenshot'),'base64')) },
      async close() {
        try { await command('DELETE','') } finally { await stopDriver() }
      }
    }
  } catch (error) {
    await stopDriver()
    throw error
  }
}

export async function launch(kind, executable, profile) {
  const driver = kind === 'electron' ? await electronDriver(executable, profile) : await rustDriver(executable, profile)
  async function ready(selector) {
    await until(() => driver.run(s => {
      const el = document.querySelector(s)
      return Boolean(el && el.getClientRects().length && !el.disabled)
    },selector),selector)
  }
  return Object.assign(driver, {
    ready,
    async activate(selector) {
      await ready(selector)
      await driver.run(s => document.querySelector(s).focus(),selector)
      await driver.key('Enter')
    },
    async check(selector) {
      await ready(selector)
      if (await driver.run(s => !document.querySelector(s).checked,selector)) {
        await driver.run(s => document.querySelector(s).focus(),selector)
        await driver.key('Space')
      }
      assert.ok(await driver.run(s => document.querySelector(s).checked,selector),selector)
    }
  })
}
