#!/usr/bin/env python3
"""Collect public job boards into Google Sheets Vacancies Inbox.

Sources: Remotive, RemoteOK, WeWorkRemotely, Habr Career, GeekJob,
Getmatch, Himalayas and YC Work at a Startup.
No credentials, no applications/messages, collection only.
"""
from __future__ import annotations

import argparse
import csv
import email.utils
import html
import json
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
try:
    from operational_reliability import collection_status
except ImportError:
    from scripts.operational_reliability import collection_status

BASE = Path('/opt/data/job-search')
DATA_DIR = BASE / 'data' / 'job_boards'

COLUMNS = [
    "status","source","search_filter","job_title","company","salary","remote_location","published_at","job_url",
    "company_website","careers_page","company_linkedin","hh_company_profile",
    "recruiter_name","recruiter_role","recruiter_linkedin","recruiter_hh_profile","hr_email","hr_phone","contact_source",
    "fit_score","why_relevant","next_action","enrichment_status","notes"
]

HEADERS = {
    'User-Agent': 'HermesJobSearchBot/0.1 (+https://as-shamshurin.xyz; collection for personal job search)',
    'Accept': 'application/json,text/xml,application/rss+xml,text/html;q=0.8,*/*;q=0.5',
    'Accept-Language': 'en,ru;q=0.8',
}

GOOD_TITLE_TERMS = [
    'product manager','product owner','product lead','head of product','director of product',
    'vp product','chief product','ai product','llm product','growth product','technical product',
    'founder associate','chief of staff','product operations','product operator','marketplace product',
    'project manager','program manager','delivery manager','implementation manager','operations lead',
    'менеджер продукта','продуктовый менеджер','владелец продукта','руководитель продукта','продакт менеджер',
    'менеджер проектов','руководитель проектов','руководитель проектного офиса','операционный руководитель',
]
# Strong negative terms in the title.  The collector is for PM/Product/Operator roles,
# not developer/designer/support/content/data jobs even if their descriptions mention a product.
BAD_TITLE_TERMS = [
    'software engineer','frontend','backend','full stack','fullstack','developer','devops','sre','data engineer',
    'designer','product design','design manager','design engineer','graphic designer','video editor','editor','account executive','account manager',
    'sales manager','sales development','customer support','support specialist','customer success',
    'content manager','marketing manager','seo','ppc','recruiter','hr manager','nurse','teacher',
    'copywriter','qa engineer','quality assurance','data scientist','ml engineer','data analyst',
    'product researcher','researcher',
]
SOFT_BAD_TEXT_TERMS = ['recruiting agency', 'customer support', 'sales pipeline', 'hands-on coding']
AI_TERMS = [' ai ', 'llm', 'machine learning', 'automation', 'rag', 'agentic', ' agents ', 'artificial intelligence']
SENIORITY_TERMS = ['lead','head','senior','principal','director','vp','founder','chief of staff']

@dataclass
class Job:
    status: str = 'scored'
    source: str = ''
    search_filter: str = ''
    job_title: str = ''
    company: str = ''
    salary: str = ''
    remote_location: str = ''
    published_at: str = ''
    job_url: str = ''
    company_website: str = ''
    careers_page: str = ''
    company_linkedin: str = ''
    hh_company_profile: str = ''
    recruiter_name: str = ''
    recruiter_role: str = ''
    recruiter_linkedin: str = ''
    recruiter_hh_profile: str = ''
    hr_email: str = ''
    hr_phone: str = ''
    contact_source: str = ''
    fit_score: str = ''
    why_relevant: str = ''
    next_action: str = ''
    enrichment_status: str = 'raw_only'
    notes: str = ''


def fetch(url: str, timeout: int = 40) -> bytes:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def strip_html(text: str) -> str:
    text = re.sub(r'<[^>]+>', ' ', text or '')
    return re.sub(r'\s+', ' ', html.unescape(text)).strip()


