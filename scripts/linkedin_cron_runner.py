#!/usr/bin/env python3
"""Bounded production runner for verified LinkedIn Easy Apply candidates."""
from __future__ import annotations
import argparse,json,subprocess,sys
from dataclasses import asdict
from pathlib import Path
from linkedin_candidate_selector import select_candidates
from local_funnel import LocalFunnel,utc_now

def main()->int:
 p=argparse.ArgumentParser();p.add_argument('--db',default='state/job_funnel.sqlite3');p.add_argument('--limit',type=int,default=5);p.add_argument('--execute',action='store_true');p.add_argument('--profile',default='/opt/data/job-search-agent-linkedin/data/browser_profiles/linkedin');p.add_argument('--resume',default='resume/resume_product_manager_alexander_shamshurin_2026-07-09.pdf');p.add_argument('--evidence-dir',default='state/linkedin-evidence');a=p.parse_args()
 candidates=select_candidates(a.db,limit=a.limit);started=utc_now().isoformat()
 with LocalFunnel(a.db) as f: run=f.begin_batch_run(channel='linkedin',max_actions=a.limit,started_at=started)
 results=[];terminal_state='completed';reason='runner_not_started'
 try:
  for c in candidates:
   if not a.execute: results.append({'job_id':c.external_id,'status':'dry_run_ready'});continue
   cmd=['node',str(Path(__file__).with_name('linkedin_easy_apply_executor.js')),'--job-id',c.external_id,'--run-id',run,'--search-url',c.search_url,'--db',str(a.db),'--profile',str(a.profile),'--resume',str(a.resume),'--evidence-dir',str(a.evidence_dir)]
   proc=subprocess.run(cmd,text=True,capture_output=True,timeout=600)
   results.append({'job_id':c.external_id,'exit_code':proc.returncode,'stdout':proc.stdout[-2000:],'stderr':proc.stderr[-2000:]})
  reason=f"candidates={len(candidates)} execute={a.execute}"
 except Exception as exc:
  terminal_state='failed';reason=f'{type(exc).__name__}: {exc}'[:1000];results.append({'status':'runner_failed','reason':reason})
 finally:
  with LocalFunnel(a.db) as f: f.finish_batch_run(run_id=run,state=terminal_state,reason=reason,now=utc_now().isoformat())
 print(json.dumps({'run_id':run,'state':terminal_state,'execute':a.execute,'candidate_count':len(candidates),'results':results},ensure_ascii=False,sort_keys=True));return 0 if terminal_state=='completed' else 1
if __name__=='__main__':raise SystemExit(main())
