"""Database schema and in-memory data transfer objects."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from typing import Optional

from sqlalchemy import Column, Date, DateTime, Index, String, Text, func
from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TenderStatus(str, Enum):
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    CLOSED = "CLOSED"


class Tender(SQLModel, table=True):
    """`tenders` table. All datetimes are stored in UTC."""

    __tablename__ = "tenders"
    __table_args__ = (
        Index("ix_tenders_status_closing", "status", "closing_datetime"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    ts_number: str = Field(
        sa_column=Column(String(32), unique=True, index=True, nullable=False)
    )
    procuring_agency: Optional[str] = Field(
        default=None, sa_column=Column(String(512), nullable=True)
    )
    title: Optional[str] = Field(default=None, sa_column=Column(Text, nullable=True))
    category: Optional[str] = Field(
        default=None, sa_column=Column(String(255), nullable=True)
    )
    publishing_date: Optional[date] = Field(
        default=None, sa_column=Column(Date, nullable=True)
    )
    closing_datetime: Optional[datetime] = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    document_url: Optional[str] = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )
    detail_url: Optional[str] = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )
    status: str = Field(
        default=TenderStatus.ACTIVE.value,
        sa_column=Column(
            String(16),
            nullable=False,
            index=True,
            server_default=TenderStatus.ACTIVE.value,
        ),
    )
    scraped_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(
            DateTime(timezone=True), nullable=False, server_default=func.now()
        ),
    )
    updated_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(
            DateTime(timezone=True), nullable=False, server_default=func.now()
        ),
    )


# Columns written by the pipeline (everything except id / audit timestamps).
UPSERT_COLUMNS: tuple[str, ...] = (
    "ts_number",
    "procuring_agency",
    "title",
    "category",
    "publishing_date",
    "closing_datetime",
    "document_url",
    "detail_url",
    "status",
)


@dataclass(slots=True)
class RawTender:
    """Un-normalised tender as extracted from the portal (strings only)."""

    ts_number: str
    title: str | None = None
    procuring_agency: str | None = None
    sector: str | None = None
    procurement_category: str | None = None
    advertised_raw: str | None = None
    closing_date_raw: str | None = None
    closing_time_raw: str | None = None
    document_url: str | None = None
    advertisement_url: str | None = None
    detail_url: str | None = None
    portal_status: list[str] = field(default_factory=list)
    source: str = "epms"

    def merge(self, other: "RawTender") -> None:
        """Fill fields from `other` (e.g. a detail page), preferring its values."""
        for name in (
            "title",
            "procuring_agency",
            "sector",
            "procurement_category",
            "advertised_raw",
            "closing_date_raw",
            "closing_time_raw",
            "document_url",
            "advertisement_url",
            "detail_url",
        ):
            value = getattr(other, name)
            if value:
                setattr(self, name, value)
        for badge in other.portal_status:
            if badge not in self.portal_status:
                self.portal_status.append(badge)


@dataclass(slots=True)
class ListingPage:
    page: int
    items: list[RawTender]
    total_reported: int | None = None
    has_next: bool = False
