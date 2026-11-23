export function engineMaintenance(doc, engine, api) {
  const group = doc.createElement('div'), check = doc.createElement('button');
  const status = doc.createElement('p'), command = doc.createElement('code');
  check.type = 'button';
  check.textContent = '공개 버전 확인';
  status.setAttribute('role', 'status');
  group.append(check, status, command);
  check.onclick = async () => {
    check.disabled = true; command.textContent = '';
    status.textContent = '공개 버전을 확인하고 있습니다.';
    try {
      const snapshot = await api('/engines/' + engine + '/check', {});
      status.textContent = '설치 ' + (snapshot.installed || '미확인') + ' · 공개 ' + (snapshot.latest || '미확인');
      const version = /^\d{1,6}\.\d{1,6}\.\d{1,6}$/;
      if (snapshot.update_available && /^[a-f0-9]{32}$/.test(snapshot.journal_id) &&
          [snapshot.installed, snapshot.latest].every(value => version.test(value))) {
        status.textContent += ' · 실행 호스트의 일반 사용자 터미널에서 갱신할 수 있습니다. 사용자 지정 저널은 update-engine 앞에 --db 경로를 넣으세요.';
        command.textContent = 'agent-office update-engine ' + engine + ' --from-version ' + snapshot.installed +
          ' --to-version ' + snapshot.latest + ' --journal ' + snapshot.journal_id;
      }
    } catch (error) { status.textContent = error.message; }
    finally { check.disabled = false; }
  };
  return group;
}