def canonical_url(url: str) -> str:
    try:
        u = urllib.parse.urlparse(url)
        qs = urllib.parse.parse_qsl(u.query, keep_blank_values=True)
        qs = [(k,v) for k,v in qs if not (k.startswith('utm_') or k in {'ref','ref_src','source','trk','gh_src'})]
        return urllib.parse.urlunparse((u.scheme, u.netloc.lower(), u.path.rstrip('/'), '', urllib.parse.urlencode(qs), ''))
    except Exception:
        return url


def parse_date(value: str) -> str:
    if not value:
        return ''
    value = str(value)
    # RemoteOK epoch ms sometimes
    if value.isdigit():
        try:
            ts = int(value)
            if ts > 10_000_000_000:
                ts = ts / 1000
            return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        except Exception:
            return value
    try:
        return email.utils.parsedate_to_datetime(value).astimezone(timezone.utc).isoformat()
    except Exception:
        return value


def score_job(title: str, description: str, tags: str = '') -> tuple[int, str, str]:
    title_l = (title or '').lower()
    text = f' {title} {description} {tags} '.lower()
    reasons = []

    strong_title = any(term in title_l for term in GOOD_TITLE_TERMS)
    product_title = 'product' in title_l and any(term in title_l for term in ['manager','owner','lead','head','director','vp','strategy','operations'])
    project_title = any(term in title_l for term in ['project manager','program manager','delivery manager','implementation manager'])
    ai_signal = any(term in text for term in AI_TERMS)
    bad_title = any(bad in title_l for bad in BAD_TITLE_TERMS)

    score = 15
    if strong_title or product_title or project_title:
        score += 55; reasons.append('target role title')
    elif 'product' in title_l:
        score += 25; reasons.append('product-adjacent title')

    if ai_signal:
        score += 12; reasons.append('AI/automation')
    if any(term in title_l for term in SENIORITY_TERMS):
        score += 8; reasons.append('seniority/ownership')
    if any(term in text for term in ['remote','worldwide','anywhere']):
        score += 5; reasons.append('remote')

    # Description-only product mentions are weak: many irrelevant roles mention a product.
    if 'product' in text and ('product' not in title_l) and not (strong_title or project_title):
        score += 5; reasons.append('product mention')

    if bad_title:
        score -= 60; reasons.append('off-target title')
    if any(bad in text for bad in SOFT_BAD_TEXT_TERMS):
        score -= 8; reasons.append('possible off-target')

    score = max(0, min(100, score))
    if score >= 82:
        action = 'review_for_draft'
    elif score >= 70:
        action = 'review'
    else:
        action = 'skip_likely'
    return score, ', '.join(dict.fromkeys(reasons)) or 'keyword match', action


def remotive(max_items: int) -> list[Job]:
    url = 'https://remotive.com/api/remote-jobs?category=product'
    data = json.loads(fetch(url).decode('utf-8', 'replace'))
    raw_path = DATA_DIR / 'raw' / f"remotive_{stamp()}.json"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    out = []
    for item in data.get('jobs', [])[:max_items]:
        desc = strip_html(item.get('description') or '')
        title = item.get('title') or ''
        tags = ' '.join(item.get('tags') or [])
        score, why, action = score_job(title, desc, tags)
        if score < 70:
            continue
        out.append(Job(
            source='remotive', search_filter='remotive:product', job_title=title,
            company=item.get('company_name') or '', salary=item.get('salary') or '',
            remote_location=item.get('candidate_required_location') or 'Remote',
            published_at=parse_date(item.get('publication_date') or ''),
            job_url=canonical_url(item.get('url') or ''), company_website=item.get('company_logo_url') and '' or '',
            fit_score=str(score), why_relevant=why, next_action=action,
            enrichment_status='raw_only',
            notes=f"Remotive id={item.get('id','')}; raw={raw_path}"
        ))
    return out


