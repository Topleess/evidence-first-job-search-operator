#!/usr/bin/env python3
"""Generic company career-page keyword watcher.

This is a lightweight first pass for company sites that do not expose a known ATS
API yet. It fetches public career/company pages, extracts links/snippets that look
like product/project/AI roles, and merges them into Vacancies Inbox.
"""
from __future__ import annotations
import argparse, csv, hashlib, html, json, re, urllib.parse, urllib.request
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from bs4 import BeautifulSoup

BASE=Path('/opt/data/job-search'); CONFIG=BASE/'config/company_career_sources.json'; DATA=BASE/'data/company_pages'
SHEET_ID='1O_tUG4FsSkNOrpMTQ4savhiQjQfyZWdocPc7EgbikDI'; SHEET='Vacancies Inbox'
COLUMNS=["status","source","search_filter","job_title","company","salary","remote_location","published_at","job_url","company_website","careers_page","company_linkedin","hh_company_profile","recruiter_name","recruiter_role","recruiter_linkedin","recruiter_hh_profile","hr_email","hr_phone","contact_source","fit_score","why_relevant","next_action","enrichment_status","notes"]
KEYWORDS=["product manager","product owner","product lead","head of product","project manager","program manager","delivery manager","founder associate","ai product","llm","продакт","продукт","руководитель продукта","руководитель проекта","product"]
BAD=["developer","engineer","designer","sales","support","qa","devops","sre","marketing manager","seo","copywriter"]
@dataclass
class Row:
    status:str='needs_review'; source:str='company_site'; search_filter:str='company_career_page'; job_title:str=''; company:str=''; salary:str=''; remote_location:str=''; published_at:str=''; job_url:str=''; company_website:str=''; careers_page:str=''; company_linkedin:str=''; hh_company_profile:str=''; recruiter_name:str=''; recruiter_role:str=''; recruiter_linkedin:str=''; recruiter_hh_profile:str=''; hr_email:str=''; hr_phone:str=''; contact_source:str=''; fit_score:str=''; why_relevant:str=''; next_action:str=''; enrichment_status:str='raw_company_page'; notes:str=''

def clean(x): return re.sub(r'\s+',' ',html.unescape(str(x or ''))).strip()
def canon(url,base=''):
    url=urllib.parse.urljoin(base,url); p=urllib.parse.urlparse(url)
    qs=[(k,v) for k,v in urllib.parse.parse_qsl(p.query) if not k.startswith('utm_') and k not in {'ref','source'}]
    return urllib.parse.urlunparse((p.scheme,p.netloc.lower(),p.path.rstrip('/'),' ',urllib.parse.urlencode(qs),'')).replace(' ','')
def fetch(url):
    req=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0 (compatible; personal-job-search-career-watcher/0.1)','Accept':'text/html,*/*;q=0.8','Accept-Language':'en,ru;q=0.8'})
    with urllib.request.urlopen(req,timeout=30) as r: return r.read().decode('utf-8','replace')
def score(title,txt):
    low=(title+' '+txt).lower(); pts=30; reasons=[]
    if any(k in low for k in ['product manager','product owner','product lead','продакт','руководитель продукта']): pts+=35; reasons.append('product')
    if any(k in low for k in ['project manager','program manager','delivery manager','руководитель проекта']): pts+=20; reasons.append('project')
    if any(k in low for k in ['ai','llm','ml','agent','automation','ии']): pts+=15; reasons.append('ai')
    if any(k in low for k in ['remote','удален','emea','worldwide']): pts+=10; reasons.append('remote')
    if any(b in low for b in BAD): pts-=25; reasons.append('off_target_risk')
    pts=max(0,min(100,pts)); action='draft_after_review' if pts>=80 else 'review' if pts>=60 else 'save_maybe' if pts>=45 else 'skip_likely'
    return pts, ', '.join(reasons) or 'keyword link only', action

def parse_source(src):
    url=src['url']; company=src['company']; html_text=fetch(url)
    DATA.mkdir(parents=True,exist_ok=True); (DATA/(hashlib.sha1(url.encode()).hexdigest()[:12]+'.html')).write_text(html_text,encoding='utf-8')
    soup=BeautifulSoup(html_text,'html.parser')
    rows=[]; seen=set()
    for a in soup.find_all('a'):
        text=clean(a.get_text(' ',strip=True)); href=a.get('href') or ''
        if not text or len(text)<4: continue
        low=(text+' '+href).lower()
        if not any(k in low for k in KEYWORDS): continue
        jurl=canon(href,url)
        if not jurl or jurl in seen: continue
        seen.add(jurl)
        pts,why,action=score(text,'')
        rows.append(Row(company=company,job_title=text[:160],job_url=jurl,company_website=src.get('url',''),careers_page=url,fit_score=str(pts),why_relevant=why,next_action=action,notes=f'company_page_source={url}; priority={src.get("priority","")}; origin={src.get("origin","")}; generic link extraction'))
    # if no links but page itself mentions roles, add page row for review
    if not rows:
        body=clean(soup.get_text(' ',strip=True))[:5000]
        if any(k in body.lower() for k in KEYWORDS):
            pts,why,action=score(company+' careers',body)
            rows.append(Row(company=company,job_title=f'{company} careers page keyword match',job_url=url,company_website=src.get('url',''),careers_page=url,fit_score=str(pts),why_relevant=why,next_action=action,notes=f'company_page_source={url}; generic page keyword match'))
    return rows

def merge_sheet(rows):
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    creds=Credentials.from_authorized_user_file('/opt/data/google_token.json'); svc=build('sheets','v4',credentials=creds)
    vals=svc.spreadsheets().values().get(spreadsheetId=SHEET_ID, range=f"'{SHEET}'!A:Z").execute().get('values',[]); h=vals[0]; idx={x:i for i,x in enumerate(h)}
    existing={canon((r+['']*len(h))[idx['job_url']]) for r in vals[1:]}
    app=[]
    for row in rows:
        if canon(row.job_url) in existing: continue
        app.append([getattr(row,col,'') for col in h])
    if app: svc.spreadsheets().values().append(spreadsheetId=SHEET_ID, range=f"'{SHEET}'!A:Z", valueInputOption='RAW', insertDataOption='INSERT_ROWS', body={'values':app}).execute()
    return len(app)

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--config',type=Path,default=CONFIG); ap.add_argument('--write-sheet',action='store_true'); args=ap.parse_args()
    cfg=json.loads(args.config.read_text()); allrows=[]; errs=[]
    for src in cfg.get('sources',[]):
        if not src.get('enabled',True): continue
        try: allrows.extend(parse_source(src))
        except Exception as e: errs.append({'company':src.get('company'),'url':src.get('url'),'error':type(e).__name__+': '+str(e)[:200]})
    DATA.mkdir(parents=True,exist_ok=True); run=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    jp=DATA/f'company_page_rows_{run}.json'; jp.write_text(json.dumps([asdict(r) for r in allrows],ensure_ascii=False,indent=2),encoding='utf-8')
    wrote=merge_sheet(allrows) if args.write_sheet else 0
    print(json.dumps({'rows':len(allrows),'wrote_new_rows':wrote,'errors':errs,'json':str(jp)},ensure_ascii=False,indent=2))
if __name__=='__main__': main()
