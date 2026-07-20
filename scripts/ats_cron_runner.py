#!/usr/bin/env python3
"""Single-action bounded public ATS runner."""
from __future__ import annotations
import argparse,json,subprocess,sys
from pathlib import Path
from ats_candidate_selector import select_ats_candidate
from ats_intent_preparer import prepare_ats_intent,ATSPreparationBlocked
from local_funnel import LocalFunnel,utc_now

def main()->int:
 p=argparse.ArgumentParser();p.add_argument('--db',default='state/job_funnel.sqlite3');p.add_argument('--execute',action='store_true');a=p.parse_args();candidate=select_ats_candidate(a.db);now=utc_now().isoformat()
 if candidate is None:
  with LocalFunnel(a.db) as f:run=f.begin_batch_run(channel='ats',max_actions=1,started_at=now);f.finish_batch_run(run_id=run,state='completed',reason=f'candidates=0 execute={a.execute}',now=utc_now().isoformat())
  print(json.dumps({'run_id':run,'state':'completed','execute':a.execute,'candidate_count':0,'results':[]}));return 0
 if not a.execute:
  with LocalFunnel(a.db) as f:run=f.begin_batch_run(channel='ats',max_actions=1,started_at=now);f.finish_batch_run(run_id=run,state='completed',reason='candidates=1 execute=False',now=utc_now().isoformat())
  print(json.dumps({'run_id':run,'state':'completed','execute':False,'candidate_count':1,'results':[{'external_id':candidate.external_id,'status':'dry_run_ready'}]}));return 0
 with LocalFunnel(a.db) as f:
  run=f.begin_batch_run(channel='ats',max_actions=1,started_at=now)
  try:
   m=candidate.metadata;snapshot=json.loads(Path(m['form_snapshot_path']).read_text());answers=json.loads(Path(m['answer_map_path']).read_text())
   intent=prepare_ats_intent(funnel=f,run_id=run,snapshot=snapshot,answer_map=answers,source=candidate.source,external_id=candidate.external_id,job_url=candidate.url,company=candidate.company,job_title=candidate.title,resume_path=m['resume_path'],package_path=m['package_path'],now=utc_now().isoformat())
  except Exception as exc:
   f.finish_batch_run(run_id=run,state='failed',reason=f'preparation blocked: {type(exc).__name__}: {exc}'[:1000],now=utc_now().isoformat());print(json.dumps({'run_id':run,'state':'failed','status':'preparation_blocked','reason':str(exc)}));return 2
 proc=subprocess.run([sys.executable,str(Path(__file__).with_name('ats_intent_worker.py')),'--intent-id',str(intent.intent_id)],text=True,capture_output=True,timeout=180)
 print(proc.stdout.strip() or json.dumps({'status':'worker_failed','stderr':proc.stderr[-1000:]}));return proc.returncode
if __name__=='__main__':raise SystemExit(main())
