import json
import subprocess
import sys
import pytest
from agent_office.adapters import CLIAdapter, AgentResult
from agent_office import cli


def result(status="SUCCESS",response="VERDICT: APPROVED",**extra):
    return {"conversation_id":"test-session","status":status,"response":response,**extra}


def test_antigravity_transports_one_literal_prompt_with_plan_and_sandbox(tmp_path, monkeypatch):
    prompt='한글 /model test & $(echo x) "quotes"'
    monkeypatch.setattr("agent_office.adapters.resolve_executable", lambda name: "fake-agy" if name=="agy" else None)
    def run(argv, **kwargs):
        assert prompt not in argv
        assert argv[0] == "fake-agy"
        assert argv[argv.index("--mode")+1] == "plan"
        assert "--sandbox" in argv and "--dangerously-skip-permissions" not in argv
        assert json.loads(kwargs["input"]) == {"event":"user","message":{"content":prompt}}
        assert not kwargs.get("shell")
        return subprocess.CompletedProcess(argv,0,json.dumps({"event":"result","result":result()}),"")
    monkeypatch.setattr("agent_office.adapters.subprocess.run",run)
    answer=CLIAdapter("antigravity").execute(prompt,workspace=tmp_path,allow_edits=False)
    assert answer.returncode==0 and answer.message=="VERDICT: APPROVED"


def test_antigravity_edit_mode_is_explicit_and_never_bypasses_permissions():
    argv=CLIAdapter("antigravity").command("text",allow_edits=True)
    assert argv[argv.index("--mode")+1]=="accept-edits"
    assert "--sandbox" in argv and "--dangerously-skip-permissions" not in argv


@pytest.mark.parametrize("status",["ERROR","CANCELED","WAITING","INVALID","RUNNING","INTERRUPTED"])
def test_non_success_status_is_not_success_even_if_process_exit_zero(status):
    from agent_office.adapters import parse_antigravity
    answer=parse_antigravity(json.dumps({"event":"result","result":result(status)}))
    assert answer.returncode==(124 if status=="INTERRUPTED" else 1)


@pytest.mark.parametrize("text",["not json",'[]',json.dumps({"event":"init"}),
    json.dumps(result())+'\n'+json.dumps(result()), json.dumps(result(response="")),json.dumps(result(response=True)),
    json.dumps(result(error="failure"))])
def test_missing_malformed_or_conflicting_result_fails_closed(text):
    from agent_office.adapters import parse_antigravity
    assert parse_antigravity(text).returncode!=0


def test_reported_tool_error_cannot_be_hidden_by_terminal_success():
    from agent_office.adapters import parse_antigravity
    text='\n'.join([json.dumps({"event":"step_update","step_update":{"tool_info":{"error":{"type":"permission_denied"}}}}),
                     json.dumps({"event":"result","result":result()})])
    assert parse_antigravity(text).returncode!=0


def test_plain_json_and_stream_success_have_same_response():
    from agent_office.adapters import parse_antigravity
    for text in [json.dumps(result()),json.dumps({"event":"result","result":result()})]:
        parsed=parse_antigravity(text)
        assert parsed.returncode==0 and parsed.session_id=="test-session"


def test_real_fake_subprocess_roundtrips_antigravity_unicode_stdin(tmp_path):
    class NativeFake(CLIAdapter):
        def command(self,prompt,*,allow_edits):
            script="import json,sys; p=json.loads(sys.stdin.readline()); print(json.dumps({'event':'result','result':{'status':'SUCCESS','response':p['message']['content']}}))"
            return [sys.executable,'-c',script]
    answer=NativeFake("antigravity").execute('한글 & "문자"',workspace=tmp_path,allow_edits=False)
    assert answer.returncode==0 and answer.message=='한글 & "문자"'


def test_initial_roles_and_legacy_override():
    parser=cli.make_parser()
    args=parser.parse_args(['run','--repo','.','--task','x'])
    assert (args.developer,args.reviewer)==('codex','antigravity')
    explicit=parser.parse_args(['run','--repo','.','--task','x','--developer','claude','--reviewer','codex'])
    assert (explicit.developer,explicit.reviewer)==('claude','codex')


def test_default_engine_pair_is_persisted_and_journaled(tmp_path, monkeypatch, capsys):
    from agent_office.store import TaskStore
    repo=tmp_path/'repo'
    subprocess.run(['git','init','-q',str(repo)],check=True)
    subprocess.run(['git','-C',str(repo),'-c','user.name=Test','-c','user.email=test@example.invalid','commit','--allow-empty','-qm','base'],check=True)
    check=tmp_path/'check.py'; check.write_text('pass')
    calls=[]
    class Fake:
        def __init__(self,engine,**kwargs): self.engine=engine
        def execute(self,prompt,**kwargs):
            calls.append(self.engine)
            return AgentResult(0,'VERDICT: APPROVED' if self.engine=='antigravity' else 'done')
    monkeypatch.setattr(cli,'CLIAdapter',Fake)
    db=tmp_path/'db'
    assert cli.main(['--db',str(db),'run','--repo',str(repo),'--task','x','--allow-edits','--test-command',sys.executable,str(check)])==0
    report=json.loads(capsys.readouterr().out)
    assert calls==['codex','antigravity']
    with TaskStore(db,readonly=True) as store:
        task=store.get_task(report['task_id'])
        assert (task.developer_engine,task.reviewer_engine)==('codex','antigravity')
        assert task.state=='READY_FOR_HUMAN_REVIEW'
        assert any(e.engine=='antigravity' and e.role=='reviewer' for e in store.list_events(task.task_id))


@pytest.mark.parametrize('text',[
    json.dumps({'event':'result','status':'SUCCESS','response':'VERDICT: APPROVED','result':result('ERROR')}),
    json.dumps({'event':'result','result':result()})+'\n'+json.dumps({'event':'step_update','step_update':{}}),
    json.dumps({'event':'result','result':result()})+'\n'+json.dumps(result('ERROR')),
    json.dumps({'event':'step_update','step_update':{'tool_info':{'error':{}}}})+'\n'+json.dumps({'event':'result','result':result()}),
])
def test_mixed_envelopes_and_records_after_terminal_result_fail_closed(text):
    from agent_office.adapters import parse_antigravity
    assert parse_antigravity(text).returncode!=0


def test_ndjson_line_separator_is_lf_not_unicode_inside_json_text():
    from agent_office.adapters import parse_antigravity
    text=json.dumps({'event':'init','init':{}})+'\n'+json.dumps({'event':'result','result':result(response='line\u2028text')},ensure_ascii=False)
    answer=parse_antigravity(text)
    assert answer.returncode==0 and answer.message=='line\u2028text'
