#!/usr/bin/env python3
"""
6_excel_exporter.py — Client-ready Excel generator.
Reads output/leviathan.db (written by 5_leviathan.py) and emits a real
.xlsx workbook (via openpyxl) plus a UTF-8-BOM CSV companion, each with a
DATASET SUMMARY header block followed by one row per tweet.

Column order: Tweet ID | Date | Clean Dates | Author | Full Text | ...

- Tweet IDs are stored as TEXT (number format '@'), never numeric, so
  Excel cannot round them or show scientific notation.
- Clean Dates holds real date values displayed as e.g. 15 Jan 2020
  (Excel number format 'd mmm yyyy'), derived from
  the original Date column (which is preserved unchanged).

Usage:
  python 6_excel_exporter.py --handle elonmusk
  python 6_excel_exporter.py --handle elonmusk --db output/leviathan.db --out output/custom.xlsx
  python 6_excel_exporter.py --all   # every handle in the DB + combined summary table
"""

import argparse
import csv
import re
import sqlite3
from datetime import datetime, date
from email.utils import parsedate_to_datetime
from pathlib import Path

from config import OUTPUT_DIR, ensure_output_dir, log

try:
    from export_csv import normalize_text
except ImportError:

    def normalize_text(text):
        return (text or "").replace("\r", " ").replace("\n", " ").strip()


try:
    from openpyxl import Workbook
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    HAS_OPENPYXL = True
except ImportError:  # pragma: no cover
    HAS_OPENPYXL = False

DEFAULT_DB = Path(OUTPUT_DIR) / "leviathan.db"

HEADERS = [
    "Tweet ID",
    "Date",
    "Clean Dates",
    "Author",
    "Full Text (Cleaned for Excel)",
    "Likes",
    "Retweets",
    "Replies",
    "Quotes",
    "Views",
    "URL",
]

# Columns (1-based) inside the workbook data block.
COL_TWEET_ID = 1
COL_DATE = 2
COL_CLEAN_DATES = 3

TEXT_FORMAT = "@"  # Excel "Text" number format: no numeric coercion.
CLEAN_DATE_FORMAT = "d mmm yyyy"  # Displays as e.g. 15 Jan 2020, 9 Sep 2026.

_TWEET_ID_RE = re.compile(r"^\d+$")


def validate_tweet_id(tweet_id) -> str:
    """Return tweet_id as a digit-only string, else raise ValueError.

    Guarantees: strings only, digits only, no scientific notation, no
    rounding/truncation — the exact original digits are preserved.
    """
    if isinstance(tweet_id, bool):
        raise ValueError(f"Tweet ID must be a digit string, got bool: {tweet_id!r}")
    if isinstance(tweet_id, int):
        tweet_id = str(tweet_id)
    if not isinstance(tweet_id, str):
        raise ValueError(
            f"Tweet ID must be TEXT/STRING, got {type(tweet_id).__name__}: {tweet_id!r}"
        )
    tid = tweet_id.strip()
    if not tid:
        raise ValueError("Tweet ID is empty")
    if not _TWEET_ID_RE.match(tid):
        raise ValueError(
            f"Tweet ID must contain only digits (no scientific notation): {tweet_id!r}"
        )
    return tid


def parse_clean_date(date_str):
    """Parse an original Date value into a datetime.date, or None.

    Handles Twitter 'Wed Sep 30 20:34:21 +0000 2020', ISO-8601 and
    plain '%Y-%m-%d [%H:%M:%S]' shapes. Never raises: invalid or missing
    dates return None so one bad row cannot crash the export.
    """
    if date_str is None:
        return None
    if isinstance(date_str, datetime):
        return date_str.date()
    if isinstance(date_str, date):
        return date_str
    if not isinstance(date_str, str):
        return None
    s = date_str.strip()
    if not s:
        return None
    try:  # Twitter format + RFC-2822 variants.
        return parsedate_to_datetime(s).date()
    except (ValueError, TypeError):
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def format_client_date(d) -> str:
    """Client-facing date: '15 Jan 2020' (no leading zero, full year)."""
    if d is None:
        return ""
    if isinstance(d, datetime):
        d = d.date()
    if not isinstance(d, date):
        return ""
    return f"{d.day} {d:%b} {d:%Y}"


