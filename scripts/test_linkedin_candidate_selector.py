import json,sqlite3
from linkedin_candidate_selector import select_candidates
from local_funnel import LocalFunnel

def add(db,eid,meta):
 con=sqlite3.connect(db);con.execute("INSERT INTO jobs(source,external_id,title,company,url,location,description,metadata) VALUES('linkedin',?,'AI Product Manager','C',?,'Remote','full description',?)",(eid,f'https://www.linkedin.com/jobs/view/{eid}/',json.dumps(meta)));con.commit();con.close()

def valid(): return {'hard_eligible':True,'geo_eligible':True,'salary_eligible':True,'easy_apply':True,'fresh_verified':True,'eligibility_evidence_path':'state/evidence/1.json','search_url':'https://www.linkedin.com/jobs/search/?keywords=AI'}

def test_selector_fail_closed_and_dedupes_receipts_and_intents(tmp_path):
 db=tmp_path/'f.sqlite3'
 with LocalFunnel(db): pass
 add(db,'1',valid());bad=valid();bad['salary_eligible']=None;add(db,'2',bad)
 assert [x.external_id for x in select_candidates(db)]==['1']
 with LocalFunnel(db) as f:
  f.record_application(source='linkedin',external_vacancy_id='1',job_url='https://www.linkedin.com/jobs/view/1/',company='C',job_title='PM',status='submitted',submitted_at='2026-07-19T20:00:00+00:00',read_back_verified=True,evidence_path='e')
 assert select_candidates(db)==[]
 add(db,'3',valid())
 with LocalFunnel(db) as f:
  run=f.begin_batch_run(channel='linkedin',max_actions=5,started_at='2026-07-19T20:00:00+00:00');f.reserve_action_intent(run_id=run,kind='application_submit',idempotency_key='linkedin:3:application',payload={'source':'linkedin','external_id':'3'},now='2026-07-19T20:00:01+00:00')
 assert select_candidates(db)==[]

def test_selector_caps_at_channel_ceiling(tmp_path):
 db=tmp_path/'f.sqlite3'
 with LocalFunnel(db): pass
 for i in range(8): add(db,str(10+i),valid())
 assert len(select_candidates(db,limit=5))==5
