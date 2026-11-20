const { contextBridge, ipcRenderer } = require('electron')

// Only a real click (including keyboard activation) on the registration control
// may open the native dialog. The isolated world owns this short-lived grant.
let pickerClick = null
window.addEventListener('click', event => {
  const button = event.target
  if (event.isTrusted && button.tagName === 'BUTTON' && (
    (button.id === 'pick-folders' && button.closest('dialog[open]')?.id === 'register-dialog') ||
    (button.id === 'empty-register' && button.closest('#empty-room') && !button.hidden)
  )) pickerClick = event
}, true)

contextBridge.exposeInMainWorld('officeFolders', {
  select: () => {
    const click = pickerClick
    pickerClick = null
    if (!click?.eventPhase || !navigator.userActivation.isActive) return Promise.resolve([])
    return ipcRenderer.invoke('desktop:pick-folders')
  },
})
