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

async function compose(send, value) {
  const partial = value.slice(0,Math.max(1,value.length - 1))
  await send('Input.imeSetComposition',{text:partial,selectionStart:partial.length,selectionEnd:partial.length})
  await send('Input.imeSetComposition',{text:value,selectionStart:value.length,selectionEnd:value.length})
  await send('Input.insertText',{text:value})
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
    async compose(value) {
      const session = await page.context().newCDPSession(page)
      try { await compose((method,params) => session.send(method,params),value) } finally { await session.detach() }
    },
    key: value => page.keyboard.press(value),
    size: (width,height=840) => page.setViewportSize({width,height}),
    reload: () => page.reload(),
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
  let application
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
    let capabilities = {
      browserName:'wry','tauri:options':{application:executable,args:['--profile-dir',profile]}
    }
    if (windows) {
      // Microsoft's WebView2 attach mode preserves the app's private data directory.
      const debugPort = await freePort()
      application = spawn(executable,['--profile-dir',profile],{stdio:'inherit',windowsHide:false,env:{
        ...process.env,WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS:'--remote-debugging-address=127.0.0.1 --remote-debugging-port=' + debugPort
      }})
      await until(async () => (await fetch('http://127.0.0.1:' + debugPort + '/json/version')).ok,'test WebView2 endpoint')
      capabilities = {browserName:'webview2','ms:edgeOptions':{debuggerAddress:'127.0.0.1:' + debugPort}}
    }
    const result = await request('POST','/session',{capabilities:{alwaysMatch:capabilities}})
    session = result.sessionId
    assert.ok(session)
    return {
      run,
      compose: windows ? value => compose((cmd,params) => command('POST','/ms/cdp/execute',{cmd,params}),value) : null,
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
        if (value) await command('POST',base + '/value',{text:value,value:Array.from(value)})
      },
      async key(value) {
        assert.ok(Object.hasOwn(keys,value))
        const key = keys[value]
        await command('POST','/actions',{actions:[{type:'key',id:'keyboard',actions:[{type:'keyDown',value:key},{type:'keyUp',value:key}]}]})
      },
      reload: () => command('POST','/refresh',{}),
      async size(width,height=840) {
        let outer = {width,height}
        for (let attempt=0;attempt<5;attempt++) {
          if (!windows) await command('POST','/window/rect',outer)
          else {
            // An attached WebView2 does not own the native top-level window. Account
            // for desktop DPI and non-client borders using the measured inner viewport.
            const resized = spawnSync('powershell.exe',['-NoProfile','-NonInteractive','-Command',[
              'Add-Type -TypeDefinition \'using System; using System.Runtime.InteropServices; public class Bounds {',
              '[DllImport("user32.dll")] public static extern bool MoveWindow(IntPtr h,int x,int y,int w,int t,bool repaint); }\';',
              '$w=(Get-Process -Id ' + application.pid + ').MainWindowHandle;',
              'if ($w -eq 0 -or ![Bounds]::MoveWindow($w, 16,16,' + outer.width + ',' + outer.height + ',$true)) { exit 1 }'
            ].join(' ')],{windowsHide:true})
            assert.equal(resized.status,0,'Failed to resize the test window')
          }
          await delay(80)
          const actual = await run(() => ({width:innerWidth,height:innerHeight}))
          if (actual.width === width && actual.height === height) return
          outer.width += width - actual.width
          outer.height += height - actual.height
        }
        throw Error('Unable to set the requested CSS viewport ' + width + 'x' + height)
      },
      async screenshot(path) { await writeFile(path,Buffer.from(await command('GET','/screenshot'),'base64')) },
      async close() {
        try { await command('DELETE','') } finally {
          await stopDriver()
          if (application) await closeDesktop(application,() => {
            const closed = spawnSync('powershell.exe',['-NoProfile','-NonInteractive','-Command',
              'if (!(Get-Process -Id ' + application.pid + ').CloseMainWindow()) { exit 1 }'],{windowsHide:true})
            assert.equal(closed.status,0,'Failed to close the test window')
          })
        }
      }
    }
  } catch (error) {
    await stopDriver()
    if (application?.exitCode === null) application.kill()
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
