// Installed before Studio scripts. The server checks this optional precondition
// before reading or changing a journal, including requests already in flight.
(() => {
  if (location.protocol !== 'http:' || location.hostname !== '127.0.0.1' || window.top !== window) return
  const origin = location.origin
  const key = 'ai-agent-office.rust.journal'
  const fragment = new URLSearchParams(location.hash.slice(1))
  let journal
  try {
    journal = sessionStorage.getItem(key)
    if (fragment.has('office-desktop-journal')) {
      const confirmed = fragment.get('office-desktop-journal')
      if (journal !== confirmed) {
        // A different journal can reuse a port. Pending Studio form submissions
        // in this tab belong to its former journal.
        sessionStorage.clear()
      }
      journal = confirmed
      sessionStorage.setItem(key, confirmed)
      const url = new URL(location.href)
      url.hash = fragment.get('anchor') || ''
      history.replaceState(null, '', url)
    }
  } catch {
    journal = null
  }
  const fetch = window.fetch.bind(window)
  window.fetch = (input, init) => {
    const url = new URL(input instanceof Request ? input.url : input, location.href)
    if (url.origin !== origin || !url.pathname.startsWith('/api/')) return fetch(input, init)
    const headers = new Headers(init?.headers ?? (input instanceof Request ? input.headers : undefined))
    if (!/^[a-f0-9]{32}$/.test(journal)) return Promise.reject(new Error('등록된 저널 ID를 확인할 수 없습니다.'))
    headers.set('X-Agent-Office-Expected-Journal', journal)
    const options = { ...init, headers }
    const method = init?.method ?? (input instanceof Request ? input.method : 'GET')
    if (__WEBKIT_GTK__ && method.toUpperCase() === 'POST') {
      // WebKitGTK sends Origin: null for a same-origin POST under the Studio
      // page's no-referrer policy. Restrict the override to this same-origin API.
      options.referrerPolicy = 'same-origin'
    }
    return fetch(input, options)
  }
})()
