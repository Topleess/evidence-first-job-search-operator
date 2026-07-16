#!/usr/bin/env python3
"""Collect public LinkedIn guest job pages/search pages into Vacancies Inbox.

Supported low-risk modes:
- concrete job URL: https://www.linkedin.com/jobs/view/<id>/
- public guest search URL: https://www.linkedin.com/jobs/search/?keywords=...

No login, no browser session cookies, no Easy Apply, no DM, no CAPTCHA bypass.
LinkedIn personalized/recommended pages may not expose the same data publicly;
those are best handled via user-provided specific URLs or Gmail job alerts.
"""
from __future__ import annotations

import argparse, csv, hashlib, html as ihtml, json, re, time, urllib.parse, urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from bs4 import BeautifulSoup
try:
    from operational_reliability import collection_status
except ImportError:  # imported as scripts.collect_linkedin_public_to_sheets in tests
    from scripts.operational_reliability import collection_status

BASE=Path('/opt/data/job-search')
DATA=BASE/'data/linkedin/public'
SHEET_ID='1O_tUG4FsSkNOrpMTQ4savhiQjQfyZWdocPc7EgbikDI'
SHEET='Vacancies Inbox'
COLUMNS=["status","source","search_filter","job_title","company","salary","remote_location","published_at","job_url","company_website","careers_page","company_linkedin","hh_company_profile","recruiter_name","recruiter_role","recruiter_linkedin","recruiter_hh_profile","hr_email","hr_phone","contact_source","fit_score","why_relevant","next_action","enrichment_status","notes"]
GOOD={
 'product':['product','продукт','owner','roadmap','pm'],
 'ai':['ai','llm','ml','machine learning','automation','agent','rag'],
 'lead':['lead','head','principal','руковод'],
 'ops':['operations','chief of staff','founder associate','operator'],
 'remote':['remote','emea','worldwide','anywhere','удален','hybrid'],
 'project':['project manager','program manager','delivery manager','руководитель проекта'],
}
BAD=['frontend','backend','developer','software engineer','designer','sales','account executive','support','qa','devops','sre','intern','junior']

@dataclass
class Row:
    status:str='scored'; source:str='linkedin'; search_filter:str=''; job_title:str=''; company:str=''; salary:str=''; remote_location:str=''; published_at:str=''; job_url:str=''; company_website:str=''; careers_page:str=''; company_linkedin:str=''; hh_company_profile:str=''; recruiter_name:str=''; recruiter_role:str=''; recruiter_linkedin:str=''; recruiter_hh_profile:str=''; hr_email:str=''; hr_phone:str=''; contact_source:str=''; fit_score:str=''; why_relevant:str=''; next_action:str=''; enrichment_status:str='raw_public_linkedin'; notes:str=''

def clean(x:Any)->str:
    return re.sub(r'\s+',' ',str(x or '')).strip()

def canon(url:str)->str:
    url=clean(url)
    if url.startswith('/'):
        url='https://www.linkedin.com'+url
    p=urllib.parse.urlparse(url)
    host=(p.hostname or '').lower()
    if p.scheme not in {'http','https'} or not host or not (host == 'linkedin.com' or host.endswith('.linkedin.com')):
        return ''
    m=re.search(r'/jobs/view/(?:[^/]*-)?(\d+)',p.path)
    if not m:
        m=re.search(r'-(\d+);?$',p.path)
    if m:
        return f'https://www.linkedin.com/jobs/view/{m.group(1)}'
    keep=[]
    for k,v in urllib.parse.parse_qsl(p.query,keep_blank_values=False):
        if k.lower() in {'trk','ref','position','pagenum'} or k.lower().startswith('utm_'): continue
        keep.append((k,v))
    return urllib.parse.urlunparse((p.scheme or 'https',p.netloc.lower(),p.path.rstrip('/'),' ',urllib.parse.urlencode(keep),'')).replace(' ','')

