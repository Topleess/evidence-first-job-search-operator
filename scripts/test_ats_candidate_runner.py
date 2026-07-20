import json,sqlite3,subprocess,sys
from pathlib import Path
from ats_candidate_selector import select_ats_candidate
from local_funnel import LocalFunnel
RUNNER=Path(__file__).with_name('ats_cron_runner.py')
def add(db,meta,source='ats_ashby'):
 c=sqlite3.connect(db);c.execute('insert into jobs(source,external_id,title,company,url,location,description,metadata) values(?,?,?,?,?,?,?,?)',(source,'x1','AI Product Manager','C','https://jobs.ashbyhq.com/c/uuid','Remote','full',json.dumps(meta)));c.commit();c.close()
def valid():return {'hard_eligible':True,'geo_eligible':True,'salary_eligible':True,'fresh_verified':True,'eligibility_evidence_path':'e','form_snapshot_path':'s','answer_map_path':'a','resume_path':'r','package_path':'p'}
def test_ats_selector_and_bounded_dry_runner(tmp_path):
 db=tmp_path/'f.sqlite3'
 with LocalFunnel(db):pass
 bad=valid();bad['geo_eligible']=None;add(db,bad);assert select_ats_candidate(db) is None
 c=sqlite3.connect(db);c.execute('delete from jobs');c.commit();c.close();add(db,valid());candidate=select_ats_candidate(db);assert candidate is not None and candidate.external_id=='x1'
 p=subprocess.run([sys.executable,str(RUNNER),'--db',str(db)],text=True,capture_output=True,check=True);out=json.loads(p.stdout)
 assert out['candidate_count']==1 and out['results'][0]['status']=='dry_run_ready'
 c=sqlite3.connect(db);assert c.execute('select channel,state,max_actions from batch_runs where id=?',(out['run_id'],)).fetchone()==('ats','completed',1)
 assert c.execute('select count(*) from action_intents').fetchone()[0]==0
