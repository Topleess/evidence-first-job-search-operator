#!/usr/bin/env python3
"""Bounded authoritative Gmail outreach runner."""
from __future__ import annotations
import argparse,json,sqlite3
from email_candidate_selector import select_email_candidates
from email_worker import execute_email_intent
from gmail_api_adapter import GmailApiClient,GmailApiSentStore,GmailApiTransport
from local_funnel import LocalFunnel,utc_now

def main()->int:
 p=argparse.ArgumentParser();p.add_argument('--db',default='state/job_funnel.sqlite3');p.add_argument('--limit',type=int,default=5);p.add_argument('--execute',action='store_true');p.add_argument('--token-file',default='/opt/data/google_token.json');a=p.parse_args()
 candidates=select_email_candidates(a.db,a.limit);now=utc_now().isoformat()
 with LocalFunnel(a.db) as f:run=f.begin_batch_run(channel='email',max_actions=a.limit,started_at=now)
 results=[];state='completed';reason='runner_not_started'
 try:
  transport=sent_store=None
  if a.execute:
   client=GmailApiClient(token_file=a.token_file);transport=GmailApiTransport(client=client);sent_store=GmailApiSentStore(client=client)
  for c in candidates:
   pld=c.payload
   if not a.execute:results.append({'queue_id':c.queue_id,'status':'dry_run_ready'});continue
   with LocalFunnel(a.db) as f:
    intent=f.prepare_email_intent(run_id=run,sender=pld['sender'],recipient=pld['recipient'],recipient_verified=True,recipient_provenance=f"{pld['recipient_provenance']}:{pld['recipient_evidence_path']}",vacancy_key=f"{pld['source']}:{pld['external_id']}",subject=pld['subject'],body=pld['body'],now=utc_now().isoformat())
   assert transport is not None and sent_store is not None
   with LocalFunnel(a.db) as f:
    result=execute_email_intent(funnel=f,intent_id=intent.intent_id,worker_id='gmail-authoritative-runner',transport=transport,sent_store=sent_store,now=utc_now().isoformat())
   results.append({'queue_id':c.queue_id,'intent_id':intent.intent_id,'status':result['status'],'receipt_id':result.get('receipt_id')})
   if result['status']=='verified':
    con=sqlite3.connect(a.db);con.execute("UPDATE queue SET state='done',last_error=NULL WHERE id=? AND state='pending'",(c.queue_id,));con.commit();con.close()
  remaining=max(0,a.limit-len(candidates))
  with LocalFunnel(a.db) as f:due=f.list_due_email_followups(now=utc_now().isoformat(),limit=max(1,a.limit))
  if not a.execute:
   results.extend({'followup_id':int(item['id']),'status':'followup_dry_run_ready'} for item in due[:remaining])
  else:
   assert transport is not None and sent_store is not None
   for item in due[:remaining]:
    with LocalFunnel(a.db) as f:
     follow=f.prepare_due_email_followup(followup_id=int(item['id']),run_id=run,body='Following up once in case this role is still relevant. Thank you for your time.',now=utc_now().isoformat())
     follow_result=execute_email_intent(funnel=f,intent_id=follow.intent_id,worker_id='gmail-authoritative-followup',transport=transport,sent_store=sent_store,now=utc_now().isoformat())
    results.append({'followup_id':int(item['id']),'intent_id':follow.intent_id,'status':follow_result['status'],'receipt_id':follow_result.get('receipt_id')})
  reason=f"initial_candidates={len(candidates)} due_followups={len(due)} total_cap={a.limit} execute={a.execute}"
 except Exception as exc:
  state='failed';reason=f'{type(exc).__name__}: {exc}'[:1000];results.append({'status':'runner_failed','reason':reason})
 finally:
  with LocalFunnel(a.db) as f:f.finish_batch_run(run_id=run,state=state,reason=reason,now=utc_now().isoformat())
 print(json.dumps({'run_id':run,'state':state,'execute':a.execute,'candidate_count':len(candidates),'results':results},sort_keys=True));return 0 if state=='completed' else 1
if __name__=='__main__':raise SystemExit(main())
