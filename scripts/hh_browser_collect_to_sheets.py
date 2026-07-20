#!/usr/bin/env python3
"""Collect hh.ru search-result vacancies via public HTML pages and write to Google Sheets Vacancies Inbox.

This is a browser-assisted/HTML fallback for the VPS where api.hh.ru /vacancies
returns ddos-guard 403. It does not submit applications, does not log in, and
only collects publicly visible vacancy/employer information.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html as ihtml
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

try:
    from bs4 import BeautifulSoup
except ImportError:
    print("Install deps: uv run --with beautifulsoup4 --with google-api-python-client --with google-auth-oauthlib --with google-auth-httplib2 python scripts/hh_browser_collect_to_sheets.py", file=sys.stderr)
    raise

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "identity",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}
BASE = Path("/opt/data/job-search")
DATA_DIR = BASE / "data" / "hh" / "browser"

COLUMNS = [
    "status","source","search_filter","job_title","company","salary","remote_location","published_at","job_url",
    "company_website","careers_page","company_linkedin","hh_company_profile",
    "recruiter_name","recruiter_role","recruiter_linkedin","recruiter_hh_profile","hr_email","hr_phone","contact_source",
    "fit_score","why_relevant","next_action","enrichment_status","notes"
]

ROLE_TERMS = {
    "AI": ["ai", "llm", "ml", "machine learning", "rag", "automation", "автоматиза"],
    "Product": ["product", "продукт", "продакт", "owner", "po"],
    "Lead": ["lead", "руковод", "head", "лид"],
    "Marketplace": ["marketplace", "маркетплейс", "matching", "gmv", "growth"],
}
BAD_TERMS = [
    "devops", "sre", "designer", "дизайнер", "ppc", "google ads", "sales manager",
    "account manager", "content manager", "маркетолог", "product marketing", "продакт-маркетолог",
    "аналитик", "analyst", "recruiter", "hr manager", "qa", "support", "customer support",
]
GOOD_TITLE_TERMS = [
    "product manager", "product owner", "product lead", "product/project", "technical product",
    "ai product", "продуктовый менеджер", "продакт", "менеджер продукта", "руководитель продукта",
    "owner", "cpo", "founder associate", "product operator", "growth product",
]
TARGET_TITLE_TERMS = GOOD_TITLE_TERMS + [
    "project manager", "digital project", "руководитель проектов", "менеджер проектов",
    "chief of staff",
]
HARD_BAD_TITLE_TERMS = BAD_TERMS + [
    "fashion", "фэшн", "customer support", "служба поддержки", "product analyst",
    "product designer", "product marketing", "маркетинг продукта",
]
BAD_EXTERNAL_URL_PARTS = [
    "setka.ru", "vk.com/headhunter", "hhcdn.ru", "google.com", "apple.com", "facebook.com/headhunter",
    "telegram.org", "t.me/hh", "itunes.apple.com", "play.google.com", "yandex.ru/maps",
]

@dataclass
class Vacancy:
    status: str = "new"
    source: str = "hh.ru"
    search_filter: str = ""
    job_title: str = ""
    company: str = ""
    salary: str = ""
    remote_location: str = ""
    published_at: str = ""
    job_url: str = ""
    company_website: str = ""
    careers_page: str = ""
    company_linkedin: str = ""
    hh_company_profile: str = ""
    recruiter_name: str = ""
    recruiter_role: str = ""
    recruiter_linkedin: str = ""
    recruiter_hh_profile: str = ""
    hr_email: str = ""
    hr_phone: str = ""
    contact_source: str = ""
    fit_score: str = ""
    why_relevant: str = ""
    next_action: str = ""
    enrichment_status: str = ""
    notes: str = ""


@dataclass(frozen=True)
class SearchFilter:
    label: str
    query: str
    remote_only: bool = True
    salary_min: int = 130000


def fetch(url: str, *, timeout: int = 10) -> str:
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as error:
            last_error = error
            if attempt < 2:
                time.sleep(1.0 * (attempt + 1))
    assert last_error is not None
    raise last_error


def norm_url(url: str) -> str:
    return urllib.parse.urljoin("https://hh.ru", ihtml.unescape(url))


def clean_text(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def is_target_title(title: str) -> bool:
    title_l = clean_text(title).lower()
    if any(term in title_l for term in HARD_BAD_TITLE_TERMS):
        return False
    return any(term in title_l for term in TARGET_TITLE_TERMS)


def parse_search_filter_rows(rows: list[list[str]]) -> list[SearchFilter]:
    if not rows:
        return []
    header = [clean_text(cell).lower() for cell in rows[0]]
    index = {name: pos for pos, name in enumerate(header)}

    def cell(row: list[str], name: str) -> str:
        pos = index.get(name)
        return clean_text(row[pos]) if pos is not None and pos < len(row) else ""

    filters: list[SearchFilter] = []
    for row in rows[1:]:
        if cell(row, "enabled").lower() not in {"yes", "true", "1", "да"}:
            continue
        if cell(row, "platform").lower() not in {"hh", "hh.ru", "headhunter"}:
            continue
        salary_text = re.sub(r"\D", "", cell(row, "salary_min"))
        salary_min = int(salary_text) if salary_text else 0
        remote_only = cell(row, "remote_only").lower() in {"yes", "true", "1", "да"}
        label = cell(row, "track") or "hh.ru"
        for query in re.split(r"\s+OR\s+", cell(row, "query"), flags=re.I):
            query = clean_text(query)
            if query:
                filters.append(SearchFilter(label, query, remote_only, salary_min))
    return filters


BLOCKED_EXTERNAL_HOSTS = {
    'hh.ru', 'www.hh.ru', 'api.hh.ru', 'feedback.hh.ru', 'hhcdn.ru',
    'headhunter.ru', 'www.headhunter.ru', 'dreamjob.ru', 'www.dreamjob.ru',
    'schema.org', 'www.w3.org', 'w3.org', 'vk.com', 'm.vk.com',
}

BLOCKED_EXTERNAL_URL_PARTS = BAD_EXTERNAL_URL_PARTS + [
    'vk.com/headhunter', 'vk.com/hh.ru', 'dreamjob.ru', 'hhcdn.ru',
    'feedback.hh.ru', 'api.hh.ru', 'sentry.hh.ru'
]

BAD_EMAIL_DOMAINS = {
    'example.com', 'schema.org', 'w3.org', 'sentry.hh.ru', 'hh.ru',
    'headhunter.ru', 'localhost', 'email.com', 'dreamjob.ru'
}

BAD_EMAILS = {
    'employers@dreamjob.ru',
}


def url_host(url: str) -> str:
    try:
        return urllib.parse.urlparse(url).netloc.lower().split(':')[0]
    except Exception:
        return ''


def is_good_external_url(url: str) -> bool:
    if not url.startswith('http'):
        return False
    low = url.lower()
    host = url_host(url)
    if any(host == blocked or host.endswith('.' + blocked) for blocked in BLOCKED_EXTERNAL_HOSTS):
        return False
    if any(part in low for part in BLOCKED_EXTERNAL_URL_PARTS):
        return False
    return True


def is_bad_external_url(href: str) -> bool:
    return not is_good_external_url(href)


def first_external_url(soup: BeautifulSoup) -> str:
    for a in soup.select('a[href]'):
        href = ihtml.unescape(a.get('href') or '')
        if is_good_external_url(href) and 'setka.ru' not in href.lower():
            return href
    return ""


def find_careers_url(soup: BeautifulSoup) -> str:
    candidates = []
    for a in soup.select('a[href]'):
        href = ihtml.unescape(a.get('href') or '')
        text = clean_text(a.get_text(' ', strip=True)).lower()
        low = href.lower() + ' ' + text
        if is_good_external_url(href) and any(k in low for k in ['career', 'vacanc', 'job', 'работ', 'ваканс']):
            candidates.append(href)
    return candidates[0] if candidates else ""


def is_good_public_email(email: str) -> bool:
    e = email.strip().lower().strip('.,;:()[]{}<>')
    if e in BAD_EMAILS:
        return False
    domain = e.rsplit('@', 1)[-1]
    if domain in BAD_EMAIL_DOMAINS:
        return False
    if any(domain == bad or domain.endswith('.' + bad) for bad in BAD_EMAIL_DOMAINS):
        return False
    # Avoid obvious test/placeholder/telemetry addresses.
    if any(x in e for x in ['noreply', 'no-reply', 'sentry', 'example', 'test@']):
        return False
    return True


def find_public_email_phone_from_soup(soup: BeautifulSoup) -> tuple[str, str]:
    """Extract public contacts from visible text and mailto links only.

    Raw hh/employer HTML includes platform telemetry, scripts and DreamJob helper
    contacts. Those are not vacancy/company HR contacts, so do not parse raw HTML.
    Phone extraction stays disabled until there is an explicit contact block parser.
    """
    candidates: list[str] = []
    visible = soup.get_text(' ', strip=True)
    candidates.extend(re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", visible))
    for a in soup.select('a[href^="mailto:"]'):
        href = ihtml.unescape(a.get('href') or '')
        addr = href.split(':', 1)[-1].split('?', 1)[0]
        if addr:
            candidates.append(addr)
    emails = sorted({e.strip().strip('.,;:()[]{}<>') for e in candidates if is_good_public_email(e)})
    return (emails[0] if emails else "", "")


def find_public_email_phone(text: str) -> tuple[str, str]:
    """Extract only clearly public contact-ish emails.

    hh HTML contains many technical emails / hashes in scripts (e.g. sentry.hh.ru)
    and phone-like numeric IDs. Do not treat those as HR contacts. Phone extraction
    is intentionally disabled unless a future parser sees an explicit contact block.
    """
    emails = sorted(set(re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text)))
    emails = [e for e in emails if is_good_public_email(e)]
    # Avoid extracting phone-looking IDs from scripts/minified data.
    phone = ""
    return (emails[0] if emails else "", phone)


def score(title: str, desc: str) -> tuple[int, str, str]:
    title_l = title.lower()
    text = (title + " " + desc).lower()
    score = 30
    reasons = []

    def has_term(term: str) -> bool:
        # Short latin tokens like AI/ML/PO must be standalone words; otherwise
        # "ai" matches ordinary words and inflates scores/contact quality.
        if re.fullmatch(r"[a-z]{1,3}", term):
            return re.search(rf"(?<![a-zа-я0-9]){re.escape(term)}(?![a-zа-я0-9])", text, re.I) is not None
        return term in text

    if any(t in title_l for t in GOOD_TITLE_TERMS):
        score += 28
        reasons.append("good title")
    for label, terms in ROLE_TERMS.items():
        if any(has_term(t) for t in terms):
            score += {"Product":22,"AI":18,"Lead":10,"Marketplace":12}.get(label, 5)
            reasons.append(label)
    if any(k in text for k in ["удален", "remote", "можно удал"]):
        score += 8
        reasons.append("remote")
    # Penalize off-target titles much harder than off-target words buried in description.
    if any(b in title_l for b in BAD_TERMS):
        score -= 55
        reasons.append("off-target title")
    elif any(b in text for b in BAD_TERMS):
        score -= 12
        reasons.append("possible off-target role")
    score = max(0, min(100, score))
    if score >= 78:
        action = "review_for_draft"
    elif score >= 62:
        action = "review"
    else:
        action = "skip_likely"
    return score, ", ".join(dict.fromkeys(reasons)) or "keyword match", action


def parse_search(query: str, limit: int, pages: int = 3, *, salary_min: int = 130000, remote_only: bool = True) -> list[tuple[str, str]]:
    base_params = {
        'text': query,
        'ored_clusters': 'true',
        'enable_snippets': 'true',
    }
    if salary_min:
        base_params['salary'] = str(salary_min)
    if remote_only:
        base_params['schedule'] = 'remote'
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    (DATA_DIR / 'raw').mkdir(parents=True, exist_ok=True)
    seen = set(); out=[]
    for page in range(max(1, pages)):
        params = dict(base_params)
        params['page'] = str(page)  # hh.ru pages are zero-based in search URLs
        url = 'https://hh.ru/search/vacancy?' + urllib.parse.urlencode(params)
        html = fetch(url)
        (DATA_DIR / 'raw' / f"search_{re.sub(r'[^A-Za-z0-9]+','_',query)}_p{page}_{stamp}.html").write_text(html, encoding='utf-8')
        soup = BeautifulSoup(html, 'html.parser')
        page_added = 0
        for a in soup.select('a[href*="/vacancy/"]'):
            href = norm_url(a.get('href') or '')
            title = clean_text(a.get_text(' ', strip=True))
            if not title or '/vacancy/' not in href:
                continue
            if not is_target_title(title):
                continue
            can = href.split('?')[0]
            if can in seen:
                continue
            seen.add(can)
            out.append((title, href))
            page_added += 1
            if len(out) >= limit:
                return out
        print(f"search query={query!r} page={page} new_links={page_added} total_links={len(out)}", file=sys.stderr)
        if page_added == 0 and page > 0:
            break
        time.sleep(0.4)
    return out


def parse_detail(title_from_search: str, url: str, query: str) -> Vacancy:
    html = fetch(url)
    soup = BeautifulSoup(html, 'html.parser')
    title = clean_text((soup.select_one('[data-qa="vacancy-title"]') or soup.find('h1') or {}).get_text(' ', strip=True) if (soup.select_one('[data-qa="vacancy-title"]') or soup.find('h1')) else title_from_search)
    company_el = soup.select_one('[data-qa="vacancy-company-name"]')
    company = clean_text(company_el.get_text(' ', strip=True) if company_el else '')
    employer_link = ''
    for a in soup.select('a[href*="/employer/"]'):
        txt = clean_text(a.get_text(' ', strip=True))
        if company and company.lower() in txt.lower() or not employer_link:
            employer_link = norm_url(a.get('href') or '')
            break
    salary_el = soup.select_one('[data-qa="vacancy-salary"]')
    salary = clean_text(salary_el.get_text(' ', strip=True) if salary_el else '')
    addr_el = soup.select_one('[data-qa="vacancy-view-raw-address"]')
    addr = clean_text(addr_el.get_text(' ', strip=True) if addr_el else '')
    desc_el = soup.select_one('[data-qa="vacancy-description"]')
    desc = clean_text(desc_el.get_text(' ', strip=True) if desc_el else '')
    remote = 'remote' if re.search(r'можно удал|удален|remote', html, re.I) else ''
    loc = ' / '.join(x for x in [remote, addr] if x) or remote
    email, phone = find_public_email_phone_from_soup(soup)
    recruiter = clean_text((soup.select_one('[data-qa="vacancy-contacts__fio"]') or {}).get_text(' ', strip=True) if soup.select_one('[data-qa="vacancy-contacts__fio"]') else '')
    recruiter_profile = ''

    website = careers = ''
    if employer_link:
        try:
            time.sleep(0.4)
            emp_html = fetch(employer_link)
            emp_soup = BeautifulSoup(emp_html, 'html.parser')
            website = first_external_url(emp_soup)
            careers = find_careers_url(emp_soup)
            e2, p2 = find_public_email_phone_from_soup(emp_soup)
            email = email or e2
            phone = phone or p2
        except Exception as e:
            pass

    fit, why, action = score(title, desc)
    status = 'scored'
    enriched = 'partial' if (website or careers or employer_link or email or phone or recruiter) else 'not_found'
    contact_source = []
    if employer_link: contact_source.append('hh employer profile')
    if website: contact_source.append('public company website from hh')
    if email or phone: contact_source.append('public vacancy/employer page')
    v = Vacancy(
        status=status, search_filter=query, job_title=title, company=company, salary=salary,
        remote_location=loc, published_at='', job_url=url, company_website=website, careers_page=careers,
        hh_company_profile=employer_link, recruiter_name=recruiter, recruiter_hh_profile=recruiter_profile,
        hr_email=email, hr_phone=phone, contact_source='; '.join(contact_source), fit_score=str(fit),
        why_relevant=why, next_action=action, enrichment_status=enriched,
        notes='Collected from public hh.ru HTML; no login, no apply action.'
    )
    return v


def collect(filters: list[SearchFilter], per_query: int, max_total: int, min_score: int = 62, include_skip: bool = False, pages: int = 3) -> list[Vacancy]:
    vacancies=[]; seen=set(); consecutive_search_errors = 0
    for search_filter in filters:
        q = search_filter.query
        try:
            links = parse_search(q, per_query, pages=pages, salary_min=search_filter.salary_min, remote_only=search_filter.remote_only)
        except Exception as error:
            print(f"ERROR search query={q!r}: {error}", file=sys.stderr)
            consecutive_search_errors += 1
            if consecutive_search_errors >= 3:
                print("ERROR stopping after 3 consecutive search failures", file=sys.stderr)
                break
            continue
        consecutive_search_errors = 0
        for title, url in links:
            can=url.split('?')[0]
            if can in seen:
                continue
            seen.add(can)
            try:
                time.sleep(0.5)
                v = parse_detail(title, url, q)
                v.search_filter = search_filter.label
                if not is_target_title(v.job_title):
                    print(f"skip off-target title {v.job_title}", file=sys.stderr)
                elif include_skip or int(v.fit_score or 0) >= min_score:
                    vacancies.append(v)
                    print(f"collected {len(vacancies)} score={v.fit_score} {title}", file=sys.stderr)
                else:
                    print(f"skip score={v.fit_score} {title}", file=sys.stderr)
            except Exception as e:
                print(f"ERROR detail {url}: {e}", file=sys.stderr)
            if len(vacancies) >= max_total:
                return vacancies
    return vacancies


def write_csv(vacancies: list[Vacancy], path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', newline='', encoding='utf-8') as f:
        w=csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader(); w.writerows([{k:getattr(v,k,'') for k in COLUMNS} for v in vacancies])


def canonical_job_url(url: str) -> str:
    try:
        u = urllib.parse.urlparse(url)
        # hh vacancy id is stable in path; strip query tracking.
        return urllib.parse.urlunparse((u.scheme, u.netloc, u.path, '', '', ''))
    except Exception:
        return url.split('?', 1)[0]


def merge_sheet_values(existing: list[list[str]], vacancies: list[Vacancy]) -> tuple[list[list[str]], dict[str, int]]:
    idx = {name: i for i, name in enumerate(COLUMNS)}
    rows: list[list[str]] = []
    by_url: dict[str, int] = {}
    for raw in existing[1:] if existing else []:
        row = (raw + [''] * len(COLUMNS))[:len(COLUMNS)]
        key = canonical_job_url(row[idx['job_url']]) if row[idx['job_url']] else ''
        if key and key in by_url:
            continue
        if key:
            by_url[key] = len(rows)
        rows.append(row)
    stats = {'added': 0, 'updated': 0, 'unchanged': 0, 'total': 0}
    protected = {'applied', 'ignored', 'interview', 'offer', 'approved_to_apply', 'manual_done'}
    for vacancy in vacancies:
        new_row = [getattr(vacancy, name, '') for name in COLUMNS]
        key = canonical_job_url(new_row[idx['job_url']]) if new_row[idx['job_url']] else ''
        if key and key in by_url:
            old = rows[by_url[key]]
            merged = new_row[:]
            if old[idx['status']] in protected:
                merged[idx['status']] = old[idx['status']]
                merged[idx['next_action']] = old[idx['next_action']]
            old_notes = old[idx['notes']]
            if old_notes and old_notes not in merged[idx['notes']]:
                merged[idx['notes']] = (merged[idx['notes']] + ' | previous_notes: ' + old_notes)[:45000]
            if merged == old:
                stats['unchanged'] += 1
            else:
                rows[by_url[key]] = merged
                stats['updated'] += 1
        else:
            if key:
                by_url[key] = len(rows)
            rows.append(new_row)
            stats['added'] += 1
    stats['total'] = len(rows)
    return [COLUMNS] + rows, stats


def update_sheet(vacancies: list[Vacancy], spreadsheet_id: str, *, mode: str = "merge"):
    """Write vacancies to Vacancies Inbox.

    mode=replace: clear sheet and write only this run.
    mode=merge: read existing rows, update duplicate job_url rows, append new rows,
    preserving human statuses/notes for applied/ignored/interview/approved rows.
    """
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    creds=Credentials.from_authorized_user_file('/opt/data/google_token.json')
    svc=build('sheets','v4',credentials=creds)
    if mode == "replace":
        values = [COLUMNS] + [[getattr(v, k, '') for k in COLUMNS] for v in vacancies]
        stats = {'added': len(vacancies), 'updated': 0, 'unchanged': 0, 'total': len(vacancies)}
    else:
        existing = svc.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range="'Vacancies Inbox'!A:Z"
        ).execute().get('values', [])
        values, stats = merge_sheet_values(existing, vacancies)
    svc.spreadsheets().values().clear(spreadsheetId=spreadsheet_id, range="'Vacancies Inbox'!A:Z").execute()
    svc.spreadsheets().values().update(spreadsheetId=spreadsheet_id, range="'Vacancies Inbox'!A1", valueInputOption='RAW', body={'values':values}).execute()
    return stats


def load_search_filters(spreadsheet_id: str) -> list[SearchFilter]:
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    creds = Credentials.from_authorized_user_file('/opt/data/google_token.json')
    svc = build('sheets', 'v4', credentials=creds)
    rows = svc.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id, range="'Search Filters'!A:Z"
    ).execute().get('values', [])
    return parse_search_filter_rows(rows)


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--query', action='append', default=[])
    ap.add_argument('--per-query', type=int, default=8)
    ap.add_argument('--max-total', type=int, default=15)
    ap.add_argument('--min-score', type=int, default=62)
    ap.add_argument('--pages', type=int, default=3, help='HH search pages per query, zero-based page=0..N-1')
    ap.add_argument('--include-skip', action='store_true')
    ap.add_argument('--write-sheet', action='store_true')
    ap.add_argument('--sheet-mode', choices=['merge','replace'], default='merge')
    ap.add_argument('--spreadsheet-id', default='')
    ap.add_argument('--no-sheet-filters', action='store_true', help='Use built-in/manual queries instead of Search Filters')
    args=ap.parse_args()
    sid = args.spreadsheet_id
    if not sid:
        meta = json.loads((BASE/'sheets/google_sheet_created.json').read_text())
        sid = meta['spreadsheetId']
    if args.query:
        filters = [SearchFilter(query=q, label=q) for q in args.query]
    elif not args.no_sheet_filters:
        filters = load_search_filters(sid)
        if not filters:
            raise RuntimeError('No enabled hh.ru rows found in Search Filters')
    else:
        filters = [SearchFilter(query=q, label='built-in') for q in [
            'Product Manager удаленно', 'Product Owner удаленно', 'AI Product Manager',
            'Product Lead удаленно', 'Менеджер продукта удаленно', 'Руководитель продукта удаленно',
        ]]
    vs=collect(filters, args.per_query, args.max_total, min_score=args.min_score, include_skip=args.include_skip, pages=args.pages)
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    out_json=DATA_DIR / f'vacancies_{stamp}.json'
    out_csv=DATA_DIR / f'vacancies_{stamp}.csv'
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps([asdict(v) for v in vs], ensure_ascii=False, indent=2), encoding='utf-8')
    write_csv(vs, out_csv)
    sheet_stats = update_sheet(vs, sid, mode=args.sheet_mode) if args.write_sheet else None
    print(json.dumps({
        'count': len(vs), 'filters': len(filters), 'json': str(out_json), 'csv': str(out_csv),
        'wrote_sheet': args.write_sheet, 'sheet': sheet_stats,
    }, ensure_ascii=False))

if __name__ == '__main__':
    main()
