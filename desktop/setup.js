const office = window.officeDesktop
const get = id => document.getElementById(id)
const status = get('status')
const error = get('error')
const address = get('address')
const remotes = get('remotes')
let busy = false

async function call(method, value) {
  const result = await office[method](value)
  if (!result.ok) throw new Error(result.message)
  return result.value
}

function toolRow(text, link) {
  const row = document.createElement('li')
  const name = document.createElement('span')
  name.textContent = text
  row.append(name)
  if (link) {
    const button = document.createElement('button')
    button.type = 'button'
    button.textContent = '공식 안내'
    button.addEventListener('click', () => call('docs', link).catch(showError))
    row.append(button)
  }
  return row
}

function showError(exception) {
  error.textContent = exception.message
}

async function updateTools() {
  const list = get('tools')
  list.replaceChildren(toolRow('작업실의 도구 상태를 확인하는 중…'))
  try {
    const tools = await call('tools')
    if (!tools) return
    const gitStatus = tools.git === null ? '연결한 작업실에서 확인 필요' : tools.git ? '설치됨' : '설치 필요'
    list.replaceChildren(toolRow(`Git · ${gitStatus}`, tools.git ? null : 'git'))
    for (const engine of tools.engines) {
      let state = '인증 확인 필요'
      if (!engine.installed) state = '설치 필요'
      else if (engine.probe_status === 'pending') state = '확인 중'
      else if (engine.auth_status === 'not_authenticated') state = '로그인 필요'
      else if (!engine.headless_support) state = '호환 버전 확인 필요'
      else if (engine.auth_status === 'authenticated') state = '인증 확인됨'
      list.append(toolRow(`${engine.label} · ${state}`, state === '인증 확인됨' ? null : engine.engine))
    }
  } catch (exception) {
    list.replaceChildren(toolRow(exception.message))
  }
}

async function render(value, initial = false) {
  if (initial) {
    address.value = value.address
    remotes.value = value.remotes.map(row => `${row.alias} ${row.port} ${row.journalId}`).join('\n')
  }
  if (value.startupError) error.textContent = value.startupError
  get('connected').hidden = !value.selected
  if (!value.selected) return
  const local = value.selected.mode === 'local'
  get('connection').textContent = local ? '로컬 작업실 실행 중' : '기존 작업실 연결됨'
  get('stop').hidden = !local
  await updateTools()
}

async function perform(message, action, field) {
  if (busy) return
  busy = true
  error.textContent = ''
  status.textContent = message
  const buttons = document.querySelectorAll('main button')
  buttons.forEach(element => { element.disabled = true })
  try {
    await action()
    field?.removeAttribute('aria-invalid')
  } catch (exception) {
    field?.setAttribute('aria-invalid', 'true')
    showError(exception)
  } finally {
    status.textContent = ''
    buttons.forEach(element => { element.disabled = false })
    busy = false
  }
}

get('connect').addEventListener('submit', event => {
  event.preventDefault()
  perform('작업실을 확인하는 중…', async () => render(await call('connect', address.value.trim())), address)
})
address.addEventListener('input', () => address.removeAttribute('aria-invalid'))
get('local').addEventListener('submit', event => {
  event.preventDefault()
  perform('로컬 작업실을 시작하는 중…', async () => {
    const entries = remotes.value.trim().split('\n').filter(row => row.trim()).map(row => {
      const parts = row.trim().split(/\s+/)
      if (parts.length !== 3) throw new Error('원격 작업실은 한 줄에 별칭, 포트, 저널 ID를 입력하세요.')
      const [alias, port, journalId] = parts
      return { alias, port: Number(port), journalId }
    })
    await render(await call('start', entries))
  })
})
get('open').addEventListener('click', () => perform('사무실을 여는 중…', () => call('open')))
get('stop').addEventListener('click', () => perform('로컬 작업실 종료를 확인하는 중…', async () => {
  if (await call('stop')) await render(await call('state'))
}))
get('refresh').addEventListener('click', () => perform('도구 상태를 확인하는 중…', updateTools))
office.onRefresh(() => call('state').then(render).catch(showError))
call('state').then(value => render(value, true)).catch(showError)
