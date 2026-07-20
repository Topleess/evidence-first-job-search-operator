import importlib.util,sys
from pathlib import Path
import pytest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from linkedin_easy_apply_adapter import FieldSpec,KnownProfile
from linkedin_intent_preparer import prepare_linkedin_intent,LinkedInPreparationBlocked
from local_funnel import LocalFunnel,ActionIntentConflict

def test_linkedin_preparer_uses_authoritative_intents_and_dedupes(tmp_path):
 db=tmp_path/'f.sqlite3'; resume=tmp_path/'resume.docx'; package=tmp_path/'package.json'; resume.write_bytes(b'cv'); package.write_text('{}')
 fields=[FieldSpec('email','Email','text',True),FieldSpec('choice','Claude Code experience','choice',True,options=('Yes','No'))]
 import hashlib,json
 body=json.dumps([{"key":f.key,"label":f.label,"kind":f.kind,"required":f.required,"value":f.value,"options":list(f.options)} for f in fields],ensure_ascii=False,sort_keys=True,separators=(',',':'))
 fp=hashlib.sha256(body.encode()).hexdigest(); profile=KnownProfile('A','B','a@example.com','+1')
 with LocalFunnel(db) as f:
  run=f.begin_batch_run(channel='linkedin',max_actions=1,started_at='2026-07-16T20:00:00+00:00')
  kw=dict(funnel=f,run_id=run,job_id='123',job_url='https://www.linkedin.com/jobs/view/role-123/',fields=fields,form_fingerprint=fp,profile=profile,answer_map={'choice':'Yes'},resume_path=resume,package_path=package,company='C',job_title='PM',now='2026-07-16T20:00:01+00:00')
  one=prepare_linkedin_intent(**kw); two=prepare_linkedin_intent(**kw)
  assert one.created is True and two.created is False and one.intent_id==two.intent_id
  assert f.get_action_intent(intent_id=one.intent_id)['payload']['planned_fills']['choice']=='Yes'


def test_linkedin_preparer_blocks_authoritative_readback_receipt_before_reserve(tmp_path):
 db=tmp_path/'f.sqlite3'; r=tmp_path/'r'; p=tmp_path/'p'; r.write_text('r'); p.write_text('p')
 field=FieldSpec('email','Email','text',True)
 import hashlib,json
 body=json.dumps([{"key":field.key,"label":field.label,"kind":field.kind,"required":field.required,"value":field.value,"options":list(field.options)}],ensure_ascii=False,sort_keys=True,separators=(',',':')); fp=hashlib.sha256(body.encode()).hexdigest()
 with LocalFunnel(db) as f:
  f.record_application(source='linkedin',external_vacancy_id='4440756817',job_url='https://www.linkedin.com/jobs/view/4440756817/',company='C',job_title='PM',status='submitted',submitted_at='2026-07-16T20:00:00+00:00',read_back_verified=True,evidence_path='evidence/linkedin.txt')
  run=f.begin_batch_run(channel='linkedin',max_actions=1,started_at='2026-07-19T20:00:00+00:00')
  with pytest.raises(LinkedInPreparationBlocked,match='verified receipt already exists'):
   prepare_linkedin_intent(funnel=f,run_id=run,job_id='4440756817',job_url='https://www.linkedin.com/jobs/view/4440756817/',fields=[field],form_fingerprint=fp,profile=KnownProfile('A','B','a@b.c','1'),answer_map={},resume_path=r,package_path=p,company='C',job_title='PM',now='2026-07-19T20:00:01+00:00')
  assert f.has_verified_application_receipt(source='linkedin',external_id='4440756817')


def test_linkedin_preparer_blocks_unknown_or_invalid_choice(tmp_path):
 db=tmp_path/'f.sqlite3'; r=tmp_path/'r'; p=tmp_path/'p'; r.write_text('r'); p.write_text('p')
 field=FieldSpec('choice','Required choice','choice',True,options=('Yes','No'))
 import hashlib,json
 body=json.dumps([{"key":field.key,"label":field.label,"kind":field.kind,"required":field.required,"value":field.value,"options":list(field.options)}],ensure_ascii=False,sort_keys=True,separators=(',',':')); fp=hashlib.sha256(body.encode()).hexdigest()
 with LocalFunnel(db) as f:
  run=f.begin_batch_run(channel='linkedin',max_actions=1,started_at='2026-07-16T20:00:00+00:00')
  with pytest.raises(LinkedInPreparationBlocked): prepare_linkedin_intent(funnel=f,run_id=run,job_id='123',job_url='https://www.linkedin.com/jobs/view/123/',fields=[field],form_fingerprint=fp,profile=KnownProfile('A','B','a@b.c','1'),answer_map={'choice':'Maybe'},resume_path=r,package_path=p,company='C',job_title='PM',now='2026-07-16T20:00:01+00:00')
