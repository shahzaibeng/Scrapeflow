"""PPRA / EPMS tender scraper: HTTP client, HTML + JSON parsers, pagination controller.

Source of truth for active federal tenders is the EPMS public listing
(https://epms.ppra.gov.pk/public/tenders/active-tenders), which ppra.gov.pk's
"Active Tenders" menu links to and which also lists EPADS e-tenders (TS...E).
The listing is server-rendered HTML paginated with `?page=N` (50 rows/page).
Detail pages carry the PDF links and the procurement category.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
from collections.abc import AsyncIterator, Iterable, Mapping
from types import TracebackType
from typing import Any
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup, FeatureNotFound, Tag
from loguru import logger

from config import Settings, settings as default_settings
from models import ListingPage, RawTender

TS_NUMBER_RE = re.compile(r"\bTS\d{4,}[A-Z]?\b")
SHOWING_RE = re.compile(r"Showing\s+([\d,]+)\s+of\s+([\d,]+)", re.I)
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 524}
JS_APP_MARKERS = ('id="app"', "id='app'", 'id="root"', "__NEXT_DATA__", "ng-version", "data-reactroot")


class ScraperError(Exception):
    """Base class for scraper failures."""


class FetchError(ScraperError):
    """Request failed permanently (after retries)."""


class ParseError(ScraperError):
    """Response did not have the expected structure."""


class JavaScriptRenderedError(ParseError):
    """Listing is rendered client-side; a JSON/XHR endpoint must be configured."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_soup(markup: str) -> BeautifulSoup:
    try:
        return BeautifulSoup(markup, "lxml")
    except FeatureNotFound:
        return BeautifulSoup(markup, "html.parser")


def clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = re.sub(r"\s+", " ", value).strip()
    return text or None


def node_text(node: Tag | None) -> str | None:
    return clean_text(node.get_text(" ", strip=True)) if node is not None else None


