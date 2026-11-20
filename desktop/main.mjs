import { mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs'
import { isAbsolute, join, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { app, BrowserWindow, dialog, ipcMain, Menu, session, shell } from 'electron'
import { checkStudio, LocalStudio, readTools, studioPage, validateRemotes } from './studio.mjs'

const setupFile = fileURLToPath(new URL('./setup.html', import.meta.url))
const setupURL = pathToFileURL(setupFile).href
const docs = new Map([
  ['git', 'https://git-scm.com/install/'],
  ['codex', 'https://developers.openai.com/codex/cli/'],
  ['claude', 'https://code.claude.com/docs/en/setup'],
  ['gemini', 'https://geminicli.com/docs/'],
  ['cline', 'https://docs.cline.bot/'],
  ['antigravity', 'https://antigravity.google/'],
])
const windowDefaults = { width: 1320, height: 940, minWidth: 660, minHeight: 560, backgroundColor: '#151823' }
const isolated = { sandbox: true, contextIsolation: true, nodeIntegration: false, webSecurity: true }

app.setName('AI Agent Office')
const profileOverride = app.commandLine.getSwitchValue('user-data-dir')
if (profileOverride && isAbsolute(profileOverride)) app.setPath('userData', profileOverride)

let setupWindow, officeWindow, studio, profilePath, profile, selected, startupError = ''
let confirmInProgress = false

function bundledPython() {
  const win = process.platform === 'win32'
  if (app.isPackaged) return join(process.resourcesPath, 'runtime', win ? 'python.exe' : 'bin/python3')
  if (process.env.OFFICE_DESKTOP_PYTHON && isAbsolute(process.env.OFFICE_DESKTOP_PYTHON)) {
    return process.env.OFFICE_DESKTOP_PYTHON
  }
  return resolve(app.getAppPath(), '..', '.venv', win ? 'Scripts/python.exe' : 'bin/python')
}

function readProfile() {
  try {
    const saved = JSON.parse(readFileSync(profilePath, 'utf8'))
    if (!saved || typeof saved !== 'object') throw new Error()
    return { recent: {}, ...saved, remotes: validateRemotes(saved.remotes ?? []) }
  } catch {
    return { remotes: [], recent: {} }
  }
}

function saveProfile() {
  const temp = profilePath + '.tmp'
  writeFileSync(temp, JSON.stringify(profile, null, 2), { encoding: 'utf8', mode: 0o600 })
  renameSync(temp, profilePath)
}

function choose(next, mode) {
  selected = { ...next, mode }
  profile.connection = { mode, origin: next.origin, journalId: next.journalId }
  if (mode === 'attached') profile.address = next.origin
  saveProfile()
  startupError = ''
  return sessionSummary()
}

function sessionSummary() {
  return {
    address: profile.address || 'http://127.0.0.1:8766',
    remotes: profile.remotes,
    selected,
    startupError,
  }
}

async function startLocal(remotes) {
  const requested = validateRemotes(remotes)
  const current = await studio.running()
  if (current && JSON.stringify(profile.remotes) !== JSON.stringify(requested)) {
    throw new Error('원격 작업실 설정을 적용하려면 실행 중인 로컬 작업실을 먼저 종료하세요.')
  }
  const runtime = await studio.start(requested)
  profile.remotes = requested
  return choose(runtime, 'local')
}

function secureContents(window, allowed) {
  const contents = window.webContents
  contents.setWindowOpenHandler(() => ({ action: 'deny' }))
  contents.on('will-attach-webview', event => event.preventDefault())
  contents.on('will-navigate', (event, url) => { if (!allowed(url)) event.preventDefault() })
  contents.on('will-redirect', (event, url) => { if (!allowed(url)) event.preventDefault() })
}

function showSetup() {
  if (setupWindow) {
    setupWindow.show()
    setupWindow.focus()
    return
  }
  setupWindow = new BrowserWindow({
    ...windowDefaults, width: 850, height: 780,
    webPreferences: { ...isolated, preload: fileURLToPath(new URL('./preload.cjs', import.meta.url)) },
  })
  secureContents(setupWindow, url => url === setupURL)
  setupWindow.on('closed', () => { setupWindow = null })
  setupWindow.loadFile(setupFile)
}

async function showOffice() {
  if (!selected) throw new Error('먼저 작업실에 연결하세요.')
  const connection = selected
  await checkStudio(connection.origin, connection.journalId)
  if (selected !== connection) return

  if (officeWindow && officeWindow.officeOrigin === connection.origin) {
    officeWindow.show()
    officeWindow.focus()
    setupWindow?.close()
    return
  }
  if (officeWindow) officeWindow.close()
  const isolatedSession = session.fromPartition('persist:agent-office-studio')
  isolatedSession.setPermissionRequestHandler((_contents, _permission, callback) => callback(false))
  isolatedSession.setPermissionCheckHandler(() => false)
  isolatedSession.webRequest.onBeforeRequest((details, callback) => {
    callback({ cancel: new URL(details.url).origin !== connection.origin })
  })
  const window = new BrowserWindow({
    ...windowDefaults, show: false, webPreferences: { ...isolated, session: isolatedSession },
  })
  officeWindow = window
  window.officeOrigin = connection.origin
  const key = connection.journalId || connection.origin
  const allowed = url => studioPage(url, connection.origin) !== null
  const remember = (_event, url) => {
    const page = studioPage(url, connection.origin)
    if (page && profile.recent[key] !== page) {
      profile.recent[key] = page
      saveProfile()
    }
  }
  secureContents(window, allowed)
  window.webContents.on('did-navigate', remember)
  window.webContents.on('did-navigate-in-page', (event, url, isMainFrame) => {
    if (isMainFrame) remember(event, url)
  })
  window.on('closed', () => { if (officeWindow === window) officeWindow = null })
  const page = studioPage(profile.recent[key] || '/', connection.origin) || '/'
  try {
    await window.loadURL(connection.origin + page)
  } catch {
    showSetup()
    if (!window.isDestroyed()) window.close()
    throw new Error('작업실 화면을 열 수 없습니다. Studio 연결을 다시 확인하세요.')
  }
  if (!window.isDestroyed()) window.show()
  setupWindow?.close()
}

async function stopLocal() {
  if (confirmInProgress) return false
  confirmInProgress = true
  try {
    const runtime = await studio.running()
    if (!runtime) return false
    const options = {
      type: 'warning', buttons: ['취소', '로컬 작업실 종료'], defaultId: 0, cancelId: 0,
      message: '로컬 작업실을 종료할까요?',
      detail: '창을 닫는 것과 달리 이 작업실의 실행이 중단될 수 있습니다. 기록은 저널에 남습니다.',
    }
    const parent = BrowserWindow.getFocusedWindow()
    const answer = parent ? await dialog.showMessageBox(parent, options) : await dialog.showMessageBox(options)
    if (answer.response !== 1) return false
    if (!await studio.stop(runtime.bootId)) return false
    if (selected?.mode === 'local' && selected.bootId === runtime.bootId) {
      selected = null
      showSetup()
      officeWindow?.close()
      setupWindow?.webContents.send('desktop:refresh')
    }
    return true
  } finally {
    confirmInProgress = false
  }
}

function registerActions() {
  const actions = {
    state: sessionSummary,
    connect: async address => {
      if (typeof address !== 'string' || address.length > 192) throw new Error('Studio 주소를 확인하세요.')
      const next = await checkStudio(address)
      return choose(next, 'attached')
    },
    start: startLocal,
    open: showOffice,
    stop: stopLocal,
    tools: () => selected ? readTools(selected.origin) : null,
    docs: key => {
      if (docs.has(key)) return shell.openExternal(docs.get(key))
    },
  }
  for (const [name, action] of Object.entries(actions)) {
    ipcMain.handle(`desktop:${name}`, async (event, value) => {
      const contents = setupWindow?.webContents
      if (event.sender !== contents || event.senderFrame !== contents?.mainFrame || event.senderFrame.url !== setupURL) {
        return { ok: false, message: '연결 설정 화면에서 실행하세요.' }
      }
      try {
        return { ok: true, value: await action(value) }
      } catch (error) {
        return { ok: false, message: error.message }
      }
    })
  }
}

async function initialize() {
  const directory = app.getPath('userData')
  mkdirSync(directory, { recursive: true, mode: 0o700 })
  profilePath = join(directory, 'connection.json')
  profile = readProfile()
  studio = new LocalStudio(join(directory, 'local-studio'), bundledPython())
  session.defaultSession.setPermissionRequestHandler((_contents, _permission, callback) => callback(false))
  session.defaultSession.setPermissionCheckHandler(() => false)
  registerActions()
  const act = action => () => action().catch(error => dialog.showErrorBox('AI Agent Office', error.message))
  Menu.setApplicationMenu(Menu.buildFromTemplate([
    { label: '작업실', submenu: [
      { label: '연결 설정 및 도구 상태', click: showSetup },
      { label: '작업실 열기', click: act(showOffice) },
      { label: '로컬 작업실 종료…', click: act(stopLocal) },
      { type: 'separator' },
      { label: '앱 종료', role: 'quit' },
    ] },
    { label: '편집', role: 'editMenu' },
    { label: '보기', submenu: [{ role: 'reload' }, { role: 'resetZoom' }, { role: 'zoomIn' }, { role: 'zoomOut' }] },
  ]))

  const saved = profile.connection
  try {
    if (saved?.mode === 'attached') choose(await checkStudio(saved.origin, saved.journalId), 'attached')
    if (saved?.mode === 'local') await startLocal(profile.remotes)
    if (selected) {
      await showOffice()
      return
    }
  } catch (error) {
    startupError = error.message
  }
  showSetup()
}

if (!app.requestSingleInstanceLock()) {
  app.quit()
} else {
  app.on('second-instance', () => { (officeWindow || setupWindow)?.focus() })
  app.on('window-all-closed', () => app.quit())
  app.whenReady().then(initialize).catch(error => {
    dialog.showErrorBox('AI Agent Office', error.message)
    app.quit()
  })
}