def job_id(url:str)->str:
    m=re.search(r'/jobs/view/(?:[^/?#]*-)?(\d+)',url or '')
    if not m:
        m=re.search(r'-(\d+)(?:;|/|\?|#|$)', url or '')
    return m.group(1) if m else ''

def fetch(url:str)->str:
    req=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36','Accept-Language':'en-US,en;q=0.9,ru;q=0.8'})
    with urllib.request.urlopen(req,timeout=45) as r:
        return r.read().decode('utf-8','replace')

def score(title, desc, loc, salary, company):
    text=' '.join([title,desc,loc,salary,company]).lower(); pts=30; reasons=[]
    weights={'product':25,'ai':18,'lead':10,'ops':10,'remote':10,'project':12}
    def tm(term):
        term=term.lower()
        if re.search(r'[a-z0-9]',term): return re.search(rf'(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])',text) is not None
        return term in text
    for lab,terms in GOOD.items():
        if any(tm(t) for t in terms): pts+=weights[lab]; reasons.append(lab)
    if salary: pts+=5; reasons.append('salary_signal')
    if any(tm(b) for b in BAD): pts-=25; reasons.append('off_target_risk')
    pts=max(0,min(100,pts))
    action='draft_after_review' if pts>=80 else 'review' if pts>=65 else 'save_maybe' if pts>=50 else 'skip_likely'
    return pts, ', '.join(reasons) or 'weak keyword signal', action

def parse_detail(url:str, html:str, search_filter:str)->Row:
    soup=BeautifulSoup(html,'html.parser')
    title=clean((soup.select_one('.top-card-layout__title') or soup.select_one('.topcard__title') or soup.select_one('h1')).get_text(' ',strip=True) if (soup.select_one('.top-card-layout__title') or soup.select_one('.topcard__title') or soup.select_one('h1')) else '')
    company=clean((soup.select_one('.topcard__org-name-link') or soup.select_one('.topcard__flavor')).get_text(' ',strip=True) if (soup.select_one('.topcard__org-name-link') or soup.select_one('.topcard__flavor')) else '')
    locs=[clean(x.get_text(' ',strip=True)) for x in soup.select('.topcard__flavor')]
    loc='; '.join([x for x in locs if x and x!=company][:2])
    desc_el=soup.select_one('.show-more-less-html__markup') or soup.select_one('.description__text')
    desc=clean(desc_el.get_text(' ',strip=True) if desc_el else '')
    posted=clean((soup.select_one('.posted-time-ago__text') or soup.select_one('time')).get_text(' ',strip=True) if (soup.select_one('.posted-time-ago__text') or soup.select_one('time')) else '')
    salary=''
    for el in soup.select('.salary, .compensation__salary, .job-details-jobs-unified-top-card__job-insight'):
        txt=clean(el.get_text(' ',strip=True))
        if re.search(r'\$|€|£|salary|compensation|руб|₽',txt,re.I): salary=txt[:160]; break
    company_link=''
    a=soup.select_one('.topcard__org-name-link')
    if a and a.get('href'): company_link=canon(a['href'])
    fit,why,action=score(title,desc,loc,salary,company)
    recruiter_name=''; recruiter_role=''; recruiter_link=''
    poster=soup.select_one('.hirer-card__hirer-information a[href*="/in/"], .hiring-team a[href*="/in/"], a.job-poster[href*="/in/"]')
    if poster:
        recruiter_name=clean(poster.get_text(' ',strip=True)); recruiter_link=canon(poster.get('href') or '')
        role_el=soup.select_one('.hirer-card__hirer-job-title, .hiring-team__job-title')
        recruiter_role=clean(role_el.get_text(' ',strip=True) if role_el else '')
    contact_source='linkedin_public_job_poster' if recruiter_link else 'linkedin_company_people_search_pending'
    enrichment='contact_found_public' if recruiter_link else 'needs_authenticated_contact_research'
    return Row(search_filter=search_filter,job_title=title,company=company,salary=salary,remote_location=loc,published_at=posted,job_url=canon(url),company_linkedin=company_link,recruiter_name=recruiter_name,recruiter_role=recruiter_role,recruiter_linkedin=recruiter_link,contact_source=contact_source,fit_score=str(fit),why_relevant=why,next_action=action,enrichment_status=enrichment,notes=f'source_mode=linkedin_public_guest_detail; source_job_id={job_id(url)}; No login/apply/DM. description_available={"yes" if desc else "no"}')

