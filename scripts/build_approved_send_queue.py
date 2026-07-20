#!/usr/bin/env python3
"""Build Approved Send Queue from Applications.

No external messages are sent. This creates the final review surface for rows
where the user marked Approval Dashboard.user_decision = yes and Applications
status is approved_by_user_needs_final_send_approval.
"""
from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
try:
    from operational_reliability import DuplicateGuard, vacancy_keys
except ImportError:
    from scripts.operational_reliability import DuplicateGuard, vacancy_keys

ROOT='/opt/data/job-search'
SHEET_ID='1O_tUG4FsSkNOrpMTQ4savhiQjQfyZWdocPc7EgbikDI'
TOKEN='/opt/data/google_token.json'

HEADERS=['final_send_approval','send_status','channel','priority','lane','company','job_title','source','job_url','draft_text','application_id','notes']

def svc():
    creds=Credentials.from_authorized_user_file(TOKEN)
    return build('sheets','v4',credentials=creds)

def ensure_sheet(service,title):
    meta=service.spreadsheets().get(spreadsheetId=SHEET_ID).execute()
    titles={s['properties']['title']:s['properties']['sheetId'] for s in meta['sheets']}
    if title not in titles:
        service.spreadsheets().batchUpdate(spreadsheetId=SHEET_ID, body={'requests':[{'addSheet':{'properties':{'title':title}}}]}).execute()
        meta=service.spreadsheets().get(spreadsheetId=SHEET_ID).execute()
        titles={s['properties']['title']:s['properties']['sheetId'] for s in meta['sheets']}
    return titles[title]

def build_queue_rows(apps, guard: DuplicateGuard):
    """Build only never-submitted rows; return rows and duplicate suppression count."""
    rows=[]; duplicates=0; seen=set()
    if not apps:
        return rows, duplicates
    h=apps[0]; idx={x:i for i,x in enumerate(h)}
    required = {
        'application_id', 'status', 'priority', 'lane', 'channel', 'company',
        'job_title', 'source', 'job_url', 'draft_text', 'send_status',
    }
    if not required.issubset(idx):
        return rows, duplicates
    for r in apps[1:]:
        r=r+['']*len(h)
        status=r[idx.get('status',0)]
        send_status=r[idx.get('send_status',0)]
        if status!='approved_by_user_needs_final_send_approval' or send_status not in ('not_sent',''):
            continue
        external_id = r[idx['external_vacancy_id']] if 'external_vacancy_id' in idx else ''
        source = r[idx['source']] if 'source' in idx else ''
        keys = vacancy_keys(r[idx['job_url']], external_id, source=source)
        if not keys:
            continue
        if keys.intersection(seen) or guard.is_duplicate(r[idx['job_url']], external_id=external_id, source=source):
            duplicates += 1
            continue
        seen.update(keys)
        rows.append([
            '', 'not_sent', r[idx['channel']], r[idx['priority']], r[idx['lane']], r[idx['company']], r[idx['job_title']], r[idx['source']], r[idx['job_url']], r[idx['draft_text']], r[idx['application_id']], f"created_at={datetime.now(timezone.utc).isoformat(timespec='seconds')}; waiting explicit final send approval"
        ])
    return rows, duplicates


def main():
    service=svc()
    apps=service.spreadsheets().values().get(spreadsheetId=SHEET_ID, range="'Applications'!A:Z").execute().get('values',[])
    app_dicts=[]
    if apps:
        h=apps[0]
        app_dicts=[dict(zip(h,(r+['']*len(h))[:len(h)])) for r in apps[1:]]
    guard=DuplicateGuard.from_sources(
        applications=app_dicts,
        receipt_dirs=(Path(ROOT)/'applications', Path(ROOT)/'data'/'acceptance'),
    )
    rows, duplicates=build_queue_rows(apps,guard)
    sid=ensure_sheet(service,'Approved Send Queue')
    service.spreadsheets().values().clear(spreadsheetId=SHEET_ID, range="'Approved Send Queue'!A:Z").execute()
    instruction=[['Approved Send Queue — only rows with user_decision=yes appear here. Set final_send_approval=yes to authorize the actual send/apply step. Nothing is sent by this script.'],[]]
    service.spreadsheets().values().update(spreadsheetId=SHEET_ID, range="'Approved Send Queue'!A1", valueInputOption='RAW', body={'values':instruction+[HEADERS]+rows}).execute()
    try:
        service.spreadsheets().batchUpdate(spreadsheetId=SHEET_ID, body={'requests':[
            {'updateSheetProperties':{'properties':{'sheetId':sid,'gridProperties':{'frozenRowCount':3}},'fields':'gridProperties.frozenRowCount'}},
            {'setBasicFilter':{'filter':{'range':{'sheetId':sid,'startRowIndex':2,'startColumnIndex':0,'endColumnIndex':len(HEADERS)}}}},
            {'autoResizeDimensions':{'dimensions':{'sheetId':sid,'dimension':'COLUMNS','startIndex':0,'endIndex':len(HEADERS)}}},
        ]}).execute()
    except Exception:
        pass
    print({'approved_send_queue_rows':len(rows),'duplicates_suppressed':duplicates})

if __name__=='__main__':
    main()
