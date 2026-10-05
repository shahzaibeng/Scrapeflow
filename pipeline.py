"""Orchestrates scrape -> enrich -> clean -> upsert -> expire."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from loguru import logger

from config import Settings, settings as default_settings
from database import (
    UpsertResult,
    count_by_status,
    fetch_existing_state,
    get_session,
    init_db,
    mark_expired_tenders,
    upsert_tenders,
)
from models import RawTender, TenderStatus, utcnow
from scraper import JavaScriptRenderedError, PPRAScraper

DATE_FORMATS: tuple[str, ...] = (
    "%b %d, %Y",      # Oct 02, 2026
    "%B %d, %Y",      # October 02, 2026
    "%d %b %Y",       # 02 Oct 2026
    "%d %B %Y",       # 02 October 2026
    "%d-%m-%Y",
    "%d/%m/%Y",
    "%d.%m.%Y",
    "%Y-%m-%d",
    "%d-%b-%Y",
    "%d-%b-%y",
)
TIME_FORMATS: tuple[str, ...] = ("%I:%M %p", "%I:%M%p", "%H:%M", "%H:%M:%S", "%I %p")
CLOSED_BADGES = ("cancel", "closed", "withdrawn", "terminated", "annulled")
ISO_DT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")


# ---------------------------------------------------------------------------
# Cleaning
# ---------------------------------------------------------------------------

def parse_date(raw: str | None) -> date | None:
    if not raw:
        return None
    text = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", raw.strip(), flags=re.I)
    text = re.sub(r"\s+", " ", text)
    if ISO_DT_RE.match(text):
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
        except ValueError:
            pass
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def parse_time(raw: str | None) -> dtime | None:
    if not raw:
        return None
    text = re.sub(r"\s+", " ", raw.strip().upper().replace(".", ""))
    for fmt in TIME_FORMATS:
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    return None


def parse_closing(date_raw: str | None, time_raw: str | None, tz: ZoneInfo) -> datetime | None:
    """Combine portal date + time (local PKT) into an aware UTC datetime."""
    if date_raw and ISO_DT_RE.match(date_raw.strip()):
        try:
            parsed = datetime.fromisoformat(date_raw.strip().replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=tz)
            return parsed.astimezone(timezone.utc)
        except ValueError:
            pass
    day = parse_date(date_raw)
    if day is None:
        return None
    # No time published -> treat the whole closing day as open.
    clock = parse_time(time_raw) or dtime(23, 59, 59)
    return datetime.combine(day, clock, tzinfo=tz).astimezone(timezone.utc)


def derive_status(badges: list[str], closing_utc: datetime | None, now: datetime) -> str:
    lowered = " ".join(badges).lower()
    if any(word in lowered for word in CLOSED_BADGES):
        return TenderStatus.CLOSED.value
    if closing_utc is not None and closing_utc < now:
        return TenderStatus.EXPIRED.value
    return TenderStatus.ACTIVE.value


def build_category(raw: RawTender) -> str | None:
    parts = [p for p in (raw.procurement_category, raw.sector) if p]
    if not parts:
        return None
    if len(parts) == 2 and parts[0].lower() == parts[1].lower():
        parts = parts[:1]
    return " / ".join(parts)[:255]


def normalise(raw: RawTender, tz: ZoneInfo, now: datetime) -> dict[str, Any] | None:
    """Convert a RawTender into a DB row dict. Returns None if unusable."""
    ts = (raw.ts_number or "").strip().upper()
    if not ts:
        return None
    closing = parse_closing(raw.closing_date_raw, raw.closing_time_raw, tz)
    published = parse_date(raw.advertised_raw)
    if raw.closing_date_raw and closing is None:
        logger.debug("{}: unparseable closing date {!r} {!r}", ts, raw.closing_date_raw, raw.closing_time_raw)
    if raw.advertised_raw and published is None:
        logger.debug("{}: unparseable advertised date {!r}", ts, raw.advertised_raw)

    return {
        "ts_number": ts,
        "procuring_agency": (raw.procuring_agency or None) and raw.procuring_agency[:512],
        "title": raw.title,
        "category": build_category(raw),
        "publishing_date": published,
        "closing_datetime": closing,
        "document_url": raw.document_url or raw.advertisement_url,
        "detail_url": raw.detail_url,
        "status": derive_status(raw.portal_status, closing, now),
    }


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class PipelineOptions:
    max_pages: int | None = None
    fetch_details: bool | None = None
    refresh_details: bool = False
    dry_run: bool = False


@dataclass(slots=True)
class PipelineStats:
    pages: int = 0
    scraped: int = 0
    details_fetched: int = 0
    details_skipped: int = 0
    invalid: int = 0
    db: UpsertResult = field(default_factory=UpsertResult)
    expired_marked: int = 0
    status_totals: dict[str, int] = field(default_factory=dict)
    elapsed: float = 0.0
    aborted: str | None = None


async def _needs_detail(items: list[RawTender], refresh: bool, dry_run: bool) -> list[RawTender]:
    if refresh or dry_run:
        return list(items)
    async with get_session() as session:
        known = await fetch_existing_state(session, (t.ts_number for t in items))
    return [
        t for t in items
        if t.ts_number not in known
        or not known[t.ts_number].has_document
        or not known[t.ts_number].has_category
    ]


async def run_pipeline(
    options: PipelineOptions | None = None, cfg: Settings | None = None
) -> PipelineStats:
    opts = options or PipelineOptions()
    cfg = cfg or default_settings
    want_details = cfg.fetch_details if opts.fetch_details is None else opts.fetch_details
    tz = cfg.tz
    stats = PipelineStats()
    started = time.perf_counter()

    if not opts.dry_run:
        await init_db()

    async with PPRAScraper(cfg) as scraper:
        try:
            async for listing in scraper.iter_pages(max_pages=opts.max_pages):
                stats.pages += 1
                stats.scraped += len(listing.items)

                if want_details:
                    targets = await _needs_detail(listing.items, opts.refresh_details, opts.dry_run)
                    stats.details_skipped += len(listing.items) - len(targets)
                    stats.details_fetched += await scraper.enrich_with_details(targets)

                now = utcnow()
                rows: list[dict[str, Any]] = []
                for raw in listing.items:
                    try:
                        row = normalise(raw, tz, now)
                    except Exception as exc:
                        logger.warning("Normalisation failed for {}: {}", raw.ts_number, exc)
                        row = None
                    if row is None:
                        stats.invalid += 1
                    else:
                        rows.append(row)

                if opts.dry_run:
                    for row in rows[:3]:
                        logger.info("[dry-run] {}", row)
                    continue

                result = await upsert_tenders(rows)
                stats.db += result
                logger.success(
                    "Page {} saved: {} inserted, {} updated, {} failed | running total {} ins / {} upd",
                    listing.page, result.inserted, result.updated, result.failed,
                    stats.db.inserted, stats.db.updated,
                )
        except JavaScriptRenderedError as exc:
            stats.aborted = str(exc)
            logger.error(str(exc))

    if not opts.dry_run:
        stats.expired_marked = await mark_expired_tenders()
        if stats.expired_marked:
            logger.info("Marked {} tenders as EXPIRED", stats.expired_marked)
        stats.status_totals = await count_by_status()

    stats.elapsed = time.perf_counter() - started
    return stats
