"""Async engine, schema initialisation, session management and upsert helpers."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from loguru import logger
from sqlalchemy import event, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlmodel import SQLModel

from config import Settings, settings
from models import UPSERT_COLUMNS, Tender, TenderStatus, utcnow


def _build_engine(cfg: Settings) -> AsyncEngine:
    kwargs: dict[str, Any] = {"echo": cfg.db_echo, "future": True}
    if cfg.is_sqlite:
        kwargs["connect_args"] = {"timeout": 30}
    else:
        kwargs.update(pool_size=5, max_overflow=5, pool_pre_ping=True, pool_recycle=1800)
    engine = create_async_engine(cfg.database_url, **kwargs)

    if cfg.is_sqlite:
        @event.listens_for(engine.sync_engine, "connect")
        def _sqlite_pragmas(dbapi_conn: Any, _record: Any) -> None:
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


engine: AsyncEngine = _build_engine(settings)
SessionFactory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    engine, expire_on_commit=False, class_=AsyncSession
)


async def init_db() -> None:
    """Create tables and indexes if they don't exist."""
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    logger.info("Database ready ({})", engine.url.render_as_string(hide_password=True))


async def dispose_db() -> None:
    await engine.dispose()


@asynccontextmanager
async def get_session() -> AsyncIterator[AsyncSession]:
    """Transactional session: commits on success, rolls back on any error."""
    session = SessionFactory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


# ---------------------------------------------------------------------------
# Repository helpers
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class ExistingTenderState:
    ts_number: str
    has_document: bool
    has_category: bool


@dataclass(slots=True)
class UpsertResult:
    inserted: int = 0
    updated: int = 0
    failed: int = 0

    def __iadd__(self, other: "UpsertResult") -> "UpsertResult":
        self.inserted += other.inserted
        self.updated += other.updated
        self.failed += other.failed
        return self


def _chunks(items: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    for start in range(0, len(items), size):
        yield items[start:start + size]


async def fetch_existing_state(
    session: AsyncSession, ts_numbers: Iterable[str]
) -> dict[str, ExistingTenderState]:
    """Return what we already know about the given TS numbers."""
    keys = list(dict.fromkeys(ts_numbers))
    found: dict[str, ExistingTenderState] = {}
    table = Tender.__table__
    for chunk in _chunks(keys, 500):
        stmt = select(
            table.c.ts_number, table.c.document_url, table.c.category
        ).where(table.c.ts_number.in_(chunk))
        for ts, doc, cat in (await session.execute(stmt)).all():
            found[ts] = ExistingTenderState(ts, bool(doc), bool(cat))
    return found


def _upsert_statement(rows: list[dict[str, Any]], now: datetime) -> Any:
    """Dialect-specific INSERT ... ON CONFLICT (ts_number) DO UPDATE.

    New non-null values win; NULLs never wipe out data captured on an earlier run.
    `scraped_at` keeps the first-seen timestamp; `updated_at` is bumped.
    """
    table = Tender.__table__
    dialect = engine.dialect.name
    if dialect == "postgresql":
        stmt = pg_insert(table).values(rows)
    elif dialect == "sqlite":
        stmt = sqlite_insert(table).values(rows)
    else:  # pragma: no cover - guarded by config
        raise RuntimeError(f"Unsupported database dialect for upsert: {dialect}")

    excluded = stmt.excluded
    set_: dict[str, Any] = {
        col: func.coalesce(getattr(excluded, col), table.c[col])
        for col in UPSERT_COLUMNS
        if col not in ("ts_number", "status")
    }
    set_["status"] = excluded.status
    set_["updated_at"] = now
    return stmt.on_conflict_do_update(index_elements=[table.c.ts_number], set_=set_)


async def upsert_tenders(rows: list[dict[str, Any]], batch_size: int | None = None) -> UpsertResult:
    """Upsert normalised tender rows in batches, isolating failing rows."""
    result = UpsertResult()
    if not rows:
        return result

    # Collapse duplicates inside the batch (last one wins) - Postgres rejects
    # "ON CONFLICT DO UPDATE command cannot affect row a second time".
    unique: dict[str, dict[str, Any]] = {}
    for row in rows:
        unique[row["ts_number"]] = row
    deduped = list(unique.values())
    size = batch_size or settings.db_upsert_batch_size

    for chunk in _chunks(deduped, size):
        result += await _upsert_chunk(list(chunk))
    return result


async def _upsert_chunk(chunk: list[dict[str, Any]]) -> UpsertResult:
    now = utcnow()
    prepared = [{**row, "scraped_at": now, "updated_at": now} for row in chunk]
    try:
        async with get_session() as session:
            existing = await fetch_existing_state(session, (r["ts_number"] for r in chunk))
            await session.execute(_upsert_statement(prepared, now))
        updated = len(existing)
        return UpsertResult(inserted=len(chunk) - updated, updated=updated)
    except (IntegrityError, SQLAlchemyError) as exc:
        if len(chunk) == 1:
            logger.error("DB rejected tender {}: {}", chunk[0].get("ts_number"), _short(exc))
            return UpsertResult(failed=1)
        logger.warning(
            "Batch upsert of {} rows failed ({}); retrying row-by-row", len(chunk), _short(exc)
        )
        result = UpsertResult()
        for row in chunk:
            result += await _upsert_chunk([row])
        return result


async def mark_expired_tenders() -> int:
    """Flag every ACTIVE tender whose closing time has passed as EXPIRED."""
    table = Tender.__table__
    now = utcnow()
    stmt = (
        update(table)
        .where(table.c.status == TenderStatus.ACTIVE.value)
        .where(table.c.closing_datetime.is_not(None))
        .where(table.c.closing_datetime < now)
        .values(status=TenderStatus.EXPIRED.value, updated_at=now)
    )
    try:
        async with get_session() as session:
            res = await session.execute(stmt)
            return int(res.rowcount or 0)
    except SQLAlchemyError as exc:
        logger.error("Failed to mark expired tenders: {}", _short(exc))
        return 0


async def count_by_status() -> dict[str, int]:
    table = Tender.__table__
    async with get_session() as session:
        rows = (
            await session.execute(
                select(table.c.status, func.count()).group_by(table.c.status)
            )
        ).all()
    return {status: int(n) for status, n in rows}


def _short(exc: BaseException, limit: int = 300) -> str:
    text = str(getattr(exc, "orig", None) or exc).replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "..."
