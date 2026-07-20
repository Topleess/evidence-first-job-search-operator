from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
CORE_SRC = Path("/opt/data/job-funnel-public/src")
for p in (SCRIPTS, CORE_SRC):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


def load(name):
    path = SCRIPTS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec); sys.modules[name] = module
    spec.loader.exec_module(module); return module


def snapshot(fields, url="https://jobs.example.com/acme/pm/application"):
    reduced=[{"label":f.get("label",""),"type":f.get("type",""),"required":bool(f.get("required",False)),"options":f.get("options") or []} for f in fields]
    fp=hashlib.sha256(json.dumps(reduced,ensure_ascii=False,separators=(",", ":")).encode()).hexdigest()
    return {"schema_version":"ats_form_snapshot.v1","source_url":url,"form_fingerprint":fp,"fields":fields}


def args(funnel, run_id, snap, resume, package):
    return dict(funnel=funnel,run_id=run_id,snapshot=snap,answer_map={"full_name":"Candidate","email":"candidate@example.com","resume":str(resume)},source="ats",external_id="acme-pm",job_url="https://jobs.example.com/acme/pm",company="Acme",job_title="Product Manager",resume_path=resume,package_path=package,now="2026-07-16T12:00:01+00:00")


def test_blocked_or_tampered_snapshot_never_reserves(tmp_path):
    prep=load("ats_intent_preparer"); local=load("local_funnel")
    resume=tmp_path/"cv.docx"; resume.write_bytes(b"cv")
    package=tmp_path/"package.json"; package.write_text("{}")
    db=tmp_path/"f.sqlite3"
    with local.LocalFunnel(db) as f:
        run=f.begin_batch_run(channel="ats",max_actions=2,started_at="2026-07-16T12:00:00+00:00")
        blocked=snapshot([{"id":"q","label":"Visa sponsorship?","type":"choice","required":True,"options":["Yes","No"]}])
        with pytest.raises(prep.ATSPreparationBlocked): prep.prepare_ats_intent(**args(f,run,blocked,resume,package))
        ready=snapshot([{"id":"name","label":"Name","type":"text","required":True}]); ready["fields"][0]["label"]="Changed"
        with pytest.raises(prep.ATSPreparationBlocked): prep.prepare_ats_intent(**args(f,run,ready,resume,package))
    import sqlite3
    assert sqlite3.connect(db).execute("select count(*) from action_intents").fetchone()[0] == 0


def test_ready_intent_is_restart_idempotent_and_artifact_bound(tmp_path):
    prep=load("ats_intent_preparer"); local=load("local_funnel")
    resume=tmp_path/"cv.docx"; resume.write_bytes(b"cv")
    package=tmp_path/"package.json"; package.write_text("{}")
    db=tmp_path/"f.sqlite3"; snap=snapshot([{"id":"name","label":"Name","type":"text","required":True},{"id":"email","label":"Email","type":"email","required":True},{"id":"resume","label":"Resume","type":"file","required":True}])
    with local.LocalFunnel(db) as f:
        run=f.begin_batch_run(channel="ats",max_actions=2,started_at="2026-07-16T12:00:00+00:00")
        first=prep.prepare_ats_intent(**args(f,run,snap,resume,package))
    with local.LocalFunnel(db) as f:
        replay=prep.prepare_ats_intent(**args(f,run,snap,resume,package))
        resume.write_bytes(b"changed")
        with pytest.raises(local.ActionIntentConflict): prep.prepare_ats_intent(**args(f,run,snap,resume,package))
    assert first.created is True and replay.created is False and first.intent_id == replay.intent_id