def _to_int(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(value.replace(",", ""))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# HTTP client with retries + UA rotation
# ---------------------------------------------------------------------------

class HttpClient:
    def __init__(self, cfg: Settings) -> None:
        self.cfg = cfg
        timeout = httpx.Timeout(cfg.request_timeout, connect=cfg.connect_timeout)
        self._client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            verify=cfg.verify_ssl,
            http2=False,
            limits=httpx.Limits(max_connections=cfg.detail_concurrency + 2, max_keepalive_connections=8),
            headers={
                "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Connection": "keep-alive",
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def _headers(self, extra: Mapping[str, str] | None) -> dict[str, str]:
        headers = {"User-Agent": random.choice(self.cfg.user_agents)}
        if extra:
            headers.update(extra)
        return headers

    def _backoff(self, attempt: int, response: httpx.Response | None) -> float:
        if response is not None:
            retry_after = response.headers.get("Retry-After")
            if retry_after and retry_after.isdigit():
                return min(float(retry_after), self.cfg.retry_backoff_max)
        delay = self.cfg.retry_backoff_base ** (attempt + 1) + random.uniform(0, 1)
        return min(delay, self.cfg.retry_backoff_max)

    async def get(
        self,
        url: str,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        attempts = self.cfg.max_retries + 1
        last_error: str = "unknown error"
        for attempt in range(attempts):
            response: httpx.Response | None = None
            try:
                response = await self._client.get(url, params=params, headers=self._headers(headers))
                if response.status_code in RETRYABLE_STATUS:
                    last_error = f"HTTP {response.status_code}"
                elif response.is_error:
                    raise FetchError(f"GET {response.url} -> HTTP {response.status_code}")
                else:
                    return response
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                last_error = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
            except httpx.HTTPError as exc:
                raise FetchError(f"GET {url} failed: {exc}") from exc

            if attempt < attempts - 1:
                wait = self._backoff(attempt, response)
                logger.warning(
                    "GET {} failed ({}); retry {}/{} in {:.1f}s",
                    url, last_error, attempt + 1, attempts - 1, wait,
                )
                await asyncio.sleep(wait)
        raise FetchError(f"GET {url} failed after {attempts} attempts: {last_error}")


# ---------------------------------------------------------------------------
# HTML parser (EPMS)
# ---------------------------------------------------------------------------

class EpmsHtmlParser:
    """Parses the server-rendered EPMS active-tenders listing and detail pages."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/") + "/"

    def _abs(self, href: str | None) -> str | None:
        return urljoin(self.base_url, href.strip()) if href else None

    # -- listing ------------------------------------------------------------

    def parse_listing(self, html: str, page: int) -> ListingPage:
        soup = make_soup(html)
        table = self._find_tender_table(soup)

        if table is None:
            if any(marker in html for marker in JS_APP_MARKERS) and not TS_NUMBER_RE.search(html):
                raise JavaScriptRenderedError(
                    "Listing appears to be rendered by JavaScript (no tender table in HTML). "
                    "Set JSON_API_URL to the XHR endpoint seen in the browser's Network tab."
                )
            if TS_NUMBER_RE.search(html):
                raise ParseError(f"Page {page}: TS numbers present but tender table layout not recognised")
            # Genuine empty page (past the last page).
            return ListingPage(page=page, items=[], total_reported=self._total(soup), has_next=False)

        items: list[RawTender] = []
        for row in table.select("tbody > tr"):
            try:
                tender = self._parse_row(row)
            except Exception as exc:  # one broken row must not kill the page
                logger.warning("Page {}: skipping unparseable row ({}: {})", page, type(exc).__name__, exc)
                continue
            if tender is not None:
                items.append(tender)

        return ListingPage(
            page=page,
            items=items,
            total_reported=self._total(soup),
            has_next=self._has_next(soup, page),
        )

    @staticmethod
    def _find_tender_table(soup: BeautifulSoup) -> Tag | None:
        for table in soup.find_all("table"):
            headers = " ".join(th.get_text(" ", strip=True).lower() for th in table.select("thead th"))
            if "tender no" in headers and "closing" in headers:
                return table
        return None

    @staticmethod
    def _total(soup: BeautifulSoup) -> int | None:
        match = SHOWING_RE.search(soup.get_text(" ", strip=True))
        return _to_int(match.group(2)) if match else None

    @staticmethod
    def _has_next(soup: BeautifulSoup, page: int) -> bool:
        for link in soup.select(".pagination a.page-link, .pagination a"):
            label = link.get_text(" ", strip=True).lower()
            if label.startswith("next"):
                return True
            num = _to_int(label)
            if num is not None and num > page:
                return True
        return False

    def _parse_row(self, row: Tag) -> RawTender | None:
        cells = row.find_all("td", recursive=False)
        if len(cells) < 7:
            return None

        ts_number = node_text(cells[1].find("strong")) or ""
        match = TS_NUMBER_RE.search(ts_number) or TS_NUMBER_RE.search(cells[1].get_text(" "))
        if not match:
            return None
        ts_number = match.group(0)

        details_cell = cells[2]
        title = node_text(details_cell.find("strong"))
        sector = None
        for badge in details_cell.select("small.badge"):
            classes = badge.get("class") or []
            if "text-muted" not in classes and not badge.find("i"):
                sector = node_text(badge)
                break

        org_cell = cells[3]
        agency = node_text(org_cell.select_one(".tender-org"))
        if not agency:
            agency = node_text(org_cell.find("small"))

        badges = [t for t in (node_text(b) for b in cells[4].select(".tender-badge, .badge")) if t]

        advertised = node_text(cells[5])
        closing_cell = cells[6]
        closing_date = node_text(closing_cell.find("strong")) or node_text(closing_cell)
        closing_time = node_text(closing_cell.find("small"))

        detail_href = None
        actions = cells[7] if len(cells) > 7 else row
        for link in actions.find_all("a", href=True):
            if "tender-details" in link["href"]:
                detail_href = link["href"]
                break
        if not detail_href:
            detail_href = f"/public/tenders/tender-details/{ts_number}"

        return RawTender(
            ts_number=ts_number,
            title=title,
            procuring_agency=agency,
            sector=sector,
            advertised_raw=advertised,
            closing_date_raw=closing_date,
            closing_time_raw=closing_time,
            detail_url=self._abs(detail_href),
            portal_status=list(dict.fromkeys(badges)),
            source="epms",
        )

    # -- detail ---------------------------------------------------------------

    def parse_detail(self, html: str, ts_number: str, detail_url: str) -> RawTender:
        soup = make_soup(html)
        fields: dict[str, str] = {}
        for label in soup.select(".detail-label"):
            key = (node_text(label) or "").rstrip(":").strip().lower()
            value_node = label.find_next_sibling()
            value = node_text(value_node)
            if key and value and key not in fields:
                fields[key] = value

        if not fields and not TS_NUMBER_RE.search(html):
            raise ParseError(f"Detail page for {ts_number} has no recognisable fields")

        document_url = advertisement_url = None
        for link in soup.find_all("a", href=True):
            label = (node_text(link) or "").lower()
            href = link["href"]
            looks_like_doc = "/pdf" in href or href.lower().endswith(".pdf")
            if not looks_like_doc and "download" not in label:
                continue
            if "advertisement" in label:
                advertisement_url = advertisement_url or self._abs(href)
            elif "document" in label or looks_like_doc:
                document_url = document_url or self._abs(href)

        closing_raw = fields.get("closing date & time") or fields.get("closing date")
        closing_date, closing_time = None, None
        if closing_raw:
            parts = re.split(r"\s+at\s+", closing_raw, maxsplit=1, flags=re.I)
            closing_date = parts[0]
            closing_time = parts[1] if len(parts) > 1 else None

        badges = [t for t in (node_text(b) for b in soup.select(".tender-badge")) if t]

        return RawTender(
            ts_number=ts_number,
            title=node_text(soup.find("h1")),
            procuring_agency=fields.get("organization name") or fields.get("office name"),
            sector=fields.get("sector"),
            procurement_category=fields.get("procurement category"),
            advertised_raw=fields.get("advertisement date"),
            closing_date_raw=closing_date,
            closing_time_raw=closing_time,
            document_url=document_url,
            advertisement_url=advertisement_url,
            detail_url=detail_url,
            portal_status=badges,
            source="epms",
        )


# ---------------------------------------------------------------------------
# JSON / XHR parser
# ---------------------------------------------------------------------------

class JsonApiParser:
    """Maps a paginated JSON tender API (Laravel/DRF/custom styles) onto RawTender."""

    KEY_ALIASES: dict[str, tuple[str, ...]] = {
        "ts_number": ("ts_number", "tender_no", "tenderNo", "ts_no", "tsNumber", "tender_number", "tender_id", "code"),
        "title": ("title", "tender_title", "tenderTitle", "name", "description"),
        "procuring_agency": ("procuring_agency", "organization", "organization_name", "agency", "department", "org_name"),
        "sector": ("sector", "sector_name"),
        "procurement_category": ("category", "procurement_category", "category_name"),
        "advertised_raw": ("advertise_date", "advertised_date", "publishing_date", "published_at", "advertisement_date"),
        "closing_date_raw": ("closing_date", "closing_datetime", "closingDate", "deadline", "closing_at"),
        "closing_time_raw": ("closing_time", "closingTime"),
        "document_url": ("document_url", "tender_document", "document", "pdf_url", "attachment"),
        "detail_url": ("detail_url", "url", "link"),
        "status": ("status", "status_name", "tender_status"),
    }
    LIST_KEYS = ("data", "results", "items", "tenders", "records", "rows")

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/") + "/"

    def parse_listing(self, payload: Any, page: int) -> ListingPage:
        records, meta = self._extract_records(payload)
        items: list[RawTender] = []
        for record in records:
            if not isinstance(record, Mapping):
                continue
            try:
                tender = self._parse_record(record)
            except Exception as exc:
                logger.warning("Page {}: skipping JSON record ({}: {})", page, type(exc).__name__, exc)
                continue
            if tender:
                items.append(tender)

        total = _to_int(str(self._first(meta, ("total", "count", "total_count", "recordsTotal")) or ""))
        last_page = _to_int(str(self._first(meta, ("last_page", "total_pages", "pages", "num_pages")) or ""))
        next_url = self._first(meta, ("next_page_url", "next"))
        if last_page is not None:
            has_next = page < last_page
        elif next_url is not None or "next_page_url" in meta or "next" in meta:
            has_next = bool(next_url)
        else:
            has_next = bool(items)
        return ListingPage(page=page, items=items, total_reported=total, has_next=has_next)

    def _extract_records(self, payload: Any) -> tuple[list[Any], Mapping[str, Any]]:
        if isinstance(payload, list):
            return payload, {}
        if not isinstance(payload, Mapping):
            raise ParseError(f"Unexpected JSON payload type: {type(payload).__name__}")
        meta: dict[str, Any] = dict(payload)
        for nested in ("meta", "pagination", "links"):
            if isinstance(payload.get(nested), Mapping):
                meta.update(payload[nested])
        for key in self.LIST_KEYS:
            value = payload.get(key)
            if isinstance(value, list):
                return value, meta
            if isinstance(value, Mapping):  # e.g. {"data": {"data": [...], "last_page": n}}
                inner, inner_meta = self._extract_records(value)
                meta.update(inner_meta)
                return inner, meta
        raise ParseError(f"No record list found in JSON keys: {sorted(payload)[:15]}")

    @staticmethod
    def _first(record: Mapping[str, Any], keys: Iterable[str]) -> Any:
        for key in keys:
            value = record.get(key)
            if value not in (None, ""):
                return value
        return None

    def _str(self, record: Mapping[str, Any], field: str) -> str | None:
        value = self._first(record, self.KEY_ALIASES[field])
        if isinstance(value, Mapping):
            value = self._first(value, ("name", "title", "value"))
        return clean_text(str(value)) if value is not None else None

    def _parse_record(self, record: Mapping[str, Any]) -> RawTender | None:
        ts_raw = self._str(record, "ts_number") or ""
        match = TS_NUMBER_RE.search(ts_raw)
        if not match:
            return None
        ts_number = match.group(0)

        closing = self._str(record, "closing_date_raw")
        closing_time = self._str(record, "closing_time_raw")
        doc = self._str(record, "document_url")
        detail = self._str(record, "detail_url")
        status = self._str(record, "status")

        return RawTender(
            ts_number=ts_number,
            title=self._str(record, "title"),
            procuring_agency=self._str(record, "procuring_agency"),
            sector=self._str(record, "sector"),
            procurement_category=self._str(record, "procurement_category"),
            advertised_raw=self._str(record, "advertised_raw"),
            closing_date_raw=closing,
            closing_time_raw=closing_time,
            document_url=urljoin(self.base_url, doc) if doc else None,
            detail_url=urljoin(self.base_url, detail) if detail
            else urljoin(self.base_url, f"public/tenders/tender-details/{ts_number}"),
            portal_status=[status] if status else [],
            source="json-api",
        )


# ---------------------------------------------------------------------------
# Scraper / pagination controller
# ---------------------------------------------------------------------------

class PPRAScraper:
    """Async scraper. Use as `async with PPRAScraper() as s: async for page in s.iter_pages(): ...`."""

    def __init__(self, cfg: Settings | None = None) -> None:
        self.cfg = cfg or default_settings
        self.http = HttpClient(self.cfg)
        self.html_parser = EpmsHtmlParser(self.cfg.epms_base_url)
        self.json_parser = JsonApiParser(self.cfg.epms_base_url)
        self._detail_semaphore = asyncio.Semaphore(self.cfg.detail_concurrency)

    async def __aenter__(self) -> "PPRAScraper":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.http.aclose()

    # -- listing --------------------------------------------------------------

    async def fetch_listing_page(self, page: int) -> ListingPage:
        if self.cfg.json_api_url:
            url = self.cfg.json_api_url.replace("{page}", str(page))
            params = None if "{page}" in self.cfg.json_api_url else {"page": page}
            response = await self.http.get(
                url, params=params,
                headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"},
            )
        else:
            response = await self.http.get(self.cfg.listing_url, params={"page": page})
        return self._parse_listing_response(response, page)

    def _parse_listing_response(self, response: httpx.Response, page: int) -> ListingPage:
        content_type = response.headers.get("content-type", "").lower()
        body = response.text
        if "json" in content_type or body.lstrip().startswith(("{", "[")):
            try:
                payload = json.loads(body)
            except json.JSONDecodeError as exc:
                raise ParseError(f"Page {page}: invalid JSON ({exc})") from exc
            return self.json_parser.parse_listing(payload, page)
        return self.html_parser.parse_listing(body, page)

    async def iter_pages(self, max_pages: int | None = None) -> AsyncIterator[ListingPage]:
        """Yield every listing page until the portal runs out of rows.

        Stops on: an empty page, no "next" link, a page that only repeats already-seen
        TS numbers, or the safety cap. Items are de-duplicated across pages because
        the listing can shift while new tenders are being published mid-crawl.
        """
        cap = max_pages if max_pages is not None else self.cfg.max_pages
        seen: set[str] = set()
        page = 1
        total_reported: int | None = None
        consecutive_failures = 0
        capped = False

        while True:
            if cap and page > cap:
                logger.info("Reached page cap ({}); stopping", cap)
                capped = True
                break
            try:
                listing = await self.fetch_listing_page(page)
                consecutive_failures = 0
            except JavaScriptRenderedError:
                raise
            except (FetchError, ParseError) as exc:
                consecutive_failures += 1
                logger.error("Page {} failed: {}", page, exc)
                if consecutive_failures >= 3:
                    logger.error("3 consecutive page failures; aborting pagination")
                    break
                page += 1
                continue

            if listing.total_reported is not None:
                total_reported = listing.total_reported

            fresh = [item for item in listing.items if item.ts_number not in seen]
            dupes = len(listing.items) - len(fresh)
            seen.update(item.ts_number for item in fresh)

            if not listing.items:
                logger.info("Page {} is empty; pagination complete", page)
                break

            if page == 1 and total_reported:
                logger.info(
                    "Portal reports {} active tenders (~{} pages of {})",
                    total_reported, -(-total_reported // len(listing.items)), len(listing.items),
                )
            logger.info(
                "Page {}: {} tenders ({} new, {} repeated) | collected {}{}",
                page, len(listing.items), len(fresh), dupes, len(seen),
                f" of {total_reported}" if total_reported else "",
            )

            if fresh:
                listing.items = fresh
                yield listing
            elif page > 1:
                logger.info("Page {} only repeated known tenders; stopping", page)
                break

            if not listing.has_next:
                break
            page += 1
            await asyncio.sleep(random.uniform(self.cfg.min_delay, self.cfg.max_delay))

        if not capped and total_reported is not None and len(seen) < total_reported:
            logger.warning(
                "Collected {} unique tenders but portal reported {} - some may have been "
                "unpublished or shifted during the crawl", len(seen), total_reported,
            )

    # -- details --------------------------------------------------------------

    async def fetch_detail(self, tender: RawTender) -> RawTender | None:
        if not tender.detail_url:
            return None
        async with self._detail_semaphore:
            try:
                response = await self.http.get(tender.detail_url)
                detail = self.html_parser.parse_detail(response.text, tender.ts_number, tender.detail_url)
            except (FetchError, ParseError) as exc:
                logger.warning("Detail {} failed: {}", tender.ts_number, exc)
                return None
            finally:
                if self.cfg.detail_delay:
                    await asyncio.sleep(random.uniform(0, self.cfg.detail_delay * 2))
        return detail

    async def enrich_with_details(self, tenders: list[RawTender]) -> int:
        """Fetch detail pages concurrently and merge them in place. Returns success count."""
        if not tenders:
            return 0
        results = await asyncio.gather(*(self.fetch_detail(t) for t in tenders), return_exceptions=True)
        ok = 0
        for tender, result in zip(tenders, results):
            if isinstance(result, BaseException):
                logger.warning("Detail {} raised {}: {}", tender.ts_number, type(result).__name__, result)
            elif result is not None:
                tender.merge(result)
                ok += 1
        return ok
