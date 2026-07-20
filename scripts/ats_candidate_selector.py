#!/usr/bin/env python3
"""Fail-closed selector for public ATS application candidates."""
from __future__ import annotations
import json,sqlite3
from dataclasses import dataclass
from pathlib import Path

@dataclass(frozen=True)
class ATSCandidate:
 job_id:int;source:str;external_id:str;title:str;company:str;url:str;metadata:dict

def select_ats_candidate(db_path:str|Path)->ATSCandidate|None:
 con=sqlite3.connect(db_path);con.row_factory=sqlite3.Row
 try:
  for row in con.execute("SELECT id,source,external_id,title,company,url,metadata FROM jobs WHERE source LIKE 'ats_%' ORDER BY id DESC"):
   try:m=json.loads(row['metadata'] or '{}')
   except json.JSONDecodeError:continue
   if row['source']!='ats_ashby':continue
   if any(m.get(k) is not True for k in ('hard_eligible','geo_eligible','salary_eligible','fresh_verified')):continue
   required=('eligibility_evidence_path','form_snapshot_path','answer_map_path','resume_path','package_path')
   if any(not str(m.get(k) or '').strip() for k in required):continue
   if not str(row['url']).startswith('https://jobs.ashbyhq.com/'):continue
   dup=con.execute("SELECT 1 FROM application_receipts WHERE source=? AND external_vacancy_id=? AND read_back_verified=1 LIMIT 1",(row['source'],str(row['external_id']))).fetchone()
   if dup:continue
   active=con.execute("SELECT 1 FROM action_intents WHERE kind='application_submit' AND state IN ('reserved','executing','ambiguous','verified') AND json_extract(payload,'$.source')=? AND json_extract(payload,'$.external_id')=? LIMIT 1",(row['source'],str(row['external_id']))).fetchone()
   if active:continue
   return ATSCandidate(int(row['id']),str(row['source']),str(row['external_id']),str(row['title']),str(row['company']),str(row['url']),m)
  return None
 finally:con.close()