def format_clean_date_csv(date_str) -> str:
    """Clean Dates as '15 Jan 2020' text for CSV output ('' when invalid)."""
    return format_client_date(parse_clean_date(date_str))


def build_methodology() -> str:
    """Academic methodology paragraph: exactly 5 sentences, all true."""
    return (
        "Data was extracted using a Patchright-driven browser session that navigates X natively "
        "and intercepts SearchTimeline GraphQL responses (5_leviathan.py), paging each target handle "
        "in date slices with scrolling and cursor pagination. "
        "Raw tweets were deduplicated and stored in SQLite (output/leviathan.db) with resume-safe "
        "per-slice progress tracking. "
        "Extracted records were validated in Python by checking that every Tweet ID is a digit-only "
        "string unchanged from the source, that no ID contains scientific notation, and that dates "
        "parse against the expected formats. "
        "Data-quality checks flagged missing values, duplicate records, and invalid or missing dates, "
        "which are exported as blank Clean Dates without aborting the run. "
        "Cleaned datasets were exported with openpyxl into Excel workbooks that retain the original "
        "Date alongside a standardized Clean Dates column (15 Jan 2020 format), preserve Tweet IDs as text to "
        "avoid precision loss, and list Author, Full Text and engagement columns, with per-dataset "
        "sizes presented in a summary table."
    )


def _dataset_stats(conn, handle):
    """(total, min_raw, max_raw, min_clean, max_clean) for one handle."""
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*), MIN(created_at), MAX(created_at) FROM tweets WHERE handle=?",
        (handle,),
    )
    total, min_raw, max_raw = cur.fetchone()
    cur.execute("SELECT created_at FROM tweets WHERE handle=?", (handle,))
    cleansed = [parse_clean_date(r[0]) for r in cur.fetchall()]
    cleansed = [d for d in cleansed if d]
    if cleansed:
        return total, min_raw, max_raw, min(cleansed), max(cleansed)
    return total, min_raw, max_raw, None, None


def _date_range_label(min_clean, max_clean, min_raw, max_raw):
    if min_clean and max_clean:
        return f"{format_client_date(min_clean)} – {format_client_date(max_clean)}"
    return f"{min_raw or 'N/A'} – {max_raw or 'N/A'}"


def _fetch_rows(conn, handle):
    cur = conn.cursor()
    cur.execute(
        """
        SELECT tweet_id, created_at, handle, full_text,
               likes, retweets, replies, quotes, views, url
        FROM tweets WHERE handle=? ORDER BY created_at DESC
        """,
        (handle,),
    )
    return cur.fetchall()


def _build_data_rows(db_rows):
    """Validate + convert DB rows. Returns (rows, invalid_ids, invalid_dates).

    Each row: (tweet_id_str, created_at_raw, clean_date|None, handle,
               full_text, likes, retweets, replies, quotes, views, url).
    Rows with invalid Tweet IDs are skipped (reported); rows with bad
    dates keep a blank Clean Dates value (reported, never fatal).
    """
    rows, invalid_ids, invalid_dates = [], 0, 0
    for r in db_rows:
        try:
            tid = validate_tweet_id(r[0])
        except ValueError as e:
            log.warning("Skipping row with invalid Tweet ID: %s", e)
            invalid_ids += 1
            continue
        clean = parse_clean_date(r[1])
        if clean is None:
            invalid_dates += 1
        rows.append(
            (tid, r[1] or "", clean, r[2] or "", normalize_text(str(r[3] or "")),
             r[4], r[5], r[6], r[7], r[8], r[9] or "")
        )
    return rows, invalid_ids, invalid_dates