def parse_search(url:str, html:str, search_filter:str)->list[Row]:
    soup=BeautifulSoup(html,'html.parser'); rows=[]
    cards=soup.select('.base-search-card') or soup.select('.jobs-search__results-list li')
    for c in cards:
        title_el=c.select_one('.base-search-card__title') or c.select_one('.job-search-card__title')
        comp_el=c.select_one('.base-search-card__subtitle') or c.select_one('.job-search-card__subtitle')
        meta_el=c.select_one('.base-search-card__metadata') or c.select_one('.job-search-card__location')
        a=c.select_one('a.base-card__full-link') or c.select_one('a[href*="/jobs/view/"]')
        title=clean(title_el.get_text(' ',strip=True) if title_el else '')
        company=clean(comp_el.get_text(' ',strip=True) if comp_el else '')
        loc=clean(meta_el.get_text(' ',strip=True) if meta_el else '')
        jurl=canon(a.get('href') if a else '')
        if not title or not jurl: continue
        fit,why,action=score(title,'',loc,'',company)
        rows.append(Row(search_filter=search_filter,job_title=title,company=company,remote_location=loc,job_url=jurl,fit_score=str(fit),why_relevant=why,next_action=action,enrichment_status='needs_public_detail',notes=f'source_mode=linkedin_public_guest_search; source_job_id={job_id(jurl)}; No login/apply/DM.'))
    return rows

def collect(urls:list[str], max_details:int=30, detail_min_score:int=65, delay:float=0.7)->tuple[list[Row],list[dict]]:
    DATA.mkdir(parents=True,exist_ok=True); out=[]; errors=[]; details_used=0
    for url in urls:
        try:
            html=fetch(url)
            fn=DATA/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'_'+hashlib.sha1(url.encode()).hexdigest()[:8]+'.html')
            fn.write_text(html,encoding='utf-8')
            if '/jobs/view/' in urllib.parse.urlparse(url).path:
                out.append(parse_detail(url,html,'manual_linkedin_job_url'))
            else:
                rows=parse_search(url,html,'configured_linkedin_guest_search')
                enriched={}
                for row in sorted(rows,key=lambda r:int(r.fit_score or 0),reverse=True):
                    if details_used>=max_details or int(row.fit_score or 0)<detail_min_score:
                        break
                    try:
                        detail_html=fetch(row.job_url); details_used+=1
                        key=job_id(row.job_url) or hashlib.sha1(row.job_url.encode()).hexdigest()[:8]
                        dfn=DATA/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+f'_detail_{key}.html')
                        dfn.write_text(detail_html,encoding='utf-8')
                        detail=parse_detail(row.job_url,detail_html,row.search_filter)
                        if detail.job_title:
                            enriched[job_id(row.job_url) or row.job_url]=detail
                        time.sleep(max(0,delay))
                    except Exception as exc:
                        row.notes += f'; detail_error={type(exc).__name__}'
                        errors.append({'stage':'detail','url':row.job_url,'error':repr(exc)})
                out.extend(enriched.get(job_id(r.job_url) or r.job_url,r) for r in rows)
        except Exception as exc:
            errors.append({'stage':'source','url':url,'error':repr(exc)})
    seen=set(); uniq=[]
    for r in out:
        k=job_id(r.job_url) or r.job_url
        if k in seen: continue
        seen.add(k); uniq.append(r)
    return uniq,errors

