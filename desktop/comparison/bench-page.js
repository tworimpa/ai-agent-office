// Appended only to the disposable benchmark Studio's workspace-office.js.
async function observeOffice(config) {
  const initialRun = new URL(location.href).searchParams.get('run')
  const frame = () => new Promise(resolve => requestAnimationFrame(resolve))
  const visible = selector => {
    const element = document.querySelector(selector)
    return !document.hidden && element?.getClientRects().length && !element.disabled &&
      getComputedStyle(element).visibility === 'visible' && element
  }
  async function painted(read, description) {
    const started = performance.now()
    while (performance.now() - started < 30000) {
      if (read()) {
        await frame()
        await frame()
        if (read()) return read()
      }
      await frame()
    }
    throw Error(description)
  }
  async function emit(event, values = {}) {
    const response = await fetch('/api/benchmark-event', {method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-Agent-Office': 'studio'},
      body: JSON.stringify({nonce: config.nonce, event, initialRun, ...values})})
    if (!response.ok) throw Error('Benchmark receipt ' + response.status)
  }
  const view = () => ({userAgent: navigator.userAgent, width: innerWidth, height: innerHeight, dpr: devicePixelRatio})
  try {
    await painted(() => !document.hidden, 'visible native window')
    const resizing = performance.now()
    await emit('viewport', {renderer: view()})
    await painted(() => innerWidth === 1280 && innerHeight === 900, '1280 × 900 CSS viewport')
    const sizingMs = performance.now() - resizing
    if (!config.active) {
      const button = await painted(() => visible('#empty-register'), 'empty register control')
      await emit('ready', {renderer: view(), sizingMs})
      const started = performance.now()
      button.click()
      await painted(() => visible('#register-dialog[open] #workspace-paths'), 'workspace registration')
      const registerMs = performance.now() - started
      document.querySelector('#register-dialog form[method=dialog] button').click()
      await emit('done', {registerMs})
      return
    }
    const question = '#brief-questions [data-action="answer"][data-run="' + config.active + '"]'
    const candidate = '#brief-review [data-action="candidate"][data-run="' + config.candidate + '"]'
    await painted(() => visible(question) && visible(candidate) &&
      document.querySelectorAll('#brief-work article').length === 10, 'ten goals, question and candidate')
    if (config.reopened && initialRun !== config.active) throw Error('Reopened a different run')
    await emit('ready', {renderer: view(), sizingMs})
    const started = performance.now()
    document.querySelector(candidate).click()
    const diff = await painted(() => visible('[data-goal-attempt="1"][open] [data-goal-diff]'), 'candidate details')
    diff.click()
    await painted(() => visible('[data-goal-attempt="1"][open] [data-goal-diff] + pre')?.textContent?.includes('+RESTORE = True'),
      'candidate patch')
    const candidateMs = performance.now() - started
    const answering = performance.now()
    document.querySelector(question).click()
    await painted(() => document.activeElement === visible('#thread-answer-' + config.active + '-' + config.question),
      'selected question editor')
    const selected = new URL(location.href).searchParams.get('run')
    if (selected !== config.active) throw Error('Selected a different question owner')
    await emit('done', {candidateMs, questionMs: performance.now() - answering, selected})
  } catch (error) {
    await emit('error', {message: error.message, renderer: view(), run: new URL(location.href).searchParams.get('run'),
      work: document.querySelectorAll('#brief-work article').length, question: document.querySelectorAll('[data-action="answer"]').length,
      candidate: document.querySelectorAll('[data-action="candidate"]').length, status: document.querySelector('#brief-note')?.textContent,
      goalNote: document.querySelector('#goal-note')?.textContent, connection: document.querySelector('#connection')?.textContent})
  }
}
