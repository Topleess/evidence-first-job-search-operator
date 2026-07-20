#!/usr/bin/env python3
"""Telegram vacancy post parser for the shared Vacancies Inbox.

MVP scope: parse explicit user-provided/forwarded Telegram posts or channel
exports. This module does not read Telegram by itself and never sends messages.
"""
from __future__ import annotations

import hashlib
import re
import urllib.parse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

VACANCY_KEYWORDS_RU = [
    "ищем", "вакансия", "вакансии", "удаленно", "удалёнка", "зарплата", "ставка",
    "отклик", "резюме", "hr", "продакт", "product", "руководитель продукта",
]
VACANCY_KEYWORDS_EN = [
    "hiring", "vacancy", "remote", "product manager", "ai product", "founder associate",
    "apply", "cv", "resume", "product owner", "product lead",
]
ROLE_HINTS = [
    "AI Product Manager", "Product Manager", "Product Owner", "Product Lead",
    "Founder Associate", "Chief of Staff", "Product Operations", "Project Manager",
    "Delivery Manager", "Growth Product Manager", "Руководитель продукта", "Продакт",
    "Продуктовый менеджер", "Менеджер продукта",
]
GOOD_TERMS = {
    "product": ["product", "продукт", "продакт", "owner", "pm"],
    "ai": ["ai", "llm", "rag", "automation", "автоматиза", "нейро", "ии"],
    "lead": ["lead", "head", "руковод", "лид"],
    "startup_operator": ["founder", "chief of staff", "operator", "founder's office"],
    "marketplace_growth": ["marketplace", "growth", "gmv", "маркетплейс"],
    "remote": ["remote", "удален", "удалён", "relocation", "релокац"],
}
BAD_TERMS = [
    "frontend developer", "backend developer", "full stack", "devops", "sre", "designer",
    "дизайнер", "sales manager", "account executive", "sdr", "junior", "intern",
    "стажер", "стажёр", "ppc", "media buyer", "content manager",
    "маркетолог", "product marketing", "продакт-маркетолог", "pmm", "growth-маркетолог",
    "менеджер по работе с клиентами", "клиентского сервиса", "customer support", "fashion",
]

@dataclass
class TelegramJobRow:
    status: str = "needs_review"
    source: str = "telegram"
    search_filter: str = "telegram_forwarded"
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
    next_action: str = "review_post_then_draft_reply"
    enrichment_status: str = "raw_telegram_post"
    notes: str = ""


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        value = " ".join(str(v) for v in value if v is not None)
    return re.sub(r"\s+", " ", str(value)).strip()


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonicalize_url(url: str) -> str:
    url = clean_text(url)
    if not url:
        return ""
    if url.startswith("@"):
        return "https://t.me/" + url[1:]
    if url.startswith("t.me/"):
        url = "https://" + url
    parsed = urllib.parse.urlparse(url)
    if not parsed.netloc:
        return url
    qs = []
    for k, v in urllib.parse.parse_qsl(parsed.query, keep_blank_values=False):
        low = k.lower()
        if low.startswith("utm_") or low in {"ref", "trk", "fbclid", "gclid"}:
            continue
        qs.append((k, v))
    return urllib.parse.urlunparse((parsed.scheme or "https", parsed.netloc.lower(), parsed.path.rstrip("/"), "", urllib.parse.urlencode(qs), ""))


def dedupe_key(row: TelegramJobRow, channel: str = "") -> str:
    if row.job_url:
        raw = f"telegram:{canonicalize_url(row.job_url)}"
    else:
        raw = f"telegram:{channel.lower()}:{row.company.lower()}:{row.job_title.lower()}:{row.remote_location.lower()}:{row.salary.lower()}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def is_vacancy_like(text: str) -> bool:
    low = text.lower()
    return any(k.lower() in low for k in VACANCY_KEYWORDS_RU + VACANCY_KEYWORDS_EN)


def extract_urls(text: str) -> list[str]:
    urls = re.findall(r"https?://[^\s)\]>\"']+|t\.me/[^\s)\]>\"']+|@[A-Za-z0-9_]{4,}", text)
    out: list[str] = []
    for u in urls:
        cu = canonicalize_url(u.rstrip(".,;"))
        if cu not in out:
            out.append(cu)
    return out


def extract_contact(text: str, urls: list[str]) -> tuple[str, str, str]:
    # Prefer explicit Telegram handles / t.me links as contact_url; emails stay in hr_email.
    handle_match = re.search(r"(?:contact|контакт|писать|отклик|apply|cv|резюме)[:\s\-–—]*(@[A-Za-z0-9_]{4,})", text, re.I)
    if handle_match:
        return "", canonicalize_url(handle_match.group(1)), "telegram_handle"
    for u in urls:
        if "t.me/" in u and not re.search(r"/\d+$", u):
            return "", u, "telegram_link"
    email_match = re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text)
    if email_match:
        return email_match.group(0), "", "email_in_post"
    return "", "", ""


def extract_salary(text: str) -> str:
    patterns = [
        r"(?:от\s*)?\d{2,4}[\s\u00a0]*(?:000|к|k)?\s*(?:[-–—]|до)\s*\d{2,4}[\s\u00a0]*(?:000|к|k)?\s*(?:₽|руб|rub|usd|eur|\$|€)?",
        r"(?:от|до)\s*\d{2,4}[\s\u00a0]*(?:000|к|k)?\s*(?:₽|руб|rub|usd|eur|\$|€)?",
        r"\$\s*\d{2,4}[\s\u00a0]*(?:k|000)?(?:\s*[-–—]\s*\$?\d{2,4}[\s\u00a0]*(?:k|000)?)?",
        r"\d{2,4}[\s\u00a0]*(?:k|000)?\s*(?:usd|eur|rub|₽|руб|\$|€)",
    ]
    for p in patterns:
        m = re.search(p, text, re.I)
        if m:
            return clean_text(m.group(0))
    return ""


