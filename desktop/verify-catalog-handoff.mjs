import assert from 'node:assert/strict'

export async function verifyCatalogHandoff(app, window, origin) {
  const alias = origin + '/bot-catalog/links/browser-fixture/0123456789abcdef0123456789abcdef'
  const blocked = [alias + '?', alias + '#', origin + '/api/hosts',
    alias.replace('127.0.0.1', 'localhost'), 'https://example.invalid/']
  await app.evaluate(({ shell }) => {
    const original = shell.openExternal
    const opened = []
    shell.openExternal = async url => { opened.push(url) }
    globalThis.catalogHandoffFixture = {
      opened,
      restore() {
        shell.openExternal = original
        delete globalThis.catalogHandoffFixture
      },
    }
  })
  try {
    await window.evaluate(urls => {
      for (const url of urls) {
        const link = document.createElement('a')
        link.href = url
        link.target = '_blank'
        link.rel = 'noopener noreferrer'
        document.body.append(link)
        link.click()
        link.remove()
      }
    }, [...blocked, alias])
    const result = await app.evaluate(async ({ BrowserWindow }, expected) => {
      const { opened } = globalThis.catalogHandoffFixture
      const deadline = Date.now() + 3000
      while (!opened.includes(expected) && Date.now() < deadline) {
        await new Promise(resolve => setTimeout(resolve, 25))
      }
      return { opened, windows: BrowserWindow.getAllWindows().length }
    }, alias)
    assert.deepEqual(result, { opened: [alias], windows: 1 })
    assert.equal(new URL(window.url()).origin, origin)
  } finally {
    await app.evaluate(() => globalThis.catalogHandoffFixture.restore())
  }
  console.log('Catalog alias handed to the browser opener without opening a new embedded window.')
}
