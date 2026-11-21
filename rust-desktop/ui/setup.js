const $ = id => document.getElementById(id)
const invoke = (command, args) => window.__TAURI_INTERNALS__.invoke(command, args)
let initialized = false
let busy = false

async function refresh() {
  const state = await invoke('snapshot')
  if (!initialized) {
    if (state.saved) $('address').value = state.saved.origin
    initialized = true
  }
  $('retry').hidden = !state.saved && !state.unreadable
  $('connect').disabled = busy || state.unreadable
  $('retry').disabled = busy
  $('address').disabled = busy || state.unreadable
  $('confirm').disabled = busy
  $('preview').hidden = !state.candidate
  if (state.candidate) {
    const readOnly = state.candidate.access === 'readOnly'
    $('identity').textContent = state.candidate.origin + '\n저널 ID ' + state.candidate.journalId +
      (readOnly ? '\n이 Studio에서는 기록만 열람할 수 있습니다. 작업 제출과 응답에는 Studio 업데이트가 필요합니다.' : '')
    $('confirm').textContent = readOnly ? '읽기 전용으로 열기' : '이 저널로 연결'
  }
  $('status').textContent = state.message
}
async function action(name, args) {
  if (busy) return
  busy = true
  $('connect').disabled = $('retry').disabled = $('confirm').disabled = true
  try {
    await invoke(name, args)
  } catch (error) {
    $('status').textContent = String(error)
    return
  } finally {
    busy = false
  }
  await refresh()
}
$('setup').addEventListener('submit', event => {
  event.preventDefault()
  action('select', { address: $('address').value })
})
$('address').addEventListener('input', () => { $('preview').hidden = true })
$('retry').addEventListener('click', () => action('retry'))
$('confirm').addEventListener('click', () => action('confirm'))
window.addEventListener('office-state', refresh)
refresh()
