#!/usr/bin/env python3
"""Collect public ATS jobs (Ashby, Greenhouse, Lever) into Vacancies Inbox.

Collection-only. No applications or external messages.
"""
from __future__ import annotations
import argparse, html, json, re, urllib.request, urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

BASE=Path('/opt/data/job-search'); CONFIG=BASE/'config/ats_sources.json'; DATA=BASE/'data/ats'
SHEET_ID='1O_tUG4FsSkNOrpMTQ4savhiQjQfyZWdocPc7EgbikDI'; SHEET='Vacancies Inbox'
COLUMNS=["status","source","search_filter","job_title","company","salary","remote_location","published_at","job_url","company_website","careers_page","company_linkedin","hh_company_profile","recruiter_name","recruiter_role","recruiter_linkedin","recruiter_hh_profile","hr_email","hr_phone","contact_source","fit_score","why_relevant","next_action","enrichment_status","notes"]
TARGET=['product manager','product owner','product lead','product operations','head of product','program manager','project manager','operations','business operations','strategy','chief of staff','founder','automation','customer success manager','implementation manager','solutions consultant','delivery manager']
EXCLUDE=['engineer','developer','designer','account executive','sales development','sales manager','account director','regional sales','recruiter','counsel','legal','finance','security engineer','data scientist','scientist','researcher','marketing','nurse']

def clean(x):
    return re.sub(r'\s+',' ',html.unescape(str(x or ''))).strip()

def fetch_json(url):
    req=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0 (Hermes personal job search collector)','Accept':'application/json'})
    with urllib.request.urlopen(req,timeout=40) as r:
        return json.loads(r.read().decode('utf-8','replace'))

def canonical(url):
    try:
        p=urllib.parse.urlparse(url); qs=[(k,v) for k,v in urllib.parse.parse_qsl(p.query) if not k.startswith('utm_')]
        return urllib.parse.urlunparse((p.scheme,p.netloc.lower(),p.path.rstrip('/'),'',urllib.parse.urlencode(qs),''))
    except Exception: return url

def score(title, dept='', loc='', text=''):
    low=' '.join([title,dept,loc,text]).lower(); pts=35; why=[]
    if any(x in low for x in ['product manager','product lead','head of product','product operations','product owner']): pts+=40; why.append('product')
    elif 'product' in low: pts+=25; why.append('product_related')
    if any(x in low for x in ['program manager','project manager','delivery','implementation']): pts+=20; why.append('project/program')
    if any(x in low for x in ['business operations','operations','strategy','chief of staff','founder']): pts+=20; why.append('ops/strategy')
    if any(x in low for x in ['ai','llm','automation','agent']): pts+=15; why.append('ai/automation')
    if any(x in low for x in ['remote','emea','europe','worldwide']): pts+=10; why.append('remote/emea')
    if any(x in low for x in EXCLUDE): pts-=35; why.append('off_target_risk')
    pts=max(0,min(100,pts))
    action='review_for_draft' if pts>=75 else 'volume_apply_review' if pts>=55 else 'skip_likely'
    return pts, ', '.join(dict.fromkeys(why)) or 'ats_keyword_match', action

def ashby(src):
    url=f"https://api.ashbyhq.com/posting-api/job-board/{src['slug']}?includeCompensation=true"; data=fetch_json(url); out=[]
    for j in data.get('jobs',[]):
        title=clean(j.get('title')); loc=clean(', '.join([l.get('name','') for l in j.get('locationNames') or []]) or j.get('locationName',''))
        dept=clean(j.get('departmentName','')); desc=clean(j.get('descriptionPlain') or j.get('descriptionHtml',''))[:20000]
        job_url=canonical(j.get('jobUrl') or f"https://jobs.ashbyhq.com/{src['slug']}/{j.get('id','')}")
        comp=j.get('compensation') or {}; salary=clean(comp.get('compensationTierSummary') or '')
        out.append((title,dept,loc,desc,job_url,salary,''))
    return out

def greenhouse(src):
    url=f"https://boards-api.greenhouse.io/v1/boards/{src['slug']}/jobs?content=true"; data=fetch_json(url); out=[]
    for j in data.get('jobs',[]):
        title=clean(j.get('title')); loc=clean((j.get('location') or {}).get('name','')); dept=clean(', '.join(d.get('name','') for d in j.get('departments',[])))
        desc=clean(j.get('content',''))[:20000]; job_url=canonical(j.get('absolute_url') or '')
        out.append((title,dept,loc,desc,job_url,'',j.get('updated_at') or ''))
    return out

