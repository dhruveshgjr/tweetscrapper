#!/usr/bin/env python3
"""
6_excel_exporter.py — Client-ready Excel CSV generator.
Reads output/leviathan.db (written by 5_leviathan.py) and emits a UTF-8-BOM CSV
with a DATASET SUMMARY header block followed by one row per tweet.

Usage:
  python 6_excel_exporter.py --handle elonmusk
  python 6_excel_exporter.py --handle elonmusk --db output/leviathan.db --out output/custom.csv
"""

import argparse
import csv
import sqlite3
from datetime import datetime
from pathlib import Path

from config import OUTPUT_DIR, ensure_output_dir, log

try:
    from export_csv import normalize_text
except ImportError:

    def normalize_text(text):
        return (text or "").replace("\r", " ").replace("\n", " ").strip()


DEFAULT_DB = Path(OUTPUT_DIR) / "leviathan.db"

HEADERS = [
    "Tweet ID",
    "Date",
    "Author",
    "Full Text (Cleaned for Excel)",
    "Likes",
    "Retweets",
    "Replies",
    "Quotes",
    "Views",
    "URL",
]


def export_to_excel(handle: str, db_path: Path = DEFAULT_DB, out_path: Path | None = None) -> Path | None:
    handle = handle.lstrip("@")
    db_path = Path(db_path)
    if not db_path.exists():
        log.error("Database not found at %s. Run 5_leviathan.py first.", db_path)
        print(f"ERROR: database not found at {db_path}. Run 5_leviathan.py first.")
        return None

    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*), MIN(created_at), MAX(created_at) FROM tweets WHERE handle=?",
            (handle,),
        )
        total, min_date, max_date = cur.fetchone()

        if not total:
            log.error("No tweets found for @%s in %s.", handle, db_path)
            print(f"ERROR: no tweets found for @{handle} in database.")
            return None

        ensure_output_dir()
        if out_path is None:
            out_path = (
                Path(OUTPUT_DIR) / f"X_Dataset_{handle}_{datetime.now().strftime('%Y%m%d')}.csv"
            )
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)

        # UTF-8 BOM is critical for Excel emoji/special-char rendering.
        with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(["DATASET SUMMARY REPORT"])
            writer.writerow(["Target Handle", f"@{handle}"])
            writer.writerow(["Total Dataset Size (Quantity)", f"{total} Tweets"])
            writer.writerow(["Date Range Coverage", f"{min_date or 'N/A'} to {max_date or 'N/A'}"])
            writer.writerow(["Generated On", datetime.now().strftime("%Y-%m-%d %H:%M:%S")])
            writer.writerow([])
            writer.writerow(HEADERS)

            cur.execute(
                """
                SELECT tweet_id, created_at, handle, full_text,
                       likes, retweets, replies, quotes, views, url
                FROM tweets WHERE handle=? ORDER BY created_at DESC
                """,
                (handle,),
            )
            for row in cur.fetchall():
                writer.writerow(
                    [
                        str(row[0]),  # string: stops Excel sci-notation truncation
                        row[1],
                        row[2],
                        normalize_text(str(row[3] or "")),
                        row[4],
                        row[5],
                        row[6],
                        row[7],
                        row[8],
                        row[9],
                    ]
                )
    finally:
        conn.close()

    log.info("Excel CSV generated: %s (%d rows)", out_path, total)
    print(f"Done: {out_path} ({total} rows)")
    return out_path


def main():
    parser = argparse.ArgumentParser(description="Export leviathan.db to client Excel CSV")
    parser.add_argument("--handle", required=True, help="The handle you scraped")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite path")
    parser.add_argument("--out", default=None, help="Output CSV path")
    args = parser.parse_args()
    result = export_to_excel(args.handle, Path(args.db), Path(args.out) if args.out else None)
    if result is None:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
