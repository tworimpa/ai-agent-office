import json
import subprocess
from agent_office import cli
import pytest


def test_catalog_uses_only_metadata_commands_and_hides_raw_output(monkeypatch):
    from agent_office.engine_status import collect_engine_status
    calls=[]
    monkeypatch.setattr('agent_office.engine_status.shutil.which', lambda name:'/fake/'+name)
    def run(argv,**kwargs):
        calls.append(argv[1:])
        if argv[1:]==['--version']:
            text='private-token-hidden\nversion 1.4.2'
        elif argv[1:]==['exec','--help']:
            text='--json --sandbox'
        elif argv[1:]==['--help']:
            text='--input-format --output-format --mode --sandbox --disable-slash-commands --print-timeout'
        elif argv[1:]==['login','status']:
            text='Logged in using ChatGPT\nprivate-account-hidden'
        else:
            raise AssertionError(argv)
        return subprocess.CompletedProcess(argv,0,text,'')
    monkeypatch.setattr('agent_office.engine_status.subprocess.run',run)
    result=collect_engine_status()
    assert [x['engine'] for x in result['engines']]==['codex','antigravity']
    codex,agy=result['engines']
    assert codex['auth_status']=='authenticated'
    assert agy['auth_status']=='unknown'
    assert all(x['execution_status']=='not_checked' for x in result['engines'])
    assert all(x['headless_support'] for x in result['engines'])
    assert calls.count(['login','status'])==1
    encoded=json.dumps(result)
    assert 'private-token-hidden' not in encoded and 'private-account-hidden' not in encoded and '/fake/' not in encoded
    assert all('-p' not in argv and '--print' not in argv for argv in calls)


def test_missing_tools_do_not_probe_auth_or_mark_ready(monkeypatch):
    from agent_office.engine_status import collect_engine_status
    monkeypatch.setattr('agent_office.engine_status.shutil.which',lambda name:None)
    def forbidden(*a,**kw): raise AssertionError('unexpected subprocess')
    monkeypatch.setattr('agent_office.engine_status.subprocess.run',forbidden)
    for item in collect_engine_status()['engines']:
        assert not item['installed'] and item['auth_status']=='unknown'
        assert item['probe_status']=='missing' and not item['headless_support']


@pytest.mark.parametrize('failure',['timeout','error','unsupported'])
def test_probe_failures_are_not_success(monkeypatch,failure):
    from agent_office.engine_status import collect_engine_status
    monkeypatch.setattr('agent_office.engine_status.shutil.which',lambda name:'/fake/'+name)
    def run(argv,**kwargs):
        if failure=='timeout':raise subprocess.TimeoutExpired(argv,5)
        return subprocess.CompletedProcess(argv,1 if failure=='error' else 0,'version 1.2.3','')
    monkeypatch.setattr('agent_office.engine_status.subprocess.run',run)
    for item in collect_engine_status()['engines']:
        assert not item['headless_support']
        assert item['auth_status']=='unknown' and item['execution_status']=='not_checked'


def test_engines_cli_does_not_open_database_or_return_vendor_account_data(tmp_path,monkeypatch,capsys):
    from agent_office import engine_status
    snapshot={'checked_at':'2026-11-06T00:00:00Z','platform':'test','engines':[]}
    monkeypatch.setattr(engine_status,'collect_engine_status',lambda:snapshot)
    def forbidden(*a,**kw): raise AssertionError('database opened')
    monkeypatch.setattr(cli,'TaskStore',forbidden)
    assert cli.main(['--db',str(tmp_path/'db'),'engines'])==0
    assert json.loads(capsys.readouterr().out)==snapshot
    assert not (tmp_path/'db').exists()
