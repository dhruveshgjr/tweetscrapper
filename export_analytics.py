#!/usr/bin/env python3
"""
export_analytics.py — Enhanced CSV with marketing-ready columns
"""

import argparse
import csv
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

try:
    from export_csv import clean_tweet
except ImportError:

    def clean_tweet(tweet):
        legacy = tweet.get("legacy", {})
        return {
            "id": tweet.get("rest_id") or legacy.get("id_str"),
            "screen_name": "unknown",
            "text": legacy.get("full_text", ""),
            "created_at": legacy.get("created_at"),
        }


def calculate_engagement_rate(tweet: Dict) -> float:
    legacy = tweet.get("legacy", {})
    views = tweet.get("views", {}).get("count", "0")
    try:
        views_int = int(views) if views and views != "0" else 0
        if views_int == 0:
            return 0.0
        engagements = (
            legacy.get("favorite_count", 0)
            or 0 + legacy.get("retweet_count", 0)
            or 0 + legacy.get("reply_count", 0)
            or 0
        )
        rate = (engagements / views_int) * 100
        # Cap at 100% (some retweets have anomalous view counts)
        return round(min(rate, 100.0), 4)
    except (ValueError, TypeError):
        return 0.0
        engagements = (
            legacy.get("favorite_count", 0)
            or 0 + legacy.get("retweet_count", 0)
            or 0 + legacy.get("reply_count", 0)
            or 0
        )
        return round((engagements / views_int) * 100, 4)
    except (ValueError, TypeError):
        return 0.0


def calculate_viral_score(tweet: Dict) -> float:
    legacy = tweet.get("legacy", {})
    views = max(1, int(tweet.get("views", {}).get("count", "1") or 1))
    weighted = (
        (legacy.get("retweet_count", 0) or 0) * 0.40
        + (legacy.get("quote_count", 0) or 0) * 0.25
        + (legacy.get("favorite_count", 0) or 0) * 0.20
        + (legacy.get("reply_count", 0) or 0) * 0.15
    )
    score = min(100, (weighted / views) * 10000)
    return round(score, 2)


def categorize_content(tweet: Dict) -> str:
    legacy = tweet.get("legacy", {})
    text = (legacy.get("full_text") or "").lower()

    if legacy.get("is_retweet") or "retweeted_status" in tweet:
        return "retweet"

    if legacy.get("entities", {}).get("media") or tweet.get("extended_entities", {}).get("media"):
        return "media"

    product_keywords = [
        "tesla",
        "spacex",
        "neuralink",
        "xai",
        "boring company",
        "starship",
        "fsd",
        "cybertruck",
        "x.com",
        "twitter",
    ]
    if any(kw in text for kw in product_keywords):
        return "product"

    if legacy.get("entities", {}).get("urls") or "http" in text:
        return "link"

    return "other"


def extract_posting_hour(tweet: Dict) -> Optional[int]:
    created_at = tweet.get("legacy", {}).get("created_at")
    if not created_at:
        return None
    try:
        dt = datetime.strptime(created_at, "%a %b %d %H:%M:%S %z %Y")
        return dt.hour
    except ValueError:
        return None


def get_text_length_bucket(text: str) -> str:
    if not text:
        return "empty"
    length = len(text)
    if length <= 100:
        return "short"
    elif length <= 200:
        return "medium"
    elif length <= 280:
        return "long"
    else:
        return "note_tweet"