def remoteok(max_items: int) -> list[Job]:
    url = 'https://remoteok.com/api'
    data = json.loads(fetch(url).decode('utf-8', 'replace'))
    raw_path = DATA_DIR / 'raw' / f"remoteok_{stamp()}.json"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    out = []
    for item in data[1:max_items+1] if isinstance(data, list) else []:
        title = item.get('position') or item.get('title') or ''
        desc = strip_html(item.get('description') or '')
        tags = ' '.join(item.get('tags') or [])
        score, why, action = score_job(title, desc, tags)
        if score < 70:
            continue
        salary = ''
        if item.get('salary_min') or item.get('salary_max'):
            salary = f"{item.get('salary_min') or ''}-{item.get('salary_max') or ''} {item.get('salary_currency') or 'USD'}".strip('- ')
        out.append(Job(
            source='remoteok', search_filter='remoteok:api', job_title=title,
            company=item.get('company') or '', salary=salary,
            remote_location=item.get('location') or 'Remote', published_at=parse_date(str(item.get('epoch') or '')),
            job_url=canonical_url(item.get('url') or item.get('apply_url') or ''),
            company_website=item.get('company_url') or '', fit_score=str(score), why_relevant=why,
            next_action=action, enrichment_status='partial' if item.get('company_url') else 'raw_only',
            notes=f"RemoteOK id={item.get('id','')}; raw={raw_path}"
        ))
    return out


def wwr(max_items: int) -> list[Job]:
    url = 'https://weworkremotely.com/remote-jobs.rss'
    raw = fetch(url).decode('utf-8', 'replace')
    raw_path = DATA_DIR / 'raw' / f"wwr_{stamp()}.rss"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(raw, encoding='utf-8')
    root = ET.fromstring(raw)
    out = []
    for item in root.findall('.//item')[:max_items]:
        title_full = item.findtext('title') or ''
        link = item.findtext('link') or ''
        desc = strip_html(item.findtext('description') or '')
        published = parse_date(item.findtext('pubDate') or '')
        company = ''
        title = title_full
        # Usually "Company: Job Title"
        if ':' in title_full:
            company, title = [x.strip() for x in title_full.split(':', 1)]
        score, why, action = score_job(title, desc)
        if score < 70:
            continue
        out.append(Job(
            source='weworkremotely', search_filter='wwr:rss', job_title=title,
            company=company, remote_location='Remote', published_at=published,
            job_url=canonical_url(link), fit_score=str(score), why_relevant=why,
            next_action=action, enrichment_status='raw_only', notes=f"WWR RSS; raw={raw_path}"
        ))
    return out


def _save_raw(source: str, suffix: str, content: str | bytes) -> Path:
    raw_path = DATA_DIR / 'raw' / f"{source}_{stamp()}.{suffix}"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        raw_path.write_bytes(content)
    else:
        raw_path.write_text(content, encoding='utf-8')
    return raw_path


def _rss_blocks(raw: str) -> list[dict[str, str]]:
    """Tolerant RSS parser: GeekJob occasionally emits invalid XML chars."""
    result = []
    for block in re.findall(r'<item\b[^>]*>(.*?)</item>', raw, re.I | re.S):
        item = {}
        for tag in ('title', 'description', 'author', 'pubDate', 'link', 'guid'):
            m = re.search(rf'<{tag}\b[^>]*>(.*?)</{tag}>', block, re.I | re.S)
            value = m.group(1) if m else ''
            value = re.sub(r'^\s*<!\[CDATA\[|\]\]>\s*$', '', value, flags=re.S)
            item[tag] = html.unescape(value.strip())
        result.append(item)
    return result


def _rss_jobs(source: str, url: str, max_items: int) -> list[Job]:
    raw = fetch(url).decode('utf-8', 'replace')
    raw_path = _save_raw(source, 'rss', raw)
    out = []
    for item in _rss_blocks(raw)[:max_items]:
        title = strip_html(item.get('title', ''))
        if source == 'habr':
            quoted = re.search(r'[«“"](.+?)[»”"]', title)
            if quoted:
                title = quoted.group(1).strip()
        desc = strip_html(item.get('description', ''))
        score, why, action = score_job(title, desc)
        if score < 70:
            continue
        location = 'Remote' if re.search(r'удален|remote', desc, re.I) else ''
        salary = ''
        salary_match = re.search(r'(\d[\d\s]{3,}\s*[—–-]\s*\d[\d\s]{3,}\s*[₽$€]|от\s+\d[\d\s]{3,}\s*[₽$€])', desc, re.I)
        if salary_match:
            salary = re.sub(r'\s+', ' ', salary_match.group(1)).strip()
        out.append(Job(
            source=source, search_filter=f'{source}:rss', job_title=title,
            company=strip_html(item.get('author', '')), salary=salary,
            remote_location=location, published_at=parse_date(item.get('pubDate', '')),
            job_url=canonical_url(strip_html(item.get('link', ''))),
            fit_score=str(score), why_relevant=why, next_action=action,
            enrichment_status='raw_only',
            notes=f"{source} guid={strip_html(item.get('guid',''))}; raw={raw_path}"
        ))
    return out