def write_outputs(rows:list[Row]):
    run=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    outdir=DATA/'normalized'; outdir.mkdir(parents=True,exist_ok=True)
    jp=outdir/f'vacancies_inbox_{run}.json'; cp=outdir/f'vacancies_inbox_{run}.csv'
    jp.write_text(json.dumps([asdict(r) for r in rows],ensure_ascii=False,indent=2),encoding='utf-8')
    with cp.open('w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=COLUMNS); w.writeheader(); w.writerows([asdict(r) for r in rows])
    qp=outdir/f'contact_enrichment_queue_{run}.json'
    queue=[{'job_id':job_id(r.job_url),'job_url':r.job_url,'job_title':r.job_title,'company':r.company,'company_linkedin':r.company_linkedin,'fit_score':r.fit_score,'contact_status':r.enrichment_status,'preferred_contact':'job_poster_or_recruiter_then_hiring_manager'} for r in rows if int(r.fit_score or 0)>=65 and r.enrichment_status in {'needs_authenticated_contact_research','contact_found_public'}]
    qp.write_text(json.dumps(queue,ensure_ascii=False,indent=2),encoding='utf-8')
    return jp,cp,qp

def merge_sheet(rows:list[Row]):
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    creds=Credentials.from_authorized_user_file('/opt/data/google_token.json')
    svc=build('sheets','v4',credentials=creds)
    vals=svc.spreadsheets().values().get(spreadsheetId=SHEET_ID, range=f"'{SHEET}'!A:Z").execute().get('values',[])
    if not vals: return
    header=vals[0]; idx={h:i for i,h in enumerate(header)}
    existing={}
    for n,r in enumerate(vals[1:],start=2):
        r=r+['']*(len(header)-len(r)); existing[canon(r[idx['job_url']])]=n
    app=[]; updates=[]
    for row in rows:
        data=[getattr(row,h,'') for h in header]
        key=canon(row.job_url)
        if key in existing:
            # Preserve manually advanced states, but batch all LinkedIn refreshes
            # into one Sheets write request to stay below the per-user quota.
            n=existing[key]
            old=vals[n-1]+['']*(len(header)-len(vals[n-1]))
            protected={'applied','interview','offer','rejected','ignored','approved_to_apply','manual_done'}
            if old[idx.get('status',0)] in protected:
                data[idx['status']]=old[idx['status']]
                data[idx['next_action']]=old[idx['next_action']]
            updates.append({'range':f"'{SHEET}'!A{n}", 'values':[data]})
        else:
            app.append(data)
    if updates:
        svc.spreadsheets().values().batchUpdate(
            spreadsheetId=SHEET_ID,
            body={'valueInputOption':'RAW','data':updates},
        ).execute()
    if app:
        svc.spreadsheets().values().append(spreadsheetId=SHEET_ID, range=f"'{SHEET}'!A:Z", valueInputOption='RAW', insertDataOption='INSERT_ROWS', body={'values':app}).execute()
    return len(app)

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('urls',nargs='+'); ap.add_argument('--write-sheet',action='store_true'); ap.add_argument('--max-details',type=int,default=30); ap.add_argument('--detail-min-score',type=int,default=65); ap.add_argument('--delay',type=float,default=0.7)
    args=ap.parse_args(); rows,errors=collect(args.urls,args.max_details,args.detail_min_score,args.delay); jp,cp,qp=write_outputs(rows); wrote=merge_sheet(rows) if args.write_sheet else 0
    counts={k:sum(r.enrichment_status==k for r in rows) for k in sorted({r.enrichment_status for r in rows})}
    status=collection_status(len(rows),errors)
    print(json.dumps({'source':'linkedin_public','status':status,'rows':len(rows),'errors':errors,'enrichment_counts':counts,'contact_queue':str(qp),'json':str(jp),'csv':str(cp),'wrote_new_rows':wrote},ensure_ascii=False,indent=2))
    return {'success': 0, 'degraded': 1, 'error': 2}[status]
if __name__=='__main__': raise SystemExit(main())
