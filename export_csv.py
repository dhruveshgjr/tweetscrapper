#!/usr/bin/env python3
"""
export_csv.py
Convert tweet JSON to clean CSV with production-grade data quality.

Phase 1 improvements:
- Booleans standardized to 0/1
- is_link_only derived field
- view_count_available flag
- Retweet original metadata
- Export report with null rates

Phase 2 fixes:
- Emoji preservation (ensure_ascii=False)
- Retweet full_text extraction from nested structure
- Entity extraction from retweet's entities
- is_quote=0 for retweets
- tweet_id as string (no scientific notation)
- retweeted_from_screen_name and retweeted_tweet_id populated

Can run standalone: python3 export_csv.py --input ./output/tweets_JeffBezos_browser.json
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


def is_link_only_tweet(full_text: str, entities: dict) -> int:
    if not full_text:
        return 1
    media = entities.get("media", []) if isinstance(entities, dict) else []
    urls_in_text = re.findall(r"https?://\S+", full_text)
    if len(urls_in_text) == 1 and len(media) >= 1:
        url_without_protocol = re.sub(r"^https?://", "", urls_in_text[0])
        if url_without_protocol in full_text.replace(urls_in_text[0], "").strip():
            return 1
    stripped = full_text.strip()
    if stripped.startswith("http://") or stripped.startswith("https://"):
        return 1
    return 0


def extract_retweet_metadata(tweet: dict, full_text: str = "") -> Tuple[str, str, str]:
    """
    For retweets, extract:
    - Original author's screen_name
    - Original tweet_id (may not be available)
    - Original full_text (from nested structure or parsed from RT text)

    Checks:
    1. retweeted_status_result nested structure (if available in JSON)
    2. full_text parsing as fallback ("RT @screen_name: ...")
    """
    retweeted_result = tweet.get("retweeted_status_result", {})
    if retweeted_result:
        result = retweeted_result.get("result", {})
        if result:
            core = result.get("core", {})
            user_results = core.get("user_results", {})
            user_result = user_results.get("result", {})
            user_core = user_result.get("core", {})
            screen_name = user_core.get("screen_name", "")

            legacy = result.get("legacy", {})
            original_id = str(legacy.get("id_str", ""))
            original_full_text = normalize_text(legacy.get("full_text", ""))

            return screen_name, original_id, original_full_text

    if full_text.startswith("RT @"):
        match = re.match(r"RT @(\w+):", full_text)
        if match:
            screen_name = match.group(1)
            return screen_name, "", ""

    return "", "", ""


def extract_entities(tweet: dict) -> Tuple[List[str], List[str], List[str]]:
    """
    Extract hashtags, mentions, and URLs from tweet entities.
    Checks:
    1. legacy.entities
    2. retweeted_status_result entities (for RTs)
    3. note_tweet.entity_set (for long tweets)
    """
    hashtags = []
    mentions = []
    urls = []

    legacy = tweet.get("legacy", {}) if isinstance(tweet.get("legacy"), dict) else {}
    entities = legacy.get("entities", {})

    if isinstance(entities, dict):
        hashtags_raw = entities.get("hashtags", [])
        mentions_raw = entities.get("user_mentions", [])
        urls_raw = entities.get("urls", [])

        if isinstance(hashtags_raw, list):
            hashtags = [
                h.get("text", "") for h in hashtags_raw if isinstance(h, dict) and h.get("text")
            ]
        if isinstance(mentions_raw, list):
            mentions = [
                m.get("screen_name", "")
                for m in mentions_raw
                if isinstance(m, dict) and m.get("screen_name")
            ]
        if isinstance(urls_raw, list):
            urls = [
                u.get("expanded_url") or u.get("url", "") for u in urls_raw if isinstance(u, dict)
            ]

    if not hashtags or not mentions or not urls:
        retweeted_result = tweet.get("retweeted_status_result", {})
        if isinstance(retweeted_result, dict):
            result = retweeted_result.get("result", {})
            if isinstance(result, dict):
                rt_legacy = result.get("legacy", {})
                if isinstance(rt_legacy, dict):
                    rt_entities = rt_legacy.get("entities", {})
                    if isinstance(rt_entities, dict):
                        if not hashtags:
                            rt_hashtags = rt_entities.get("hashtags", [])
                            if isinstance(rt_hashtags, list):
                                hashtags = [
                                    h.get("text", "")
                                    for h in rt_hashtags
                                    if isinstance(h, dict) and h.get("text")
                                ]
                        if not mentions:
                            rt_mentions = rt_entities.get("user_mentions", [])
                            if isinstance(rt_mentions, list):
                                mentions = [
                                    m.get("screen_name", "")
                                    for m in rt_mentions
                                    if isinstance(m, dict) and m.get("screen_name")
                                ]
                        if not urls:
                            rt_urls = rt_entities.get("urls", [])
                            if isinstance(rt_urls, list):
                                urls = [
                                    u.get("expanded_url") or u.get("url", "")
                                    for u in rt_urls
                                    if isinstance(u, dict)
                                ]

    if not hashtags or not mentions or not urls:
        note_tweet = tweet.get("note_tweet", {})
        if isinstance(note_tweet, dict):
            note_results = note_tweet.get("note_tweet_results", {})
            if isinstance(note_results, dict):
                note_result = note_results.get("result", {})
                if isinstance(note_result, dict):
                    entity_set = note_result.get("entity_set", {})
                    if isinstance(entity_set, dict):
                        if not hashtags:
                            nt_hashtags = entity_set.get("hashtags", [])
                            if isinstance(nt_hashtags, list):
                                hashtags = [
                                    h.get("text", "")
                                    for h in nt_hashtags
                                    if isinstance(h, dict) and h.get("text")
                                ]
                        if not mentions:
                            nt_mentions = entity_set.get("user_mentions", [])
                            if isinstance(nt_mentions, list):
                                mentions = [
                                    m.get("screen_name", "")
                                    for m in nt_mentions
                                    if isinstance(m, dict) and m.get("screen_name")
                                ]
                        if not urls:
                            nt_urls = entity_set.get("urls", [])
                            if isinstance(nt_urls, list):
                                urls = [
                                    u.get("expanded_url") or u.get("url", "")
                                    for u in nt_urls
                                    if isinstance(u, dict)
                                ]

    return hashtags, mentions, urls


def extract_full_text(tweet: dict) -> Tuple[str, bool]:
    """
    Extract full_text from multiple possible paths.
    For retweets, prefer the original tweet's text.

    Returns (full_text, is_retweet).
    """
    legacy = tweet.get("legacy", {}) if isinstance(tweet.get("legacy"), dict) else {}
    full_text = legacy.get("full_text", "")

    retweeted_result = tweet.get("retweeted_status_result", {})
    has_retweet_structure = bool(retweeted_result)

    if has_retweet_structure:
        result = retweeted_result.get("result", {})
        if result:
            rt_legacy = result.get("legacy", {})
            rt_text = rt_legacy.get("full_text", "") if isinstance(rt_legacy, dict) else ""
            if rt_text:
                return normalize_text(rt_text), True

    if full_text and len(full_text) > 10:
        return normalize_text(full_text), full_text.startswith("RT @")

    note_tweet = tweet.get("note_tweet", {})
    if isinstance(note_tweet, dict):
        note_results = note_tweet.get("note_tweet_results", {})
        if isinstance(note_results, dict):
            note_result = note_results.get("result", {})
            if isinstance(note_result, dict):
                note_text = note_result.get("text", "")
                if note_text:
                    return normalize_text(note_text), False

    return normalize_text(full_text) if full_text else "", full_text.startswith("RT @")


def extract_tweet_row(t: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    tweet_id = t.get("rest_id") or deep_get(t, ["legacy", "id_str"], "")
    if not tweet_id:
        return None

    tweet_id = str(tweet_id)

    legacy = t.get("legacy", {}) if isinstance(t.get("legacy"), dict) else {}
    core_user = deep_get(t, ["core", "user_results", "result", "core"], {}) or {}

    full_text, is_retweet_from_text = extract_full_text(t)

    created_at = legacy.get("created_at", "")
    created_at_iso = parse_created_at_iso(created_at)

    source_label, source_url = parse_source_html(t.get("source", ""))

    hashtags, mentions, urls = extract_entities(t)

    screen_name = core_user.get("screen_name", "")
    name = core_user.get("name", "")

    is_retweet_flag = 1 if ("retweeted_status_result" in t or is_retweet_from_text) else 0
    is_quote_flag = 1 if (bool(legacy.get("is_quote_status", False)) and not is_retweet_flag) else 0

    is_link_only = is_link_only_tweet(full_text, legacy.get("entities", {}))

    retweeted_from_screen_name, retweeted_tweet_id, _ = extract_retweet_metadata(t, full_text)

    view_count_raw = deep_get(t, ["views", "count"], "0")
    view_count = safe_int(view_count_raw, 0)
    view_count_available = 0 if view_count_raw == "0" or view_count_raw == 0 else 1

    row = {
        "tweet_id": tweet_id,
        "created_at": created_at,
        "created_at_iso": created_at_iso,
        "screen_name": screen_name,
        "name": name,
        "full_text": full_text,
        "retweet_count": safe_int(legacy.get("retweet_count", 0)),
        "favorite_count": safe_int(legacy.get("favorite_count", 0)),
        "reply_count": safe_int(legacy.get("reply_count", 0)),
        "quote_count": safe_int(legacy.get("quote_count", 0)),
        "view_count": view_count,
        "view_count_available": view_count_available,
        "lang": legacy.get("lang", ""),
        "is_retweet": is_retweet_flag,
        "is_quote": is_quote_flag,
        "is_link_only": is_link_only,
        "source_label": source_label,
        "source_url": source_url,
        "hashtags": list_join(hashtags),
        "mentions": list_join(mentions),
        "urls": list_join(urls),
        "retweeted_from_screen_name": retweeted_from_screen_name,
        "retweeted_tweet_id": retweeted_tweet_id,
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

    rows: List[Dict[str, Any]] = []
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
    parser = argparse.ArgumentParser(description="Convert tweet JSON to clean CSV.")
    parser.add_argument("--input", required=True, help="Path to input JSON file")
    parser.add_argument("--output", required=False, help="Path to output CSV file")
    parser.add_argument("--report", required=False, help="Path to export report JSON file")
    parser.add_argument("--no-dedup", action="store_true", help="Disable dedup by tweet_id")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    if args.output:
        output_path = Path(args.output)
    else:
        output_path = input_path.with_suffix(".csv")

    report_path = None
    if args.report:
        report_path = Path(args.report)
    else:
        report_path = output_path.with_suffix(".report.json")

    stats = convert_json_to_csv(
        input_json=input_path,
        output_csv=output_path,
        output_report=report_path,
        dedup=not args.no_dedup,
    )

    print("CSV conversion complete")
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