def habr(max_items: int) -> list[Job]:
    return _rss_jobs('habr', f'https://career.habr.com/vacancies/rss?page=1&per_page={min(max_items, 100)}', max_items)


def geekjob(max_items: int) -> list[Job]:
    return _rss_jobs('geekjob', 'https://geekjob.ru/vacancies/rss', max_items)


def himalayas(max_items: int) -> list[Job]:
    url = f'https://himalayas.app/jobs/api?limit={min(max_items, 100)}'
    data = json.loads(fetch(url).decode('utf-8', 'replace'))
    raw_path = _save_raw('himalayas', 'json', json.dumps(data, ensure_ascii=False, indent=2))
    out = []
    for item in data.get('jobs', [])[:max_items]:
        title = item.get('title') or ''
        desc = strip_html(item.get('description') or item.get('excerpt') or '')
        tags = ' '.join((item.get('categories') or []) + (item.get('parentCategories') or []))
        score, why, action = score_job(title, desc, tags)
        if score < 70:
            continue
        salary = ''
        if item.get('minSalary') or item.get('maxSalary'):
            salary = f"{item.get('minSalary') or ''}-{item.get('maxSalary') or ''} {item.get('currency') or ''}/{item.get('salaryPeriod') or ''}".strip('- /')
        restrictions = item.get('locationRestrictions') or []
        location = ', '.join(restrictions) if isinstance(restrictions, list) else str(restrictions or 'Remote')
        company_slug = item.get('companySlug') or ''
        out.append(Job(
            source='himalayas', search_filter='himalayas:api', job_title=title,
            company=item.get('companyName') or '', salary=salary,
            remote_location=location or 'Remote', published_at=parse_date(item.get('pubDate') or ''),
            job_url=canonical_url(item.get('guid') or item.get('applicationLink') or ''),
            company_website=(f'https://himalayas.app/companies/{company_slug}' if company_slug else ''),
            fit_score=str(score), why_relevant=why, next_action=action,
            enrichment_status='partial' if company_slug else 'raw_only',
            notes=f"Himalayas API; apply={item.get('applicationLink','')}; raw={raw_path}"
        ))
    return out


def getmatch(max_items: int) -> list[Job]:
    out = []
    for specialization in ('product_management', 'project_management'):
        url = ('https://getmatch.ru/api/offers?sa=any&p=1&offset=0&pa=all&'
               + urllib.parse.urlencode({'limit': min(max_items, 100), 'sp': specialization}))
        data = json.loads(fetch(url).decode('utf-8', 'replace'))
        raw_path = _save_raw(f'getmatch_{specialization}', 'json', json.dumps(data, ensure_ascii=False, indent=2))
        for item in data.get('offers', [])[:max_items]:
            title = item.get('position') or ''
            desc = strip_html(item.get('offer_description') or item.get('description_html') or '')
            tags = ' '.join((x.get('name') or x.get('title') or '') for x in (item.get('skills_objects') or []) if isinstance(x, dict))
            score, why, action = score_job(title, desc, tags)
            if score < 70:
                continue
            company = item.get('company') or {}
            location = '; '.join(f"{x.get('label','')} ({x.get('format','')})".strip() for x in (item.get('location_items') or []) if isinstance(x, dict))
            salary = item.get('salary_description') or ''
            if not salary and (item.get('salary_display_from') or item.get('salary_display_to')):
                salary = f"{item.get('salary_display_from') or ''}-{item.get('salary_display_to') or ''} {item.get('salary_currency') or ''}".strip('- ')
            out.append(Job(
                source='getmatch', search_filter=f'getmatch:{specialization}', job_title=title,
                company=company.get('name') or '', salary=salary, remote_location=location,
                published_at=parse_date(item.get('published_at') or ''),
                job_url=canonical_url(urllib.parse.urljoin('https://getmatch.ru', item.get('url') or '')),
                careers_page=urllib.parse.urljoin('https://getmatch.ru', company.get('url') or ''),
                fit_score=str(score), why_relevant=why, next_action=action,
                enrichment_status='partial', notes=f"Getmatch id={item.get('id','')}; raw={raw_path}"
            ))
    return out


