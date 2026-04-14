#!/usr/bin/env python3
"""
export_csv.py – Fixed version with full text extraction
Handles: long tweets (note_tweet), retweets, quoted tweets
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


def get_full_text(tweet_obj: Dict[str, Any]) -> str:
    """
    Extract the complete, non-truncated text from a tweet object.
    Prioritises note_tweet (tweets >280 chars), then legacy.full_text.
    """
    note = tweet_obj.get("note_tweet")
    if note and isinstance(note, dict):
        text = note.get("text")
        if text:
            return normalize_text(text)

    legacy = tweet_obj.get("legacy", {})
    text = legacy.get("full_text", "")
    return normalize_text(text)


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
    screen_name = core_user.get("screen_name", "")
    name = core_user.get("name", "")

    is_retweet = "retweeted_status_result" in t or "retweeted_status_result" in legacy
    is_quote = bool(legacy.get("is_quote_status", False))

    if is_retweet:
        rt_obj = legacy.get("retweeted_status_result", {})
        if not rt_obj:
            rt_obj = t.get("retweeted_status_result", {})
        rt_result = rt_obj.get("result", {}) if isinstance(rt_obj, dict) else {}
        full_text = get_full_text(rt_result)
        retweeted_screen_name = deep_get(
            rt_result, ["core", "user_results", "result", "core", "screen_name"], ""
        )
        retweeted_tweet_id = rt_result.get("rest_id") or deep_get(
            rt_result, ["legacy", "id_str"], ""
        )
        retweeted_tweet_url = (
            f"https://x.com/{retweeted_screen_name}/status/{retweeted_tweet_id}"
            if retweeted_screen_name
            else ""
        )
    else:
        full_text = get_full_text(t)
        retweeted_screen_name = ""
        retweeted_tweet_id = ""
        retweeted_tweet_url = ""

    quoted_tweet_id = ""
    quoted_screen_name = ""
    quoted_full_text = ""
    quoted_tweet_url = ""
    if is_quote and not is_retweet:
        quoted_obj = deep_get(t, ["quoted_status_result", "result"], {})
        if quoted_obj:
            quoted_tweet_id = quoted_obj.get("rest_id") or deep_get(
                quoted_obj, ["legacy", "id_str"], ""
            )
            quoted_screen_name = deep_get(
                quoted_obj, ["core", "user_results", "result", "core", "screen_name"], ""
            )
            quoted_full_text = get_full_text(quoted_obj)
            quoted_tweet_url = (
                f"https://x.com/{quoted_screen_name}/status/{quoted_tweet_id}"
                if quoted_screen_name
                else ""
            )

    created_at = legacy.get("created_at", "")
    created_at_iso = parse_created_at_iso(created_at)
    source_label, source_url = parse_source_html(t.get("source", ""))

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

    is_link_only = False
    if not is_retweet:
        raw_text = legacy.get("full_text", "")
        if re.fullmatch(r"\s*https?://\S+\s*", raw_text):
            is_link_only = True

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
        "view_count_available": 1 if deep_get(t, ["views", "count"], 0) is not None else 0,
        "lang": legacy.get("lang", ""),
        "is_retweet": 1 if is_retweet else 0,
        "is_quote": 1 if is_quote else 0,
        "is_link_only": 1 if is_link_only else 0,
        "source_label": source_label,
        "source_url": source_url,
        "hashtags": list_join(hashtags),
        "mentions": list_join(mentions),
        "urls": list_join(urls),
        "retweeted_from_screen_name": retweeted_screen_name,
        "retweeted_tweet_id": str(retweeted_tweet_id) if retweeted_tweet_id else "",
        "retweeted_tweet_url": retweeted_tweet_url,
        "quoted_tweet_id": str(quoted_tweet_id) if quoted_tweet_id else "",
        "quoted_screen_name": quoted_screen_name,
        "quoted_full_text": quoted_full_text,
        "quoted_tweet_url": quoted_tweet_url,
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


def compute_null_rates(rows: List[Dict[str, Any]]) -> Dict[str, float]:
    if not rows:
        return {}
    null_counts = {}
    total = len(rows)
    for row in rows:
        for key, value in row.items():
            if key not in null_counts:
                null_counts[key] = 0
            is_null = value is None or value == "" or value == [] or value == {}
            if is_null:
                null_counts[key] += 1
    return {key: round(count / total, 3) for key, count in null_counts.items()}


def convert_json_to_csv(
    input_json: Path, output_csv: Path, output_report: Optional[Path] = None, dedup: bool = True
) -> Dict[str, Any]:
    tweets = load_json(input_json)
    rows = []
    seen = set()
    dropped_no_id = 0
    dropped_dupe = 0
    parse_failures = 0

    for t in tweets:
        try:
            row = extract_tweet_row(t)
        except Exception:
            parse_failures += 1
            continue

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
        "view_count_available",
        "lang",
        "is_retweet",
        "is_quote",
        "is_link_only",
        "source_label",
        "source_url",
        "hashtags",
        "mentions",
        "urls",
        "retweeted_from_screen_name",
        "retweeted_tweet_id",
        "retweeted_tweet_url",
        "quoted_tweet_id",
        "quoted_screen_name",
        "quoted_full_text",
        "quoted_tweet_url",
        "tweet_url",
    ]

    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    null_rates = compute_null_rates(rows)

    report = {
        "input_file": str(input_json),
        "output_file": str(output_csv),
        "input_rows": len(tweets),
        "output_rows": len(rows),
        "dedup_dropped": dropped_dupe,
        "no_id_dropped": dropped_no_id,
        "parse_failures": parse_failures,
        "null_rates": null_rates,
        "dedup_enabled": dedup,
    }

    if output_report:
        output_report.parent.mkdir(parents=True, exist_ok=True)
        with output_report.open("w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)

    return report


def main():
    parser = argparse.ArgumentParser(
        description="Convert tweet JSON to clean CSV (full text fixed)."
    )
    parser.add_argument("--input", required=True, help="Path to input JSON file")
    parser.add_argument("--output", required=False, help="Path to output CSV file")
    parser.add_argument("--report", required=False, help="Path to export report JSON file")
    parser.add_argument("--no-dedup", action="store_true", help="Disable dedup by tweet_id")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    output_path = Path(args.output) if args.output else input_path.with_suffix(".csv")

    report_path = None
    if args.report:
        report_path = Path(args.report)
    else:
        report_path = output_path.with_suffix(".report.json")

    stats = convert_json_to_csv(input_path, output_path, report_path, dedup=not args.no_dedup)

    print("CSV conversion complete (full text extracted)")
    print(f"   Input:        {input_path}")
    print(f"   Output:       {output_path}")
    print(f"   Report:       {report_path}")
    print(f"   Input rows:   {stats['input_rows']}")
    print(f"   Written rows: {stats['output_rows']}")
    print(f"   Dedup dropped:{stats['dedup_dropped']}")
    print(f"   No-id dropped:{stats['no_id_dropped']}")
    print(f"   Parse failures:{stats['parse_failures']}")
    print()
    print("Null rates (top 5):")
    sorted_rates = sorted(stats["null_rates"].items(), key=lambda x: x[1], reverse=True)
    for col, rate in sorted_rates[:5]:
        print(f"   {col}: {rate:.1%}")


if __name__ == "__main__":
    main()
