export function watchProgress(doc, win) {
  const node = doc.getElementById('provider-progress');
  let target = {}, last = null, stream = null;
  function display(row) {
    if (!node || !row || !Number.isSafeInteger(row.id) || row.id <= (last?.id ?? 0) ||
        typeof row.label !== 'string' || !Number.isFinite(Date.parse(row.at))) return;
    last = row;
    node.textContent = row.engine + ' · ' + row.label + ' · 마지막 확인 ' + new Date(row.at).toLocaleTimeString('ko-KR');
    node.hidden = false;
  }
  function close() { stream?.close(); stream = null; }
  function connect() {
    if (doc.hidden || !target.active || !target.url) { close(); return; }
    if (stream || !win.EventSource) return;
    const source = stream = new win.EventSource(target.url);
    source.onmessage = event => {
      if (source !== stream) return;
      try { display(JSON.parse(event.data)); } catch { /* Keep the last confirmed record. */ }
    };
    source.addEventListener('finished', () => { if (source === stream) close(); });
  }
  doc.addEventListener('visibilitychange', connect);
  return {
    select(value) {
      if (value.id !== target.id || value.url !== target.url) {
        close(); last = null;
        if (node) node.hidden = true;
      }
      target = value; display(value.latest); connect();
    },
    dispose() { close(); doc.removeEventListener('visibilitychange', connect); }
  };
}