def lever(src):
    url=f"https://api.lever.co/v0/postings/{src['slug']}?mode=json"; data=fetch_json(url); out=[]
    for j in data if isinstance(data,list) else []:
        title=clean(j.get('text')); loc=clean((j.get('categories') or {}).get('location','')); dept=clean((j.get('categories') or {}).get('team',''))
        desc=clean(j.get('descriptionPlain') or j.get('description',''))[:20000]; job_url=canonical(j.get('hostedUrl') or j.get('applyUrl') or '')
        out.append((title,dept,loc,desc,job_url,'',j.get('createdAt') or ''))
    return out

def collect(cfg):
    rows=[]; stats=[]; adapters={'ashby':ashby,'greenhouse':greenhouse,'lever':lever}
    for src in cfg['sources']:
        if not src.get('enabled',True): continue
        try:
            jobs=adapters[src['ats']](src); kept=0
            for title,dept,loc,desc,url,salary,pub in jobs:
                title_low=' '.join([title,dept]).lower()
                # Keep ATS intake title/department-first. Descriptions often mention 'product' or 'AI' for every role and flood the CRM.
                if not any(t in title_low for t in TARGET):
                    continue
                if any(b in title_low for b in EXCLUDE):
                    continue
                pts,why,action=score(title,dept,loc,desc)
                if pts<55: continue
                kept+=1
                rows.append({
                    'status':'needs_review' if pts>=55 else 'scored','source':'ats_'+src['ats'],'search_filter':src['company']+'/'+src['slug'],'job_title':title,'company':src['company'],'salary':salary,'remote_location':loc,'published_at':str(pub or ''),'job_url':url,'description':desc,'company_website':'','careers_page':url,'company_linkedin':'','hh_company_profile':'','recruiter_name':'','recruiter_role':'','recruiter_linkedin':'','recruiter_hh_profile':'','hr_email':'','hr_phone':'','contact_source':'','fit_score':str(pts),'why_relevant':why,'next_action':action,'enrichment_status':'ats_structured','notes':f"ats={src['ats']}; slug={src['slug']}; priority={src.get('priority','')}; category={src.get('category','')}; dept={dept}"
                })
            stats.append({'company':src['company'],'ats':src['ats'],'total':len(jobs),'kept':kept})
        except Exception as e:
            stats.append({'company':src.get('company'),'ats':src.get('ats'),'error':type(e).__name__+': '+str(e)[:160]})
    return rows,stats

def merge(rows):
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    creds=Credentials.from_authorized_user_file('/opt/data/google_token.json'); svc=build('sheets','v4',credentials=creds)
    vals=svc.spreadsheets().values().get(spreadsheetId=SHEET_ID, range=f"'{SHEET}'!A:Z").execute().get('values',[]); h=vals[0]; idx={x:i for i,x in enumerate(h)}
    existing={canonical((r+['']*len(h))[idx['job_url']]) for r in vals[1:] if len(r)>idx['job_url']}
    app=[]
    for row in rows:
        if canonical(row.get('job_url','')) in existing: continue
        app.append([row.get(c,'') for c in h])
    if app:
        svc.spreadsheets().values().append(spreadsheetId=SHEET_ID, range=f"'{SHEET}'!A:Z", valueInputOption='RAW', insertDataOption='INSERT_ROWS', body={'values':app}).execute()
    return len(app)

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--config',type=Path,default=CONFIG); ap.add_argument('--write-sheet',action='store_true'); args=ap.parse_args()
    cfg=json.loads(args.config.read_text()); rows,stats=collect(cfg)
    DATA.mkdir(parents=True,exist_ok=True); run=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    (DATA/f'ats_rows_{run}.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2)); (DATA/f'ats_stats_{run}.json').write_text(json.dumps(stats,ensure_ascii=False,indent=2))
    wrote=merge(rows) if args.write_sheet else 0
    print(json.dumps({'rows':len(rows),'wrote_new_rows':wrote,'stats':stats,'run':run},ensure_ascii=False,indent=2))
if __name__=='__main__': main()
