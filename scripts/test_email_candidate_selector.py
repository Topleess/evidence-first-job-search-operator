import json,sqlite3
from email_candidate_selector import select_email_candidates
from local_funnel import LocalFunnel

def enqueue(db,payload):
 c=sqlite3.connect(db);c.execute("insert into queue(kind,payload,idempotency_key,state,available_at,attempts,max_attempts) values('email_outreach_draft',?,?,'pending','2026-07-19T00:00:00+00:00',0,1)",(json.dumps(payload),payload['external_id']));c.commit();c.close()

def good():return {'source':'ats','external_id':'1','sender':'a@example.com','recipient':'hr@example.com','recipient_verified':True,'recipient_provenance':'vacancy_description','recipient_evidence_path':'recipient.json','eligibility':{'eligible':True,'evidence':'eligibility.json'},'subject':'Application','body':'Truthful body','message_evidence_path':'message.json'}

def test_email_selector_requires_vacancy_specific_provenance_and_content(tmp_path):
 db=tmp_path/'f.sqlite3'
 with LocalFunnel(db):pass
 bad=good();bad['recipient_provenance']='company_website';enqueue(db,bad);assert select_email_candidates(db)==[]
 c=sqlite3.connect(db);c.execute('delete from queue');c.commit();c.close();enqueue(db,good());assert len(select_email_candidates(db))==1