def extract_remote_location(text: str) -> str:
    low = text.lower()
    bits: list[str] = []
    if any(k in low for k in ["удален", "удалён", "remote", "remotely"]):
        bits.append("remote")
    m = re.search(r"(?:location|локация|город|релокация|relocation)[:\s\-–—]+([^\n.;]{2,80})", text, re.I)
    if m:
        bits.append(clean_text(m.group(1)))
    elif any(k in low for k in ["кипр", "cyprus"]):
        bits.append("Cyprus")
    elif any(k in low for k in ["сербия", "serbia"]):
        bits.append("Serbia")
    return "; ".join(dict.fromkeys(bits))


def extract_title(text: str) -> str:
    # Markdown/bullet common forms: "Ищем Product Manager", "Вакансия: ...", title line.
    lines = [clean_text(re.sub(r"^[#*•\-–—\s]+", "", l)) for l in text.splitlines() if clean_text(l)]
    for line in lines[:8]:
        m = re.search(r"(?:ищем|вакансия|hiring|role|позиция)[:\s\-–—]+(.{3,90})", line, re.I)
        if m:
            return clean_text(m.group(1)).strip(" .")
    joined = " | ".join(lines[:6])
    for hint in ROLE_HINTS:
        m = re.search(re.escape(hint), joined, re.I)
        if m:
            return m.group(0)
    return lines[0][:90] if lines else ""


def extract_company(text: str) -> str:
    patterns = [
        r"(?:company|компания|в компанию|работодатель)[:\s\-–—]+([A-ZА-ЯЁ0-9][^\n,.;]{1,60})",
        r"@\s*([A-ZА-ЯЁ][A-Za-zА-Яа-яЁё0-9 ._-]{1,40})\s+(?:ищет|is hiring|hiring)",
    ]
    for p in patterns:
        m = re.search(p, text, re.I)
        if m:
            value = clean_text(m.group(1)).strip(" .")
            value = re.sub(r"\s+(ищет|is hiring|hiring).*$", "", value, flags=re.I)
            return value[:80]
    return ""


def score_post(title: str, text: str, remote_location: str, salary: str) -> tuple[int, str, str]:
    low = " ".join([title, text, remote_location, salary]).lower()
    points = 30
    reasons: list[str] = []
    weights = {"product": 25, "ai": 18, "lead": 10, "startup_operator": 10, "marketplace_growth": 10, "remote": 10}
    for label, terms in GOOD_TERMS.items():
        if any(t in low for t in terms):
            points += weights[label]
            reasons.append(label)
    if salary:
        points += 5
        reasons.append("salary_signal")
    if any(t in low for t in BAD_TERMS):
        points -= 25
        reasons.append("off_target_risk")
    if not is_vacancy_like(text):
        points -= 25
        reasons.append("not_vacancy_like")
    points = max(0, min(100, points))
    action = "review_post_then_draft_reply" if points >= 65 else "skip_likely_or_review_later"
    return points, ", ".join(reasons) or "weak keyword signal", action


def parse_telegram_post(text: str, *, channel: str = "", post_url: str = "", published_at: str = "", search_filter: str = "telegram_forwarded") -> TelegramJobRow:
    text = text.strip()
    urls = extract_urls(text)
    if post_url:
        source_url = canonicalize_url(post_url)
    else:
        post_links = [u for u in urls if "t.me/" in u and re.search(r"/\d+$", u)]
        source_url = post_links[0] if post_links else ""
    hr_email, contact_url, contact_kind = extract_contact(text, urls)
    title = extract_title(text)
    company = extract_company(text)
    salary = extract_salary(text)
    remote_location = extract_remote_location(text)
    fit, why, action = score_post(title, text, remote_location, salary)

    risk_flags: list[str] = []
    if not company:
        risk_flags.append("telegram_unclear_company")
    if not title:
        risk_flags.append("telegram_unclear_title")
    if not salary:
        risk_flags.append("no_salary")
    if not remote_location:
        risk_flags.append("unclear_remote")
    if contact_url and not source_url:
        risk_flags.append("telegram_contact_only")
    if not is_vacancy_like(text):
        risk_flags.append("not_vacancy_like")

    status = "needs_review" if risk_flags or fit < 80 else "scored"
    row = TelegramJobRow(
        status=status,
        search_filter=search_filter,
        job_title=title,
        company=company,
        salary=salary,
        remote_location=remote_location,
        published_at=published_at,
        job_url=source_url or contact_url,
        recruiter_hh_profile="",
        hr_email=hr_email,
        contact_source=contact_kind or ("telegram_post_url" if source_url else ""),
        fit_score=str(fit),
        why_relevant=why,
        next_action=action,
        notes="; ".join([
            "source_adapter=telegram_forwarded_message_v1",
            f"channel={channel or 'unknown'}",
            f"dedupe_key_pending=true",
            f"risk_flags={','.join(risk_flags) if risk_flags else 'none'}",
            "No Telegram DM/send/apply action performed.",
            f"collected_at={utc_iso()}",
        ]),
    )
    dk = dedupe_key(row, channel=channel)
    row.notes = row.notes.replace("dedupe_key_pending=true", f"dedupe_key={dk}")
    return row


def row_to_dict(row: TelegramJobRow) -> dict[str, str]:
    return {k: clean_text(v) for k, v in asdict(row).items()}
