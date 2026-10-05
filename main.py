"""CLI entry point for the PPRA tenders scraper.

Examples:
    python main.py                       # full run (all pages, details, upsert, expire)
    python main.py --max-pages 2         # quick smoke run
    python main.py --no-details          # listing only (no PDF links / category)
    python main.py --refresh-details     # re-fetch detail pages for known tenders too
    python main.py --dry-run --max-pages 1
    python main.py --init-db             # create tables and exit
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from loguru import logger

from config import settings


def configure_logging(level: str) -> None:
    logger.remove()
    logger.add(
        sys.stderr,
        level=level.upper(),
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | "
        "<cyan>{module}</cyan> - <level>{message}</level>",
        enqueue=False,
    )
    log_dir = Path(settings.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    logger.add(
        log_dir / "ppra_scraper_{time:YYYY-MM-DD}.log",
        level="DEBUG",
        rotation="00:00",
        retention="30 days",
        compression="zip",
        encoding="utf-8",
        enqueue=True,
        backtrace=True,
        diagnose=False,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ppra-scraper",
        description="Scrape PPRA/EPMS active tenders and upsert them into the database.",
    )
    parser.add_argument("--max-pages", type=int, default=None,
                        help="Stop after N listing pages (default: MAX_PAGES env, 0 = all).")
    details = parser.add_mutually_exclusive_group()
    details.add_argument("--no-details", action="store_true",
                         help="Skip detail pages (no document URL / procurement category).")
    details.add_argument("--refresh-details", action="store_true",
                         help="Re-fetch detail pages even for tenders already complete in the DB.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Scrape and normalise but do not touch the database.")
    parser.add_argument("--init-db", action="store_true", help="Create tables and exit.")
    parser.add_argument("--expire-only", action="store_true",
                        help="Only mark past-deadline tenders as EXPIRED, no scraping.")
    parser.add_argument("--log-level", default=settings.log_level,
                        choices=["TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR"],
                        type=str.upper)
    return parser


async def _run(args: argparse.Namespace) -> int:
    # Imported lazily so `--help` works without DB drivers installed.
    from database import dispose_db, init_db, mark_expired_tenders
    from pipeline import PipelineOptions, run_pipeline

    try:
        if args.init_db:
            await init_db()
            return 0

        if args.expire_only:
            await init_db()
            n = await mark_expired_tenders()
            logger.info("Marked {} tenders as EXPIRED", n)
            return 0

        options = PipelineOptions(
            max_pages=args.max_pages,
            fetch_details=False if args.no_details else None,
            refresh_details=args.refresh_details,
            dry_run=args.dry_run,
        )
        stats = await run_pipeline(options)

        logger.info("=" * 64)
        logger.info("Run finished in {:.1f}s", stats.elapsed)
        logger.info("Pages crawled      : {}", stats.pages)
        logger.info("Tenders scraped    : {}", stats.scraped)
        logger.info("Detail pages       : {} fetched, {} skipped (already complete)",
                    stats.details_fetched, stats.details_skipped)
        logger.info("Invalid / dropped  : {}", stats.invalid)
        if not args.dry_run:
            logger.info("DB inserted/updated: {} / {} ({} failed)",
                        stats.db.inserted, stats.db.updated, stats.db.failed)
            logger.info("Marked EXPIRED     : {}", stats.expired_marked)
            logger.info("Totals by status   : {}", stats.status_totals)
        logger.info("=" * 64)

        if stats.aborted:
            return 2
        if stats.pages == 0:
            logger.error("No pages were scraped")
            return 1
        return 0
    finally:
        await dispose_db()


def main() -> None:
    args = build_parser().parse_args()
    configure_logging(args.log_level)
    try:
        code = asyncio.run(_run(args))
    except KeyboardInterrupt:
        logger.warning("Interrupted by user")
        code = 130
    except Exception:
        logger.exception("Fatal error")
        code = 1
    sys.exit(code)


if __name__ == "__main__":
    main()