class _YCPageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.data_page = ''

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if values.get('id', '').startswith(('WaasLandingPage-', 'WaasJobListingsPage-')):
            self.data_page = values.get('data-page', '')


def yc(max_items: int) -> list[Job]:
    out = []
    for role in ('product-manager', 'operations'):
        url = f'https://www.ycombinator.com/jobs/role/{role}/remote'
        raw = fetch(url).decode('utf-8', 'replace')
        raw_path = _save_raw(f'yc_{role}', 'html', raw)
        parser = _YCPageParser(); parser.feed(raw)
        if not parser.data_page:
            raise ValueError(f'YC data-page not found for {role}')
        jobs = json.loads(parser.data_page).get('props', {}).get('jobPostings', [])
        for item in jobs[:max_items]:
            title = item.get('title') or ''
            desc = ' '.join([item.get('companyOneLiner') or '', item.get('prettyRole') or '', ' '.join(item.get('skills') or [])])
            score, why, action = score_job(title, desc)
            if score < 70:
                continue
            hiring = item.get('hiringManager') or {}
            recruiter_name = hiring.get('name') if isinstance(hiring, dict) else ''
            recruiter_linkedin = hiring.get('linkedinUrl') if isinstance(hiring, dict) else ''
            out.append(Job(
                source='yc_jobs', search_filter=f'yc:{role}:remote', job_title=title,
                company=item.get('companyName') or '', salary=item.get('salaryRange') or '',
                remote_location=item.get('location') or 'Remote',
                job_url=canonical_url(urllib.parse.urljoin('https://www.ycombinator.com', item.get('url') or '')),
                careers_page=urllib.parse.urljoin('https://www.ycombinator.com', item.get('companyUrl') or ''),
                recruiter_name=recruiter_name or '', recruiter_linkedin=recruiter_linkedin or '',
                contact_source='yc_job_hiring_manager' if recruiter_name else '',
                fit_score=str(score), why_relevant=why, next_action=action,
                enrichment_status='partial',
                notes=f"YC id={item.get('id','')}; visa={item.get('visa','')}; active={item.get('lastActive','')}; raw={raw_path}"
            ))
    return out


def stamp() -> str:
    return datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')


def write_artifacts(jobs: list[Job], run_id: str):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    json_path = DATA_DIR / f'vacancies_{run_id}.json'
    csv_path = DATA_DIR / f'vacancies_{run_id}.csv'
    json_path.write_text(json.dumps([asdict(j) for j in jobs], ensure_ascii=False, indent=2), encoding='utf-8')
    with csv_path.open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader(); w.writerows([{k:getattr(j,k,'') for k in COLUMNS} for j in jobs])
    return json_path, csv_path


