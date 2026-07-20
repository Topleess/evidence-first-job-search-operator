#!/usr/bin/env python3
"""Read-only Gmail reply reconciliation and bounded follow-up discovery."""
from __future__ import annotations
import argparse,json,sqlite3
from email_worker import reconcile_email_response
from gmail_api_adapter import GmailApiClient,GmailApiReplyStore
from local_funnel import LocalFunnel,utc_now

def main()->int:
 p=argparse.ArgumentParser();p.add_argument('--db',default='state/job_funnel.sqlite3');p.add_argument('--token-file',default='/opt/data/google_token.json');p.add_argument('--limit',type=int,default=20);a=p.parse_args()
 if not 1<=a.limit<=100:raise SystemExit('limit must be 1..100')
 con=sqlite3.connect(a.db);rows=con.execute("SELECT id FROM action_intents WHERE kind='email_send' AND state='verified' ORDER BY id DESC LIMIT ?",(a.limit,)).fetchall();con.close()
 reply_store=GmailApiReplyStore(client=GmailApiClient(token_file=a.token_file));results=[]
 with LocalFunnel(a.db) as f:
  for (intent_id,) in rows:
   try:results.append(reconcile_email_response(funnel=f,initial_intent_id=int(intent_id),reply_store=reply_store))
   except Exception as exc:results.append({'intent_id':int(intent_id),'status':'reconciliation_error','error':f'{type(exc).__name__}: {exc}'[:500]})
  due=f.list_due_email_followups(now=utc_now().isoformat(),limit=20)
 print(json.dumps({'checked_verified_initials':len(rows),'responses_verified':sum(x.get('status')=='response_verified' for x in results),'results':results,'due_followups':len(due),'followup_send_delegated_to_email_runner':True},sort_keys=True));return 0
if __name__=='__main__':raise SystemExit(main())