def export_analytics_csv(
    input_json: Path, output_csv: Path, include_dashboard_metrics: bool = False
) -> Dict:
    with open(input_json, "r", encoding="utf-8") as f:
        tweets = json.load(f)

    base_columns = [
        "tweet_id",
        "created_at",
        "screen_name",
        "name",
        "full_text",
        "retweet_count",
        "favorite_count",
        "reply_count",
        "quote_count",
        "view_count",
        "lang",
    ]

    analytics_columns = [
        "engagement_rate",
        "viral_score",
        "content_category",
        "posting_hour",
        "text_length",
        "text_length_bucket",
        "has_media",
        "has_link",
        "has_hashtag",
        "is_reply",
        "is_quote",
    ]

    all_columns = base_columns + analytics_columns

    rows = []
    engagement_rates = []
    viral_scores = []
    categories = {}
    hours = {}

    for tweet in tweets:
        legacy = tweet.get("legacy", {})

        row = {
            "tweet_id": tweet.get("rest_id") or legacy.get("id_str", ""),
            "created_at": legacy.get("created_at", ""),
            "screen_name": tweet.get("core", {})
            .get("user_results", {})
            .get("result", {})
            .get("legacy", {})
            .get("screen_name", "")
            or tweet.get("core", {})
            .get("user_results", {})
            .get("result", {})
            .get("core", {})
            .get("screen_name", ""),
            "name": tweet.get("core", {})
            .get("user_results", {})
            .get("result", {})
            .get("legacy", {})
            .get("name", ""),
            "full_text": legacy.get("full_text", "").replace("\n", " "),
            "retweet_count": legacy.get("retweet_count", 0),
            "favorite_count": legacy.get("favorite_count", 0),
            "reply_count": legacy.get("reply_count", 0),
            "quote_count": legacy.get("quote_count", 0),
            "view_count": tweet.get("views", {}).get("count", "0"),
            "lang": legacy.get("lang", ""),
        }

        row["engagement_rate"] = calculate_engagement_rate(tweet)
        row["viral_score"] = calculate_viral_score(tweet)
        row["content_category"] = categorize_content(tweet)
        row["posting_hour"] = extract_posting_hour(tweet) or ""
        row["text_length"] = len(legacy.get("full_text", ""))
        row["text_length_bucket"] = get_text_length_bucket(legacy.get("full_text", ""))
        row["has_media"] = bool(legacy.get("entities", {}).get("media"))
        row["has_link"] = bool(legacy.get("entities", {}).get("urls"))
        row["has_hashtag"] = bool(legacy.get("entities", {}).get("hashtags"))
        row["is_reply"] = bool(legacy.get("in_reply_to_status_id_str"))
        row["is_quote"] = bool(legacy.get("is_quote_status"))

        rows.append(row)

        if row["engagement_rate"] > 0:
            engagement_rates.append(row["engagement_rate"])
        viral_scores.append(row["viral_score"])

        cat = row["content_category"]
        categories[cat] = categories.get(cat, 0) + 1

        hour = row["posting_hour"]
        if hour != "":
            hours[hour] = hours.get(hour, 0) + 1

    with open(output_csv, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=all_columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    stats = {
        "total_tweets": len(rows),
        "avg_engagement_rate": round(sum(engagement_rates) / len(engagement_rates), 3)
        if engagement_rates
        else 0,
        "median_viral_score": sorted(viral_scores)[len(viral_scores) // 2] if viral_scores else 0,
        "category_distribution": categories,
        "posting_hour_distribution": hours,
        "csv_path": str(output_csv),
    }

    if include_dashboard_metrics:
        dashboard_path = output_csv.with_suffix(".dashboard.json")
        with open(dashboard_path, "w", encoding="utf-8") as f:
            json.dump(stats, f, indent=2, ensure_ascii=False)
        stats["dashboard_path"] = str(dashboard_path)

    return stats


def main():
    parser = argparse.ArgumentParser(description="Export TweetScrape data with analytics columns")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", default=None)
    parser.add_argument("--dashboard", action="store_true")
    args = parser.parse_args()

    input_path = Path(args.input)
    output_path = Path(args.output) if args.output else input_path.with_suffix(".analytics.csv")

    print(f"Converting {input_path} -> {output_path}")

    stats = export_analytics_csv(input_path, output_path, include_dashboard_metrics=args.dashboard)

    print(f"Exported {stats['total_tweets']} tweets")
    print(f"Avg engagement rate: {stats['avg_engagement_rate']}%")
    print(f"Median viral score: {stats['median_viral_score']}/100")
    print(f"Category breakdown: {stats['category_distribution']}")

    if args.dashboard:
        print(f"Dashboard: {stats['dashboard_path']}")


if __name__ == "__main__":
    main()
