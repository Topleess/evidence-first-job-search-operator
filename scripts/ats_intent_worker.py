#!/usr/bin/env python3
"""Execute one reserved ATS intent with a two-phase browser handoff."""
from __future__ import annotations
import argparse, json, os, subprocess, uuid
from datetime import datetime, timezone
from pathlib import Path
from local_funnel import LocalFunnel, IntentFenceViolation

ROOT=Path(__file__).resolve().parents[1]
DB=ROOT/'state/job_funnel.sqlite3'

def now(): return datetime.now(timezone.utc).isoformat()

def main()->int:
    ap=argparse.ArgumentParser(); ap.add_argument('--intent-id',type=int,required=True); args=ap.parse_args()
    with LocalFunnel(DB) as funnel: intent=funnel.get_action_intent(intent_id=args.intent_id)
    if intent['state']!='reserved': raise IntentFenceViolation(f"intent state is {intent['state']}, expected reserved")
    payload=intent['payload']; private=ROOT/'state/private/ats_execution'; private.mkdir(parents=True,exist_ok=True)
    payload_path=private/f'intent-{args.intent_id}.json'; payload_path.write_text(json.dumps(payload,ensure_ascii=False)); payload_path.chmod(0o600)
    evidence=ROOT/'state/evidence/ats'/f"intent-{args.intent_id}"
    proc=subprocess.Popen(['node',str(ROOT/'scripts/ats_browser_execute.js'),str(payload_path),str(evidence)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,bufsize=1,cwd=ROOT)
    first=proc.stdout.readline().strip() if proc.stdout else ''
    try: event=json.loads(first)
    except Exception:
        proc.kill(); stderr=proc.stderr.read() if proc.stderr else ''; print(json.dumps({'status':'pre_submit_failed','raw':first,'stderr':stderr[-1000:]})); return 2
    if event.get('phase')!='ready':
        proc.wait(timeout=20); print(json.dumps({'status':'pre_submit_failed','event':event},ensure_ascii=False)); return 2
    worker=f'ats-worker-{uuid.uuid4().hex[:10]}'
    with LocalFunnel(DB) as funnel: token=funnel.mark_intent_executing(intent_id=args.intent_id,worker_id=worker,now=now())
    assert proc.stdin is not None; proc.stdin.write(f'GO {token}\n'); proc.stdin.flush(); proc.stdin.close()
    second=proc.stdout.readline().strip() if proc.stdout else ''; rc=proc.wait(timeout=90)
    try: result=json.loads(second)
    except Exception: result={'phase':'error','submit_clicked':True,'error':'invalid_worker_result','raw':second}
    evidence_path=str(evidence/'result.json')
    if rc==0 and result.get('confirmed') is True:
        with LocalFunnel(DB) as funnel:
            receipt=funnel.record_application(source=payload['source'],external_vacancy_id=payload['external_id'],job_url=payload['job_url'],company=payload['company'],job_title=payload['job_title'],status='submitted',submitted_at=result.get('at') or now(),read_back_verified=True,evidence_path=evidence_path,intent_id=args.intent_id,execution_token=token)
            funnel.finish_batch_run(run_id=intent['run_id'],state='completed',reason='one verified ATS submission',now=now())
        print(json.dumps({'status':'verified','intent_id':args.intent_id,'receipt_id':receipt,'readback':result,'evidence':evidence_path},ensure_ascii=False)); return 0
    with LocalFunnel(DB) as funnel:
        if result.get('submit_clicked') is False:
            funnel.close_execution_blocked(intent_id=args.intent_id,execution_token=token,now=now(),error_code='ats_pre_side_effect_blocked')
            funnel.finish_batch_run(run_id=intent['run_id'],state='failed',reason='ATS blocked before submit side effect',now=now())
            print(json.dumps({'status':'blocked_pre_side_effect','intent_id':args.intent_id,'worker_rc':rc,'result':result,'evidence':evidence_path},ensure_ascii=False)); return 2
        funnel.mark_intent_ambiguous(intent_id=args.intent_id,execution_token=token,now=now(),error_code='submit_readback_unconfirmed')
        funnel.finish_batch_run(run_id=intent['run_id'],state='failed',reason='submit clicked but read-back unconfirmed',now=now())
    print(json.dumps({'status':'ambiguous','intent_id':args.intent_id,'worker_rc':rc,'result':result,'evidence':evidence_path},ensure_ascii=False)); return 3

if __name__=='__main__': raise SystemExit(main())
