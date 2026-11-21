// Wait for the process, even if Playwright returns before exit or retains a detached Studio's pipe.
export async function closeDesktop(child, requestClose) {
  let timer, onExit
  const exited = new Promise((resolve, reject) => {
    onExit = (code, signal) => code === 0 ? resolve() : reject(Error('Desktop exited with ' + (signal || code)))
    child.once('exit', onExit)
    if (child.exitCode !== null || child.signalCode !== null) onExit(child.exitCode, child.signalCode)
  })
  try {
    if (child.exitCode !== null || child.signalCode !== null) return await exited
    const deadline = new Promise((_, reject) => {
      timer = setTimeout(() => reject(Error('Desktop process did not exit within 15 seconds (pid ' + child.pid + ').')), 15000)
    })
    await Promise.race([exited, Promise.resolve().then(requestClose).then(() => exited), deadline])
  } catch (error) {
    if (child.exitCode === null && child.signalCode === null) child.kill()
    throw error
  } finally {
    clearTimeout(timer)
    child.removeListener('exit', onExit)
  }
}

export async function finishVerification(primaryError, ...cleanups) {
  const errors = primaryError ? [primaryError] : []
  for (const cleanup of cleanups) {
    try { await cleanup() } catch (error) { errors.push(error) }
  }
  if (errors.length === 1) throw errors[0]
  if (errors.length) throw new AggregateError(errors, 'Desktop verification and cleanup failed', {cause: errors[0]})
}
