const { contextBridge, ipcRenderer } = require('electron')

contextBridge.exposeInMainWorld('officeDesktop', {
  state: () => ipcRenderer.invoke('desktop:state'),
  connect: address => ipcRenderer.invoke('desktop:connect', address),
  start: remotes => ipcRenderer.invoke('desktop:start', remotes),
  open: () => ipcRenderer.invoke('desktop:open'),
  stop: () => ipcRenderer.invoke('desktop:stop'),
  tools: () => ipcRenderer.invoke('desktop:tools'),
  docs: engine => ipcRenderer.invoke('desktop:docs', engine),
  onRefresh: callback => ipcRenderer.on('desktop:refresh', () => callback()),
})
