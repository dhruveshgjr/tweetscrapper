#!/usr/bin/env python3
"""
4_export_csv.py
Convert tweet JSON to clean CSV.
Can run standalone: python3 4_export_csv.py --input ./output/tweets_JeffBezos_browser.json
Or import for use in other scripts.
"""

import argparse
import csv
import json
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def deep_get(obj: Dict[str, Any], path: List[str], default=None):
    cur = obj
    for p in path:
        if not isinstance(cur, dict) or p not in cur:
            return default
        cur = cur[p]
    return cur


def parse_source_html(source_html: Optional[str]) -> Tuple[str, str]:
    if not source_html:
        return "", ""
    s = unescape(source_html)
    m_href = re.search(r'href="([^"]+)"', s)
    m_text = re.search(r">([^<]+)<", s)
    return (m_text.group(1).strip() if m_text else "", m_href.group(1).strip() if m_href else "")


def parse_created_at_iso(created_at: Optional[str]) -> str:
    if not created_at:
        return ""
    try:
        dt = parsedate_to_datetime(created_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        return ""


def normalize_text(text: Optional[str]) -> str:
    if not text:
        return ""
    text = text.replace("\r", " ").replace("\n", " ")
    return re.sub(r"\s+", " ", text).strip()


def list_join(items: List[str]) -> str:
    return "|".join([i for i in items if i])


def safe_int(v: Any, default: int = 0) -> int:
    try:
        if v is None or v == "":
            return default
        return int(v)
    except Exception:
        return default


def extract_tweet_row(t: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    tweet_id = t.get("rest_id") or deep_get(t, ["legacy", "id_str"], "")
    if not tweet_id:
        return None

    legacy = t.get("legacy", {}) if isinstance(t.get("legacy"), dict) else {}
    core_user = deep_get(t, ["core", "user_results", "result", "core"], {}) or {}

    full_text = normalize_text(legacy.get("full_text", ""))
    created_at = legacy.get("created_at", "")
    created_at_iso = parse_created_at_iso(created_at)

    source_label, source_url = parse_source_html(legacy.get("source", ""))

    entities = legacy.get("entities", {}) if isinstance(legacy.get("entities"), dict) else {}
    hashtags = [h.get("text", "") for h in entities.get("hashtags", []) if isinstance(h, dict)]
    mentions = [
        m.get("screen_name", "") for m in entities.get("user_mentions", []) if isinstance(m, dict)
    ]
    urls = [
        u.get("expanded_url") or u.get("url", "")
        for u in entities.get("urls", [])
        if isinstance(u, dict)
    ]

    screen_name = core_user.get("screen_name", "")
    name = core_user.get("name", "")

    is_retweet = "retweeted_status_result" in t or full_text.startswith("RT @")
    is_quote = bool(legacy.get("is_quote_status", False))

    row = {
        "tweet_id": str(tweet_id),
        "created_at": created_at,
        "created_at_iso": created_at_iso,
        "screen_name": screen_name,
        "name": name,
        "full_text": full_text,
        "retweet_count": safe_int(legacy.get("retweet_count", 0)),
        "favorite_count": safe_int(legacy.get("favorite_count", 0)),
        "reply_count": safe_int(legacy.get("reply_count", 0)),
        "quote_count": safe_int(legacy.get("quote_count", 0)),
        "view_count": safe_int(deep_get(t, ["views", "count"], 0)),
        "lang": legacy.get("lang", ""),
        "is_retweet": is_retweet,
        "is_quote": is_quote,
        "source_label": source_label,
        "source_url": source_url,
        "hashtags": list_join(hashtags),
        "mentions": list_join(mentions),
        "urls": list_join(urls),
        "tweet_url": f"https://x.com/{screen_name}/status/{tweet_id}"
        if screen_name
        else f"https://x.com/i/web/status/{tweet_id}",
    }
    return row


def load_json(path: Path) -> List[Dict[str, Any]]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        if "tweets" in data and isinstance(data["tweets"], list):
            return [x for x in data["tweets"] if isinstance(x, dict)]
    raise ValueError("Unsupported JSON shape. Expected a list of tweet objects.")


def convert_json_to_csv(input_json: Path, output_csv: Path, dedup: bool = True) -> Dict[str, int]:
    tweets = load_json(input_json)

    rows: List[Dict[str, Any]] = []
    seen = set()
    dropped_no_id = 0
    dropped_dupe = 0

    for t in tweets:
        row = extract_tweet_row(t)
        if not row:
            dropped_no_id += 1
            continue
        tid = row["tweet_id"]
        if dedup and tid in seen:
            dropped_dupe += 1
            continue
        seen.add(tid)
        rows.append(row)

    columns = [
        "tweet_id",
        "created_at",
        "created_at_iso",
        "screen_name",
        "name",
        "full_text",
        "retweet_count",
        "favorite_count",
        "reply_count",
        "quote_count",
        "view_count",
        "lang",
        "is_retweet",
        "is_quote",
        "source_label",
        "source_url",
        "hashtags",
        "mentions",
        "urls",
        "tweet_url",
    ]

    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    return {
        "input_count": len(tweets),
        "written_count": len(rows),
        "dropped_no_id": dropped_no_id,
        "dropped_dupe": dropped_dupe,
    }


def main():
    parser = argparse.ArgumentParser(description="Convert tweet JSON to clean CSV.")
    parser.add_argument("--input", required=True, help="Path to input JSON file")
    parser.add_argument("--output", required=False, help="Path to output CSV file")
    parser.add_argument("--no-dedup", action="store_true", help="Disable dedup by tweet_id")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    if args.output:
        output_path = Path(args.output)
    else:
        output_path = input_path.with_suffix(".csv")

    stats = convert_json_to_csv(
        input_json=input_path,
        output_csv=output_path,
        dedup=not args.no_dedup,
    )

    print("CSV conversion complete")
    print(f"   Input:   {input_path}")
    print(f"   Output:  {output_path}")
    print(f"   Input rows:      {stats['input_count']}")
    print(f"   Written rows:    {stats['written_count']}")
    print(f"   Dropped no id:   {stats['dropped_no_id']}")
    print(f"   Dropped dupes:   {stats['dropped_dupe']}")


if __name__ == "__main__":
    main()
