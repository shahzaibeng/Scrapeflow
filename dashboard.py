"""Streamlit dashboard for browsing PPRA tenders stored in SQLite.

Run with:  streamlit run dashboard.py
"""

from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = PROJECT_DIR / "ppra_tenders.db"
SCRAPER_LOG_PATH = PROJECT_DIR / "logs" / "dashboard_scraper_run.log"
DISPLAY_TZ = "Asia/Karachi"
STATUSES = ("ALL", "ACTIVE", "EXPIRED", "CLOSED")
CATEGORY_SEPARATOR = " / "
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")

st.set_page_config(
    page_title="PPRA Tenders Dashboard",
    page_icon="📑",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
      .block-container { padding-top: 1.6rem; padding-bottom: 2rem; }
      div[data-testid="stMetric"] {
          border: 1px solid rgba(128, 128, 128, 0.25);
          border-radius: 12px;
          padding: 14px 18px;
          background: rgba(128, 128, 128, 0.06);
      }
      div[data-testid="stMetric"] label p { font-size: 0.85rem; opacity: 0.8; }
      .app-subtitle { opacity: 0.7; margin-top: -0.6rem; margin-bottom: 1.2rem; }
    </style>
    """,
    unsafe_allow_html=True,
)


# ---------------------------------------------------------------------------
# Data access
# ---------------------------------------------------------------------------

def resolve_db_path() -> Path:
    """Use DATABASE_URL from config when it points to SQLite, else ./ppra_tenders.db."""
    try:
        from config import settings

        url = settings.database_url
        if url.startswith("sqlite"):
            raw = url.split(":///", 1)[1] if ":///" in url else ""
            if raw:
                path = Path(raw)
                return path if path.is_absolute() else (PROJECT_DIR / path).resolve()
    except Exception:
        pass
    return DEFAULT_DB_PATH


DB_PATH = resolve_db_path()


@st.cache_data(ttl=60, show_spinner="Loading tenders…")
def load_tenders(db_path: str, _mtime: float) -> pd.DataFrame:
    """Read the tenders table (read-only). `_mtime` busts the cache when the DB changes."""
    uri = f"file:{Path(db_path).as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=10) as conn:
        df = pd.read_sql_query(
            """
            SELECT ts_number, procuring_agency, title, category, publishing_date,
                   closing_datetime, document_url, detail_url, status, scraped_at, updated_at
            FROM tenders
            """,
            conn,
        )

    df["publishing_date"] = pd.to_datetime(df["publishing_date"], errors="coerce").dt.date
    for col in ("closing_datetime", "scraped_at", "updated_at"):
        df[col] = pd.to_datetime(df[col], errors="coerce", utc=True, format="mixed")

    # The DB status is refreshed only when the scraper runs; recompute expiry live.
    now = pd.Timestamp.now(tz="UTC")
    overdue = (df["status"] == "ACTIVE") & df["closing_datetime"].notna() & (df["closing_datetime"] < now)
    df.loc[overdue, "status"] = "EXPIRED"

    df["closing_local"] = df["closing_datetime"].dt.tz_convert(DISPLAY_TZ).dt.tz_localize(None)
    df["days_left"] = ((df["closing_datetime"] - now).dt.total_seconds() / 86400).where(
        df["status"] == "ACTIVE"
    ).apply(lambda d: int(d) if pd.notna(d) else None)
    df["category_tags"] = df["category"].fillna("").apply(
        lambda c: [p.strip() for p in c.split(CATEGORY_SEPARATOR) if p.strip()]
    )
    return df.sort_values(["closing_datetime", "ts_number"], na_position="last").reset_index(drop=True)


def db_mtime(path: Path) -> float:
    """Latest modification time across the DB and its WAL file."""
    stamps = [p.stat().st_mtime for p in (path, Path(f"{path}-wal")) if p.exists()]
    return max(stamps, default=0.0)


# ---------------------------------------------------------------------------
# Background scraper runner
# ---------------------------------------------------------------------------

@dataclass
class ScraperJob:
    process: subprocess.Popen[bytes] | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    return_code: int | None = None
    args: list[str] = field(default_factory=list)

    @property
    def running(self) -> bool:
        if self.process is None:
            return False
        code = self.process.poll()
        if code is None:
            return True
        if self.return_code is None:
            self.return_code = code
            self.finished_at = datetime.now()
            self.process = None
        return False


@st.cache_resource
def scraper_job() -> ScraperJob:
    """Single job shared across browser sessions so the scraper can't run twice."""
    return ScraperJob()


def start_scraper(max_pages: int, skip_details: bool) -> None:
    job = scraper_job()
    if job.running:
        return
    args = [sys.executable, "main.py"]
    if max_pages > 0:
        args += ["--max-pages", str(max_pages)]
    if skip_details:
        args.append("--no-details")

    SCRAPER_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    log_file = SCRAPER_LOG_PATH.open("wb")
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8", "NO_COLOR": "1"}
    creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        job.process = subprocess.Popen(
            args,
            cwd=PROJECT_DIR,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            env=env,
            creationflags=creationflags,
        )
    except OSError as exc:
        log_file.close()
        st.sidebar.error(f"Could not start scraper: {exc}")
        return
    finally:
        # The child process has inherited the handle; the parent doesn't need it.
        if not log_file.closed:
            log_file.close()
    job.args = args[1:]
    job.started_at = datetime.now()
    job.finished_at = None
    job.return_code = None


def read_log_tail(lines: int = 15) -> str:
    if not SCRAPER_LOG_PATH.exists():
        return ""
    text = ANSI_ESCAPE_RE.sub("", SCRAPER_LOG_PATH.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(text.strip().splitlines()[-lines:])


@st.fragment(run_every=4)
def scraper_status_panel() -> None:
    job = scraper_job()
    if job.running:
        elapsed = int((datetime.now() - job.started_at).total_seconds()) if job.started_at else 0
        st.info(f"⏳ Scraper running… {elapsed // 60}m {elapsed % 60:02d}s")
        st.code(read_log_tail(8) or "Starting…", language=None)
        if st.button("⏹ Stop scraper", width="stretch"):
            if job.process is not None:
                job.process.terminate()
            st.rerun()
        return

    if job.return_code is not None and job.finished_at is not None:
        when = job.finished_at.strftime("%H:%M:%S")
        if job.return_code == 0:
            st.success(f"✅ Last run finished at {when}")
        else:
            st.error(f"❌ Last run exited with code {job.return_code} at {when}")
        with st.expander("Last run log"):
            st.code(read_log_tail(25) or "(empty)", language=None)
        # Pick up freshly scraped rows once, right after the run finishes.
        if st.session_state.get("_reloaded_after") != job.finished_at:
            st.session_state["_reloaded_after"] = job.finished_at
            st.cache_data.clear()
            st.rerun(scope="app")


# ---------------------------------------------------------------------------
# Sidebar: scraper controls + filters
# ---------------------------------------------------------------------------

def render_scraper_controls() -> None:
    st.sidebar.header("⚙️ Scraper")
    job = scraper_job()
    with st.sidebar.expander("Run options", expanded=False):
        max_pages = st.number_input(
            "Max pages (0 = all)", min_value=0, max_value=500, value=0, step=1,
            help="A full crawl is ~50 pages and takes 15–20 minutes on the first run.",
        )
        skip_details = st.checkbox(
            "Skip detail pages", value=False,
            help="Faster, but new tenders won't get PDF links or procurement category.",
        )
    if st.sidebar.button(
        "🚀 Run Scraper Now", type="primary", width="stretch", disabled=job.running
    ):
        start_scraper(int(max_pages), bool(skip_details))
        st.rerun()
    with st.sidebar:
        scraper_status_panel()
    st.sidebar.divider()


@dataclass
class Filters:
    keyword: str
    categories: list[str]
    status: str
    date_range: tuple[date, date] | None
    include_undated: bool


def render_filters(df: pd.DataFrame) -> Filters:
    st.sidebar.header("🔎 Filters")
    keyword = st.sidebar.text_input(
        "Keyword", placeholder="Title, agency or TS number…", key="f_keyword",
    ).strip()

    all_tags = sorted({tag for tags in df["category_tags"] for tag in tags})
    categories = st.sidebar.multiselect("Sector / Category", all_tags, placeholder="All categories", key="f_categories")

    status = st.sidebar.radio("Status", STATUSES, index=0, horizontal=True, key="f_status")

    dates = df["publishing_date"].dropna()
    date_range: tuple[date, date] | None = None
    include_undated = True
    if not dates.empty:
        lo, hi = min(dates), max(dates)
        picked: Any = st.sidebar.date_input(
            "Publishing date", value=(lo, hi), min_value=lo, max_value=hi, format="YYYY-MM-DD", key="f_pubdate",
        )
        # While the user is mid-selection Streamlit returns a 1-tuple.
        if isinstance(picked, (tuple, list)) and len(picked) == 2:
            start, end = picked
        elif isinstance(picked, (tuple, list)) and len(picked) == 1:
            start = end = picked[0]
        else:
            start = end = picked
        if (start, end) != (lo, hi):
            date_range = (start, end)
            include_undated = False

    if st.sidebar.button("Reset filters", width="stretch"):
        for key in ("f_keyword", "f_categories", "f_status", "f_pubdate"):
            st.session_state.pop(key, None)
        st.rerun()

    return Filters(keyword, categories, status, date_range, include_undated)


def apply_filters(df: pd.DataFrame, f: Filters) -> pd.DataFrame:
    mask = pd.Series(True, index=df.index)
    if f.keyword:
        kw = f.keyword.lower()
        haystack = (
            df["title"].fillna("") + " " + df["procuring_agency"].fillna("") + " " + df["ts_number"]
        ).str.lower()
        mask &= haystack.str.contains(kw, regex=False)
    if f.categories:
        wanted = set(f.categories)
        mask &= df["category_tags"].apply(lambda tags: bool(wanted.intersection(tags)))
    if f.status != "ALL":
        mask &= df["status"] == f.status
    if f.date_range:
        start, end = f.date_range
        in_range = df["publishing_date"].apply(lambda d: d is not None and pd.notna(d) and start <= d <= end)
        mask &= in_range | (df["publishing_date"].isna() & f.include_undated)
    return df[mask]


# ---------------------------------------------------------------------------
# Main layout
# ---------------------------------------------------------------------------

def render_metrics(df: pd.DataFrame) -> None:
    total = len(df)
    active = int((df["status"] == "ACTIVE").sum())
    expired = int((df["status"] == "EXPIRED").sum())
    closed = int((df["status"] == "CLOSED").sum())
    agencies = df["procuring_agency"].dropna().str.strip().str.lower().nunique()

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("📦 Total Scraped Tenders", f"{total:,}")
    c2.metric("🟢 Active Tenders", f"{active:,}")
    c3.metric("🔴 Expired / Closed", f"{expired + closed:,}", help=f"{expired:,} expired · {closed:,} closed")
    c4.metric("🏛️ Unique Procuring Agencies", f"{agencies:,}")


def render_table(view: pd.DataFrame) -> None:
    table = view[
        [
            "ts_number", "procuring_agency", "title", "category", "publishing_date",
            "closing_local", "days_left", "status", "document_url", "detail_url",
        ]
    ]
    st.dataframe(
        table,
        hide_index=True,
        width="stretch",
        height=min(640, 38 + 35 * max(len(table), 1)),
        column_config={
            "ts_number": st.column_config.TextColumn("TS Number", width="small"),
            "procuring_agency": st.column_config.TextColumn("Procuring Agency", width="medium"),
            "title": st.column_config.TextColumn("Title", width="large"),
            "category": st.column_config.TextColumn("Category", width="medium"),
            "publishing_date": st.column_config.DateColumn("Publishing Date", format="DD MMM YYYY"),
            "closing_local": st.column_config.DatetimeColumn(
                "Closing Date (PKT)", format="DD MMM YYYY, hh:mm a"
            ),
            "days_left": st.column_config.NumberColumn("Days Left", format="%d", width="small"),
            "status": st.column_config.TextColumn("Status", width="small"),
            "document_url": st.column_config.LinkColumn(
                "Document", display_text="📄 Open PDF", width="small"
            ),
            "detail_url": st.column_config.LinkColumn(
                "Details", display_text="🔗 View", width="small"
            ),
        },
    )


def to_csv(view: pd.DataFrame) -> bytes:
    export = view[
        [
            "ts_number", "procuring_agency", "title", "category", "publishing_date",
            "closing_local", "status", "document_url", "detail_url",
        ]
    ].rename(
        columns={
            "ts_number": "TS Number",
            "procuring_agency": "Procuring Agency",
            "title": "Title",
            "category": "Category",
            "publishing_date": "Publishing Date",
            "closing_local": "Closing Date (PKT)",
            "status": "Status",
            "document_url": "Document URL",
            "detail_url": "Detail URL",
        }
    )
    # utf-8-sig so Excel opens Urdu / special characters correctly.
    return export.to_csv(index=False).encode("utf-8-sig")


def main() -> None:
    st.title("📑 PPRA Tenders Dashboard")
    st.markdown(
        '<p class="app-subtitle">Public Procurement Regulatory Authority, Pakistan: '
        "active and historical tenders scraped from EPMS / EPADS</p>",
        unsafe_allow_html=True,
    )

    render_scraper_controls()

    if not DB_PATH.exists():
        st.warning(
            f"No database found at `{DB_PATH}`. Click **🚀 Run Scraper Now** in the sidebar "
            "(or run `python main.py`) to collect tenders."
        )
        return

    try:
        df = load_tenders(str(DB_PATH), db_mtime(DB_PATH))
    except (sqlite3.Error, pd.errors.DatabaseError) as exc:
        st.error(f"Could not read tenders from `{DB_PATH}`: {exc}")
        return

    if df.empty:
        st.info("The `tenders` table is empty. Run the scraper to populate it.")
        return

    render_metrics(df)
    filters = render_filters(df)
    view = apply_filters(df, filters)

    st.divider()
    head_left, head_right = st.columns([3, 1], vertical_alignment="bottom")
    last_update = df["updated_at"].max()
    updated_txt = (
        last_update.tz_convert(DISPLAY_TZ).strftime("%d %b %Y, %I:%M %p PKT")
        if pd.notna(last_update) else "n/a"
    )
    head_left.subheader(f"Tenders ({len(view):,} of {len(df):,})")
    head_left.caption(f"Last database update: {updated_txt}")
    head_right.download_button(
        "⬇️ Download Filtered Data as CSV",
        data=to_csv(view),
        file_name=f"ppra_tenders_{datetime.now():%Y%m%d_%H%M}.csv",
        mime="text/csv",
        width="stretch",
        disabled=view.empty,
    )

    if view.empty:
        st.info("No tenders match the current filters.")
    else:
        render_table(view)


if __name__ == "__main__":
    main()