def export_to_workbook(handle: str, db_path: Path = DEFAULT_DB,
                       out_path: Path | None = None) -> Path | None:
    """Export one handle to a real .xlsx workbook. Returns path or None."""
    if not HAS_OPENPYXL:
        log.error("openpyxl is required for Excel export. Run: pip install openpyxl")
        print("ERROR: openpyxl not installed. Run: pip install openpyxl")
        return None
    handle = handle.lstrip("@")
    db_path = Path(db_path)
    if not db_path.exists():
        log.error("Database not found at %s. Run 5_leviathan.py first.", db_path)
        print(f"ERROR: database not found at {db_path}. Run 5_leviathan.py first.")
        return None

    conn = sqlite3.connect(str(db_path))
    try:
        total, min_raw, max_raw, min_clean, max_clean = _dataset_stats(conn, handle)
        if not total:
            log.error("No tweets found for @%s in %s.", handle, db_path)
            print(f"ERROR: no tweets found for @{handle} in database.")
            return None
        rows, invalid_ids, invalid_dates = _build_data_rows(_fetch_rows(conn, handle))
    finally:
        conn.close()

    ensure_output_dir()
    if out_path is None:
        out_path = (
            Path(OUTPUT_DIR) / f"X_Dataset_{handle}_{datetime.now().strftime('%Y%m%d')}.xlsx"
        )
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    wb = Workbook()
    ws = wb.active
    ws.title = f"@{handle}"[:31]
    bold = Font(bold=True)

    summary = [
        ("DATASET SUMMARY REPORT", ""),
        ("Target Handle", f"@{handle}"),
        ("Total Dataset Size (Quantity)", f"{total} Tweets"),
        ("Date Range Coverage", _date_range_label(min_clean, max_clean, min_raw, max_raw)),
        ("Generated On", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    ]
    for label, value in summary:
        ws.append([label, value])
    ws["A1"].font = bold
    ws.append([])

    header_row_idx = ws.max_row + 1
    ws.append(HEADERS)
    for col in range(1, len(HEADERS) + 1):
        ws.cell(row=header_row_idx, column=col).font = bold

    for tid, created, clean, author, text, likes, rt, rep, quo, views, url in rows:
        ws.append([tid, created, clean, author, text, likes, rt, rep, quo, views, url])
        r = ws.max_row
        id_cell = ws.cell(row=r, column=COL_TWEET_ID)
        id_cell.value = tid          # actual string value — no formula
        id_cell.data_type = "s"      # force shared-string storage
        id_cell.number_format = TEXT_FORMAT  # '@' = Text
        clean_cell = ws.cell(row=r, column=COL_CLEAN_DATES)
        if clean is None:
            clean_cell.value = ""    # invalid/missing date: blank, never fatal
        else:
            clean_cell.value = datetime(clean.year, clean.month, clean.day)
            clean_cell.number_format = CLEAN_DATE_FORMAT

    ws.append([])
    ws.append(["METHODOLOGY"])
    ws["A%d" % ws.max_row].font = bold
    ws.append([build_methodology()])

    widths = [22, 30, 13, 18, 80, 10, 10, 10, 10, 12, 50]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w

    wb.save(out_path)
    log.info("Excel workbook generated: %s (%d rows)", out_path, len(rows))
    if invalid_ids:
        log.warning("%d row(s) skipped: invalid Tweet ID", invalid_ids)
    if invalid_dates:
        log.warning("%d row(s) with invalid/missing date (blank Clean Dates)", invalid_dates)
    print(f"Done: {out_path} ({len(rows)} rows)")
    return out_path


def export_to_csv(handle: str, db_path: Path = DEFAULT_DB,
                  out_path: Path | None = None) -> Path | None:
    """Export one handle to a UTF-8-BOM CSV companion (full-digit ID strings)."""
    handle = handle.lstrip("@")
    db_path = Path(db_path)
    if not db_path.exists():
        log.error("Database not found at %s. Run 5_leviathan.py first.", db_path)
        print(f"ERROR: database not found at {db_path}. Run 5_leviathan.py first.")
        return None

    conn = sqlite3.connect(str(db_path))
    try:
        total, min_raw, max_raw, min_clean, max_clean = _dataset_stats(conn, handle)
        if not total:
            log.error("No tweets found for @%s in %s.", handle, db_path)
            print(f"ERROR: no tweets found for @{handle} in database.")
            return None
        rows, invalid_ids, invalid_dates = _build_data_rows(_fetch_rows(conn, handle))
    finally:
        conn.close()

    ensure_output_dir()
    if out_path is None:
        out_path = (
            Path(OUTPUT_DIR) / f"X_Dataset_{handle}_{datetime.now().strftime('%Y%m%d')}.csv"
        )
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # UTF-8 BOM is critical for Excel emoji/special-char rendering.
    # QUOTE_NONNUMERIC keeps Tweet IDs quoted so spreadsheet apps read
    # them as strings instead of coercing to scientific notation.
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f, quoting=csv.QUOTE_NONNUMERIC)
        writer.writerow(["DATASET SUMMARY REPORT"])
        writer.writerow(["Target Handle", f"@{handle}"])
        writer.writerow(["Total Dataset Size (Quantity)", f"{total} Tweets"])
        writer.writerow(
            ["Date Range Coverage",
             _date_range_label(min_clean, max_clean, min_raw, max_raw)]
        )
        writer.writerow(["Generated On", datetime.now().strftime("%Y-%m-%d %H:%M:%S")])
        writer.writerow([])
        writer.writerow(HEADERS)
        for tid, created, clean, author, text, likes, rt, rep, quo, views, url in rows:
            writer.writerow(
                [f'="{tid}"', created,
                 format_client_date(clean),
                 author, text, likes, rt, rep, quo, views, url]
            )
        writer.writerow([])
        writer.writerow(["METHODOLOGY"])
        writer.writerow([build_methodology()])

    log.info("Excel CSV generated: %s (%d rows)", out_path, len(rows))
    print(f"Done: {out_path} ({len(rows)} rows)")
    return out_path


