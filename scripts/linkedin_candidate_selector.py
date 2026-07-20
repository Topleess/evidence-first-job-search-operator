#!/usr/bin/env python3
"""Fail-closed LinkedIn production candidate selection from authoritative jobs."""
from __future__ import annotations
import json,sqlite3
from dataclasses import dataclass
from pathlib import Path

@dataclass(frozen=True)
class LinkedInCandidate:
 job_id:int;external_id:str;title:str;company:str;url:str;search_url:str

REQUIRED_TRUE=('hard_eligible','geo_eligible','salary_eligible','easy_apply','fresh_verified')

def select_candidates(db_path:str|Path,limit:int=5)->list[LinkedInCandidate]:
 if isinstance(limit,bool) or not isinstance(limit,int) or not 1<=limit<=5: raise ValueError('LinkedIn limit must be 1..5')
 con=sqlite3.connect(db_path);con.row_factory=sqlite3.Row;out=[]
 try:
  rows=con.execute("SELECT id,external_id,title,company,url,metadata FROM jobs WHERE source='linkedin' ORDER BY id DESC").fetchall()
  for row in rows:
   try: meta=json.loads(row['metadata'] or '{}')
   except json.JSONDecodeError: continue
   if any(meta.get(k) is not True for k in REQUIRED_TRUE): continue
   if not str(meta.get('eligibility_evidence_path') or '').strip(): continue
   exists=con.execute("SELECT 1 FROM application_receipts WHERE source='linkedin' AND external_vacancy_id=? AND read_back_verified=1 LIMIT 1",(str(row['external_id']),)).fetchone()
   if exists: continue
   active=con.execute("SELECT 1 FROM action_intents WHERE kind='application_submit' AND state IN ('reserved','executing','ambiguous','verified') AND json_extract(payload,'$.source')='linkedin' AND json_extract(payload,'$.external_id')=? LIMIT 1",(str(row['external_id']),)).fetchone()
   if active: continue
   search_url=str(meta.get('search_url') or '').strip()
   if not search_url.startswith('https://www.linkedin.com/jobs/search/'): continue
   out.append(LinkedInCandidate(int(row['id']),str(row['external_id']),str(row['title']),str(row['company']),str(row['url']),search_url))
   if len(out)>=limit: break
  return out
 finally: con.close()
