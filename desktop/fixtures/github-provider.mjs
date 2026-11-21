import assert from 'node:assert/strict'
import {execFileSync} from 'node:child_process'
import {randomUUID, verify} from 'node:crypto'
import {readFile, writeFile} from 'node:fs/promises'
import {createServer} from 'node:https'
import {join} from 'node:path'
import {fileURLToPath} from 'node:url'

export async function githubFixture(root, python, project, title) {
  const bare = join(root, 'github-fixture.git'), index = join(root, 'github-fixture.index')
  execFileSync('git', ['clone', '--bare', '--no-hardlinks', '-q', project, bare])
  const git = (args, input) => execFileSync('git', ['--git-dir=' + bare, '-c', 'user.name=Fixture',
    '-c', 'user.email=fixture@example.invalid', ...args], {input, encoding: 'utf8', env: {...process.env, GIT_INDEX_FILE: index}}).trim()
  execFileSync(python, [fileURLToPath(new URL('./github_tls.py', import.meta.url)), root])
  const [key, cert, appKey] = await Promise.all(['tls.pem', 'ca.pem', 'app.pem'].map(name => readFile(join(root, name))))
  const token = 'fixture_' + randomUUID()
  const repo = {id: 71, full_name: 'fixture/settings', name: 'settings', default_branch: 'main', owner: {login: 'fixture'}}
  const user = {id: 42, login: 'fixture-owner', type: 'User'}, bot = {id: 550, login: 'office-fixture[bot]', type: 'Bot'}
  const created = new Date().toISOString(), label = {name: 'office:ready'}
  const issue = {id: 107, number: 7, title, body: '설정 복원과 전체 회귀 결과를 확인하세요.', labels: [label], user,
    state: 'open', created_at: created, updated_at: created}
  const timeline = [{id: 201, event: 'labeled', label, actor: user, created_at: created}]
  const comments = [], calls = [], errors = []
  const head = git(['rev-parse', 'HEAD'])
  let pull = null, branch = null
  let origin, feed = false, dropFirstReceipt = true
  const closed = {...issue, id: 108, number: 8, state: 'closed', title: '접수 전 닫힌 이슈'}
  function issueComment(body, author) {
    const id = 3000 + comments.length
    const stamp = new Date().toISOString()
    const row = {id, body, user: author, html_url: origin + '/fixture/settings/issues/7#issuecomment-' + id,
      created_at: stamp, updated_at: stamp, ...(author === bot && {performed_via_github_app: {id: 55}})}
    comments.push(row)
    timeline.push({...row, event: 'commented', actor: author})
    issue.updated_at = stamp
    return row
  }
  const server = createServer({key, cert}, async (request, response) => {
    const url = new URL(request.url, origin), path = url.pathname.replace(/^\/api\/v3/, ''), method = request.method
    const route = path.replace(/^\/repos\/fixture\/settings/, '')
    calls.push(method + ' ' + path)
    const send = (data, status = 200) => {
      response.writeHead(status, {'Content-Type': 'application/json'})
      response.end(JSON.stringify(data))
    }
    try {
      const parts = []
      for await (const piece of request) parts.push(piece)
      const data = parts.length ? JSON.parse(Buffer.concat(parts).toString('utf8')) : {}
      if (path === '/meta') return send({installed_version: '3.20.1'})
      if (path === '/app/installations/72/access_tokens' || path === '/app') {
        const jwt = request.headers.authorization?.slice(7).split('.')
        assert.equal(jwt?.length, 3)
        assert.ok(verify('RSA-SHA256', Buffer.from(jwt.slice(0, 2).join('.')), appKey, Buffer.from(jwt[2], 'base64url')))
        assert.equal(JSON.parse(Buffer.from(jwt[1], 'base64url')).iss, '55')
        return path === '/app' ? send({id: 55, slug: 'office-fixture'}) : send({token, expires_at: new Date(Date.now() + 3600000).toISOString()}, 201)
      }
      assert.equal(request.headers.authorization === 'Bearer ' + token, true, 'Expected the synthetic installation token')
      if (path === '/installation/repositories') return send({repositories: [repo], total_count: 1})
      if (path === '/installation/token' && method === 'DELETE') return send(null, 204)
      if (path === '/repositories/71' || path === '/repos/fixture/settings') return send(repo)
      if (route === '/git/ref/heads/main') return send({ref: 'refs/heads/main', object: {sha: head, type: 'commit'}})
      if (route.startsWith('/git/ref/heads/ao/')) {
        const ref = route.slice('/git/ref/'.length)
        return ref === branch ? send({ref: 'refs/' + ref, object: {sha: git(['rev-parse', 'refs/' + ref]), type: 'commit'}}) : send({}, 404)
      }
      if (/^\/git\/commits\/[a-f0-9]{40}$/.test(route)) {
        const sha = route.split('/').at(-1)
        const [tree, parents] = git(['show', '-s', '--format=%T%n%P', sha]).split('\n')
        return send({sha, tree: {sha: tree}, parents: parents ? parents.split(' ').map(s => ({sha: s})) : []})
      }
      if (route === '/git/blobs' && method === 'POST') {
        assert.equal(data.encoding, 'base64')
        return send({sha: git(['hash-object', '-w', '--stdin'], Buffer.from(data.content, 'base64'))}, 201)
      }
      if (route === '/git/trees' && method === 'POST') {
        git(['read-tree', data.base_tree])
        for (const file of data.tree) {
          assert.equal(file.path, 'settings.py', 'Only the expected fixture source should change')
          git(['update-index', '--add', '--cacheinfo', file.mode, file.sha, file.path])
        }
        return send({sha: git(['write-tree'])}, 201)
      }
      if (route === '/git/commits' && method === 'POST') {
        assert.deepEqual(data.parents, [head])
        return send({sha: git(['commit-tree', data.tree, '-p', head, '-m', data.message])}, 201)
      }
      if (route === '/git/refs' && method === 'POST') {
        assert.match(data.ref, /^refs\/heads\/ao\/issue-7-[0-9a-f]+$/)
        git(['update-ref', data.ref, data.sha, '0'.repeat(40)])
        branch = data.ref.slice('refs/'.length)
        return send({ref: data.ref, object: {sha: data.sha}}, 201)
      }
      if (route === '/pulls' && method === 'POST') {
        assert.ok(!pull)
        assert.equal(data.draft, true)
        assert.equal(data.base, 'main')
        assert.equal('heads/' + data.head, branch)
        pull = {number: 9, state: 'open', draft: true, user: bot, performed_via_github_app: {id: 55},
          base: {ref: 'main', sha: head, repo}, head: {ref: data.head, sha: git(['rev-parse', 'refs/' + branch]), repo}}
        return send(pull, 201)
      }
      if (route === '/pulls') return send(pull ? [pull] : [])
      if (route === '/pulls/9') return send(pull, pull ? 200 : 404)
      if (route === '/pulls/9/reviews' || route.endsWith('/statuses')) return send([])
      if (route.endsWith('/check-runs')) return send({check_runs: []})
      if (route === '/branches/main') return send({name: 'main', commit: {sha: head}})
      if (route === '/collaborators/fixture-owner/permission') return send({permission: 'write', user})
      if (route === '/issues') return send(feed ? [issue, closed] : [])
      if (route === '/issues/7') return send(issue)
      if (route === '/issues/8') return send(closed)
      if (route === '/issues/8/timeline') return send([timeline[0], {id: 202, event: 'closed', actor: user, created_at: closed.closed_at}])
      if (route === '/issues/7/timeline') return send(timeline)
      if (route === '/issues/7/comments') {
        if (method !== 'POST') return send(comments)
        const posted = issueComment(data.body, bot)
        if (dropFirstReceipt) {
          dropFirstReceipt = false
          return response.destroy()
        }
        return send(posted, 201)
      }
      if (/^\/issues\/comments\/\d+$/.test(route)) {
        const row = comments.find(c => c.id === Number(route.split('/').at(-1)))
        if (!row) return send({}, 404)
        if (method === 'PATCH') Object.assign(row, {body: data.body, updated_at: new Date().toISOString()})
        return send(row)
      }
      if (route === '/issues/7/labels' && method === 'POST') {
        for (const name of data.labels) if (!issue.labels.some(l => l.name === name)) issue.labels.push({name})
        return send(issue.labels)
      }
      if (route.startsWith('/issues/7/labels/') && method === 'DELETE') {
        issue.labels = issue.labels.filter(l => l.name !== decodeURIComponent(route.split('/').at(-1)))
        return send(issue.labels)
      }
      errors.push(method + ' ' + path)
      return send({message: 'Unknown fixture route'}, 404)
    } catch (err) {
      errors.push(method + ' ' + path + ' ' + err.message)
      return send({message: 'Fixture request rejected'}, 400)
    }
  })
  await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve) })
  origin = 'https://127.0.0.1:' + server.address().port
  issue.html_url = origin + '/fixture/settings/issues/7'
  closed.html_url = origin + '/fixture/settings/issues/8'
  repo.html_url = origin + '/fixture/settings'
  repo.clone_url = repo.html_url + '.git'
  const config = join(root, 'github.json')
  await writeFile(config, JSON.stringify({connections: [{id: 'fixture', name: '검증용 GitHub', kind: 'ghes', web_url: origin,
    app_id: 55, installation_id: 72, private_key_file: join(root, 'app.pem'), ca_file: join(root, 'ca.pem'), proxy: 'direct'}]}), {mode: 0o600})
  return {config, origin, calls, errors, issue, comments, feed: () => {
    const stamp = new Date().toISOString()
    issue.updated_at = timeline[0].created_at = stamp
    closed.updated_at = closed.closed_at = stamp
    feed = true
  },
    draft: () => pull,
    reply: body => issueComment(body, user),
    close: () => new Promise(resolve => server.close(resolve))}
}
