#!/usr/bin/env python3
"""Dependency-free parser for saved public HH vacancy HTML."""
from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


VACANCY_RE = re.compile(r"/vacancy/(\d+)(?:[/?#]|$)")


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _canonical_vacancy(href: str) -> tuple[str, str] | None:
    match = VACANCY_RE.search(href)
    if match is None:
        return None
    vacancy_id = match.group(1)
    host = (urlsplit(href).hostname or "hh.ru").lower()
    if host not in {"hh.ru", "www.hh.ru", "hh.kz", "www.hh.kz"}:
        return None
    suffix = "kz" if host.endswith("hh.kz") else "ru"
    return vacancy_id, f"https://hh.{suffix}/vacancy/{vacancy_id}"


class _HHSearchParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.card_depth: int | None = None
        self.card: dict[str, Any] | None = None
        self.field: str | None = None
        self.field_depth: int | None = None
        self.rows: list[dict[str, Any]] = []
        self.loose_anchor: dict[str, Any] | None = None
        self.loose_anchor_depth: int | None = None
        self.loose_rows: list[dict[str, Any]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.depth += 1
        attributes = {key: value or "" for key, value in attrs}
        qa = attributes.get("data-qa", "")
        if self.card is None and qa == "vacancy-serp__vacancy":
            self.card_depth = self.depth
            self.card = {"title": [], "company": [], "location": [], "salary": []}
        if tag == "a" and _canonical_vacancy(attributes.get("href", "")):
            target: dict[str, Any] = self.card if self.card is not None else {"title": []}
            target["href"] = attributes["href"]
            self.field = "title"
            self.field_depth = self.depth
            if self.card is None:
                self.loose_anchor = target
                self.loose_anchor_depth = self.depth
        elif self.card is not None:
            field = None
            if qa in {"vacancy-serp__vacancy-employer", "vacancy-serp__vacancy-employer-text"}:
                field = "company"
            elif qa in {"vacancy-serp__vacancy-address", "vacancy-serp__vacancy-address-text"}:
                field = "location"
            elif qa in {"vacancy-serp__vacancy-compensation", "vacancy-serp__vacancy-salary"}:
                field = "salary"
            if field:
                self.field = field
                self.field_depth = self.depth

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self.field is None:
            return
        target = self.card if self.card is not None else self.loose_anchor
        if target is not None:
            target.setdefault(self.field, []).append(data)

    def handle_endtag(self, tag: str) -> None:
        if self.loose_anchor is not None and self.loose_anchor_depth == self.depth:
            self.loose_rows.append(self.loose_anchor)
            self.loose_anchor = None
            self.loose_anchor_depth = None
        if self.field_depth == self.depth:
            self.field = None
            self.field_depth = None
        if self.card is not None and self.card_depth == self.depth:
            self.rows.append(self.card)
            self.card = None
            self.card_depth = None
            self.field = None
            self.field_depth = None
        self.depth = max(0, self.depth - 1)


def parse_hh_html(path: str | Path, *, limit: int) -> list[dict[str, Any]]:
    """Parse saved HH search HTML into LocalFunnel-compatible public rows."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    parser = _HHSearchParser()
    parser.feed(Path(path).read_text(encoding="utf-8"))
    candidates = parser.rows or parser.loose_rows
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in candidates:
        canonical = _canonical_vacancy(str(candidate.get("href") or ""))
        title = _clean("".join(candidate.get("title") or []))
        if canonical is None or not title:
            continue
        vacancy_id, url = canonical
        if vacancy_id in seen:
            continue
        seen.add(vacancy_id)
        rows.append(
            {
                "source": "hh",
                "external_vacancy_id": vacancy_id,
                "job_title": title,
                "company": _clean("".join(candidate.get("company") or [])) or "Не указана",
                "job_url": url,
                "remote_location": _clean("".join(candidate.get("location") or [])),
                "salary": _clean("".join(candidate.get("salary") or [])),
                "notes": "Collected from saved public HH HTML; no login or apply action.",
            }
        )
        if len(rows) >= limit:
            break
    return rows
