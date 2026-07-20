#!/usr/bin/env python3
"""Run configured public LinkedIn guest searches into Vacancies Inbox."""
from __future__ import annotations
import argparse, json, subprocess, sys
from pathlib import Path
BASE=Path('/opt/data/job-search')
CONFIG=BASE/'config/linkedin_public_sources.json'

def run_collector(cmd:list[str])->dict:
    """Run a collector, preserving its diagnostic stream and structured status."""
    proc=subprocess.run(cmd,cwd=str(BASE),text=True,capture_output=True)
    if proc.stderr:
        sys.stderr.write(proc.stderr)
        sys.stderr.flush()
    try:
        payload=json.loads(proc.stdout)
    except (json.JSONDecodeError, TypeError):
        payload=None
    valid_statuses={'success','degraded','error'}
    parsed_status=payload.get('status') if isinstance(payload,dict) else None
    if parsed_status not in valid_statuses:
        status='error'
    elif proc.returncode == 0:
        status=parsed_status
    elif parsed_status == 'degraded' and proc.returncode == 1:
        status='degraded'
    else:
        status='error'
    return {'status':status,'exit_code':proc.returncode,'stdout':proc.stdout,'stderr':proc.stderr}

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--config',type=Path,default=CONFIG); ap.add_argument('--dry-run',action='store_true'); ap.add_argument('--max-sources',type=int,default=0); args=ap.parse_args()
    cfg=json.loads(args.config.read_text())
    urls=[]; names=[]
    for s in cfg.get('sources',[]):
        if not s.get('enabled',True): continue
        if s.get('type')!='search_url': continue
        url=str(s.get('url') or '').strip()
        if not url: continue
        urls.append(url); names.append(s.get('name',''))
    if args.max_sources: urls=urls[:args.max_sources]
    if not urls:
        print(json.dumps({
            'status':'error',
            'urls':0,
            'error':'no enabled LinkedIn search URLs',
        }))
        return 2
    cmd=[sys.executable,str(BASE/'scripts/collect_linkedin_public_to_sheets.py')]
    if not args.dry_run: cmd.append('--write-sheet')
    cmd+=urls
    result=run_collector(cmd)
    print(json.dumps({'dry_run':args.dry_run,'sources':names[:len(urls)],'status':result['status'],'exit_code':result['exit_code'],'stdout':result['stdout'][-2000:],'stderr':result['stderr'][-2000:]},ensure_ascii=False,indent=2))
    return {'success': 0, 'degraded': 1, 'error': 2}[result['status']]
if __name__=='__main__': raise SystemExit(main())
