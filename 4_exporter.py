"""
4_exporter.py
Convert raw tweet JSON to clean CSV. Optionally serve via FastAPI.

Usage:
  python 4_exporter.py                                    # export to CSV
  python 4_exporter.py --input tweets_elonmusk_full.json   # custom input
  python 4_exporter.py --serve                            # also start API server
  python 4_exporter.py --serve --port 8080                # custom port
"""

import argparse
import csv
import json
import os
import sys
from pathlib import Path
from datetime import datetime
from typing import Optional

from config import (
    OUTPUT_DIR,
    clean_tweet,
    deduplicate,
    load_json,
    output_path,
    log,
)

CSV_FIELDS = [
    "id",
    "created_at",
    "text",
    "likes",
    "retweets",
    "replies",
    "quotes",
    "views",
    "lang",
    "is_retweet",
    "possibly_sensitive",
]

DATE_FORMATS = [
    "%a %b %d %H:%M:%S %z %Y",
    "%Y-%m-%dT%H:%M:%S.%fZ",
    "%Y-%m-%d %H:%M:%S",
]


def parse_twitter_date(date_str: Optional[str]) -> str:
    if not date_str:
        return ""
    for fmt in DATE_FORMATS:
        try:
            dt = datetime.strptime(date_str, fmt)
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            continue
    return date_str


def json_to_csv(input_file: str, output_file: str) -> str:
    if not os.path.exists(input_file):
        abort(f"Input file not found: {input_file}")

    with open(input_file, "r", encoding="utf-8") as f:
        raw_tweets = json.load(f)

    if not isinstance(raw_tweets, list):
        abort(f"Expected a list of tweets, got {type(raw_tweets).__name__}")

    unique = deduplicate(raw_tweets)
    log.info("Loaded %d raw tweets, %d unique after dedup", len(raw_tweets), len(unique))

    cleaned = []
    for t in unique:
        c = clean_tweet(t)
        if c.get("id"):
            c["created_at"] = parse_twitter_date(c.get("created_at"))
            cleaned.append(c)

    cleaned.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    log.info("Exporting %d tweets to %s", len(cleaned), output_file)

    with open(output_file, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(cleaned)

    log.info("CSV written: %s (%d rows)", output_file, len(cleaned))
    return output_file


def start_server(input_file: str, port: int = 3000):
    from fastapi import FastAPI, Query
    from fastapi.responses import FileResponse
    from fastapi.middleware.cors import CORSMiddleware
    import uvicorn

    csv_file = output_path(input_file.replace(".json", ".csv"))

    if not os.path.exists(csv_file):
        log.info("CSV not found, generating from %s", input_file)
        json_to_csv(
            output_path(input_file) if os.path.exists(output_path(input_file)) else input_file,
            csv_file,
        )

    app = FastAPI(title="X Tweet Exporter", version="1.0.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET"],
        allow_headers=["*"],
    )

    _tweets_cache: Optional[list] = None

    def _load_tweets() -> list:
        nonlocal _tweets_cache
        if _tweets_cache is not None:
            return _tweets_cache

        json_path = output_path(input_file)
        if os.path.exists(json_path):
            with open(json_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            _tweets_cache = [clean_tweet(t) for t in deduplicate(raw)]
            log.info("Loaded %d tweets into cache", len(_tweets_cache))
        else:
            _tweets_cache = []
            log.warning("No JSON file found at %s", json_path)
        return _tweets_cache

    @app.get("/tweets")
    async def get_tweets(limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0)):
        tweets = _load_tweets()
        return {
            "total": len(tweets),
            "offset": offset,
            "limit": limit,
            "tweets": tweets[offset : offset + limit],
        }

    @app.get("/tweets/search")
    async def search_tweets(
        q: str = Query(..., min_length=1), limit: int = Query(100, ge=1, le=1000)
    ):
        tweets = _load_tweets()
        q_lower = q.lower()
        results = [t for t in tweets if q_lower in t.get("text", "").lower()]
        return {
            "query": q,
            "total_matches": len(results),
            "tweets": results[:limit],
        }

    @app.get("/download")
    async def download_csv():
        return FileResponse(
            csv_file,
            media_type="text/csv",
            filename=os.path.basename(csv_file),
        )

    @app.get("/stats")
    async def get_stats():
        tweets = _load_tweets()
        if not tweets:
            return {"total": 0, "stats": {}}
        total_likes = sum(t.get("likes", 0) or 0 for t in tweets)
        total_retweets = sum(t.get("retweets", 0) or 0 for t in tweets)
        return {
            "total_tweets": len(tweets),
            "total_likes": total_likes,
            "total_retweets": total_retweets,
            "avg_likes": round(total_likes / len(tweets), 1) if tweets else 0,
            "avg_retweets": round(total_retweets / len(tweets), 1) if tweets else 0,
        }

    @app.get("/health")
    async def health():
        return {"status": "ok", "tweets_loaded": len(_load_tweets())}

    log.info("Starting API server on http://localhost:%d", port)
    uvicorn.run(app, host="0.0.0.0", port=port)


def abort(msg: str):
    log.error(msg)
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Export tweets to CSV and serve via API")
    parser.add_argument(
        "--input",
        default="tweets_elonmusk_full.json",
        help="Input JSON filename (looked up in output/)",
    )
    parser.add_argument(
        "--output", default=None, help="Output CSV path (default: auto-derived from input)"
    )
    parser.add_argument(
        "--serve", action="store_true", help="Start FastAPI server after CSV export"
    )
    parser.add_argument("--port", type=int, default=3000, help="Server port (default: 3000)")
    args = parser.parse_args()

    input_path = output_path(args.input) if not os.path.isabs(args.input) else args.input
    output_csv = args.output or input_path.replace(".json", ".csv")

    json_to_csv(input_path, output_csv)

    if args.serve:
        start_server(args.input, args.port)


if __name__ == "__main__":
    main()
