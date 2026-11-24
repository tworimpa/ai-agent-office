const retryKey = 'agent-office.workspace-startup-retried';

export async function startWorkspace(win, load = () => import('./workspace-office.js')) {
  const doc = win.document;
  const stage = value => { doc.documentElement.dataset.workspaceBoot = value; };
  function showRetry(message) {
    doc.getElementById('connection').textContent = '화면을 열 수 없습니다';
    doc.getElementById('empty-note').textContent = message;
    const retry = doc.getElementById('empty-retry');
    retry.textContent = '화면 다시 열기';
    retry.hidden = false;
    retry.addEventListener('click', () => win.location.reload(), {once: true});
  }

  stage('loading');
  let workspace;
  try {
    workspace = await load();
  } catch {
    stage('module-error');
    // A failed transitive import stays failed in this document's module map.
    // Mark the retry before reloading. If tab storage is blocked, wait for the user.
    try {
      if (!win.sessionStorage.getItem(retryKey)) {
        win.sessionStorage.setItem(retryKey, '1');
        win.location.reload();
        return;
      }
    } catch { /* The retry button also works without tab storage. */ }
    showRetry('작업실 화면에 필요한 파일을 불러오지 못했습니다. 다시 열어 주세요.');
    return;
  }

  stage('mounting');
  try {
    workspace.mountWorkspaceOffice(win);
  } catch (error) {
    stage('mount-error');
    showRetry('작업실 화면을 시작하지 못했습니다. 다시 열어 주세요.');
    throw error;
  }
  stage('mounted');
  try { win.sessionStorage.removeItem(retryKey); } catch { /* Storage is optional. */ }
}

if (typeof document !== 'undefined') void startWorkspace(window);
