import json,sqlite3,subprocess,sys
from pathlib import Path
from local_funnel import LocalFunnel

SCRIPT=Path(__file__).with_name('linkedin_cron_runner.py')
def test_linkedin_runner_attributes_bounded_dry_run_without_side_effect(tmp_path):
 db=tmp_path/'f.sqlite3'
 with LocalFunnel(db): pass
 meta={'hard_eligible':True,'geo_eligible':True,'salary_eligible':True,'easy_apply':True,'fresh_verified':True,'eligibility_evidence_path':'e.json','search_url':'https://www.linkedin.com/jobs/search/?keywords=AI'}
 c=sqlite3.connect(db);c.execute("insert into jobs(source,external_id,title,company,url,location,description,metadata) values('linkedin','123','AI PM','C','https://www.linkedin.com/jobs/view/123/','Remote','full',?)",(json.dumps(meta),));c.commit();c.close()
 p=subprocess.run([sys.executable,str(SCRIPT),'--db',str(db),'--limit','1'],text=True,capture_output=True,check=True);out=json.loads(p.stdout)
 assert out['candidate_count']==1 and out['results']==[{'job_id':'123','status':'dry_run_ready'}]
 c=sqlite3.connect(db);assert c.execute('select channel,state,max_actions from batch_runs where id=?',(out['run_id'],)).fetchone()==('linkedin','completed',1)
 assert c.execute('select count(*) from action_intents').fetchone()[0]==0
