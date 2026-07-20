from __future__ import annotations
import json,subprocess,sys
from pathlib import Path
from local_funnel import LocalFunnel

SCRIPT=Path(__file__).with_name('linkedin_submit_state.py')
def call(db,*args):
 p=subprocess.run([sys.executable,str(SCRIPT),'--db',str(db),*args],text=True,capture_output=True,check=True)
 return json.loads(p.stdout)

def test_linkedin_bridge_uses_only_authoritative_ledger(tmp_path):
 db=tmp_path/'f.sqlite3'; intent_file=tmp_path/'intent.json'; rb=tmp_path/'rb.json'; evidence=tmp_path/'readback.txt'
 with LocalFunnel(db) as f: run=f.begin_batch_run(channel='linkedin',max_actions=5,started_at='2026-07-19T20:00:00+00:00')
 intent_file.write_text(json.dumps({'job_id':'4453058520','form_fingerprint':'abc','payload':{'company':'C','job_title':'PM'}}))
 reserved=call(db,'reserve','--input',str(intent_file),'--run-id',run);assert reserved['created'] is True
 token=call(db,'begin','--intent-id',str(reserved['intent_id']),'--worker-id','test')['execution_token']
 rb.write_text(json.dumps({'marker':'application_submitted','job_id':'4453058520'}));evidence.write_text('Application submitted')
 receipt=call(db,'receipt','--intent-id',str(reserved['intent_id']),'--token',token,'--readback',str(rb),'--evidence',str(evidence))
 with LocalFunnel(db) as f:
  assert f.get_action_intent(intent_id=reserved['intent_id'])['state']=='verified'
  assert f.has_verified_application_receipt(source='linkedin',external_id='4453058520')
 assert receipt['state']=='verified' and not (tmp_path/'agent-linkedin.sqlite3').exists()
 duplicate=call(db,'reserve','--input',str(intent_file),'--run-id',run)
 assert duplicate=={'created':False,'state':'duplicate_verified_receipt'}


def test_stale_linkedin_execution_becomes_ambiguous_without_replay(tmp_path):
 db=tmp_path/'f.sqlite3'
 with LocalFunnel(db) as f:
  run=f.begin_batch_run(channel='linkedin',max_actions=2,started_at='2026-07-19T18:00:00+00:00')
  reserved=f.reserve_action_intent(run_id=run,kind='application_submit',idempotency_key='linkedin:123:application',payload={'source':'linkedin','external_id':'123'},now='2026-07-19T18:00:01+00:00')
  f.mark_intent_executing(intent_id=reserved.intent_id,worker_id='crashed',now='2026-07-19T18:00:02+00:00')
  ids=f.recover_stale_executions(now='2026-07-19T20:00:00+00:00',older_than_seconds=900,source='linkedin')
  row=f.get_action_intent(intent_id=reserved.intent_id)
 assert ids==[reserved.intent_id]
 assert row['state']=='ambiguous'
 import sqlite3
 dbrow=sqlite3.connect(db).execute('select last_error_code,execution_token from action_intents where id=?',(reserved.intent_id,)).fetchone()
 assert dbrow==('worker_crash_unknown_side_effect',None)
 with LocalFunnel(db) as f: assert not f.has_verified_application_receipt(source='linkedin',external_id='123')
