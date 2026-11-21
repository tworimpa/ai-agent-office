// Installed before Studio scripts. The server checks this optional precondition
// before reading or changing a journal, including requests already in flight.
(() => {
  if (location.protocol !== 'http:' || location.hostname !== '127.0.0.1' || window.top !== window) return
  const origin = location.origin
  const key = 'ai-agent-office.rust.journal'
  const accessKey = 'ai-agent-office.rust.access'
  const fragment = new URLSearchParams(location.hash.slice(1))
  let journal, access
  try {
    journal = sessionStorage.getItem(key)
    access = sessionStorage.getItem(accessKey)
    if (fragment.has('office-desktop-journal')) {
      const confirmed = fragment.get('office-desktop-journal')
      const nextAccess = fragment.get('access')
      if (journal !== confirmed || access !== nextAccess) {
        // A different journal can reuse a port. Pending Studio form submissions
        // in this tab belong to its former journal.
        sessionStorage.clear()
      }
      journal = confirmed
      sessionStorage.setItem(key, confirmed)
      access = nextAccess
      sessionStorage.setItem(accessKey, access)
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
    if (!/^[a-f0-9]{32}$/.test(journal)) return Promise.reject(new Error('등록된 저널 ID를 확인할 수 없습니다.'))
    const request = input instanceof Request ? input : null
    const method = (init?.method ?? request?.method ?? 'GET').toUpperCase()
    if (access !== 'bound' && (access !== 'read-only' || !['GET', 'HEAD'].includes(method))) {
      return Promise.reject(new Error('이 작업실은 읽기 전용입니다. 작업 제출과 응답에는 Studio 업데이트가 필요합니다.'))
    }
    const headers = new Headers(init?.headers ?? request?.headers)
    headers.set('X-Agent-Office-Expected-Journal', journal)
    const options = { ...init, headers, redirect: 'error' }
    if (access === 'bound') {
      // Older Studios reject API query strings before dispatch. A server
      // replacing this port cannot silently ignore the journal precondition.
      url.searchParams.set('office_journal', journal)
      input = request ? new Request(url.href, request) : url.href
    }
    if (__WEBKIT_GTK__ && method === 'POST') {
      // WebKitGTK sends Origin: null for a same-origin POST under the Studio
      // page's no-referrer policy. Restrict the override to this same-origin API.
      options.referrerPolicy = 'same-origin'
    }
    return fetch(input, options)
  }
})()
