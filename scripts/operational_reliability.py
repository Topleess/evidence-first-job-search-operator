#!/usr/bin/env python3
"""Reusable reliability helpers for collection and send-queue wrappers."""
from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
import json
from pathlib import Path
import re
import sqlite3
import urllib.parse


def collection_status(row_count: int, errors: Sequence[object]) -> str:
    """Classify a run as success, degraded (partial), or error (empty)."""
    if errors:
        return "degraded" if row_count > 0 else "error"
    return "success"


APPLICATION_STATUSES = {
    "applied", "submitted", "application_submitted",
    "interview", "interviewing", "screening", "test_task", "offer",
    "rejected", "withdrawn", "accepted", "hired",
}
NON_APPLICATION_ACTIONS = {
    "outreach", "recruiter_outreach", "recruiter_message", "message", "email", "dm",
    "linkedin_message", "linkedin_note", "telegram_message", "direct_message",
    "cold_email", "recruiter_dm", "outreach_sent", "message_sent", "email_sent", "dm_sent",
}
SUPPORTED_SQLITE_TABLES = ("application_receipts", "submission_receipts")
SQLITE_ROW_LIMIT = 10_000
_SOURCE_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")


class EvidenceStoreError(RuntimeError):
    """Duplicate evidence could not be read safely; callers must fail closed."""


