# Scrapeflow: PPRA Tenders Scraper & Dashboard

Scrapeflow collects **active public tenders from PPRA (Public Procurement Regulatory Authority, Pakistan)**, stores them in a database without duplicates, and lets you browse, filter and export them through a web dashboard.

Tenders are scraped from the official EPMS portal ([epms.ppra.gov.pk](https://epms.ppra.gov.pk/public/tenders/active-tenders)), which also lists EPADS e-tenders.

---

## Features

- **Full pagination:** crawls every page of active tenders (about 2,500 tenders, 50 per page) and stops automatically at the end.
- **Rich data per tender:** TS number, procuring agency, title, category/sector, publishing date, closing date and time, PDF document link and detail page link.
- **Reliable requests:** rotating browser User-Agents, timeouts, and automatic retries with backoff on network errors and `429`/`5xx` responses.
- **No duplicates:** each tender is keyed by its TS number. Daily runs update existing records and fill in missing fields instead of inserting new rows.
- **Automatic status tracking:** tenders past their deadline are marked `EXPIRED`, and cancelled ones are marked `CLOSED`.
- **Fast repeat runs:** detail pages are only fetched for new or incomplete tenders.
- **Crash-safe:** data is saved after every page, so an interrupted run keeps what it already collected.
- **SQLite or PostgreSQL:** works out of the box with SQLite; switch to PostgreSQL by changing one setting.
- **Streamlit dashboard:** search, filter, open PDFs, export to CSV and start the scraper from the browser.

---

## Project structure

```
├── main.py            # Command-line entry point (run the scraper)
├── scraper.py         # HTTP client, HTML/JSON parsers, pagination
├── pipeline.py        # Cleans dates/status and saves each page to the database
├── database.py        # Database engine, sessions, upsert and expiry logic
├── models.py          # `tenders` table schema
├── config.py          # Settings loaded from .env
├── dashboard.py       # Streamlit dashboard
├── requirements.txt   # Python dependencies
└── .env.example       # Sample configuration
```

---

## Getting started

### 1. Requirements

- Python **3.11+** (tested on 3.12)
- Optional: PostgreSQL 13+ if you don't want to use SQLite

### 2. Install

```bash
git clone https://github.com/shahzaibeng/Scrapeflow.git
cd Scrapeflow

python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

### 3. Configure

```bash
# Windows
copy .env.example .env
# macOS / Linux
cp .env.example .env
```

The defaults work as-is and save data to a local SQLite file (`ppra_tenders.db`). To use PostgreSQL instead, set this in `.env`:

```env
DATABASE_URL=postgresql://user:password@localhost:5432/ppra
```

---

## Usage

### Run the scraper

```bash
python main.py                      # full run: all pages, details, save, mark expired
python main.py --max-pages 2        # quick test run
```

| Option | Description |
|---|---|
| `--max-pages N` | Stop after N listing pages (default: all) |
| `--no-details` | Skip detail pages (faster, but no PDF link or category) |
| `--refresh-details` | Re-fetch detail pages even for tenders already complete in the database |
| `--dry-run` | Scrape and print sample rows without writing to the database |
| `--init-db` | Only create the database tables |
| `--expire-only` | Only mark past-deadline tenders as `EXPIRED` |
| `--log-level LEVEL` | `DEBUG`, `INFO`, `WARNING`, etc. |

The first full run takes roughly **15–20 minutes** because it opens every tender's detail page. Later runs are much faster.

Logs are printed to the console and saved daily in `logs/`.

### Open the dashboard

```bash
streamlit run dashboard.py
```

Then open <http://localhost:8501>.

The dashboard includes:

- **Summary cards:** total tenders, active, expired/closed, and unique procuring agencies
- **Filters:** keyword search (title, agency or TS number), category, status and publishing date range
- **Tenders table:** with clickable **Open PDF** and **View** links and a "days left" column
- **Download Filtered Data as CSV**
- **Run Scraper Now:** starts the scraper in the background and shows live progress

### Share the dashboard (optional)

You can share your local dashboard with someone else using [Cloudflare Quick Tunnels](https://try.cloudflare.com):

```bash
cloudflared tunnel --url http://localhost:8501
```

Send the `https://….trycloudflare.com` link it prints. Note that anyone with the link can also press **Run Scraper Now**.

---

## Database schema

Table: `tenders`

| Column | Type | Notes |
|---|---|---|
| `id` | Integer | Primary key |
| `ts_number` | String | Unique tender ID, e.g. `TS0000015164E` |
| `procuring_agency` | String | Ministry / department |
| `title` | Text | Tender title |
| `category` | String | Procurement category / sector, e.g. `Goods / Miscellaneous` |
| `publishing_date` | Date | Advertised date |
| `closing_datetime` | DateTime | Deadline, stored in **UTC** (portal shows Pakistan time) |
| `document_url` | Text | Direct PDF download link |
| `detail_url` | Text | Tender detail page |
| `status` | String | `ACTIVE`, `EXPIRED` or `CLOSED` |
| `scraped_at` | DateTime | When the tender was first seen |
| `updated_at` | DateTime | Last time the record was updated |

---

## Configuration reference

All settings are optional and can be set in `.env`. See [.env.example](.env.example) for the full list.

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | `sqlite+aiosqlite:///./ppra_tenders.db` | SQLite or PostgreSQL connection string |
| `MIN_DELAY` / `MAX_DELAY` | `1.0` / `2.5` | Random pause between listing pages (seconds) |
| `FETCH_DETAILS` | `true` | Fetch detail pages for PDF links and category |
| `DETAIL_CONCURRENCY` | `4` | Detail pages fetched in parallel |
| `MAX_RETRIES` | `4` | Retries per failed request |
| `REQUEST_TIMEOUT` | `30` | Request timeout (seconds) |
| `MAX_PAGES` | `0` | Page limit (`0` = unlimited) |
| `USER_AGENTS` | built-in list | Comma-separated User-Agent strings |
| `JSON_API_URL` | *(empty)* | Optional JSON endpoint, used if the portal moves to a JavaScript-rendered listing |
| `SOURCE_TIMEZONE` | `Asia/Karachi` | Timezone of dates shown on the portal |

---

## Scheduling daily runs

**Windows (Task Scheduler):** create a daily task that runs:

```
C:\path\to\Scrapeflow\.venv\Scripts\python.exe main.py
```

with **Start in** set to the project folder.

**Linux / macOS (cron):** run every day at 7:00 AM:

```cron
0 7 * * * cd /path/to/Scrapeflow && .venv/bin/python main.py >> logs/cron.log 2>&1
```

---

## Notes

- Please scrape responsibly. The default delays and concurrency are kept low to avoid putting load on the PPRA servers.
- If the portal's page layout changes, the parsers in `scraper.py` may need updating.
- The scraped data belongs to its original publishers. This project only collects publicly available tender notices.
