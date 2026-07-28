#!/usr/bin/env python3
"""JSON bridge from LinkedIn browser worker to authoritative LocalFunnel."""
from __future__ import annotations
import argparse,json,os,re
from pathlib import Path
from urllib.parse import urlparse
from local_funnel import LocalFunnel, utc_now

MARKERS={"application_submitted","applied_state_on_job_page"}

def main()->int:
 p=argparse.ArgumentParser();p.add_argument('--db',required=True);sub=p.add_subparsers(dest='command',required=True)
 r=sub.add_parser('reserve');r.add_argument('--input',required=True);r.add_argument('--run-id',required=True)
 z=sub.add_parser('recover');z.add_argument('--older-than-seconds',required=True,type=int);z.add_argument('--source',default='linkedin')
 b=sub.add_parser('begin');b.add_argument('--intent-id',type=int,required=True);b.add_argument('--worker-id',required=True)
 c=sub.add_parser('check');c.add_argument('--intent-id',type=int,required=True);c.add_argument('--token',default=os.environ.get('LINKEDIN_EXECUTION_TOKEN',''))
 k=sub.add_parser('blocked');k.add_argument('--intent-id',type=int,required=True);k.add_argument('--token',default=os.environ.get('LINKEDIN_EXECUTION_TOKEN',''));k.add_argument('--reason',required=True)
 a=sub.add_parser('ambiguous');a.add_argument('--intent-id',type=int,required=True);a.add_argument('--token',default=os.environ.get('LINKEDIN_EXECUTION_TOKEN',''));a.add_argument('--reason',required=True)
 q=sub.add_parser('receipt');q.add_argument('--intent-id',type=int,required=True);q.add_argument('--token',default=os.environ.get('LINKEDIN_EXECUTION_TOKEN',''));q.add_argument('--readback',required=True);q.add_argument('--evidence',required=True)
 x=p.parse_args();now=utc_now().isoformat()
 with LocalFunnel(x.db) as f:
  if x.command=='recover':
   ids=f.recover_stale_executions(now=now,older_than_seconds=x.older_than_seconds,source=x.source);result={'state':'recovered_to_ambiguous','intent_ids':ids,'count':len(ids)}
  elif x.command=='reserve':
   data=json.loads(Path(x.input).read_text());jid=str(data['job_id']).strip();payload=dict(data.get('payload') or {})
   payload.update({'source':'linkedin','external_id':jid,'job_url':f'https://www.linkedin.com/jobs/view/{jid}/','form_fingerprint':str(data['form_fingerprint'])})
   if f.has_verified_application_receipt(source='linkedin',external_id=jid):
    result={'state':'duplicate_verified_receipt','created':False}
   else:
    intent=f.reserve_action_intent(run_id=x.run_id,kind='application_submit',idempotency_key=f'linkedin:{jid}:application',payload=payload,now=now)
    result={'intent_id':intent.intent_id,'created':intent.created,'state':'reserved'}
  elif x.command=='begin': result={'execution_token':f.mark_intent_executing(intent_id=x.intent_id,worker_id=x.worker_id,now=now)}
  elif x.command=='check':
   f.assert_intent_execution_fence(intent_id=x.intent_id,execution_token=x.token);result={'state':'execution_fence_valid'}
  elif x.command=='blocked':
   f.close_execution_blocked(intent_id=x.intent_id,execution_token=x.token,now=now,error_code=x.reason[:64] or 'linkedin_pre_side_effect_blocked');result={'state':'blocked_pre_side_effect'}
  elif x.command=='ambiguous':
   f.mark_intent_ambiguous(intent_id=x.intent_id,execution_token=x.token,now=now,error_code=x.reason[:64] or 'linkedin_submit_ambiguous');result={'state':'ambiguous'}
  else:
   rb=json.loads(Path(x.readback).read_text());marker=str(rb.get('marker') or '')
   if marker not in MARKERS: raise ValueError('verified LinkedIn read-back marker is required')
   observed_url=str(rb.get('observed_job_url') or '').strip();parsed=urlparse(observed_url)
   match=re.fullmatch(r'/jobs/view/(\d+)/?',parsed.path)
   if (parsed.hostname or '').lower() not in {'linkedin.com','www.linkedin.com'} or not match:
    raise ValueError('observed canonical LinkedIn job URL is required')
   jid=match.group(1)
   if rb.get('job_id') is not None and str(rb.get('job_id')) != jid:
    raise ValueError('read-back job id conflicts with observed URL')
   observed_title=str(rb.get('observed_title') or '').strip()
   observed_company=str(rb.get('observed_company') or '').strip()
   if not observed_title or not observed_company:
    raise ValueError('observed LinkedIn title and company are required')
   payload=f.execution_intent_payload(intent_id=x.intent_id,execution_token=x.token)
   if payload.get('source')!='linkedin' or str(payload.get('external_id'))!=jid: raise ValueError('read-back job identity does not match intent')
   evidence=Path(x.evidence)
   if not evidence.is_file() or not evidence.read_bytes(): raise ValueError('read-back evidence is required')
   rid=f.record_application(source='linkedin',external_vacancy_id=jid,job_url=observed_url,company=observed_company,job_title=observed_title,status='submitted',submitted_at=now,read_back_verified=True,evidence_path=str(evidence.resolve()),intent_id=x.intent_id,execution_token=x.token)
   result={'state':'verified','receipt_id':rid}
 print(json.dumps(result,sort_keys=True));return 0
if __name__=='__main__':raise SystemExit(main())
