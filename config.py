"""
config.py
Shared constants, paths, and helper functions for all scraper scripts.
"""

import os
import json
import logging
from pathlib import Path

USER_DATA_DIR = "./x_session"
OUTPUT_DIR = "./output"
DEFAULT_TARGET = "elonmusk"
DEFAULT_MAX_TWEETS = 10000
TWEETS_PER_REQUEST = 20
REQUEST_DELAY_SECONDS = 1.0

BEARER_TOKEN = (
    "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs"
    "%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)

GRAPHQL_FEATURES = {
    "responsive_web_graphql_exclude_directive_enabled": True,
    "verified_phone_label_enabled": False,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "communities_web_enable_tweet_community_results_fetch": True,
    "c9s_tweet_anatomy_moderator_badge_enabled": True,
    "articles_preview_enabled": True,
    "tweetypie_unmention_optimization_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
    "view_counts_everywhere_api_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "responsive_web_twitter_article_tweet_consumption_enabled": True,
    "tweet_awards_web_tipping_enabled": False,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "standardized_nudges_misinfo": True,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
    "rweb_video_timestamps_enabled": True,
    "longform_notetweets_rich_text_read_enabled": True,
    "longform_notetweets_inline_media_enabled": True,
    "responsive_web_enhance_cards_enabled": False,
}

KNOWN_USER_IDS = {
    "elonmusk": "44196397",
    "barackobama": "813286",
    "justinbieber": "27260086",
    "katyperry": "21447363",
    "rihanna": "791753532",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("x-scraper")


def ensure_output_dir():
    Path(OUTPUT_DIR).mkdir(parents=True, exist_ok=True)


def output_path(filename: str) -> str:
    ensure_output_dir()
    return os.path.join(OUTPUT_DIR, filename)


def save_json(data, filename: str):
    path = output_path(filename)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    log.info(
        "Saved %s (%d items) to %s", filename, len(data) if isinstance(data, list) else 1, path
    )
    return path


def load_json(filename: str):
    path = output_path(filename)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def extract_screen_name(tweet: dict) -> str:
    paths = [
        lambda: (
            tweet.get("core", {})
            .get("user_results", {})
            .get("result", {})
            .get("legacy", {})
            .get("screen_name")
        ),
        lambda: (
            tweet.get("core", {})
            .get("user_results", {})
            .get("result", {})
            .get("core", {})
            .get("screen_name")
        ),
        lambda: tweet.get("author", {}).get("screen_name"),
    ]
    for path in paths:
        val = path()
        if val:
            return val
    return ""


def is_valid_tweet(tweet: dict) -> bool:
    typename = tweet.get("__typename")
    if typename and typename != "Tweet":
        return False
    if not typename and tweet.get("card"):
        return False
    legacy = tweet.get("legacy", {})
    if not legacy.get("full_text"):
        return False
    rest_id = tweet.get("rest_id") or legacy.get("id_str")
    if not rest_id:
        return False
    return True


def clean_tweet(tweet: dict) -> dict:
    legacy = tweet.get("legacy", {})
    views = tweet.get("views", {})
    return {
        "id": tweet.get("rest_id") or legacy.get("id_str"),
        "screen_name": extract_screen_name(tweet),
        "text": (legacy.get("full_text") or "").replace("\n", " "),
        "created_at": legacy.get("created_at"),
        "likes": legacy.get("favorite_count", 0),
        "retweets": legacy.get("retweet_count", 0),
        "replies": legacy.get("reply_count", 0),
        "quotes": legacy.get("quote_count", 0),
        "views": views.get("count", "0"),
        "lang": legacy.get("lang"),
        "is_retweet": legacy.get("retweeted") is True
        or legacy.get("full_text", "").startswith("RT @"),
        "possibly_sensitive": legacy.get("possibly_sensitive", False),
        "source": tweet.get("source", ""),
        "has_media": bool(legacy.get("entities", {}).get("media")),
        "is_quote": bool(legacy.get("quoted_status_id_str")),
        "quoted_tweet_id": legacy.get("quoted_status_id_str"),
    }


def filter_valid(tweets: list) -> list:
    return [t for t in tweets if is_valid_tweet(t)]


def deduplicate(tweets: list, key=None) -> list:
    seen = set()
    unique = []
    for t in tweets:
        k = (
            t.get(key)
            if key
            else t.get("rest_id") or t.get("legacy", {}).get("id_str") or t.get("id")
        )
        if k and k not in seen:
            seen.add(k)
            unique.append(t)
    return unique
