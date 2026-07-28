import json,sqlite3,subprocess,sys
from pathlib import Path
from local_funnel import LocalFunnel

SCRIPT=Path(__file__).with_name('linkedin_cron_runner.py')
def test_linkedin_runner_attributes_bounded_dry_run_without_side_effect(tmp_path):
 db=tmp_path/'f.sqlite3'; executor=tmp_path/'dry-run-executor.js'
 with LocalFunnel(db): pass
 meta={'hard_eligible':True,'geo_eligible':True,'salary_eligible':True,'easy_apply':True,'fresh_verified':True,'eligibility_evidence_path':'e.json','search_url':'https://www.linkedin.com/jobs/search/?keywords=AI'}
 c=sqlite3.connect(db);c.execute("insert into jobs(source,external_id,title,company,url,location,description,metadata) values('linkedin','123','AI PM','C','https://www.linkedin.com/jobs/view/123/','Remote','full',?)",(json.dumps(meta),));c.commit();c.close()
 executor.write_text("process.stdout.write(JSON.stringify({status:'dry_run_ready',args:process.argv.slice(2)})+'\\n');\n")
 p=subprocess.run([sys.executable,str(SCRIPT),'--db',str(db),'--limit','1','--executor',str(executor)],text=True,capture_output=True,check=True);out=json.loads(p.stdout)
 assert out['candidate_count']==1 and out['results'][0]['job_id']=='123'
 child=json.loads(out['results'][0]['stdout'])
 assert child['status']=='dry_run_ready' and '--dry-run' in child['args']
 c=sqlite3.connect(db);assert c.execute('select channel,state,max_actions from batch_runs where id=?',(out['run_id'],)).fetchone()==('linkedin','completed',1)
 assert c.execute('select count(*) from action_intents').fetchone()[0]==0


def test_linkedin_runner_fails_batch_when_executor_exits_nonzero(tmp_path):
 db=tmp_path/'f.sqlite3'; executor=tmp_path/'failing-executor.js'
 with LocalFunnel(db): pass
 evidence=tmp_path/'eligibility.json'; evidence.write_text('{}')
 meta={'hard_eligible':True,'geo_eligible':True,'salary_eligible':True,'easy_apply':True,'fresh_verified':True,'eligibility_evidence_path':str(evidence),'search_url':'https://www.linkedin.com/jobs/search/?keywords=AI'}
 c=sqlite3.connect(db);c.execute("insert into jobs(source,external_id,title,company,url,location,description,metadata) values('linkedin','456','AI PM','C','https://www.linkedin.com/jobs/view/456/','Remote','full',?)",(json.dumps(meta),));c.commit();c.close()
 executor.write_text("process.stderr.write('executor failed safely'); process.exit(2);\n")
 p=subprocess.run([sys.executable,str(SCRIPT),'--db',str(db),'--limit','1','--execute','--executor',str(executor)],text=True,capture_output=True)
 assert p.returncode==1
 out=json.loads(p.stdout)
 assert out['state']=='failed'
 assert out['results'][0]['exit_code']==2
 c=sqlite3.connect(db)
 assert c.execute('select state from batch_runs where id=?',(out['run_id'],)).fetchone()==('failed',)
 assert c.execute('select count(*) from application_receipts').fetchone()[0]==0
