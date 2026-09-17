#!/usr/bin/env python3
"""
export_csv.py – PRODUCTION FINAL
Fixes: tweet_id as string, URL fallback extraction, lang codes documented.
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
    note = tweet_obj.get("note_tweet")
    if note and isinstance(note, dict):
        text = note.get("text")
        if text:
            return normalize_text(text)
        # Nested shapes: note_tweet_results.result.text / core.text
        result = (note.get("note_tweet_results") or {}).get("result", {}) or {}
        if isinstance(result, dict) and result.get("text"):
            return normalize_text(result["text"])
        core_text = (note.get("core") or {}).get("text") if isinstance(note.get("core"), dict) else None
        if core_text:
            return normalize_text(core_text)
    ext = tweet_obj.get("extended_tweet")
    if ext and isinstance(ext, dict) and ext.get("full_text"):
        return normalize_text(ext["full_text"])
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


def extract_entities(legacy: Dict[str, Any]) -> Tuple[str, str, str]:
    """Extract hashtags, mentions, urls from legacy.entities.
    Also extracts expanded URLs from media entities and extended_entities."""
    entities = legacy.get("entities", {}) if isinstance(legacy, dict) else {}
    hashtags = [h.get("text", "") for h in entities.get("hashtags", []) if isinstance(h, dict)]
    mentions = [
        m.get("screen_name", "") for m in entities.get("user_mentions", []) if isinstance(m, dict)
    ]
    urls = [
        u.get("expanded_url") or u.get("url", "")
        for u in entities.get("urls", [])
        if isinstance(u, dict)
    ]

    extended_entities = legacy.get("extended_entities", {}) if isinstance(legacy, dict) else {}
    media_list = extended_entities.get("media", []) if isinstance(extended_entities, dict) else []
    if not media_list:
        media_list = entities.get("media", [])
    for media in media_list:
        if isinstance(media, dict):
            expanded = media.get("expanded_url")
            if expanded and expanded not in urls:
                urls.append(expanded)

    return list_join(hashtags), list_join(mentions), list_join(urls)


def extract_urls_fallback(text: str) -> str:
    """If entities.urls is empty, extract t.co URLs from the tweet text."""
    if not text:
        return ""
    tco_urls = re.findall(r"https?://t\.co/\w+", text)
    all_urls = re.findall(r"https?://[^\s]+", text)
    unique = []
    for u in all_urls:
        if u not in unique:
            unique.append(u)
    return list_join(unique)


def extract_media(tweet_obj: Dict[str, Any]) -> Tuple[str, str, str]:
    video_urls = []
    image_urls = []
    media_type = "none"

    ext_entities = tweet_obj.get("extended_entities", {})
    media_list = ext_entities.get("media", [])
    if not media_list:
        legacy = tweet_obj.get("legacy", {})
        media_list = legacy.get("entities", {}).get("media", [])

    for media in media_list:
        if not isinstance(media, dict):
            continue
        mtype = media.get("type")
        if mtype == "video":
            media_type = "video"
            variants = media.get("video_info", {}).get("variants", [])
            for v in variants:
                if v.get("content_type") == "video/mp4":
                    url = v.get("url")
                    if url:
                        video_urls.append(url)
                        break
        elif mtype == "animated_gif":
            media_type = "gif"
            variants = media.get("video_info", {}).get("variants", [])
            for v in variants:
                if v.get("content_type") == "video/mp4":
                    url = v.get("url")
                    if url:
                        video_urls.append(url)
                        break
        elif mtype == "photo":
            media_type = "image"
            url = media.get("media_url_https") or media.get("media_url")
            if url:
                image_urls.append(url)

    return list_join(video_urls), list_join(image_urls), media_type


def extract_tweet_row(t: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    tweet_id = t.get("rest_id") or deep_get(t, ["legacy", "id_str"], "")
    if not tweet_id:
        return None

    legacy = t.get("legacy", {}) if isinstance(t.get("legacy"), dict) else {}
    core_user = deep_get(t, ["core", "user_results", "result", "core"], {}) or {}
    screen_name = core_user.get("screen_name", "")
    name = core_user.get("name", "")

    is_retweet = "retweeted_status_result" in t or "retweeted_status_result" in legacy
    is_quote = bool(deep_get(t, ["legacy", "is_quote_status"], False))

    if is_retweet:
        original = deep_get(t, ["retweeted_status_result", "result"], {})
        if not original:
            original = t.get("retweeted_status_result", {}).get("result", {})
        if not original:
            original = deep_get(legacy, ["retweeted_status_result", "result"], {})
        if not original:
            original = legacy.get("retweeted_status_result", {}).get("result", {})
        full_text = get_full_text(original)
        retweet_count = safe_int(deep_get(original, ["legacy", "retweet_count"], 0))
        favorite_count = safe_int(deep_get(original, ["legacy", "favorite_count"], 0))
        reply_count = safe_int(deep_get(original, ["legacy", "reply_count"], 0))
        quote_count = safe_int(deep_get(original, ["legacy", "quote_count"], 0))
        view_count = safe_int(deep_get(original, ["views", "count"], 0))
        lang = deep_get(original, ["legacy", "lang"], "")
        orig_legacy = original.get("legacy", {})
        hashtags, mentions, urls = extract_entities(orig_legacy)
        if not urls and full_text:
            urls = extract_urls_fallback(full_text)
        retweeted_screen_name = deep_get(
            original, ["core", "user_results", "result", "core", "screen_name"], ""
        )
        retweeted_tweet_id = original.get("rest_id") or deep_get(original, ["legacy", "id_str"], "")
        retweeted_tweet_url = (
            f"https://x.com/{retweeted_screen_name}/status/{retweeted_tweet_id}"
            if retweeted_screen_name
            else ""
        )
        video_urls, image_urls, media_type = extract_media(original)
    else:
        full_text = get_full_text(t)
        retweet_count = safe_int(legacy.get("retweet_count", 0))
        favorite_count = safe_int(legacy.get("favorite_count", 0))
        reply_count = safe_int(legacy.get("reply_count", 0))
        quote_count = safe_int(legacy.get("quote_count", 0))
        view_count = safe_int(deep_get(t, ["views", "count"], 0))
        lang = legacy.get("lang", "")
        hashtags, mentions, urls = extract_entities(legacy)
        if not urls and full_text:
            urls = extract_urls_fallback(full_text)
        retweeted_screen_name = ""
        retweeted_tweet_id = ""
        retweeted_tweet_url = ""
        video_urls, image_urls, media_type = extract_media(t)

    created_at = legacy.get("created_at", "")
    created_at_iso = parse_created_at_iso(created_at)
    source_label, source_url = parse_source_html(t.get("source", ""))

    is_link_only = False
    if not is_retweet:
        raw_text = legacy.get("full_text", "")
        if re.fullmatch(r"\s*https?://\S+\s*", raw_text):
            is_link_only = True

    quoted_tweet_id = ""
    quoted_screen_name = ""
    quoted_full_text = ""
    quoted_tweet_url = ""
    if is_quote and not is_retweet:
        quoted_obj = deep_get(t, ["quoted_status_result", "result"], {})
        if not quoted_obj:
            quoted_obj = t.get("quoted_status_result", {}).get("result", {})
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

    row = {
        "tweet_id": str(tweet_id),
        "created_at": created_at,
        "created_at_iso": created_at_iso,
        "screen_name": screen_name,
        "name": name,
        "full_text": full_text,
        "retweet_count": retweet_count,
        "favorite_count": favorite_count,
        "reply_count": reply_count,
        "quote_count": quote_count,
        "view_count": view_count,
        "lang": lang,
        "is_retweet": 1 if is_retweet else 0,
        "is_quote": 1 if is_quote else 0,
        "is_link_only": 1 if is_link_only else 0,
        "source_label": source_label,
        "source_url": source_url,
        "hashtags": hashtags,
        "mentions": mentions,
        "urls": urls,
        "retweeted_from_screen_name": retweeted_screen_name,
        "retweeted_tweet_id": str(retweeted_tweet_id) if retweeted_tweet_id else "",
        "retweeted_tweet_url": retweeted_tweet_url,
        "quoted_tweet_id": str(quoted_tweet_id) if quoted_tweet_id else "",
        "quoted_screen_name": quoted_screen_name,
        "quoted_full_text": quoted_full_text,
        "quoted_tweet_url": quoted_tweet_url,
        "video_urls": video_urls,
        "image_urls": image_urls,
        "media_type": media_type,
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
        "video_urls",
        "image_urls",
        "media_type",
        "tweet_url",
    ]

    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f, fieldnames=columns, extrasaction="ignore", quoting=csv.QUOTE_MINIMAL
        )
        writer.writeheader()
        for row in rows:
            row["tweet_id"] = str(row["tweet_id"])
            if row["retweeted_tweet_id"]:
                row["retweeted_tweet_id"] = str(row["retweeted_tweet_id"])
            if row["quoted_tweet_id"]:
                row["quoted_tweet_id"] = str(row["quoted_tweet_id"])
            writer.writerow(row)

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
        description="Convert tweet JSON to clean CSV (production final)."
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

    print("CSV conversion complete (production final)")
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