def export_to_excel(handle: str, db_path: Path = DEFAULT_DB,
                    out_path: Path | None = None) -> Path | None:
    """Primary export: real .xlsx workbook + CSV companion. Returns xlsx path."""
    xlsx_out = out_path
    if xlsx_out is not None:
        xlsx_out = Path(xlsx_out)
        if xlsx_out.suffix.lower() == ".csv":
            # Legacy caller asked for CSV: honour it, plus build the workbook.
            csv_result = export_to_csv(handle, db_path, xlsx_out)
            xlsx_out = xlsx_out.with_suffix(".xlsx")
            xlsx_result = export_to_workbook(handle, db_path, xlsx_out)
            return xlsx_result or csv_result
    xlsx_result = export_to_workbook(handle, db_path, xlsx_out)
    if xlsx_result is not None:
        export_to_csv(handle, db_path, xlsx_result.with_suffix(".csv"))
    return xlsx_result


def list_handles(db_path: Path = DEFAULT_DB) -> list[str]:
    db_path = Path(db_path)
    if not db_path.exists():
        return []
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT handle FROM tweets ORDER BY handle")
        return [r[0] for r in cur.fetchall() if r[0]]
    finally:
        conn.close()


def dataset_summary_table(db_path: Path = DEFAULT_DB,
                          handles: list[str] | None = None) -> list[dict]:
    """Per-dataset sizes from actual extracted data (zero rows shown clearly)."""
    db_path = Path(db_path)
    table = []
    if not db_path.exists():
        return table
    conn = sqlite3.connect(str(db_path))
    try:
        available = {r[0] for r in
                     conn.execute("SELECT DISTINCT handle FROM tweets").fetchall()}
        wanted = handles if handles else sorted(available)
        for h in wanted:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM tweets WHERE handle=?", (h,))
            total = cur.fetchone()[0]
            table.append({
                "dataset": h,
                "target_handle": f"@{h}",
                "total_tweets": total,
                "status": "OK" if h in available and total else
                          ("ZERO RECORDS" if h in available else "EXTRACTION FAILED / NO DATA"),
            })
    finally:
        conn.close()
    return table


