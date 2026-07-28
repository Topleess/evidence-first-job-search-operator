#!/usr/bin/env python3
"""Unified bounded job-search control plane for all production channels."""
from __future__ import annotations
import argparse,json,subprocess
from datetime import datetime,timezone
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
COLLECTORS=[('/opt/data/scripts/job_search_hh_daily.sh',),('/opt/data/scripts/job_search_linkedin_public_daily.sh',),('/opt/data/scripts/job_search_ats_daily.sh',),('/opt/data/scripts/job_search_boards_daily.sh',)]

def run(command,timeout=900):
 p=subprocess.run(list(command),cwd=ROOT,text=True,capture_output=True,timeout=timeout)
 return {'command':[str(x) for x in command],'exit_code':p.returncode,'stdout':p.stdout[-4000:],'stderr':p.stderr[-2000:]}
def main()->int:
 ap=argparse.ArgumentParser();ap.add_argument('--collect',action='store_true');ap.add_argument('--execute',action='store_true');ap.add_argument('--execute-hh',action='store_true');ap.add_argument('--hh-cap',type=int,default=20);ap.add_argument('--linkedin-cap',type=int,default=5);ap.add_argument('--email-cap',type=int,default=5);ap.add_argument('--db',default='state/job_funnel.sqlite3');ap.add_argument('--hh-profile',default='data/browser_profiles/hh_ru');ap.add_argument('--hh-truth-map',default='');ap.add_argument('--fresh-ttl-hours',type=int,default=24);ap.add_argument('--output-dir',default='state/operator-runs');a=ap.parse_args()
 if not 1<=a.hh_cap<=20 or not 1<=a.linkedin_cap<=5 or not 1<=a.email_cap<=5:raise SystemExit('caps exceed contract')
 execute_hh=a.execute or a.execute_hh
 started=datetime.now(timezone.utc).isoformat();report={'schema_version':'job_search_operator_run.v1','started_at':started,'execute':{'hh':execute_hh,'linkedin':a.execute,'ats':a.execute,'gmail':a.execute},'collection':[],'channels':{}}
 if a.collect:
  for item in COLLECTORS:report['collection'].append(run(['bash',*item]))
 report['collection'].append(run(['python3','/opt/data/scripts/job_search_local_sqlite_sync.py','--db',a.db]))
 report['collection'].append(run(['python3','scripts/hh_live_eligibility.py','--db',a.db,'--limit','30','--fresh-ttl-hours',str(a.fresh_ttl_hours)]))
 commands={
  'hh':['python3','scripts/hh_cron_runner.py','--db',a.db,'--profile',a.hh_profile,'--batch-limit',str(a.hh_cap),'--daily-cap',str(a.hh_cap),'--fresh-ttl-hours',str(a.fresh_ttl_hours),*(['--truth-map',a.hh_truth_map] if a.hh_truth_map else []),*([] if execute_hh else ['--dry-run'])],
  'linkedin':['python3','scripts/linkedin_cron_runner.py','--limit',str(a.linkedin_cap),*(['--execute'] if a.execute else [])],
  'ats':['python3','scripts/ats_cron_runner.py',*(['--execute'] if a.execute else [])],
  'gmail':['uv','run','--with','google-api-python-client','--with','google-auth','python3','scripts/email_cron_runner.py','--limit',str(a.email_cap),*(['--execute'] if a.execute else [])],
 }
 report['reconciliation']={'gmail_replies':run(['uv','run','--with','google-api-python-client','--with','google-auth','python3','scripts/email_response_reconciler.py'])}
 for channel,cmd in commands.items():report['channels'][channel]=run(cmd)
 report['finished_at']=datetime.now(timezone.utc).isoformat();report['all_invoked']=set(report['channels'])=={'hh','linkedin','ats','gmail'}
 failed_collection=[x for x in report['collection'] if x['exit_code']!=0]
 failed_channels={k:v['exit_code'] for k,v in report['channels'].items() if v['exit_code']!=0}
 reconciliation_ok=report['reconciliation']['gmail_replies']['exit_code']==0
 report['success']=report['all_invoked'] and not failed_collection and not failed_channels and reconciliation_ok
 outdir=ROOT/a.output_dir;outdir.mkdir(parents=True,exist_ok=True);stamp=started.replace(':','').replace('+00:00','Z');path=outdir/f'{stamp}.json';path.write_text(json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps({'output':str(path),'execute':report['execute'],'all_invoked':report['all_invoked'],'success':report['success'],'collection_failures':len(failed_collection),'reconciliation_ok':reconciliation_ok,'channel_exit_codes':{k:v['exit_code'] for k,v in report['channels'].items()}},sort_keys=True));return 0 if report['success'] else 1
if __name__=='__main__':raise SystemExit(main())
