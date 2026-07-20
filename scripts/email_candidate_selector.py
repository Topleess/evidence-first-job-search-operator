#!/usr/bin/env python3
"""Fail-closed selector for vacancy-specific HR email outreach."""
from __future__ import annotations
import json,sqlite3
from dataclasses import dataclass
from pathlib import Path

@dataclass(frozen=True)
class EmailCandidate:
 queue_id:int;payload:dict

ALLOWED_PROVENANCE={'vacancy_description','vacancy_contact_section','company_ats_vacancy_contact'}

def select_email_candidates(db_path:str|Path,limit:int=5)->list[EmailCandidate]:
 if isinstance(limit,bool) or not isinstance(limit,int) or not 1<=limit<=5: raise ValueError('email limit must be 1..5')
 con=sqlite3.connect(db_path);con.row_factory=sqlite3.Row;out=[]
 try:
  for row in con.execute("SELECT id,payload FROM queue WHERE kind='email_outreach_draft' AND state='pending' ORDER BY id"):
   try:p=json.loads(row['payload'])
   except (TypeError,json.JSONDecodeError):continue
   required=[p.get('recipient_verified') is True,p.get('recipient_provenance') in ALLOWED_PROVENANCE,bool(str(p.get('recipient_evidence_path') or '').strip()),p.get('eligibility',{}).get('eligible') is True,bool(str(p.get('eligibility',{}).get('evidence') or '').strip()),bool(str(p.get('sender') or '').strip()),bool(str(p.get('recipient') or '').strip()),bool(str(p.get('subject') or '').strip()),bool(str(p.get('body') or '').strip()),bool(str(p.get('message_evidence_path') or '').strip())]
   if not all(required):continue
   vacancy_key=f"{p.get('source')}:{p.get('external_id')}"
   active=con.execute("SELECT 1 FROM action_intents WHERE kind IN ('email_send','email_followup') AND state IN ('reserved','executing','ambiguous','verified') AND json_extract(payload,'$.vacancy_key')=? AND lower(json_extract(payload,'$.recipient'))=? LIMIT 1",(vacancy_key,str(p['recipient']).lower())).fetchone()
   if active:continue
   out.append(EmailCandidate(int(row['id']),p))
   if len(out)>=limit:break
  return out
 finally:con.close()
