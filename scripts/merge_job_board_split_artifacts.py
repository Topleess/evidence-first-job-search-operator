#!/usr/bin/env python3
"""Merge the two route-specific job-board artifacts for authoritative sync."""
from __future__ import annotations
import json
from datetime import datetime,timezone
from pathlib import Path
root=Path('/opt/data/job-search/data/job_boards')
files=sorted(root.glob('vacancies_*.json'),key=lambda p:p.stat().st_mtime,reverse=True)
remotive=other=None
for path in files[:20]:
 try:rows=json.loads(path.read_text())
 except Exception:continue
 sources={str(r.get('source')) for r in rows if isinstance(r,dict)}
 if sources=={'remotive'} and remotive is None:remotive=rows
 elif sources and 'remotive' not in sources and other is None:other=rows
 if remotive is not None and other is not None:break
if remotive is None or other is None:raise SystemExit('split artifacts not found')
merged=[];seen=set()
for row in [*remotive,*other]:
 key=(row.get('source'),row.get('job_url'))
 if key in seen:continue
 seen.add(key);merged.append(row)
stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ');target=root/f'vacancies_{stamp}.json';target.write_text(json.dumps(merged,ensure_ascii=False,indent=2));print(json.dumps({'status':'success','merged':len(merged),'json':str(target)}))
