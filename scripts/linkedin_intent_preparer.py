#!/usr/bin/env python3
"""Prepare a fingerprint-locked LinkedIn Easy Apply intent; never submits."""
from __future__ import annotations
import hashlib, json, re
from pathlib import Path
from typing import Any
from linkedin_easy_apply_adapter import FieldSpec, FormClassifier, KnownProfile

class LinkedInPreparationBlocked(RuntimeError): pass

def _fingerprint(fields:list[FieldSpec])->str:
    body=json.dumps([{"key":f.key,"label":f.label,"kind":f.kind,"required":f.required,"value":f.value,"options":list(f.options)} for f in fields],ensure_ascii=False,sort_keys=True,separators=(",",":"))
    return hashlib.sha256(body.encode()).hexdigest()

def _sha(path:str|Path)->tuple[str,str]:
    p=Path(path).resolve()
    if not p.is_file(): raise LinkedInPreparationBlocked(f"artifact missing: {p}")
    return str(p),hashlib.sha256(p.read_bytes()).hexdigest()

def prepare_linkedin_intent(*,funnel:Any,run_id:str,job_id:str,job_url:str,fields:list[FieldSpec],form_fingerprint:str,profile:KnownProfile,answer_map:dict[str,str],resume_path:str|Path,package_path:str|Path,company:str,job_title:str,now:str):
    jid=str(job_id).strip()
    if not re.fullmatch(r"https://(?:www\.)?linkedin\.com/jobs/view/(?:[^/?#]*-)?"+re.escape(jid)+r"/?(?:[?#].*)?",job_url):
        raise LinkedInPreparationBlocked("job id/url mismatch")
    if getattr(funnel, "has_verified_application_receipt", lambda **_: False)(source="linkedin", external_id=jid):
        raise LinkedInPreparationBlocked("authoritative verified receipt already exists")
    observed=_fingerprint(fields)
    if observed!=form_fingerprint: raise LinkedInPreparationBlocked("form fingerprint mismatch")
    plan=FormClassifier(profile,answer_map).classify(fields)
    if not plan.ready_for_review:
        raise LinkedInPreparationBlocked("required blockers: "+" | ".join(f.label for f in plan.blockers))
    resume,resume_sha=_sha(resume_path); package,package_sha=_sha(package_path)
    payload={"source":"linkedin","external_id":jid,"job_url":f"https://www.linkedin.com/jobs/view/{jid}/","company":company,"job_title":job_title,"form_fingerprint":observed,"resume_path":resume,"resume_sha256":resume_sha,"package_path":package,"package_sha256":package_sha,"planned_fills":plan.fills,"answer_provenance_required":True}
    return funnel.reserve_action_intent(run_id=run_id,kind="application_submit",idempotency_key=f"linkedin:{jid}:application",payload=payload,now=now)