def _host_is(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def _source_name(value: object) -> str:
    source = str(value or "").strip().lower()
    aliases = {
        "linkedin_public": "linkedin",
        "headhunter": "hh",
        "hh.ru": "hh",
        "greenhouse.io": "greenhouse",
    }
    source = aliases.get(source, source)
    return source if _SOURCE_RE.fullmatch(source) else ""


def _qualified_external_id(external_id: str, source: str) -> str:
    value = str(external_id or "").strip().lower()
    if not value:
        return ""
    if ":" in value:
        prefix, identifier = value.split(":", 1)
        prefix = _source_name(prefix)
        identifier = identifier.strip()
        if prefix and identifier:
            return f"id:{prefix}:{identifier}"
        return ""
    source = _source_name(source)
    return f"id:{source}:{value}" if source else ""


def _parse_http_url(url: str) -> urllib.parse.ParseResult | None:
    value = str(url or "").strip()
    if not value or any(char.isspace() for char in value):
        return None
    if "://" not in value:
        # Accept normal scheme-less web URLs, but not malformed pseudo-schemes.
        if value.startswith(":") or ":" in value.split("/", 1)[0]:
            return None
        value = "https://" + value
    try:
        parsed = urllib.parse.urlparse(value)
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not host:
        return None
    if "." not in host and host != "localhost":
        return None
    netloc = host
    if port is not None:
        netloc += f":{port}"
    return parsed._replace(scheme=parsed.scheme.lower(), netloc=netloc)


def vacancy_keys(url: str = "", external_id: str = "", *, source: str = "") -> set[str]:
    """Return source-qualified IDs and a conservative canonical HTTP URL.

    Unknown hosts retain all query parameters and fragments. Only source formats
    with established identity/tracking rules receive source-specific rewriting.
    Invalid URLs and unqualified bare IDs produce no identity.
    """
    keys: set[str] = set()
    parsed = _parse_http_url(url)
    inferred_source = _source_name(source)

    if parsed is not None:
        host = (parsed.hostname or "").lower().rstrip(".")
        path = re.sub(r"/{2,}", "/", parsed.path or "/")
        if path != "/":
            path = path.rstrip("/")
        query_pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        fragment = parsed.fragment

        if _host_is(host, "linkedin.com"):
            inferred_source = "linkedin"
            match = re.fullmatch(r"/jobs/view/(?:[^/]+-)?(\d+)", path)
            if match:
                job_id = match.group(1)
                keys.add(f"id:linkedin:{job_id}")
                path = f"/jobs/view/{job_id}"
            tracking = {"trk", "trackingid", "refid", "ref_src"}
            query_pairs = [
                (key, value) for key, value in query_pairs
                if not key.lower().startswith("utm_") and key.lower() not in tracking
            ]
        elif _host_is(host, "hh.ru"):
            inferred_source = "hh"
            match = re.fullmatch(r"/(?:vacancy|vagas)/(\d+)", path)
            if not match:
                vacancy_ids = [value for key, value in query_pairs if key.lower() == "vacancyid"]
                match_id = vacancy_ids[0] if vacancy_ids and vacancy_ids[0].isdigit() else ""
            else:
                match_id = match.group(1)
            if match_id:
                keys.add(f"id:hh:{match_id}")
                path = f"/vacancy/{match_id}"
                query_pairs = []
                fragment = ""
        elif host in {"boards.greenhouse.io", "job-boards.greenhouse.io"}:
            inferred_source = "greenhouse"
            match = re.fullmatch(r"/[^/]+/jobs/(\d+)", path)
            if match:
                keys.add(f"id:greenhouse:{match.group(1)}")
        elif host == "jobs.lever.co":
            inferred_source = "lever"
            match = re.fullmatch(r"/([^/]+)/([^/]+)", path)
            if match:
                keys.add(f"id:lever:{match.group(1).lower()}/{match.group(2).lower()}")
        elif host == "apply.workable.com":
            inferred_source = "workable"
            match = re.fullmatch(r"/([^/]+)/j/([^/]+)", path)
            if match:
                keys.add(f"id:workable:{match.group(1).lower()}/{match.group(2).lower()}")
        elif host == "jobs.ashbyhq.com":
            inferred_source = "ashby"
            match = re.fullmatch(r"/([^/]+)/([^/]+)", path)
            if match:
                keys.add(f"id:ashby:{match.group(1).lower()}/{match.group(2).lower()}")

        normalized = urllib.parse.urlunparse((
            parsed.scheme, parsed.netloc, path, "",
            urllib.parse.urlencode(sorted(query_pairs)), fragment,
        ))
        keys.add("url:" + normalized)

    qualified = _qualified_external_id(external_id, inferred_source)
    if qualified:
        keys.add(qualified)
    return keys


def _walk_mappings(value: object) -> Iterator[Mapping[str, object]]:
    if isinstance(value, Mapping):
        yield value
        for nested in value.values():
            yield from _walk_mappings(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk_mappings(nested)


def _strict_bool(value: object, *, field: str) -> bool:
    if value is None or value == "":
        return False
    if value is True or (type(value) is int and value == 1) or (
        isinstance(value, str) and value.strip().lower() in {"1", "true"}
    ):
        return True
    if value is False or (type(value) is int and value == 0) or (
        isinstance(value, str) and value.strip().lower() in {"0", "false"}
    ):
        return False
    raise EvidenceStoreError(f"invalid boolean value for {field}: {value!r}")


def _outreach_discriminator(value: object) -> str:
    return re.sub(r"[\s_-]+", "_", str(value or "").strip().lower())


def _receipt_is_application(receipt: Mapping[str, object]) -> bool:
    status = str(receipt.get("status") or receipt.get("stop_reason") or "").strip().lower()
    submitted = _strict_bool(receipt.get("submitted"), field="submitted")
    applied = _strict_bool(receipt.get("applied"), field="applied")
    verified = _strict_bool(receipt.get("read_back_verified"), field="read_back_verified")
    if submitted or applied or status in APPLICATION_STATUSES:
        return True
    action_values = {
        _outreach_discriminator(nested.get(field))
        for nested in _walk_mappings(receipt)
        for field in ("action", "action_type", "kind", "channel", "send_status")
    }
    if action_values.intersection(NON_APPLICATION_ACTIONS):
        return False
    # Receipt stores passed here are application evidence stores. Legacy submit
    # scripts sometimes emitted only a verified read-back, so preserve that
    # schema while allowing explicit outreach actions to opt out above.
    return verified


def _record_identity(record: Mapping[str, object]) -> tuple[str, str, str]:
    url = next((str(record.get(name) or "") for name in ("job_url", "vacancy_url", "job", "url") if record.get(name)), "")
    external_id = next((str(record.get(name) or "") for name in (
        "external_vacancy_id", "source_job_id", "vacancy_id", "job_id", "id"
    ) if record.get(name)), "")
    source = _source_name(record.get("source") or record.get("job_source"))
    return url, external_id, source


@dataclass(frozen=True)
class DuplicateGuard:
    submitted_keys: frozenset[str]

    @classmethod
    def from_sources(
        cls, *, applications: Iterable[Mapping[str, object]] = (),
        receipt_dir: Path | None = None, receipt_dirs: Iterable[Path] = (),
        sqlite_path: Path | None = None,
    ) -> "DuplicateGuard":
        keys: set[str] = set()
        for row in applications:
            if not _receipt_is_application(row):
                continue
            url, external_id, source = _record_identity(row)
            keys.update(vacancy_keys(url, external_id, source=source))

        directories = list(receipt_dirs)
        if receipt_dir is not None:
            directories.append(receipt_dir)
        seen_paths: set[Path] = set()
        for directory in directories:
            directory = Path(directory)
            if not directory.exists():
                raise EvidenceStoreError(f"receipt store does not exist: {directory}")
            if not directory.is_dir():
                raise EvidenceStoreError(f"receipt store is not a directory: {directory}")
            try:
                paths = sorted(directory.rglob("*.json"))
            except OSError as exc:
                raise EvidenceStoreError(f"cannot enumerate receipt store {directory}: {exc}") from exc
            for path in paths:
                resolved = path.resolve()
                if resolved in seen_paths:
                    continue
                seen_paths.add(resolved)
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                    raise EvidenceStoreError(f"cannot read receipt {path}: {exc}") from exc
                for receipt in _walk_mappings(payload):
                    try:
                        confirmed = _receipt_is_application(receipt)
                    except EvidenceStoreError as exc:
                        raise EvidenceStoreError(f"invalid receipt {path}: {exc}") from exc
                    if not confirmed:
                        continue
                    url, external_id, source = _record_identity(receipt)
                    keys.update(vacancy_keys(url, external_id, source=source))

        if sqlite_path is not None:
            sqlite_path = Path(sqlite_path)
            if not sqlite_path.is_file():
                raise EvidenceStoreError(f"SQLite receipt store does not exist: {sqlite_path}")
            uri = sqlite_path.resolve().as_uri() + "?mode=ro"
            try:
                with closing(sqlite3.connect(uri, uri=True, timeout=5)) as con:
                    con.row_factory = sqlite3.Row
                    existing = {
                        row[0] for row in con.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'"
                        ).fetchall()
                    }
                    supported = existing.intersection(SUPPORTED_SQLITE_TABLES)
                    if not supported:
                        raise EvidenceStoreError(
                            f"SQLite receipt store has no supported receipt table: {sqlite_path}"
                        )
                    for table in SUPPORTED_SQLITE_TABLES:
                        if table not in supported:
                            continue
                        quoted = '"' + table.replace('"', '""') + '"'
                        columns = [row[1].lower() for row in con.execute(f"PRAGMA table_info({quoted})")]
                        identity = {"job_url", "vacancy_url", "url", "external_vacancy_id", "source_job_id", "vacancy_id", "job_id", "id"}.intersection(columns)
                        evidence = {"status", "submitted", "applied", "read_back_verified"}.intersection(columns)
                        if not identity or not evidence:
                            raise EvidenceStoreError(f"unsupported schema for SQLite table {table}")
                        selected = [name for name in (
                            "job_url", "vacancy_url", "url", "external_vacancy_id", "source_job_id",
                            "vacancy_id", "job_id", "id", "source", "job_source", "action", "action_type",
                            "kind", "channel", "send_status",
                            "status", "submitted", "applied", "read_back_verified",
                        ) if name in columns]
                        select_sql = ", ".join('"' + name + '"' for name in selected)
                        rows = con.execute(
                            f"SELECT {select_sql} FROM {quoted} LIMIT ?", (SQLITE_ROW_LIMIT + 1,)
                        ).fetchall()
                        if len(rows) > SQLITE_ROW_LIMIT:
                            raise EvidenceStoreError(
                                f"SQLite table {table} exceeds inspection limit {SQLITE_ROW_LIMIT}"
                            )
                        for row in rows:
                            record = {key.lower(): row[key] for key in row.keys()}
                            if not _receipt_is_application(record):
                                continue
                            url, external_id, source = _record_identity(record)
                            keys.update(vacancy_keys(url, external_id, source=source))
            except EvidenceStoreError:
                raise
            except sqlite3.Error as exc:
                raise EvidenceStoreError(f"cannot inspect SQLite receipt store {sqlite_path}: {exc}") from exc

        return cls(frozenset(keys))

    def is_duplicate(self, url: str = "", *, external_id: str = "", source: str = "") -> bool:
        return bool(vacancy_keys(url, external_id, source=source).intersection(self.submitted_keys))