def print_summary_table(table: list[dict]) -> int:
    print("")
    print("| Dataset | Target Handle | Total Tweets / Records |")
    print("| ------- | ------------- | ---------------------: |")
    grand = 0
    for row in table:
        count = (f"{row['total_tweets']:,}"
                 if row["status"] == "OK" else row["status"])
        print(f"| {row['dataset']} | {row['target_handle']} | {count} |")
        grand += row["total_tweets"]
    print(f"| Total |  | {grand:,} |")
    print("")
    return grand


def export_summary_report(db_path: Path = DEFAULT_DB,
                          out_path: Path | None = None,
                          handles: list[str] | None = None) -> Path | None:
    """Combined summary workbook: size table + methodology. Returns path."""
    if not HAS_OPENPYXL:
        log.error("openpyxl is required for Excel export. Run: pip install openpyxl")
        return None
    table = dataset_summary_table(db_path, handles)
    if out_path is None:
        ensure_output_dir()
        out_path = (Path(OUTPUT_DIR)
                    / f"X_Dataset_SUMMARY_{datetime.now().strftime('%Y%m%d')}.xlsx")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    bold = Font(bold=True)
    ws.append(["DATASET SIZE SUMMARY"])
    ws["A1"].font = bold
    ws.append(["Dataset", "Target Handle", "Total Tweets / Records"])
    for c in range(1, 4):
        ws.cell(row=2, column=c).font = bold
    grand = 0
    for row in table:
        count = row["total_tweets"] if row["status"] == "OK" else row["status"]
        ws.append([row["dataset"], row["target_handle"], count])
        grand += row["total_tweets"]
    ws.append(["Total", "", grand])
    for c in range(1, 4):
        ws.cell(row=ws.max_row, column=c).font = bold
    ws.append([])
    ws.append(["METHODOLOGY"])
    ws["A%d" % ws.max_row].font = bold
    ws.append([build_methodology()])
    for w, col in zip([22, 20, 26], "ABC"):
        ws.column_dimensions[col].width = w
    wb.save(out_path)
    grand = print_summary_table(table)
    log.info("Summary report generated: %s", out_path)
    print(f"Done: {out_path}")
    return out_path


def export_all(db_path: Path = DEFAULT_DB, out_dir: Path | None = None,
               handles: list[str] | None = None) -> list[Path]:
    """Export every dataset + combined summary. Returns generated paths."""
    db_path = Path(db_path)
    wanted = handles or list_handles(db_path)
    paths = []
    for h in wanted:
        stem = (f"X_Dataset_{h}_{datetime.now().strftime('%Y%m%d')}")
        base = Path(out_dir) if out_dir else Path(OUTPUT_DIR)
        result = export_to_excel(h, db_path, base / f"{stem}.xlsx")
        if result is not None:
            paths.append(result)
    summary = export_summary_report(
        db_path,
        (Path(out_dir) if out_dir else Path(OUTPUT_DIR))
        / f"X_Dataset_SUMMARY_{datetime.now().strftime('%Y%m%d')}.xlsx",
        wanted,
    )
    if summary is not None:
        paths.append(summary)
    return paths


def main():
    parser = argparse.ArgumentParser(description="Export leviathan.db to client Excel")
    parser.add_argument("--handle", required=False, help="The handle you scraped")
    parser.add_argument("--all", action="store_true",
                        help="Export every handle in the DB + summary table")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite path")
    parser.add_argument("--out", default=None, help="Output .xlsx path")
    args = parser.parse_args()
    if args.all:
        export_all(Path(args.db))
    else:
        if not args.handle:
            parser.error("--handle is required unless --all is given")
        result = export_to_excel(args.handle, Path(args.db),
                                 Path(args.out) if args.out else None)
        if result is None:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