def update_sheet(jobs: list[Job], spreadsheet_id: str, replace_existing_source_rows: bool = False):
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    creds = Credentials.from_authorized_user_file('/opt/data/google_token.json')
    svc = build('sheets','v4',credentials=creds)
    existing = svc.spreadsheets().values().get(spreadsheetId=spreadsheet_id, range="'Vacancies Inbox'!A:Z").execute().get('values', [])
    idx = {name:i for i,name in enumerate(COLUMNS)}
    rows = []
    if existing:
        rows = [(r + ['']*len(COLUMNS))[:len(COLUMNS)] for r in existing[1:]]
    protected = {'applied','ignored','interview','offer','approved_to_apply','manual_done'}
    removed_count = 0
    if replace_existing_source_rows:
        current_sources = {j.source for j in jobs}
        kept = []
        for r in rows:
            is_this_collector_row = (
                r[idx['source']] in current_sources
                and 'data/job_boards/raw/' in r[idx['notes']]
                and r[idx['status']] not in protected
            )
            if is_this_collector_row:
                removed_count += 1
            else:
                kept.append(r)
        rows = kept
    by_url = {canonical_url(r[idx['job_url']]): n for n,r in enumerate(rows) if r[idx['job_url']]}
    new_count = upd_count = 0
    for j in jobs:
        nr = [getattr(j,k,'') for k in COLUMNS]
        key = canonical_url(j.job_url)
        if key and key in by_url:
            old = rows[by_url[key]]
            if old[idx['status']] in protected:
                nr[idx['status']] = old[idx['status']]
                nr[idx['next_action']] = old[idx['next_action']]
            rows[by_url[key]] = nr; upd_count += 1
        else:
            rows.append(nr); new_count += 1
    values = [COLUMNS] + rows
    svc.spreadsheets().values().clear(spreadsheetId=spreadsheet_id, range="'Vacancies Inbox'!A:Z").execute()
    svc.spreadsheets().values().update(spreadsheetId=spreadsheet_id, range="'Vacancies Inbox'!A1", valueInputOption='RAW', body={'values': values}).execute()
    return new_count, upd_count, len(rows), removed_count


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--sources', default='remotive,habr,geekjob,getmatch,himalayas,yc')
    ap.add_argument('--max-items', type=int, default=80)
    ap.add_argument('--write-sheet', action='store_true')
    ap.add_argument('--replace-existing-source-rows', action='store_true',
                    help='Before writing, remove unprotected rows previously produced by this job-board collector')
    ap.add_argument('--spreadsheet-id', default='')
    args = ap.parse_args()
    jobs=[]; errors=[]
    per = max(10, args.max_items)
    for src in [s.strip() for s in args.sources.split(',') if s.strip()]:
        try:
            if src == 'remotive':
                got = remotive(per)
            elif src == 'remoteok':
                got = remoteok(per)
            elif src in {'wwr','weworkremotely'}:
                got = wwr(per)
            elif src in {'habr','habr_career'}:
                got = habr(per)
            elif src == 'geekjob':
                got = geekjob(per)
            elif src == 'getmatch':
                got = getmatch(per)
            elif src == 'himalayas':
                got = himalayas(per)
            elif src in {'yc','yc_jobs'}:
                got = yc(per)
            else:
                raise ValueError(f'unknown source {src}')
            print(f'{src}: {len(got)} relevant rows', file=sys.stderr)
            jobs.extend(got)
            time.sleep(0.5)
        except Exception as e:
            errors.append({'source':src,'error':repr(e)})
            print(f'ERROR {src}: {e}', file=sys.stderr)
    # dedupe within run
    seen=set(); dedup=[]
    for j in jobs:
        key=canonical_url(j.job_url) or f'{j.source}:{j.company}:{j.job_title}'
        if key in seen: continue
        seen.add(key); dedup.append(j)
    run_id = stamp()
    jp, cp = write_artifacts(dedup, run_id)
    sheet = None
    if args.write_sheet:
        sid=args.spreadsheet_id
        if not sid:
            sid=json.loads((BASE/'sheets/google_sheet_created.json').read_text())['spreadsheetId']
        sheet = update_sheet(dedup, sid, replace_existing_source_rows=args.replace_existing_source_rows)
    status = collection_status(len(dedup), errors)
    manifest = {'run_id':run_id,'status':status,'count':len(dedup),'errors':errors,'json':str(jp),'csv':str(cp),'sheet':sheet}
    (DATA_DIR / f'manifest_{run_id}.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(manifest, ensure_ascii=False))
    return {'success': 0, 'degraded': 1, 'error': 2}[status]

if __name__ == '__main__':
    raise SystemExit(main())
